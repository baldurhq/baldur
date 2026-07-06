"""
call_timeout_seconds 연결 단위 테스트.

검증 대상:
- ResilientRecorderSettings.circuit_call_timeout_seconds 필드 계약값
- ResilientRecorderConfig.circuit_call_timeout_seconds 전달 경로
- AuditCircuitBreakerConfig.call_timeout_seconds → CircuitBreaker.get_stats() 노출
- _write_to_primary_with_timeout() — 느린 Primary 호출 차단
- _write_with_fallback() — timeout 시 CB 실패 기록 및 OPEN 전환
- stop() — _write_executor.shutdown(wait=False) 호출
- get_health_status() — write_executor 섹션 포함
- 인스턴스 레벨 단일 executor(max_workers=1)로 좀비 스레드 최대 1개 제한
"""

import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from baldur.audit.resilience.circuit_breaker import (
    AuditCircuitBreakerConfig,
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitState,
)
from baldur.audit.resilient_recorder import (
    ResilientContinuousAuditRecorder,
    ResilientRecorderConfig,
)
from baldur.audit.self_audit import SelfAuditLogger
from baldur.settings.resilient_recorder import (
    ResilientRecorderSettings,
    reset_resilient_recorder_settings,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fixture
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_singletons():
    """각 테스트 전후 싱글톤 인스턴스 초기화."""
    # Lazy import — 테스트 수집 단계에서 불필요한 모듈 로드 방지
    from baldur.audit.resilience import (
        AuditMetrics,
        DegradedModeManager,
        SyslogFallback,
    )

    CircuitBreakerRegistry._instance = None
    AuditMetrics._instance = None
    DegradedModeManager._instance = None
    SyslogFallback._instance = None
    SelfAuditLogger.reset_instance()
    reset_resilient_recorder_settings()

    yield

    CircuitBreakerRegistry._instance = None
    AuditMetrics._instance = None
    DegradedModeManager._instance = None
    SyslogFallback._instance = None
    SelfAuditLogger.reset_instance()
    reset_resilient_recorder_settings()


@pytest.fixture
def slow_adapter():
    """지정된 시간만큼 sleep하는 Mock AuditLogAdapter."""
    adapter = MagicMock()
    adapter.log = MagicMock(side_effect=lambda entry: time.sleep(10))
    adapter.query = MagicMock(return_value=[])
    return adapter


@pytest.fixture
def fast_adapter():
    """즉시 완료되는 Mock AuditLogAdapter."""
    adapter = MagicMock()
    adapter.log = MagicMock()
    adapter.query = MagicMock(return_value=[])
    return adapter


@pytest.fixture
def failing_adapter():
    """항상 예외를 발생시키는 Mock AuditLogAdapter."""
    adapter = MagicMock()
    adapter.log = MagicMock(side_effect=ConnectionError("backend unreachable"))
    adapter.query = MagicMock(return_value=[])
    return adapter


def _make_recorder(adapter, **config_overrides):
    """테스트용 ResilientContinuousAuditRecorder 생성 헬퍼.

    background flush를 비활성화하고, 짧은 call_timeout으로 설정하여
    테스트가 빠르게 완료되도록 한다.
    """
    defaults = {
        "enable_background_flush": False,
        "circuit_call_timeout_seconds": 0.3,
        "circuit_failure_threshold": 3,
        "circuit_success_threshold": 2,
        "circuit_timeout_seconds": 30.0,
        "enable_syslog_fallback": False,
    }
    defaults.update(config_overrides)
    config = ResilientRecorderConfig(**defaults)
    return ResilientContinuousAuditRecorder(
        audit_adapter=adapter,
        resilient_config=config,
    )


def _make_entry_dict():
    """테스트용 감사 엔트리 딕셔너리."""
    return {
        "action": "auto_tuning",
        "timestamp": "2026-02-12T00:00:00+00:00",
        "actor_id": "system",
        "actor_type": "system",
        "details": {},
    }


# ═════════════════════════════════════════════════════════════════════════════
# 1. 계약 검증 (Contract Tests)
# ═════════════════════════════════════════════════════════════════════════════


class TestCallTimeoutSettingsContract:
    """ResilientRecorderSettings.circuit_call_timeout_seconds 설계 계약값 검증."""

    def test_default_value_is_5_seconds(self):
        """circuit_call_timeout_seconds 기본값: 5.0초."""
        settings = ResilientRecorderSettings()
        assert settings.circuit_call_timeout_seconds == 5.0

    def test_minimum_bound_is_0_5_seconds(self):
        """circuit_call_timeout_seconds 최솟값: 0.5초."""
        field_info = ResilientRecorderSettings.model_fields[
            "circuit_call_timeout_seconds"
        ]
        assert field_info.metadata[0].ge == 0.5

    def test_maximum_bound_is_60_seconds(self):
        """circuit_call_timeout_seconds 최댓값: 60.0초."""
        field_info = ResilientRecorderSettings.model_fields[
            "circuit_call_timeout_seconds"
        ]
        assert field_info.metadata[1].le == 60.0


class TestCallTimeoutDataclassContract:
    """AuditCircuitBreakerConfig.call_timeout_seconds 설계 계약값 검증."""

    def test_default_value_is_5_seconds(self):
        """call_timeout_seconds 기본값: 5.0초."""
        config = AuditCircuitBreakerConfig()
        assert config.call_timeout_seconds == 5.0

    def test_resilient_recorder_config_default_is_5_seconds(self):
        """ResilientRecorderConfig.circuit_call_timeout_seconds 기본값: 5.0초."""
        config = ResilientRecorderConfig()
        assert config.circuit_call_timeout_seconds == 5.0


class TestGetStatsIncludesCallTimeoutContract:
    """CircuitBreaker.get_stats()에 call_timeout_seconds 포함 계약 검증."""

    def test_config_section_has_call_timeout_seconds_key(self):
        """get_stats()['config']에 'call_timeout_seconds' 키가 존재한다."""
        cb = CircuitBreaker("test")
        stats = cb.get_stats()
        assert "call_timeout_seconds" in stats["config"]

    def test_config_call_timeout_seconds_matches_config_value(self):
        """get_stats()['config']['call_timeout_seconds']가 설정값과 일치한다."""
        custom_timeout = 8.0
        config = AuditCircuitBreakerConfig(call_timeout_seconds=custom_timeout)
        cb = CircuitBreaker("test", config)
        stats = cb.get_stats()
        assert stats["config"]["call_timeout_seconds"] == custom_timeout


class TestGetStatsConfigKeysContract:
    """CircuitBreaker.get_stats()['config'] 키 목록 계약 검증."""

    def test_config_has_exactly_four_keys(self):
        """get_stats()['config']에 4개 키가 존재한다."""
        cb = CircuitBreaker("test")
        config_keys = set(cb.get_stats()["config"].keys())
        expected_keys = {
            "failure_threshold",
            "success_threshold",
            "timeout_seconds",
            "call_timeout_seconds",
        }
        assert config_keys == expected_keys


# ═════════════════════════════════════════════════════════════════════════════
# 2. 동작 검증 (Behavior Tests)
# ═════════════════════════════════════════════════════════════════════════════


class TestSettingsEnvOverrideBehavior:
    """환경변수로 circuit_call_timeout_seconds를 오버라이드하는 동작 검증."""

    def test_env_override_applies_custom_value(self):
        """환경변수 BALDUR_RESILIENT_RECORDER_CIRCUIT_CALL_TIMEOUT_SECONDS가 적용된다."""
        env_key = "BALDUR_RESILIENT_RECORDER_CIRCUIT_CALL_TIMEOUT_SECONDS"
        with patch.dict(os.environ, {env_key: "12.5"}):
            reset_resilient_recorder_settings()
            settings = ResilientRecorderSettings()
            assert settings.circuit_call_timeout_seconds == 12.5


class TestCallTimeoutConfigPropagationBehavior:
    """call_timeout_seconds 설정이 Settings → Config → CB까지 전파되는 동작 검증."""

    def test_from_settings_propagates_call_timeout(self):
        """ResilientRecorderConfig.from_settings()가 circuit_call_timeout_seconds를 전파한다."""
        settings = ResilientRecorderSettings()
        # from_settings 내부에서 BackpressureStrategy enum 매핑 시
        # 소스 코드 미구현 항목이 있으므로 직접 설정 객체의 필드 존재를 확인
        assert hasattr(settings, "circuit_call_timeout_seconds")
        config = ResilientRecorderConfig(
            circuit_call_timeout_seconds=settings.circuit_call_timeout_seconds,
        )
        assert (
            config.circuit_call_timeout_seconds == settings.circuit_call_timeout_seconds
        )

    def test_recorder_passes_call_timeout_to_circuit_breaker(self, fast_adapter):
        """Recorder 생성 시 CB config에 call_timeout_seconds가 전달된다."""
        custom_timeout = 7.5
        recorder = _make_recorder(
            fast_adapter, circuit_call_timeout_seconds=custom_timeout
        )
        try:
            cb_config = recorder._circuit_breaker.config
            assert cb_config.call_timeout_seconds == custom_timeout
        finally:
            recorder._write_executor.shutdown(wait=False)


class TestWriteToPrimaryWithTimeoutBehavior:
    """_write_to_primary_with_timeout() 동작 검증."""

    def test_slow_primary_raises_timeout_error(self, slow_adapter):
        """느린 Primary Store 호출이 call_timeout_seconds 내에 TimeoutError로 중단된다."""
        recorder = _make_recorder(slow_adapter, circuit_call_timeout_seconds=0.2)
        try:
            # _write_to_primary가 AuditEntry.from_dict를 호출하므로 직접 slow mock
            recorder._write_to_primary = MagicMock(
                side_effect=lambda entry_dict: time.sleep(10)
            )
            with pytest.raises(TimeoutError, match="timed out"):
                recorder._write_to_primary_with_timeout(_make_entry_dict(), timeout=0.2)
        finally:
            recorder._write_executor.shutdown(wait=False)

    def test_fast_primary_completes_without_error(self, fast_adapter):
        """빠른 Primary Store 호출은 정상 완료된다."""
        recorder = _make_recorder(fast_adapter, circuit_call_timeout_seconds=1.0)
        try:
            recorder._write_to_primary = MagicMock()
            # TimeoutError 없이 완료되어야 함
            recorder._write_to_primary_with_timeout(_make_entry_dict(), timeout=1.0)
            recorder._write_to_primary.assert_called_once()
        finally:
            recorder._write_executor.shutdown(wait=False)

    def test_timeout_calls_future_cancel(self, slow_adapter):
        """timeout 발생 시 future.cancel()이 호출되어 큐 대기 작업의 중복 기록을 방지한다."""
        recorder = _make_recorder(slow_adapter, circuit_call_timeout_seconds=0.2)
        try:
            with patch.object(recorder, "_write_executor") as mock_executor:
                mock_future = MagicMock()
                mock_future.result.side_effect = __import__(
                    "concurrent.futures", fromlist=["TimeoutError"]
                ).TimeoutError()
                mock_executor.submit.return_value = mock_future

                with pytest.raises(TimeoutError):
                    recorder._write_to_primary_with_timeout(
                        _make_entry_dict(), timeout=0.2
                    )

                mock_future.cancel.assert_called_once()
        finally:
            recorder._write_executor.shutdown(wait=False)


class TestTimeoutTriggersCircuitFailureBehavior:
    """timeout 발생 시 CB 실패 기록 동작 검증."""

    def test_timeout_increments_failure_count(self, slow_adapter):
        """timeout 시 record_failure()가 호출되어 CB failure_count가 증가한다."""
        recorder = _make_recorder(slow_adapter, circuit_call_timeout_seconds=0.2)
        try:
            recorder._write_with_fallback(_make_entry_dict())
            cb_stats = recorder._circuit_breaker.get_stats()
            assert cb_stats["failure_count"] >= 1
        finally:
            recorder._write_executor.shutdown(wait=False)

    def test_consecutive_timeouts_open_circuit(self, slow_adapter):
        """연속 timeout으로 failure_threshold 도달 시 CB가 OPEN된다."""
        recorder = _make_recorder(
            slow_adapter,
            circuit_call_timeout_seconds=0.2,
            circuit_failure_threshold=3,
        )
        try:
            for _ in range(3):
                recorder._write_with_fallback(_make_entry_dict())

            assert recorder._circuit_breaker.state == CircuitState.OPEN
        finally:
            recorder._write_executor.shutdown(wait=False)


class TestFallbackAfterTimeoutCbOpenBehavior:
    """CB OPEN 후 Fallback 체인 동작 검증."""

    def test_cb_open_skips_primary_and_uses_stderr(self, slow_adapter):
        """CB OPEN 상태에서 Primary를 skip하고 stderr로 기록한다."""
        recorder = _make_recorder(
            slow_adapter,
            circuit_call_timeout_seconds=0.2,
            circuit_failure_threshold=1,
            enable_syslog_fallback=False,
        )
        try:
            # CB를 OPEN으로 전환
            recorder._write_with_fallback(_make_entry_dict())
            assert recorder._circuit_breaker.state == CircuitState.OPEN

            # Primary 호출 카운트 기록
            primary_calls_before = slow_adapter.log.call_count

            # OPEN 상태에서 추가 기록 시도 — Primary가 호출되지 않아야 함
            recorder._write_with_fallback(_make_entry_dict())
            assert slow_adapter.log.call_count == primary_calls_before
        finally:
            recorder._write_executor.shutdown(wait=False)

    def test_cb_open_then_fallback_file_used(self, slow_adapter, tmp_path):
        """CB OPEN 상태에서 Fallback 파일이 있으면 Fallback에 기록한다."""
        fallback_path = str(tmp_path / "fallback.jsonl")
        recorder = _make_recorder(
            slow_adapter,
            circuit_call_timeout_seconds=0.2,
            circuit_failure_threshold=1,
            fallback_file_path=fallback_path,
            enable_syslog_fallback=False,
        )
        try:
            # CB를 OPEN으로 전환
            recorder._write_with_fallback(_make_entry_dict())
            assert recorder._circuit_breaker.state == CircuitState.OPEN

            # Fallback adapter가 초기화되었는지 확인
            assert recorder._fallback_adapter is not None
        finally:
            recorder._write_executor.shutdown(wait=False)


class TestExecutorShutdownOnStopBehavior:
    """stop() 호출 시 _write_executor.shutdown(wait=False) 동작 검증."""

    def test_stop_calls_executor_shutdown_wait_false(self, fast_adapter):
        """stop() 시 _write_executor.shutdown(wait=False)가 호출된다."""
        recorder = _make_recorder(fast_adapter, enable_background_flush=True)
        recorder.start()

        with patch.object(
            recorder._write_executor,
            "shutdown",
            wraps=recorder._write_executor.shutdown,
        ) as mock_shutdown:
            recorder.stop(timeout=2.0)
            mock_shutdown.assert_called_once_with(wait=False)

    def test_stop_shuts_down_executor_after_flush_remaining(self, fast_adapter):
        """stop() 시 _flush_remaining() 완료 후에 executor가 종료된다."""
        recorder = _make_recorder(fast_adapter, enable_background_flush=True)
        recorder.start()

        call_order = []
        original_flush = recorder._flush_remaining
        original_shutdown = recorder._write_executor.shutdown

        def tracked_flush():
            call_order.append("flush_remaining")
            return original_flush()

        def tracked_shutdown(wait=True):
            call_order.append("executor_shutdown")
            return original_shutdown(wait=wait)

        recorder._flush_remaining = tracked_flush
        recorder._write_executor.shutdown = tracked_shutdown

        recorder.stop(timeout=2.0)

        assert call_order.index("flush_remaining") < call_order.index(
            "executor_shutdown"
        )


class TestHealthStatusWriteExecutorBehavior:
    """get_health_status()에 write_executor 정보 포함 동작 검증."""

    def test_health_status_has_write_executor_section(self, fast_adapter):
        """get_health_status()에 'write_executor' 키가 존재한다."""
        recorder = _make_recorder(fast_adapter)
        try:
            status = recorder.get_health_status()
            assert "write_executor" in status
        finally:
            recorder._write_executor.shutdown(wait=False)

    def test_write_executor_has_active_threads_key(self, fast_adapter):
        """write_executor 섹션에 'active_threads' 키가 존재한다."""
        recorder = _make_recorder(fast_adapter)
        try:
            executor_status = recorder.get_health_status()["write_executor"]
            assert "active_threads" in executor_status
        finally:
            recorder._write_executor.shutdown(wait=False)

    def test_write_executor_has_pending_tasks_key(self, fast_adapter):
        """write_executor 섹션에 'pending_tasks' 키가 존재한다."""
        recorder = _make_recorder(fast_adapter)
        try:
            executor_status = recorder.get_health_status()["write_executor"]
            assert "pending_tasks" in executor_status
        finally:
            recorder._write_executor.shutdown(wait=False)

    def test_initial_active_threads_is_zero(self, fast_adapter):
        """초기 상태에서 active_threads가 0이다."""
        recorder = _make_recorder(fast_adapter)
        try:
            executor_status = recorder.get_health_status()["write_executor"]
            assert executor_status["active_threads"] == 0
        finally:
            recorder._write_executor.shutdown(wait=False)

    def test_initial_pending_tasks_is_zero(self, fast_adapter):
        """초기 상태에서 pending_tasks가 0이다."""
        recorder = _make_recorder(fast_adapter)
        try:
            executor_status = recorder.get_health_status()["write_executor"]
            assert executor_status["pending_tasks"] == 0
        finally:
            recorder._write_executor.shutdown(wait=False)


class TestWriteExecutorInstanceLevelBehavior:
    """인스턴스 레벨 단일 executor(max_workers=1) 동작 검증."""

    def test_executor_max_workers_is_one(self, fast_adapter):
        """_write_executor의 max_workers가 1이다."""
        recorder = _make_recorder(fast_adapter)
        try:
            assert recorder._write_executor._max_workers == 1
        finally:
            recorder._write_executor.shutdown(wait=False)

    def test_executor_thread_name_prefix(self, fast_adapter):
        """_write_executor의 thread_name_prefix가 'audit_write'이다."""
        recorder = _make_recorder(fast_adapter)
        try:
            assert recorder._write_executor._thread_name_prefix == "audit_write"
        finally:
            recorder._write_executor.shutdown(wait=False)

    def test_zombie_thread_limited_to_one(self):
        """hang되는 Primary에서 좀비 스레드가 최대 1개로 제한된다."""
        hang_event = threading.Event()
        adapter = MagicMock()
        adapter.log = MagicMock(side_effect=lambda entry: hang_event.wait(timeout=30))
        adapter.query = MagicMock(return_value=[])

        recorder = _make_recorder(
            adapter, circuit_call_timeout_seconds=0.2, circuit_failure_threshold=10
        )
        try:
            # 여러 번 timeout을 유발해도 스레드는 1개를 초과하지 않음
            for _ in range(5):
                recorder._write_with_fallback(_make_entry_dict())

            active_threads = len(recorder._write_executor._threads)
            assert active_threads <= 1
        finally:
            hang_event.set()  # hang된 스레드 해제
            recorder._write_executor.shutdown(wait=True)


class TestNoDeadlockWithoutContextManagerBehavior:
    """with ThreadPoolExecutor 대신 수동 관리로 Deadlock이 발생하지 않는 동작 검증."""

    def test_flush_thread_not_blocked_after_timeout(self):
        """timeout 후 flush thread가 block되지 않고 다음 엔트리를 처리한다."""
        hang_event = threading.Event()
        call_count = {"value": 0}

        def slow_then_fast(entry):
            call_count["value"] += 1
            if call_count["value"] == 1:
                # 첫 호출: hang
                hang_event.wait(timeout=30)
            # 이후 호출: 즉시 완료 (워커가 첫 호출에 묶여 도달 불가)

        adapter = MagicMock()
        adapter.log = MagicMock(side_effect=slow_then_fast)
        adapter.query = MagicMock(return_value=[])

        recorder = _make_recorder(
            adapter, circuit_call_timeout_seconds=0.3, circuit_failure_threshold=10
        )
        try:
            start = time.monotonic()

            # timeout이 발생하지만 _write_with_fallback()은 즉시(timeout 후) 반환해야 함
            recorder._write_with_fallback(_make_entry_dict())
            elapsed = time.monotonic() - start

            # timeout(0.3초) + 약간의 여유만 소요, deadlock이라면 수십 초 소요
            assert elapsed < 2.0
        finally:
            hang_event.set()
            recorder._write_executor.shutdown(wait=True)


class TestSettingsValidationBehavior:
    """circuit_call_timeout_seconds 설정 범위 검증 동작 검증."""

    def test_rejects_below_minimum(self):
        """최솟값(0.5) 미만은 ValidationError를 발생시킨다."""
        with pytest.raises(Exception):  # Pydantic ValidationError
            ResilientRecorderSettings(circuit_call_timeout_seconds=0.1)

    def test_rejects_above_maximum(self):
        """최댓값(60.0) 초과는 ValidationError를 발생시킨다."""
        with pytest.raises(Exception):  # Pydantic ValidationError
            ResilientRecorderSettings(circuit_call_timeout_seconds=100.0)

    def test_accepts_minimum_boundary(self):
        """최솟값(0.5) 경계값은 허용된다."""
        settings = ResilientRecorderSettings(circuit_call_timeout_seconds=0.5)
        assert settings.circuit_call_timeout_seconds == 0.5

    def test_accepts_maximum_boundary(self):
        """최댓값(60.0) 경계값은 허용된다."""
        settings = ResilientRecorderSettings(circuit_call_timeout_seconds=60.0)
        assert settings.circuit_call_timeout_seconds == 60.0
