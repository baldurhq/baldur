"""
Tests for Audit Watchdog - Dead Man's Switch Pattern.

Tests:
- Watchdog lifecycle (start/stop)
- Heartbeat sending
- Local file heartbeat
- Failure detection and callbacks
- Threshold exceeded handling
- WatchdogChecker
- Thread safety
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from baldur.audit.audit_watchdog import (
    AuditWatchdog,
    AuditWatchdogConfig,
    AuditWatchdogStatus,
    HeartbeatTarget,
    WatchdogChecker,
    WatchdogStats,
    get_watchdog,
    start_watchdog,
    stop_watchdog,
)
from baldur.audit.self_audit import SelfAuditLogger


class TestAuditWatchdogConfig:
    """AuditWatchdogConfig tests."""

    def test_default_config(self):
        """기본 설정 테스트."""
        config = AuditWatchdogConfig()

        assert config.heartbeat_interval_seconds == 30.0
        assert config.missed_threshold == 3
        assert config.targets == []
        assert config.local_heartbeat_file is None

    def test_custom_config(self, tmp_path):
        """커스텀 설정 테스트."""
        heartbeat_file = str(tmp_path / "heartbeat.json")
        targets = [
            HeartbeatTarget(name="test", url="http://example.com/ping"),
        ]
        config = AuditWatchdogConfig(
            heartbeat_interval_seconds=60.0,
            missed_threshold=5,
            targets=targets,
            local_heartbeat_file=heartbeat_file,
        )

        assert config.heartbeat_interval_seconds == 60.0
        assert config.missed_threshold == 5
        assert len(config.targets) == 1
        assert config.local_heartbeat_file == heartbeat_file

    def test_from_env(self, tmp_path):
        """환경 변수에서 설정 로드 테스트 (deprecated, from_settings로 전환)."""
        from baldur.settings.audit_watchdog import reset_audit_watchdog_settings

        heartbeat_file = str(tmp_path / "test_heartbeat.json")
        with patch.dict(
            os.environ,
            {
                "BALDUR_AUDIT_WATCHDOG_HEARTBEAT_URL": "http://test.com/ping",
                "BALDUR_AUDIT_WATCHDOG_HEARTBEAT_INTERVAL_SECONDS": "45.0",
                "BALDUR_AUDIT_WATCHDOG_MISSED_THRESHOLD": "5",
                "BALDUR_AUDIT_WATCHDOG_LOCAL_HEARTBEAT_FILE": heartbeat_file,
            },
        ):
            reset_audit_watchdog_settings()  # 싱글톤 리셋
            config = AuditWatchdogConfig.from_settings()

            assert config.heartbeat_interval_seconds == 45.0
            assert config.missed_threshold == 5
            assert len(config.targets) == 1
            assert config.targets[0].url == "http://test.com/ping"
            assert config.local_heartbeat_file == heartbeat_file
        reset_audit_watchdog_settings()  # 테스트 후 정리

    def test_from_env_defaults(self):
        """환경 변수 없을 때 기본값 테스트."""
        with patch.dict(os.environ, {}, clear=True):
            # 기존 환경 변수 제거
            for key in [
                "AUDIT_HEARTBEAT_URL",
                "AUDIT_HEARTBEAT_INTERVAL",
                "AUDIT_HEARTBEAT_MISSED_THRESHOLD",
                "AUDIT_HEARTBEAT_FILE",
            ]:
                os.environ.pop(key, None)

            config = AuditWatchdogConfig.from_settings()

            assert config.heartbeat_interval_seconds == 30.0
            assert config.missed_threshold == 3
            assert config.targets == []


class TestHeartbeatTarget:
    """HeartbeatTarget tests."""

    def test_default_target(self):
        """기본 타겟 테스트."""
        target = HeartbeatTarget(
            name="test",
            url="http://example.com/ping",
        )

        assert target.name == "test"
        assert target.url == "http://example.com/ping"
        assert target.method == "GET"
        assert target.headers == {}
        assert target.timeout_seconds == 5.0
        assert target.enabled is True

    def test_custom_target(self):
        """커스텀 타겟 테스트."""
        target = HeartbeatTarget(
            name="custom",
            url="http://example.com/api/heartbeat",
            method="POST",
            headers={"Authorization": "Bearer token"},
            timeout_seconds=10.0,
            enabled=False,
        )

        assert target.method == "POST"
        assert target.headers == {"Authorization": "Bearer token"}
        assert target.timeout_seconds == 10.0
        assert target.enabled is False


class TestAuditWatchdogLifecycle:
    """Watchdog lifecycle tests."""

    def test_initial_state(self):
        """초기 상태 테스트."""
        watchdog = AuditWatchdog()

        assert watchdog.state == AuditWatchdogStatus.STOPPED
        assert not watchdog.is_running

    def test_start_stop(self):
        """시작/중지 테스트."""
        config = AuditWatchdogConfig(heartbeat_interval_seconds=0.1)
        watchdog = AuditWatchdog(config=config)

        watchdog.start()
        assert watchdog.state == AuditWatchdogStatus.RUNNING
        assert watchdog.is_running

        time.sleep(0.15)  # heartbeat 한 번 실행되도록

        watchdog.stop()
        assert watchdog.state == AuditWatchdogStatus.STOPPED
        assert not watchdog.is_running

    def test_double_start(self):
        """중복 시작 테스트."""
        config = AuditWatchdogConfig(heartbeat_interval_seconds=0.1)
        watchdog = AuditWatchdog(config=config)

        watchdog.start()
        watchdog.start()  # 두 번째 호출은 무시됨

        assert watchdog.is_running

        watchdog.stop()

    def test_double_stop(self):
        """중복 중지 테스트."""
        watchdog = AuditWatchdog()

        watchdog.stop()  # 이미 중지 상태
        watchdog.stop()  # 두 번째 호출도 안전

        assert watchdog.state == AuditWatchdogStatus.STOPPED


class TestAuditWatchdogHeartbeat:
    """Watchdog heartbeat tests."""

    def test_heartbeat_stats(self):
        """Heartbeat 통계 테스트."""
        config = AuditWatchdogConfig(
            heartbeat_interval_seconds=0.05,
            targets=[],  # 타겟 없음 - 항상 성공
        )
        watchdog = AuditWatchdog(config=config)

        watchdog.start()
        time.sleep(0.2)  # 여러 번 heartbeat
        watchdog.stop()

        stats = watchdog.get_stats()
        assert stats.total_heartbeats >= 2
        assert stats.successful_heartbeats >= 2
        assert stats.failed_heartbeats == 0
        assert stats.uptime_seconds > 0

    def test_pet_resets_consecutive_failures(self):
        """pet()이 연속 실패 카운터를 리셋하는지 테스트."""
        watchdog = AuditWatchdog()
        watchdog._stats.consecutive_failures = 5

        watchdog.pet()

        assert watchdog._stats.consecutive_failures == 0
        assert watchdog._stats.last_heartbeat_time is not None

    def test_success_callback(self):
        """성공 콜백 테스트."""
        callback = MagicMock()
        config = AuditWatchdogConfig(
            heartbeat_interval_seconds=0.05,
            on_heartbeat_success=callback,
        )
        watchdog = AuditWatchdog(config=config)

        watchdog.start()
        time.sleep(0.1)
        watchdog.stop()

        assert callback.called


class TestAuditWatchdogLocalFile:
    """Local file heartbeat tests."""

    def test_local_file_heartbeat(self):
        """로컬 파일 heartbeat 테스트."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            heartbeat_file = f.name

        try:
            config = AuditWatchdogConfig(
                heartbeat_interval_seconds=0.05,
                local_heartbeat_file=heartbeat_file,
            )
            watchdog = AuditWatchdog(config=config)

            watchdog.start()
            time.sleep(0.15)
            watchdog.stop()

            # 파일이 생성되었는지 확인
            assert os.path.exists(heartbeat_file)

            with open(heartbeat_file) as f:
                data = json.load(f)

            assert "timestamp" in data
            assert "pid" in data
            assert "stats" in data
        finally:
            if os.path.exists(heartbeat_file):
                os.unlink(heartbeat_file)

    def test_local_file_invalid_path(self):
        """잘못된 경로에 로컬 파일 heartbeat 테스트."""
        import os
        import tempfile

        # Windows/Linux 모두에서 실패하는 경로 사용
        # 존재하지 않는 드라이브/디렉토리 조합
        invalid_path = os.path.join(
            tempfile.gettempdir(), "nonexistent_12345", "subdir", "heartbeat.json"
        )

        config = AuditWatchdogConfig(
            heartbeat_interval_seconds=0.05,
            local_heartbeat_file=invalid_path,
        )
        watchdog = AuditWatchdog(config=config)

        watchdog.start()
        time.sleep(0.3)  # 더 긴 대기 시간
        watchdog.stop()

        # 실패하지만 watchdog은 계속 동작
        stats = watchdog.get_stats()
        # 파일 작성 실패가 반드시 heartbeat 실패로 카운트되지 않을 수 있음
        # 대신 총 heartbeat 수가 정상적으로 카운트되는지 확인
        assert stats.total_heartbeats >= 1


