"""
PRO Registration Flow Integration Tests

Verifies the end-to-end composition:
  register_pro_services() → get_entitlement_status() → _register_all_pro_services()

This tests the real call chain (no internal mocking) — only the
entitlement settings and importlib are mocked to control the flow.

Test Categories:
    A. Entitlement Gating:
        - Missing license key → PRO skipped
        - Invalid token → PRO skipped
    B. Active Entitlement:
        - Active entitlement → every PRO service module imported
    C. Partial Failure:
        - One module import failure → remaining modules still loaded

Note: All tests use mocked environment and importlib — no infra dependency.
      This enables parallel test execution with pytest-xdist.

Import-count caveat: the total ``importlib.import_module`` call count is NOT
asserted. Beyond the canonical PRO service-module loop, the singleton-provider
factories and the relocated-feature registrations trigger additional lazy
imports whose count is process-state-dependent (e.g. 40 vs 43 across runs), so
an exact-count assertion is both stale-prone and non-deterministic. These tests
assert the canonical service modules are imported and that the loop survives a
single module's failure — the behavior that actually matters.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.core.entitlement import (
    EntitlementStatus,
    reset_entitlement_status,
)

# The canonical PRO service modules imported by _register_all_pro_services'
# _pro_service_modules loop (src/baldur_pro/__init__.py). Each is imported for
# its module-import-time provider registrations. Mirrored here as the Contract:
# every advertised PRO service must be imported under ACTIVE entitlement.
_PRO_SERVICE_MODULES = {
    "baldur_pro.services.dlq",
    "baldur_pro.services.replay",
    "baldur_pro.services.audit",
    "baldur_pro.services.emergency_mode",
    "baldur_pro.services.error_budget",
    "baldur_pro.services.error_budget_gate",
    "baldur_pro.services.coordination",
    "baldur_pro.services.canary",
    "baldur_pro.services.runtime_config",
    "baldur_pro.services.throttle",
    "baldur_pro.services.corruption_shield",
    "baldur_pro.services.auto_tuning",
    "baldur_pro.services.chaos",
    "baldur_pro.services.governance",
    "baldur_pro.services.postmortem",
    "baldur_pro.services.saga",
    "baldur_pro.services.security_notification",
    "baldur_pro.services.unified_notification",
    "baldur_pro.services.bulkhead",
    "baldur_pro.services.hedging",
    "baldur_pro.services.pool_monitor",
    "baldur_pro.services.meta_watchdog",
}


@pytest.fixture(autouse=True)
def _reset_entitlement():
    """Reset the entitlement singleton before and after each test."""
    reset_entitlement_status()
    yield
    reset_entitlement_status()


class TestProRegistrationFlowIntegration:
    """End-to-end: entitlement validation → service registration."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_missing_license_key_skips_registration(self):
        """
        Purpose:
            Verify PRO services are not registered when LICENSE_KEY is empty.
        Expected:
            - Evaluated as EntitlementStatus.MISSING
            - importlib.import_module is never called (no PRO module loading)
        """
        from baldur.settings.license import reset_entitlement_settings

        reset_entitlement_settings()

        with (
            patch.dict(
                "os.environ",
                {"BALDUR_LICENSE_KEY": "", "BALDUR_LICENSE_FILE": ""},
            ),
            patch("importlib.import_module") as mock_import,
        ):
            from baldur_pro import register_pro_services

            register_pro_services()

            # No PRO modules should be imported
            mock_import.assert_not_called()

    def test_invalid_token_skips_registration(self):
        """
        Purpose:
            Verify PRO services are not registered when an invalid token is set.
        Expected:
            - Evaluated as EntitlementStatus.INVALID
            - importlib.import_module is never called
        """
        from baldur.settings.license import reset_entitlement_settings

        reset_entitlement_settings()

        with (
            patch.dict(
                "os.environ",
                {
                    "BALDUR_LICENSE_KEY": "not-a-valid-token",
                    "BALDUR_LICENSE_FILE": "",
                },
            ),
            patch("importlib.import_module") as mock_import,
        ):
            from baldur_pro import register_pro_services

            register_pro_services()

            mock_import.assert_not_called()

    def test_active_entitlement_imports_every_pro_service_module(self):
        """
        Purpose:
            Verify every canonical PRO service module is imported under an
            ACTIVE entitlement.
        Expected:
            - The set of imported modules is a superset of _PRO_SERVICE_MODULES
              (the total import count is intentionally not asserted — see the
              module docstring's import-count caveat).
        """
        from baldur_pro import register_pro_services

        with (
            patch(
                "baldur_pro._validate_and_log_entitlement",
                return_value=EntitlementStatus.ACTIVE,
            ),
            patch("importlib.import_module") as mock_import,
        ):
            mock_import.return_value = MagicMock()

            register_pro_services()

        imported = {c.args[0] for c in mock_import.call_args_list if c.args}
        assert _PRO_SERVICE_MODULES <= imported

    def test_partial_module_failure_does_not_block_others(self):
        """
        Purpose:
            Verify one module's ImportError does not abort the import loop.
        Expected:
            - The failing module (replay) is attempted and raises.
            - A module ordered AFTER it in the loop (meta_watchdog, last) is
              still imported — proving the loop continued.
            - register_pro_services() does not propagate the ImportError.
        """
        from baldur_pro import register_pro_services

        failing_module = "baldur_pro.services.replay"
        later_module = "baldur_pro.services.meta_watchdog"
        seen: list[str] = []

        def selective_fail(module_path, *args, **kwargs):
            seen.append(module_path)
            if module_path == failing_module:
                raise ImportError("replay unavailable")
            return MagicMock()

        with (
            patch(
                "baldur_pro._validate_and_log_entitlement",
                return_value=EntitlementStatus.ACTIVE,
            ),
            patch("importlib.import_module", side_effect=selective_fail),
        ):
            register_pro_services()  # must not raise

        assert failing_module in seen
        # A module after the failure still loaded → the loop was not aborted.
        assert later_module in seen


