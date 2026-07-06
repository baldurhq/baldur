"""
BackpressureGuard 단위 테스트.

테스트 대상: baldur.resilience.policies.guards.backpressure.BackpressureGuard

검증 범위:
- name 속성 계약값
- RateController.should_process() 결과에 따른 허용/거부
- RateController None 시 통과
- RateController 예외 시 Fail-Open
- Lazy import 및 캐싱 동작
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from structlog.testing import capture_logs

from baldur.resilience.policies.guards.backpressure import (
    BackpressureGuard,
)


def _emitted_warning(cap_logs: list[dict], event_name: str) -> bool:
    """Check structlog event was emitted at WARNING level (D5 lock)."""
    return any(
        e.get("event") == event_name and e.get("log_level") == "warning"
        for e in cap_logs
    )


# =============================================================================
# name 계약 검증
# =============================================================================


class TestBackpressureGuardNameContract:
    """BackpressureGuard.name 계약값 검증."""

    def test_name_is_backpressure(self):
        """name은 'backpressure'여야 한다."""
        guard = BackpressureGuard()
        assert guard.name == "backpressure"


# =============================================================================
# check() 동작 검증
# =============================================================================


class TestBackpressureGuardCheckBehavior:
    """BackpressureGuard.check() 동작 검증."""

    def test_no_controller_passes(self):
        """RateController가 None이면 통과."""
        guard = BackpressureGuard(rate_controller=None)
        guard._initialized = True  # lazy import 스킵
        result = guard.check()
        assert result.allowed is True

    def test_should_process_true_allows(self):
        """should_process()=True이면 allowed=True."""
        mock_controller = MagicMock()
        mock_controller.should_process.return_value = True
        guard = BackpressureGuard(rate_controller=mock_controller)

        result = guard.check()
        assert result.allowed is True

    def test_should_process_false_rejects(self):
        """should_process()=False이면 allowed=False."""
        mock_controller = MagicMock()
        mock_controller.should_process.return_value = False
        mock_state = MagicMock()
        mock_state.level.value = "HIGH"
        mock_state.queue_size = 1000
        mock_state.current_rate = 50.0
        mock_controller.get_state.return_value = mock_state
        guard = BackpressureGuard(rate_controller=mock_controller)

        result = guard.check()
        assert result.allowed is False
        assert "backpressure" in result.reason

    def test_reject_metadata_contains_state_info(self):
        """거부 시 metadata에 backpressure_level, queue_size 등이 포함."""
        mock_controller = MagicMock()
        mock_controller.should_process.return_value = False
        mock_state = MagicMock()
        mock_state.level.value = "CRITICAL"
        mock_state.queue_size = 5000
        mock_state.current_rate = 200.0
        mock_controller.get_state.return_value = mock_state
        guard = BackpressureGuard(rate_controller=mock_controller)

        result = guard.check()
        assert result.metadata["backpressure_level"] == "CRITICAL"
        assert result.metadata["queue_size"] == 5000
        assert result.metadata["current_rate"] == 200.0

    def test_controller_exception_failopen(self):
        """should_process() 예외 시 Fail-Open (통과) + WARNING 로깅 (D5)."""
        mock_controller = MagicMock()
        mock_controller.should_process.side_effect = RuntimeError("controller error")
        guard = BackpressureGuard(rate_controller=mock_controller)

        with capture_logs() as cap_logs:
            result = guard.check()

        assert result.allowed is True
        assert _emitted_warning(cap_logs, "guard.check_failed_fail_open")


# =============================================================================
# Lazy import Fail-Open 동작 검증
# =============================================================================


class TestBackpressureGuardLazyImportBehavior:
    """RateController lazy import Fail-Open 동작 검증."""

    def test_import_error_failopen(self):
        """RateController import 실패 시 None 반환 → 통과."""
        guard = BackpressureGuard(rate_controller=None)
        with patch.dict("sys.modules", {"baldur.scaling.rate_controller": None}):
            instance = guard._get_rate_controller()
            assert instance is None
            assert guard._initialized is True

    def test_lazy_import_caches_result(self):
        """lazy import 결과가 캐싱되어 재사용."""
        mock_controller = MagicMock()
        guard = BackpressureGuard(rate_controller=None)
        mock_rc_module = MagicMock()
        mock_rc_module.get_rate_controller.return_value = mock_controller

        with patch.dict(
            "sys.modules", {"baldur.scaling.rate_controller": mock_rc_module}
        ):
            first = guard._get_rate_controller()
            second = guard._get_rate_controller()
            assert first is second
            assert guard._initialized is True

    def test_injected_controller_used_directly(self):
        """생성자 주입된 controller를 직접 사용."""
        mock_controller = MagicMock()
        guard = BackpressureGuard(rate_controller=mock_controller)
        assert guard._get_rate_controller() is mock_controller

    def test_controller_init_exception_failopen_warns(self):
        """lazy import 중 Exception 발생 시 Fail-Open + WARNING 로깅 (D5)."""
        guard = BackpressureGuard(rate_controller=None)
        mock_rc_module = MagicMock()
        mock_rc_module.get_rate_controller.side_effect = RuntimeError("init failed")

        with (
            patch.dict(
                "sys.modules", {"baldur.scaling.rate_controller": mock_rc_module}
            ),
            capture_logs() as cap_logs,
        ):
            instance = guard._get_rate_controller()

        assert instance is None
        assert guard._initialized is True
        assert _emitted_warning(cap_logs, "guard.check_failed_fail_open")