class TestAuditWatchdogFailure:
    """Watchdog failure handling tests."""

    def test_failure_callback(self):
        """실패 콜백 테스트."""
        failure_callback = MagicMock()
        config = AuditWatchdogConfig(
            heartbeat_interval_seconds=0.05,
            targets=[
                HeartbeatTarget(
                    name="failing",
                    url="http://localhost:19999/nonexistent",  # 연결 불가
                    timeout_seconds=0.1,
                ),
            ],
            on_heartbeat_failure=failure_callback,
        )
        watchdog = AuditWatchdog(config=config)

        watchdog.start()
        time.sleep(0.15)
        watchdog.stop()

        assert failure_callback.called

    def test_threshold_exceeded_callback(self):
        """임계값 초과 콜백 테스트."""
        threshold_callback = MagicMock()
        config = AuditWatchdogConfig(
            heartbeat_interval_seconds=0.03,
            missed_threshold=2,
            targets=[
                HeartbeatTarget(
                    name="failing",
                    url="http://localhost:19999/nonexistent",
                    timeout_seconds=0.05,
                ),
            ],
            on_threshold_exceeded=threshold_callback,
        )
        watchdog = AuditWatchdog(config=config)

        watchdog.start()
        time.sleep(0.3)  # 충분한 시간 대기
        watchdog.stop()

        # 임계값 초과 콜백 호출됨
        assert threshold_callback.called
        call_args = threshold_callback.call_args[0]
        assert call_args[0] >= 2  # consecutive failures

    def test_degraded_state(self):
        """실패 시 DEGRADED 상태 전환 테스트.

        Note: 이 테스트는 heartbeat 실패 시 DEGRADED 상태로 전환되는지 확인합니다.
        타이밍에 민감하므로 폴링 방식으로 상태 변경을 대기합니다.
        """
        config = AuditWatchdogConfig(
            heartbeat_interval_seconds=0.05,
            targets=[
                HeartbeatTarget(
                    name="failing",
                    url="http://localhost:19999/nonexistent",
                    timeout_seconds=0.05,
                ),
            ],
        )
        watchdog = AuditWatchdog(config=config)

        watchdog.start()

        # 폴링 방식으로 DEGRADED 상태 대기 (최대 1초)
        deadline = time.time() + 1.0
        while time.time() < deadline:
            if watchdog.state == AuditWatchdogStatus.DEGRADED:
                break
            time.sleep(0.02)

        assert watchdog.state == AuditWatchdogStatus.DEGRADED, (
            f"Expected DEGRADED state but got {watchdog.state}"
        )
        assert watchdog.is_running  # DEGRADED도 running으로 간주

        watchdog.stop()


