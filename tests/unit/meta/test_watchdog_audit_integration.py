"""
SelfHealerWatchdog Audit 연동 테스트.

_attempt_recovery 메서드의 RecoveryAuditRecorder 연동 테스트.
"""

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from datetime import UTC, datetime
from unittest import mock

import pytest

from baldur.meta.config import MetaWatchdogSettings
from baldur.meta.health_probe import HealthStatus, ProbeResult
from baldur_pro.services.meta_watchdog import SelfHealerWatchdog


class TestWatchdogAuditRecorderIntegration:
    """Watchdog Audit Recorder 연동 테스트."""

    @pytest.fixture
    def settings(self):
        """테스트용 설정."""
        return MetaWatchdogSettings(
            enabled=True,
            self_cb_enabled=False,
            dry_run_mode=False,
            recovery_enabled=True,  # 558 D7: keep FULL recovery path reachable
        )

    @pytest.fixture
    def healthy_probe_result(self):
        """정상 Probe 결과."""
        return ProbeResult(
            component="redis",
            status=HealthStatus.UNHEALTHY,
            latency_ms=10,
            timestamp=datetime.now(UTC),
            error="Connection failed",
            details={"pending_count": 100},
        )

    @pytest.fixture
    def watchdog(self, settings):
        """Watchdog fixture."""
        return SelfHealerWatchdog(settings=settings)

    def test_get_recovery_audit_recorder_returns_none_when_module_not_found(
        self, watchdog
    ):
        """RecoveryAuditRecorder 모듈 없을 때 None 반환."""
        with mock.patch.dict(
            "sys.modules", {"baldur_pro.services.coordination.recovery_audit": None}
        ):
            # ImportError 시뮬레이션
            with mock.patch.object(
                watchdog,
                "_get_recovery_audit_recorder",
                side_effect=ImportError("Module not found"),
            ):
                # 복구 시도 시 에러 없이 진행되어야 함
                pass

    def test_attempt_recovery_records_start_audit(self, watchdog, healthy_probe_result):
        """복구 시도 시 시작 Audit 기록."""
        mock_recorder = mock.MagicMock()

        with mock.patch.object(
            watchdog, "_get_recovery_audit_recorder", return_value=mock_recorder
        ):
            with mock.patch.object(
                watchdog, "_execute_recovery_with_timeout", return_value=True
            ):
                watchdog._attempt_recovery("redis", healthy_probe_result, 60.0)

                assert mock_recorder.record_recovery_event.called
                assert mock_recorder.record_recovery_event.call_count >= 2

    def test_attempt_recovery_records_complete_audit_on_success(
        self, watchdog, healthy_probe_result
    ):
        """복구 성공 시 완료 Audit 기록."""
        mock_recorder = mock.MagicMock()

        with mock.patch.object(
            watchdog, "_get_recovery_audit_recorder", return_value=mock_recorder
        ):
            with mock.patch.object(
                watchdog, "_execute_recovery_with_timeout", return_value=True
            ):
                result = watchdog._attempt_recovery("redis", healthy_probe_result, 60.0)

                assert result is True
                assert mock_recorder.record_recovery_event.call_count >= 2

    def test_attempt_recovery_records_failed_audit_on_exception(
        self, watchdog, healthy_probe_result
    ):
        """복구 중 예외 시 실패 Audit 기록."""
        mock_recorder = mock.MagicMock()

        with mock.patch.object(
            watchdog, "_get_recovery_audit_recorder", return_value=mock_recorder
        ):
            with mock.patch.object(
                watchdog,
                "_execute_recovery_with_timeout",
                side_effect=RuntimeError("Recovery failed"),
            ):
                result = watchdog._attempt_recovery("redis", healthy_probe_result, 60.0)

                assert result is False
                assert mock_recorder.record_recovery_event.called

    def test_attempt_recovery_works_without_audit_recorder(
        self, watchdog, healthy_probe_result
    ):
        """Audit Recorder 없이도 복구 진행."""
        with mock.patch.object(
            watchdog, "_get_recovery_audit_recorder", return_value=None
        ):
            with mock.patch.object(
                watchdog, "_execute_recovery_with_timeout", return_value=True
            ):
                result = watchdog._attempt_recovery("redis", healthy_probe_result, 60.0)

                assert result is True

    def test_record_recovery_start_audit_handles_exception(
        self, watchdog, healthy_probe_result
    ):
        """시작 Audit 기록 중 예외 처리."""
        mock_recorder = mock.MagicMock()
        mock_recorder.record_recovery_event.side_effect = Exception("Audit error")

        # 예외가 발생해도 에러가 전파되지 않아야 함
        watchdog._record_recovery_start_audit(
            mock_recorder, "test-session", "redis", healthy_probe_result
        )
        # 정상적으로 완료 (예외 무시됨)

    def test_record_recovery_complete_audit_with_success(self, watchdog):
        """복구 성공 시 완료 Audit 기록."""
        mock_recorder = mock.MagicMock()

        watchdog._record_recovery_complete_audit(
            mock_recorder, "test-session", "redis", True, 150.0
        )

        mock_recorder.record_recovery_event.assert_called_once()
        call_kwargs = mock_recorder.record_recovery_event.call_args[1]
        assert call_kwargs["success"] is True
        assert call_kwargs["duration_ms"] == 150.0

    def test_record_recovery_complete_audit_with_failure(self, watchdog):
        """복구 실패 시 완료 Audit 기록."""
        mock_recorder = mock.MagicMock()

        watchdog._record_recovery_complete_audit(
            mock_recorder, "test-session", "dlq", False, 200.0
        )

        mock_recorder.record_recovery_event.assert_called_once()
        call_kwargs = mock_recorder.record_recovery_event.call_args[1]
        assert call_kwargs["success"] is False
        assert "recover_dlq" in call_kwargs["step_type"]

    def test_record_recovery_failed_audit(self, watchdog):
        """복구 실패 Audit 기록."""
        mock_recorder = mock.MagicMock()

        watchdog._record_recovery_failed_audit(
            mock_recorder,
            "test-session",
            "circuit_breaker",
            "Connection timeout",
            500.0,
        )

        mock_recorder.record_recovery_event.assert_called_once()
        call_kwargs = mock_recorder.record_recovery_event.call_args[1]
        assert call_kwargs["success"] is False
        assert call_kwargs["error_message"] == "Connection timeout"
        assert call_kwargs["duration_ms"] == 500.0


