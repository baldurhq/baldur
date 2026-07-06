"""NoOpGovernanceChecker + GovernanceChecker Protocol unit tests (516 D2).

Scope:
- ``NoOpGovernanceChecker`` shape: every method returns the fail-open answer
  so OSS callers that resolve via ``ProviderRegistry.governance.get()`` never
  receive ``None`` and never need ``is None`` branches.
- ``GovernanceChecker`` is a ``runtime_checkable`` Protocol — both the NoOp
  default and any duck-typed conforming class must pass ``isinstance(..,
  GovernanceChecker)``.

These tests do NOT cover the PRO concrete adapter; that lives in
``tests/integration/services/governance/`` (PRO surface) and in
``tests/unit/services/governance/test_pro_governance_checker.py``.
"""

from __future__ import annotations

import pytest

from baldur.interfaces.governance import (
    GovernanceChecker,
    NoOpGovernanceChecker,
)
from baldur.models.governance import GovernanceCheckResult

# =============================================================================
# NoOp shape — fail-open answers
# =============================================================================


class TestNoOpGovernanceCheckerBehavior:
    """NoOpGovernanceChecker returns fail-open answers for every method."""

    def test_check_all_governance_returns_allowed_result(self):
        checker = NoOpGovernanceChecker()

        result = checker.check_all_governance()

        assert isinstance(result, GovernanceCheckResult)
        assert result.allowed is True

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"operation_name": "smoke_test"},
            {"check_kill_switch": False, "check_emergency": False},
            {
                "operation_name": "replay",
                "service_name": "dlq",
                "domain": "baldur",
                "tier_id": "premium",
                "region": "us-east-1",
                "resource_context": {"shard": 7},
                "audit_on_block": False,
            },
            {"emergency_min_level": 3, "check_error_budget": True},
        ],
    )
    def test_check_all_governance_ignores_kwargs_and_stays_open(self, kwargs):
        """No kwarg combination changes the fail-open answer.

        The NoOp default fails open by construction — it has no governance
        pipeline to consult. Any kwarg shape that PRO accepts must therefore
        result in ``allowed=True`` here so OSS callers can invoke the method
        unconditionally without branching on absence of PRO.
        """
        checker = NoOpGovernanceChecker()

        result = checker.check_all_governance(**kwargs)

        assert result.allowed is True

    def test_is_system_enabled_returns_true(self):
        """Kill Switch is "not active" when PRO is absent."""
        checker = NoOpGovernanceChecker()

        assert checker.is_system_enabled() is True

    @pytest.mark.parametrize("min_level", [None, 1, 2, 3, 4])
    def test_is_emergency_blocking_returns_not_blocked(self, min_level):
        """``is_emergency_blocking`` returns ``(False, "UNKNOWN")`` regardless
        of ``min_level`` — without a PRO pipeline there is no emergency state.
        """
        checker = NoOpGovernanceChecker()

        is_blocked, level_name = checker.is_emergency_blocking(min_level=min_level)

        assert is_blocked is False
        assert level_name == "UNKNOWN"

    @pytest.mark.parametrize(
        ("tier_id", "region"),
        [
            (None, None),
            ("premium", None),
            (None, "us-east-1"),
            ("premium", "us-east-1"),
        ],
    )
    def test_is_error_budget_blocking_returns_full_budget(self, tier_id, region):
        """Error budget is "full" when PRO is absent.

        The triple ``(is_blocked, current_pct, threshold_pct)`` reports
        ``(False, 100.0, 0.0)`` — every (tier, region) shape used by
        ``ThrottleGovernanceGuard._check_error_budget`` must keep the guard
        fail-open.
        """
        checker = NoOpGovernanceChecker()

        is_blocked, current_pct, threshold_pct = checker.is_error_budget_blocking(
            tier_id=tier_id, region=region
        )

        assert is_blocked is False
        assert current_pct == 100.0
        assert threshold_pct == 0.0

    def test_invalidate_governance_cache_returns_none(self):
        """518 (b): added to the Protocol; NoOp default is a no-op returning None.

        OSS callers (event-bus default_handlers) invoke this on Kill Switch /
        Emergency / Error Budget state transitions. Without a PRO pipeline
        there is no cache to invalidate, so the contract is "swallow the call
        silently and return None" — no exception, no None-branch on the caller.
        """
        checker = NoOpGovernanceChecker()

        assert checker.invalidate_governance_cache() is None

    def test_reset_governance_pipeline_cache_returns_none(self):
        """518 (b): added to the Protocol; NoOp default is a no-op returning None.

        ``protect.reset_protect_caches()`` calls this in tests/REPL flows that
        mutate settings. NoOp must accept the call so OSS-only test envs don't
        need to branch on PRO presence.
        """
        checker = NoOpGovernanceChecker()

        assert checker.reset_governance_pipeline_cache() is None


# =============================================================================
# Protocol conformance — runtime_checkable structural typing
# =============================================================================


class TestGovernanceCheckerProtocolContract:
    """GovernanceChecker is a runtime_checkable Protocol — structural conformance."""

    def test_noop_checker_satisfies_governance_checker_protocol(self):
        checker = NoOpGovernanceChecker()

        assert isinstance(checker, GovernanceChecker)

    def test_unrelated_class_does_not_satisfy_protocol(self):
        class _Unrelated:
            def hello(self) -> str:
                return "world"

        assert not isinstance(_Unrelated(), GovernanceChecker)

    def test_duck_typed_checker_satisfies_protocol(self):
        """A class implementing all required methods conforms structurally."""

        class _DuckChecker:
            def check_all_governance(self, **kwargs):
                return GovernanceCheckResult.allowed_result()

            def is_system_enabled(self) -> bool:
                return True

            def is_emergency_blocking(self, min_level=None):
                return False, "UNKNOWN"

            def is_error_budget_blocking(self, tier_id=None, region=None):
                return False, 100.0, 0.0

            def invalidate_governance_cache(self) -> None:
                return None

            def reset_governance_pipeline_cache(self) -> None:
                return None

        assert isinstance(_DuckChecker(), GovernanceChecker)
