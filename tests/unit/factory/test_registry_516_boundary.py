"""ProviderRegistry 516 OSS->PRO boundary sub-registries (516 D2).

Scope:
- ``ProviderRegistry`` exposes three new sub-registries — ``pool_monitor``,
  ``governance``, ``shutdown_integrations`` — created with the generic
  ``GenericProviderRegistry`` plumbing.
- OSS NoOp defaults are pre-registered for ``pool_monitor`` + ``governance``
  at module-import time so callers always resolve a usable provider without
  PRO. ``shutdown_integrations`` has no default — empty registry is the
  correct OSS shape.
- ``override()`` context manager swaps the resolved instance for tests
  without leaking state, which is the recommended seam for OSS callsites
  that consume the new sub-registries.

These tests do NOT exercise the PRO concrete adapter (see
``test_pro_governance_checker.py``) nor the bootstrap iteration over
``shutdown_integrations`` (see ``test_shutdown_order.py``).
"""

from __future__ import annotations

import pytest

from baldur.factory.base import GenericProviderRegistry
from baldur.factory.registry import ProviderRegistry
from baldur.interfaces.governance import (
    GovernanceChecker,
    NoOpGovernanceChecker,
)
from baldur.interfaces.pool_monitor import (
    NoOpPoolStatsProvider,
    PoolStatsProvider,
)


@pytest.fixture(autouse=True)
def ensure_oss_noop_defaults():
    """Restore the 516 OSS NoOp pre-registrations for each test.

    Other tests in the suite call ``ProviderRegistry.reset()`` which wipes
    the module-load NoOp registrations performed in
    ``baldur.factory.registry`` at import time (lines ~1090-1100). The
    project's conftest only re-asserts ``cache`` / ``queue`` defaults
    after such resets — the 516 sub-registries fall back to empty under
    xdist parallel scheduling and break this file's invariants.

    Re-registering here keeps the tests deterministic without expanding
    conftest's surface. Snapshot+restore preserves any PRO override the
    earlier test left in place.
    """
    gov_snapshot = ProviderRegistry.governance.save_state()
    pool_snapshot = ProviderRegistry.pool_monitor.save_state()

    if not ProviderRegistry.governance.has_provider("oss-noop"):
        ProviderRegistry.governance.register("oss-noop", NoOpGovernanceChecker)
    if ProviderRegistry.governance.get_default_name() is None:
        ProviderRegistry.governance.set_default("oss-noop")

    if not ProviderRegistry.pool_monitor.has_provider("oss-noop"):
        ProviderRegistry.pool_monitor.register("oss-noop", NoOpPoolStatsProvider)
    if ProviderRegistry.pool_monitor.get_default_name() is None:
        ProviderRegistry.pool_monitor.set_default("oss-noop")

    try:
        yield
    finally:
        ProviderRegistry.governance.restore_state(gov_snapshot)
        ProviderRegistry.pool_monitor.restore_state(pool_snapshot)


# =============================================================================
# Sub-registry existence + type — Contract
# =============================================================================


class TestRegistry516BoundaryContract:
    """Sub-registries exist and are typed GenericProviderRegistry instances."""

    def test_pool_monitor_sub_registry_exists(self):
        assert isinstance(ProviderRegistry.pool_monitor, GenericProviderRegistry)

    def test_governance_sub_registry_exists(self):
        assert isinstance(ProviderRegistry.governance, GenericProviderRegistry)

    def test_shutdown_integrations_sub_registry_exists(self):
        assert isinstance(
            ProviderRegistry.shutdown_integrations, GenericProviderRegistry
        )

    def test_oss_noop_governance_default_name_is_oss_noop(self):
        """Module-load default for ``governance`` is ``"oss-noop"``.

        Acceptable when PRO has registered "pro" and switched the default
        (a process that already imported baldur_pro): the only invariant
        we pin here is the OSS pre-registration of ``oss-noop``.
        """
        assert ProviderRegistry.governance.has_provider("oss-noop")

    def test_oss_noop_pool_monitor_default_name_is_oss_noop(self):
        assert ProviderRegistry.pool_monitor.has_provider("oss-noop")

    def test_shutdown_integrations_has_no_oss_noop_default(self):
        """``shutdown_integrations`` ships no NoOp — an empty registry means
        "no extra handlers to register," which is the correct OSS behavior
        (the bootstrap iteration simply loops zero times).
        """
        assert not ProviderRegistry.shutdown_integrations.has_provider("oss-noop")


# =============================================================================
# Default resolution — Behavior
# =============================================================================


class TestRegistry516DefaultResolutionBehavior:
    """``get()`` on the new sub-registries resolves to a usable instance."""

    def test_governance_get_resolves_to_governance_checker(self):
        instance = ProviderRegistry.governance.get()

        # Structural conformance — works for either OSS NoOp or PRO concrete.
        assert isinstance(instance, GovernanceChecker)

    def test_pool_monitor_get_resolves_to_pool_stats_provider(self):
        instance = ProviderRegistry.pool_monitor.get()

        assert isinstance(instance, PoolStatsProvider)

    def test_governance_oss_noop_resolves_to_fail_open(self):
        """Resolving "oss-noop" by name returns a fail-open NoOpGovernanceChecker."""
        instance = ProviderRegistry.governance.get("oss-noop")

        assert isinstance(instance, NoOpGovernanceChecker)
        assert instance.is_system_enabled() is True

    def test_pool_monitor_oss_noop_resolves_to_empty_stats(self):
        instance = ProviderRegistry.pool_monitor.get("oss-noop")

        assert isinstance(instance, NoOpPoolStatsProvider)
        assert instance.get_stats().max_connections == 0


# =============================================================================
# Override context manager — Behavior
# =============================================================================


class TestRegistry516OverrideBehavior:
    """``override()`` swaps the resolved instance and restores on exit."""

    def test_governance_override_resolves_to_mock(self):
        class _CountingChecker:
            def __init__(self) -> None:
                self.calls = 0

            def is_system_enabled(self) -> bool:
                self.calls += 1
                return False

            def check_all_governance(self, **kwargs):
                from baldur.models.governance import GovernanceCheckResult

                return GovernanceCheckResult.allowed_result()

            def is_emergency_blocking(self, min_level=None):
                return False, "UNKNOWN"

            def is_error_budget_blocking(self, tier_id=None, region=None):
                return False, 100.0, 0.0

        mock_checker = _CountingChecker()

        with ProviderRegistry.governance.override(mock_checker):
            resolved = ProviderRegistry.governance.get()
            assert resolved is mock_checker
            assert resolved.is_system_enabled() is False

        # After exit, the original NoOp default returns True again.
        assert ProviderRegistry.governance.get().is_system_enabled() is True

    def test_pool_monitor_override_resolves_to_mock(self):
        class _StaticProvider(PoolStatsProvider):
            def get_stats(self):
                from baldur.interfaces.pool_monitor import PoolStats

                return PoolStats(
                    pool_name="mock",
                    max_connections=42,
                    active_connections=7,
                    available_connections=35,
                )

        mock_provider = _StaticProvider()

        with ProviderRegistry.pool_monitor.override(mock_provider):
            stats = ProviderRegistry.pool_monitor.get().get_stats()
            assert stats.pool_name == "mock"
            assert stats.max_connections == 42

        # Defaults restored — pool_name reverts.
        assert ProviderRegistry.pool_monitor.get().get_stats().pool_name != "mock"
