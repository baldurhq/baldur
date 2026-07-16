"""
resilience/policies/__init__.py 및 sinks re-export 단위 테스트 (#231).

테스트 대상:
- resilience/policies/__init__.py (통합 re-export)
- resilience/policies/sinks/__init__.py (DLQSink re-export)
- resilience/policies/sinks/dlq.py (DLQSink re-export 원본)

UNIT_TEST_GUIDELINES.md 준수:
- 계약 검증(Contract): __all__ 목록에 명시된 이름들이 import 가능한지 하드코딩 검증
"""

from __future__ import annotations

import pytest

# =============================================================================
# 계약 검증 — resilience/policies/__init__.py re-export
# =============================================================================


class TestPoliciesInitReexportContract:
    """policies/__init__.py re-export 계약 검증 — __all__에 선언된 이름 import 확인."""

    def test_policy_outcome_export(self):
        """PolicyOutcome이 패키지에서 import 가능하다."""
        from baldur.resilience.policies import PolicyOutcome

        assert PolicyOutcome is not None

    def test_policy_result_export(self):
        """PolicyResult가 패키지에서 import 가능하다."""
        from baldur.resilience.policies import PolicyResult

        assert PolicyResult is not None

    def test_policy_context_export(self):
        """PolicyContext가 패키지에서 import 가능하다."""
        from baldur.resilience.policies import PolicyContext

        assert PolicyContext is not None

    def test_policy_rejected_exception_export(self):
        """PolicyRejectedException이 패키지에서 import 가능하다."""
        from baldur.resilience.policies import PolicyRejectedException

        assert PolicyRejectedException is not None

    def test_resilience_policy_export(self):
        """ResiliencePolicy가 패키지에서 import 가능하다."""
        from baldur.resilience.policies import ResiliencePolicy

        assert ResiliencePolicy is not None

    def test_async_resilience_policy_export(self):
        """AsyncResiliencePolicy가 패키지에서 import 가능하다."""
        from baldur.resilience.policies import AsyncResiliencePolicy

        assert AsyncResiliencePolicy is not None

    def test_policy_composer_export(self):
        """PolicyComposer가 패키지에서 import 가능하다."""
        from baldur.resilience.policies import PolicyComposer

        assert PolicyComposer is not None

    def test_async_policy_composer_export(self):
        """AsyncPolicyComposer가 패키지에서 import 가능하다."""
        from baldur.resilience.policies import AsyncPolicyComposer

        assert AsyncPolicyComposer is not None

    def test_compose_export(self):
        """compose가 패키지에서 import 가능하다."""
        from baldur.resilience.policies import compose

        assert compose is not None

    def test_compose_async_export(self):
        """compose_async가 패키지에서 import 가능하다."""
        from baldur.resilience.policies import compose_async

        assert compose_async is not None

    def test_fallback_policy_export(self):
        """FallbackPolicy가 패키지에서 import 가능하다."""
        from baldur.resilience.policies import FallbackPolicy

        assert FallbackPolicy is not None

    def test_async_fallback_policy_export(self):
        """AsyncFallbackPolicy가 패키지에서 import 가능하다."""
        from baldur.resilience.policies import AsyncFallbackPolicy

        assert AsyncFallbackPolicy is not None

    def test_partition_aware_chain_export(self):
        """partition_aware_chain이 패키지에서 import 가능하다."""
        from baldur.resilience.policies import partition_aware_chain

        assert partition_aware_chain is not None

    def test_kill_switch_guard_export(self):
        """KillSwitchGuard가 패키지에서 import 가능하다."""
        from baldur.resilience.policies import KillSwitchGuard

        assert KillSwitchGuard is not None

    def test_error_budget_guard_export(self):
        """ErrorBudgetGuard가 패키지에서 import 가능하다."""
        from baldur.resilience.policies import ErrorBudgetGuard

        assert ErrorBudgetGuard is not None

    def test_audit_hook_export(self):
        """AuditHook이 패키지에서 import 가능하다."""
        from baldur.resilience.policies import AuditHook

        assert AuditHook is not None

    def test_metrics_hook_export(self):
        """MetricsHook이 패키지에서 import 가능하다."""
        from baldur.resilience.policies import MetricsHook

        assert MetricsHook is not None

    def test_event_bus_hook_export(self):
        """EventBusHook이 패키지에서 import 가능하다."""
        from baldur.resilience.policies import EventBusHook

        assert EventBusHook is not None

    def test_dlq_sink_export(self):
        """DLQSink가 패키지에서 import 가능하다."""
        from baldur.resilience.policies import DLQSink

        assert DLQSink is not None

    def test_standard_pipeline_export(self):
        """standard_pipeline이 패키지에서 import 가능하다."""
        from baldur.resilience.policies import standard_pipeline

        assert standard_pipeline is not None

    def test_ha_pipeline_export(self):
        """ha_pipeline이 패키지에서 import 가능하다."""
        from baldur.resilience.policies import ha_pipeline

        assert ha_pipeline is not None