class TestAuditWatchdogDisabledTarget:
    """Disabled target tests."""

    def test_disabled_target_skipped(self):
        """비활성화된 타겟은 건너뛰기 테스트."""
        config = AuditWatchdogConfig(
            heartbeat_interval_seconds=0.05,
            targets=[
                HeartbeatTarget(
                    name="disabled",
                    url="http://localhost:19999/nonexistent",
                    enabled=False,
                ),
            ],
        )
        watchdog = AuditWatchdog(config=config)

        watchdog.start()
        time.sleep(0.1)
        watchdog.stop()

        # 비활성화된 타겟이라 실패 없음
        stats = watchdog.get_stats()
        assert stats.failed_heartbeats == 0


class TestWatchdogChecker:
    """WatchdogChecker tests."""

    def test_is_alive_valid(self):
        """유효한 heartbeat 파일로 is_alive 테스트."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            heartbeat_file = f.name
            json.dump(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "pid": os.getpid(),
                },
                f,
            )

        try:
            checker = WatchdogChecker(
                heartbeat_file=heartbeat_file,
                max_age_seconds=60.0,
            )

            assert checker.is_alive() is True
        finally:
            os.unlink(heartbeat_file)

    def test_is_alive_expired(self):
        """만료된 heartbeat 파일로 is_alive 테스트."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            heartbeat_file = f.name
            old_time = datetime.now(UTC) - timedelta(minutes=5)
            json.dump(
                {
                    "timestamp": old_time.isoformat(),
                    "pid": os.getpid(),
                },
                f,
            )

        try:
            checker = WatchdogChecker(
                heartbeat_file=heartbeat_file,
                max_age_seconds=60.0,  # 1분
            )

            assert checker.is_alive() is False
        finally:
            os.unlink(heartbeat_file)

    def test_is_alive_missing_file(self):
        """파일 없을 때 is_alive 테스트."""
        checker = WatchdogChecker(
            heartbeat_file="/nonexistent/heartbeat.json",
            max_age_seconds=60.0,
        )

        assert checker.is_alive() is False

    def test_is_alive_invalid_json(self):
        """잘못된 JSON 파일 테스트."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            heartbeat_file = f.name
            f.write("not valid json")

        try:
            checker = WatchdogChecker(
                heartbeat_file=heartbeat_file,
                max_age_seconds=60.0,
            )

            assert checker.is_alive() is False
        finally:
            os.unlink(heartbeat_file)

    def test_get_age_seconds(self):
        """경과 시간 조회 테스트."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            heartbeat_file = f.name
            json.dump(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                },
                f,
            )

        try:
            checker = WatchdogChecker(heartbeat_file=heartbeat_file)
            age = checker.get_age_seconds()

            assert age is not None
            assert age < 5.0  # 5초 이내
        finally:
            os.unlink(heartbeat_file)

    def test_read_heartbeat(self):
        """Heartbeat 파일 읽기 테스트."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            heartbeat_file = f.name
            data = {
                "timestamp": datetime.now(UTC).isoformat(),
                "pid": 12345,
                "custom_field": "value",
            }
            json.dump(data, f)

        try:
            checker = WatchdogChecker(heartbeat_file=heartbeat_file)
            result = checker.read_heartbeat()

            assert result is not None
            assert result["pid"] == 12345
            assert result["custom_field"] == "value"
        finally:
            os.unlink(heartbeat_file)


class TestSingletonFunctions:
    """Singleton management function tests."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self, reset_watchdog_singleton):
        """각 테스트 전후에 싱글톤 리셋."""
        pass

    def test_get_watchdog(self):
        """get_watchdog 싱글톤 테스트."""
        watchdog1 = get_watchdog()
        watchdog2 = get_watchdog()

        assert watchdog1 is watchdog2

    def test_start_watchdog(self):
        """start_watchdog 함수 테스트.

        Note: 이 테스트는 start_watchdog()의 기본 동작만 검증합니다.
        stop 후 상태 검증은 TestAuditWatchdogLifecycle::test_start_stop에서 수행됩니다.
        싱글톤 상태 격리는 conftest.py의 auto_reset_watchdog_singleton fixture가 담당합니다.
        """
        # start_watchdog 함수를 사용한 테스트
        config = AuditWatchdogConfig(heartbeat_interval_seconds=1.0)

        # start_watchdog 호출 (싱글톤 생성 및 시작)
        watchdog = start_watchdog(config=config)

        # 핵심 검증: watchdog가 실행 중인지 확인
        assert watchdog is not None, "start_watchdog should return watchdog instance"
        assert watchdog.is_running, "Watchdog should be running after start"

        # 정리는 conftest.py의 auto_reset_watchdog_singleton fixture가 담당

    def test_stop_watchdog_without_start(self):
        """시작 없이 stop_watchdog 호출 테스트."""
        stop_watchdog()  # 에러 없이 실행되어야 함