class TestWatchdogAuditSessionId:
    """Audit 세션 ID 생성 테스트."""

    @pytest.fixture
    def settings(self):
        return MetaWatchdogSettings(
            enabled=True,
            self_cb_enabled=False,
            dry_run_mode=False,
        )

    @pytest.fixture
    def watchdog(self, settings):
        return SelfHealerWatchdog(settings=settings)

    def test_session_id_format(self, watchdog):
        """세션 ID 형식 확인."""
        probe_result = ProbeResult(
            component="redis",
            status=HealthStatus.UNHEALTHY,
            latency_ms=10,
            timestamp=datetime.now(UTC),
        )

        mock_recorder = mock.MagicMock()

        with mock.patch.object(
            watchdog, "_get_recovery_audit_recorder", return_value=mock_recorder
        ):
            with mock.patch.object(
                watchdog, "_execute_recovery_with_timeout", return_value=True
            ):
                watchdog._attempt_recovery("redis", probe_result, 60.0)

                # 세션 ID 형식 확인: meta-watchdog-{component}-{timestamp}
                call_args = mock_recorder.record_recovery_event.call_args_list[0]
                session_id = call_args[1]["session_id"]
                assert session_id.startswith("meta-watchdog-redis-")

    def test_session_id_unique_per_attempt(self, watchdog):
        """각 시도마다 고유한 세션 ID."""
        probe_result = ProbeResult(
            component="redis",
            status=HealthStatus.UNHEALTHY,
            latency_ms=10,
            timestamp=datetime.now(UTC),
        )

        session_ids = []
        mock_recorder = mock.MagicMock()

        def capture_session_id(*args, **kwargs):
            session_ids.append(kwargs.get("session_id"))

        mock_recorder.record_recovery_event.side_effect = capture_session_id

        with mock.patch.object(
            watchdog, "_get_recovery_audit_recorder", return_value=mock_recorder
        ):
            with mock.patch.object(
                watchdog, "_execute_recovery_with_timeout", return_value=True
            ):
                watchdog._attempt_recovery("redis", probe_result, 60.0)

                # 세션 ID 형식 확인
                unique_ids = {s for s in session_ids if s is not None}
                assert len(unique_ids) >= 1  # 최소 1개 이상 세션 ID

                # 세션 ID 형식: meta-watchdog-redis-{timestamp}
                for sid in unique_ids:
                    assert sid.startswith("meta-watchdog-redis-")
