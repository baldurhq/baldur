"""
Unit tests for resilience/policies/__init__.py and sinks re-exports (#231).

Test targets:
- resilience/policies/__init__.py (unified re-export)
- resilience/policies/sinks/__init__.py (DLQSink re-export)
- resilience/policies/sinks/dlq.py (original source of the DLQSink re-export)

Complies with UNIT_TEST_GUIDELINES.md:
- Contract: hardcoded check that the names listed in __all__ are importable
"""

from __future__ import annotations

import importlib.util

import pytest

# =============================================================================
# Contract — resilience/policies/__init__.py re-export
# =============================================================================


class TestPoliciesInitReexportContract:
    """policies/__init__.py re-export contract — verifies names declared in __all__ are importable."""

    def test_policy_outcome_export(self):
        """PolicyOutcome is importable from the package."""
        from baldur.resilience.policies import PolicyOutcome

        assert PolicyOutcome is not None

    def test_policy_result_export(self):
        """PolicyResult is importable from the package."""
        from baldur.resilience.policies import PolicyResult

        assert PolicyResult is not None

    def test_policy_context_export(self):
        """PolicyContext is importable from the package."""
        from baldur.resilience.policies import PolicyContext

        assert PolicyContext is not None

    def test_policy_rejected_exception_export(self):
        """PolicyRejectedException is importable from the package."""
        from baldur.resilience.policies import PolicyRejectedException

        assert PolicyRejectedException is not None

    def test_resilience_policy_export(self):
        """ResiliencePolicy is importable from the package."""
        from baldur.resilience.policies import ResiliencePolicy

        assert ResiliencePolicy is not None

    def test_async_resilience_policy_export(self):
        """AsyncResiliencePolicy is importable from the package."""
        from baldur.resilience.policies import AsyncResiliencePolicy

        assert AsyncResiliencePolicy is not None

    def test_policy_composer_export(self):
        """PolicyComposer is importable from the package."""
        from baldur.resilience.policies import PolicyComposer

        assert PolicyComposer is not None

    def test_async_policy_composer_export(self):
        """AsyncPolicyComposer is importable from the package."""
        from baldur.resilience.policies import AsyncPolicyComposer

        assert AsyncPolicyComposer is not None

    def test_compose_export(self):
        """compose is importable from the package."""
        from baldur.resilience.policies import compose

        assert compose is not None

    def test_compose_async_export(self):
        """compose_async is importable from the package."""
        from baldur.resilience.policies import compose_async

        assert compose_async is not None

    def test_fallback_policy_export(self):
        """FallbackPolicy is importable from the package."""
        from baldur.resilience.policies import FallbackPolicy

        assert FallbackPolicy is not None

    def test_async_fallback_policy_export(self):
        """AsyncFallbackPolicy is importable from the package."""
        from baldur.resilience.policies import AsyncFallbackPolicy

        assert AsyncFallbackPolicy is not None

    def test_partition_aware_chain_export(self):
        """partition_aware_chain is importable from the package."""
        from baldur.resilience.policies import partition_aware_chain

        assert partition_aware_chain is not None

    def test_kill_switch_guard_not_exported(self):
        """No kill-switch guard is exported: the switch rides the resolver."""
        import baldur.resilience.policies as policies

        assert not hasattr(policies, "KillSwitchGuard")
        assert "KillSwitchGuard" not in policies.__all__

    def test_error_budget_guard_export(self):
        """ErrorBudgetGuard is importable from the package."""
        from baldur.resilience.policies import ErrorBudgetGuard

        assert ErrorBudgetGuard is not None

    def test_audit_hook_export(self):
        """AuditHook is importable from the package."""
        from baldur.resilience.policies import AuditHook

        assert AuditHook is not None

    def test_metrics_hook_export(self):
        """MetricsHook is importable from the package."""
        from baldur.resilience.policies import MetricsHook

        assert MetricsHook is not None

    def test_event_bus_hook_export(self):
        """EventBusHook is importable from the package."""
        from baldur.resilience.policies import EventBusHook

        assert EventBusHook is not None

    def test_dlq_sink_export(self):
        """DLQSink is importable from the package."""
        from baldur.resilience.policies import DLQSink

        assert DLQSink is not None

    def test_standard_pipeline_export(self):
        """standard_pipeline is importable from the package."""
        from baldur.resilience.policies import standard_pipeline

        assert standard_pipeline is not None

    def test_ha_pipeline_export(self):
        """ha_pipeline is importable from the package."""
        from baldur.resilience.policies import ha_pipeline

        assert ha_pipeline is not None


# =============================================================================
# Contract — lazy import (HedgingPolicy, etc.)
# =============================================================================


class TestPoliciesLazyImportContract:
    """__getattr__ lazy import contract."""

    def test_hedging_policy_lazy_import(self):
        """HedgingPolicy is accessible via lazy import."""
        from baldur.resilience.policies import HedgingPolicy

        assert HedgingPolicy is not None

    def test_async_hedging_policy_lazy_import(self):
        """AsyncHedgingPolicy is accessible via lazy import."""
        from baldur.resilience.policies import AsyncHedgingPolicy

        assert AsyncHedgingPolicy is not None

    def test_hedging_config_update_hook_lazy_import(self):
        """HedgingConfigUpdateHook is accessible via lazy import."""
        from baldur.resilience.policies import HedgingConfigUpdateHook

        assert HedgingConfigUpdateHook is not None

    def test_invalid_attr_raises_attribute_error(self):
        """Accessing a nonexistent attribute raises AttributeError.

        Note: a from ... import statement automatically converts the AttributeError
        from __getattr__ into ImportError. Verify with getattr directly.
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
        """``ThrottlePolicy`` is absent from ``__all__`` (honest advertisement).
        With the PRO package installed the name still resolves for existing
        import statements; pure OSS gets an actionable PRO-tier
        ``AttributeError`` instead of a bare unknown-attribute error."""
        import baldur.resilience.policies as policies_mod

        assert "ThrottlePolicy" not in policies_mod.__all__
        if importlib.util.find_spec("baldur_pro") is not None:
            assert policies_mod.ThrottlePolicy is not None
        else:
            with pytest.raises(AttributeError, match="PRO tier"):
                _ = policies_mod.ThrottlePolicy


# =============================================================================
# Contract — sinks/__init__.py re-export
# =============================================================================


class TestSinksInitReexportContract:
    """sinks/__init__.py re-export contract."""

    def test_dlq_sink_from_sinks_package(self):
        """DLQSink is importable from the sinks package."""
        from baldur.resilience.policies.sinks import DLQSink

        assert DLQSink is not None

    def test_dlq_sink_is_same_class(self):
        """The sinks package's DLQSink and the original DLQSink are the same class."""
        from baldur.resilience.policies.sinks import DLQSink as SinksDLQ
        from baldur.services.retry_handler.sinks import DLQSink as OrigDLQ

        assert SinksDLQ is OrigDLQ