# =============================================================================
# Bulkhead slot chain — end-to-end resolution through the provider slot
# =============================================================================


def _bulkhead_status_response():
    """Call the framework-agnostic bulkhead handler through the real chain."""
    from baldur.api.handlers.bulkhead import bulkhead_status
    from baldur.interfaces.web_framework import HttpMethod, RequestContext

    ctx = RequestContext(
        method=HttpMethod("GET"),
        path="/bulkheads",
        query_params={},
        path_params={},
    )
    return bulkhead_status(ctx)


class TestBulkheadSlotChainIntegration:
    """Extension-present chain leg: slot registration → chain → consumer surface.

    Composes the real components (no internal mocking): the provider slot is
    populated with the production registration shape (lazy import + getter
    factory), the chain getter resolves through it, and the REST handler
    reports the thread-pool compartment the overlay built.
    """

    @pytest.fixture(autouse=True)
    def _bulkhead_registries(self):
        """Gate on the extension package and reset both tiers' singletons."""
        pytest.importorskip("baldur_pro")
        from baldur.services.bulkhead.registry import (
            reset_bulkhead_registry as reset_core,
        )
        from baldur_pro.services.bulkhead.registry import (
            reset_bulkhead_registry as reset_pro,
        )

        reset_core()
        reset_pro()
        yield
        reset_pro()
        reset_core()

    def test_slot_registration_routes_chain_to_thread_pool_overlay(self):
        """Registered slot → chain returns the overlay → handler reports the pool."""
        import importlib

        from baldur.core.connection_health import ConnectionType
        from baldur.factory.registry import ProviderRegistry
        from baldur.services.bulkhead.registry import (
            get_bulkhead_registry as chain_get_bulkhead_registry,
        )
        from baldur_pro.services.bulkhead.registry import ProBulkheadRegistry
        from baldur_pro.services.bulkhead.threadpool import ThreadPoolBulkhead

        def _pro_slot_factory():
            # The production registration shape: lazy module import + getter
            # call at first slot resolution.
            module = importlib.import_module("baldur_pro.services.bulkhead")
            return module.get_bulkhead_registry()

        external_api = None
        with ProviderRegistry.bulkhead_registry.snapshot():
            ProviderRegistry.bulkhead_registry.register("pro", _pro_slot_factory)
            try:
                # The chain resolves the slot to the overlay singleton…
                resolved = chain_get_bulkhead_registry()
                assert isinstance(resolved, ProBulkheadRegistry)

                # …whose EXTERNAL_API built-in is a real worker pool…
                external_api = resolved.get(ConnectionType.EXTERNAL_API)
                assert isinstance(external_api, ThreadPoolBulkhead)

                # …and the REST consumer surface reflects it through the
                # same chain.
                resp = _bulkhead_status_response()
                assert resp.status_code == 200
                assert resp.body["bulkheads"]["external_api"]["type"] == ("thread_pool")
            finally:
                if external_api is not None:
                    external_api.shutdown(wait=True)


class TestBulkheadSlotEmptyChainIntegration:
    """Base-only chain leg: empty slot → base singleton → semaphore built-ins.

    Runs on any install (no extension package needed): the slot is forced
    empty via the documented mock point, so the chain, the built-in
    construction, and the REST consumer surface compose on the base tier.
    """

    @pytest.fixture(autouse=True)
    def _bare_install_chain(self, monkeypatch):
        """Force the chain's fallback leg and a fresh base singleton."""
        from baldur.factory.registry import ProviderRegistry
        from baldur.services.bulkhead.registry import reset_bulkhead_registry

        monkeypatch.setattr(
            ProviderRegistry.bulkhead_registry, "safe_get", lambda name=None: None
        )
        reset_bulkhead_registry()
        yield
        reset_bulkhead_registry()

    def test_empty_slot_falls_back_to_base_registry_with_semaphore_builtins(self):
        """Empty slot → exact base registry → semaphore EXTERNAL_API → handler 200."""
        from baldur.core.connection_health import ConnectionType
        from baldur.services.bulkhead.registry import (
            BulkheadRegistry,
            get_bulkhead_registry,
        )

        # The chain lands on its fallback leg — the exact base class, not an
        # overlay.
        resolved = get_bulkhead_registry()
        assert type(resolved) is BulkheadRegistry

        # The built-ins exist and EXTERNAL_API is the semaphore fallback.
        states = resolved.get_all_states()
        builtin_names = {ct.value for ct in ConnectionType}
        assert builtin_names <= set(states)
        assert states["external_api"].bulkhead_type.value == "semaphore"

        # The REST consumer surface composes through the same chain.
        resp = _bulkhead_status_response()
        assert resp.status_code == 200
        assert resp.body["bulkheads"]["external_api"]["type"] == "semaphore"
