"""
LoadSheddingGuard 단위 테스트.

테스트 대상: baldur.resilience.policies.guards.load_shedding.LoadSheddingGuard

검증 범위:
- name 속성 계약값
- context.extra["priority"] 기반 우선순위 체크
- context=None 또는 priority 미지정 시 통과
- CascadeLoadShedding import 실패 시 Fail-Open
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from structlog.testing import capture_logs

from baldur.interfaces.resilience_policy import PolicyContext
from baldur.resilience.policies.guards.load_shedding import (
    LoadSheddingGuard,
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


class TestLoadSheddingGuardNameContract:
    """LoadSheddingGuard.name 계약값 검증."""

    def test_name_is_load_shedding(self):
        """name은 'load_shedding'이어야 한다."""
        guard = LoadSheddingGuard()
        assert guard.name == "load_shedding"


# =============================================================================
# check() 동작 검증
# =============================================================================


class TestLoadSheddingGuardCheckBehavior:
    """LoadSheddingGuard.check() 동작 검증."""

    def test_no_shedding_instance_passes(self):
        """load_shedding 인스턴스가 None이면 통과."""
        guard = LoadSheddingGuard(load_shedding=None)
        guard._initialized = True  # lazy import 스킵
        result = guard.check()
        assert result.allowed is True

    def test_shedding_accepts_returns_allowed(self):
        """should_accept()가 통과하면 allowed=True."""
        mock_shedding = MagicMock()
        mock_shedding.should_accept.return_value = {"accepted": True}
        guard = LoadSheddingGuard(load_shedding=mock_shedding)

        ctx = PolicyContext(extra={"priority": 5})
        result = guard.check(context=ctx)
        assert result.allowed is True

    def test_shedding_rejects_returns_not_allowed(self):
        """should_accept()가 거부하면 allowed=False."""
        mock_shedding = MagicMock()
        mock_shedding.should_accept.return_value = {"accepted": False}
        guard = LoadSheddingGuard(load_shedding=mock_shedding)

        ctx = PolicyContext(extra={"priority": 10})
        result = guard.check(context=ctx)
        assert result.allowed is False
        assert "load_shedding_rejected" in result.reason

    def test_context_none_passes_with_priority_0(self):
        """context=None이면 priority=0으로 체크 (기본 통과)."""
        mock_shedding = MagicMock()
        mock_shedding.should_accept.return_value = {"accepted": True}
        guard = LoadSheddingGuard(load_shedding=mock_shedding)

        result = guard.check(context=None)
        assert result.allowed is True
        mock_shedding.should_accept.assert_called_once_with(priority=0)

    def test_context_without_priority_uses_0(self):
        """context.extra에 priority가 없으면 0 사용."""
        mock_shedding = MagicMock()
        mock_shedding.should_accept.return_value = {"accepted": True}
        guard = LoadSheddingGuard(load_shedding=mock_shedding)

        ctx = PolicyContext(extra={"other": "value"})
        guard.check(context=ctx)
        mock_shedding.should_accept.assert_called_once_with(priority=0)

    def test_shedding_exception_failopen(self):
        """should_accept() 예외 시 Fail-Open (통과) + WARNING 로깅 (D5)."""
        mock_shedding = MagicMock()
        mock_shedding.should_accept.side_effect = RuntimeError("shedding error")
        guard = LoadSheddingGuard(load_shedding=mock_shedding)

        with capture_logs() as cap_logs:
            result = guard.check()

        assert result.allowed is True
        assert _emitted_warning(cap_logs, "guard.check_failed_fail_open")


# =============================================================================
# Lazy import Fail-Open 동작 검증
# =============================================================================


class TestLoadSheddingGuardLazyImportBehavior:
    """CascadeLoadShedding lazy import Fail-Open 동작 검증."""

    def test_import_error_failopen(self):
        """CascadeLoadShedding import 실패 시 None 반환 → 통과."""
        guard = LoadSheddingGuard(load_shedding=None)
        with patch.dict("sys.modules", {"baldur.audit.cascade_load_shedding": None}):
            instance = guard._get_load_shedding()
            assert instance is None

    def test_lazy_import_caches_result(self):
        """lazy import 결과가 캐싱되어 두 번째 호출 시 재사용."""
        mock_shedding = MagicMock()
        guard = LoadSheddingGuard(load_shedding=None)
        mock_cls_module = MagicMock()
        mock_cls_module.get_cascade_load_shedding.return_value = mock_shedding

        with patch.dict(
            "sys.modules", {"baldur.audit.cascade_load_shedding": mock_cls_module}
        ):
            first = guard._get_load_shedding()
            second = guard._get_load_shedding()
            assert first is second
            assert guard._initialized is True

    def test_controller_init_exception_failopen_warns(self):
        """lazy import 중 Exception 발생 시 Fail-Open + WARNING 로깅 (D5)."""
        guard = LoadSheddingGuard(load_shedding=None)
        mock_cls_module = MagicMock()
        mock_cls_module.get_cascade_load_shedding.side_effect = RuntimeError(
            "init failed"
        )

        with (
            patch.dict(
                "sys.modules", {"baldur.audit.cascade_load_shedding": mock_cls_module}
            ),
            capture_logs() as cap_logs,
        ):
            instance = guard._get_load_shedding()

        assert instance is None
        assert guard._initialized is True
        assert _emitted_warning(cap_logs, "guard.check_failed_fail_open")