# =============================================================================
# 계약 검증 — lazy import (HedgingPolicy 등)
# =============================================================================


class TestPoliciesLazyImportContract:
    """__getattr__ lazy import 계약 검증."""

    def test_hedging_policy_lazy_import(self):
        """HedgingPolicy가 lazy import로 접근 가능하다."""
        from baldur.resilience.policies import HedgingPolicy

        assert HedgingPolicy is not None

    def test_async_hedging_policy_lazy_import(self):
        """AsyncHedgingPolicy가 lazy import로 접근 가능하다."""
        from baldur.resilience.policies import AsyncHedgingPolicy

        assert AsyncHedgingPolicy is not None

    def test_hedging_config_update_hook_lazy_import(self):
        """HedgingConfigUpdateHook이 lazy import로 접근 가능하다."""
        from baldur.resilience.policies import HedgingConfigUpdateHook

        assert HedgingConfigUpdateHook is not None

    def test_invalid_attr_raises_attribute_error(self):
        """존재하지 않는 속성 접근 시 AttributeError가 발생한다.

        주의: from ... import 구문은 __getattr__의 AttributeError를
        ImportError로 자동 변환한다. 직접 getattr로 검증.
        """
        import baldur.resilience.policies as policies_mod

        with pytest.raises(AttributeError):
            policies_mod.NonExistentPolicy


# =============================================================================
# BulkheadPolicy (core import) / ThrottlePolicy (PEP 562 lazy)
# =============================================================================


class TestPoliciesLazyImportBehavior:
    """BulkheadPolicy is a real core import; ThrottlePolicy stays __getattr__.

    BulkheadPolicy went core-tier (``baldur.services.bulkhead.policy``) with a
    module-level import — no PEP 562 involved. ThrottlePolicy's engine stays
    in the licensed package, so it remains resolvable-but-not-advertised via
    the module-level ``__getattr__`` lazy import.
    """

    def test_bulkhead_policy_resolves_core_concrete_class(self):
        """``from baldur.resilience.policies import BulkheadPolicy`` returns the
        core concrete class (identity-preserving)."""
        from baldur.resilience.policies import BulkheadPolicy
        from baldur.services.bulkhead.policy import (
            BulkheadPolicy as CoreBulkheadPolicy,
        )

        assert BulkheadPolicy is CoreBulkheadPolicy

    def test_throttle_policy_lazy_import_resolves_pro_concrete_class(self):
        """``ThrottlePolicy`` already routes through PEP 562 (existing precedent)."""
        pytest.importorskip("baldur_pro")
        from baldur.resilience.policies import ThrottlePolicy
        from baldur_pro.services.throttle.policy import (
            ThrottlePolicy as PROThrottlePolicy,
        )

        assert ThrottlePolicy is PROThrottlePolicy

    def test_bulkhead_policy_in_module_all(self):
        """``BulkheadPolicy`` is advertised in ``__all__`` so star-import works."""
        import baldur.resilience.policies as policies_mod

        assert "BulkheadPolicy" in policies_mod.__all__

    def test_throttle_policy_soft_removed_but_resolvable(self):
        """``ThrottlePolicy`` is absent from ``__all__`` (honest advertisement)
        yet still resolvable for existing import statements."""
        import baldur.resilience.policies as policies_mod

        assert "ThrottlePolicy" not in policies_mod.__all__
        assert policies_mod.ThrottlePolicy is not None


# =============================================================================
# 계약 검증 — sinks/__init__.py re-export
# =============================================================================


class TestSinksInitReexportContract:
    """sinks/__init__.py re-export 계약 검증."""

    def test_dlq_sink_from_sinks_package(self):
        """DLQSink가 sinks 패키지에서 import 가능하다."""
        from baldur.resilience.policies.sinks import DLQSink

        assert DLQSink is not None

    def test_dlq_sink_is_same_class(self):
        """sinks 패키지의 DLQSink와 원본 DLQSink는 동일 클래스이다."""
        from baldur.resilience.policies.sinks import DLQSink as SinksDLQ
        from baldur.services.retry_handler.sinks import DLQSink as OrigDLQ

        assert SinksDLQ is OrigDLQ
