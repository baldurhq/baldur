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