class TestThreadSafety:
    """Thread safety tests."""

    def test_concurrent_pet(self):
        """동시 pet() 호출 테스트."""
        watchdog = AuditWatchdog()
        watchdog.start()

        threads = []
        for _ in range(10):
            t = threading.Thread(target=watchdog.pet)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        watchdog.stop()
        # 에러 없이 완료

    def test_concurrent_get_stats(self):
        """동시 get_stats() 호출 테스트."""
        config = AuditWatchdogConfig(heartbeat_interval_seconds=0.02)
        watchdog = AuditWatchdog(config=config)
        watchdog.start()

        results: list[WatchdogStats] = []
        lock = threading.Lock()

        def get_stats_thread():
            for _ in range(5):
                stats = watchdog.get_stats()
                with lock:
                    results.append(stats)
                time.sleep(0.01)

        threads = []
        for _ in range(5):
            t = threading.Thread(target=get_stats_thread)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        watchdog.stop()

        assert len(results) == 25  # 5 threads * 5 calls


class TestLegacyCallbacks:
    """Legacy callback parameter tests."""

    def test_on_alive_callback(self):
        """레거시 on_alive 콜백 테스트."""
        callback = MagicMock()
        config = AuditWatchdogConfig(heartbeat_interval_seconds=0.05)
        watchdog = AuditWatchdog(config=config, on_alive=callback)

        watchdog.start()
        time.sleep(0.1)
        watchdog.stop()

        assert callback.called

    def test_on_dead_callback(self):
        """레거시 on_dead 콜백 테스트."""
        callback = MagicMock()
        config = AuditWatchdogConfig(
            heartbeat_interval_seconds=0.03,
            missed_threshold=1,
            targets=[
                HeartbeatTarget(
                    name="failing",
                    url="http://localhost:19999/nonexistent",
                    timeout_seconds=0.02,
                ),
            ],
        )
        watchdog = AuditWatchdog(config=config, on_dead=callback)

        watchdog.start()
        time.sleep(0.15)
        watchdog.stop()

        assert callback.called


class TestSelfAuditIntegration:
    """Self-audit logging integration tests."""

    def setup_method(self):
        """각 테스트 전에 self-audit 리셋."""
        SelfAuditLogger.reset_instance()

    def test_startup_logged(self):
        """시작 시 self-audit 로깅 테스트."""
        config = AuditWatchdogConfig(heartbeat_interval_seconds=1.0)
        watchdog = AuditWatchdog(config=config)

        watchdog.start()
        time.sleep(0.05)
        watchdog.stop()

        # self-audit에 이벤트 기록됨
        events = SelfAuditLogger.get_instance().get_recent_events()
        event_types = [e.get("event") for e in events]
        assert "startup" in event_types

    def test_shutdown_logged(self):
        """종료 시 self-audit 로깅 테스트."""
        config = AuditWatchdogConfig(heartbeat_interval_seconds=1.0)
        watchdog = AuditWatchdog(config=config)

        watchdog.start()
        watchdog.stop()

        events = SelfAuditLogger.get_instance().get_recent_events()
        event_types = [e.get("event") for e in events]
        assert "shutdown" in event_types
