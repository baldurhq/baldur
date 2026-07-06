"""615 D1 — startup-integration registration/start decoupling (integration).

The ACTIVE-entitlement registration flow
(``register_pro_services`` → ``_register_all_pro_services`` →
``register_startup_integrations``) must populate
``ProviderRegistry.startup_integrations`` with the six PRO starters while
spawning NO daemon thread and creating NO EventBus subscription — the start
happens only later, in ``start_background_workers()``.

Proving the two are genuinely decoupled requires the real registration flow
plus thread enumeration before/after, not a single function's behavior — hence
an integration test rather than a unit test. The PRO service-import loop is
mocked via ``importlib.import_module`` (no infra dependency, xdist-safe);
``register_startup_integrations()`` is a direct call, so the slot is populated
for real.

Mock-based — no infra. The slot registrations are cleared by the root conftest
``auto_reset_all_state`` autouse reset.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from baldur.core.entitlement import reset_entitlement_status

EXPECTED_STARTER_NAMES = [
    "bulkhead_metrics_updater",
    "crisis_multiplier_invalidation",
    "auto_tuning",
    "circuit_mesh",
    "cb_propagation",
    "canary_interlock_refresher",
]


@pytest.fixture(autouse=True)
def _reset_entitlement():
    """Reset the entitlement singleton before and after each test."""
    reset_entitlement_status()
    yield
    reset_entitlement_status()


class TestStartupIntegrationRegistrationWithoutStart:
    def test_active_entitlement_populates_slot_without_starting_anything(
        self, monkeypatch
    ):
        pytest.importorskip("baldur_pro")

        from baldur.core.entitlement import EntitlementStatus
        from baldur.factory.registry import ProviderRegistry
        from baldur_pro import register_pro_services

        # No gunicorn role should leak into the flow.
        monkeypatch.delenv("SERVER_SOFTWARE", raising=False)
        monkeypatch.delenv("GUNICORN_WORKER", raising=False)

        # Given an empty slot (root conftest resets it per function).
        assert ProviderRegistry.startup_integrations.list_providers() == []
        threads_before = {t.name for t in threading.enumerate()}

        # When the real ACTIVE-entitlement registration flow runs. The service
        # import loop is mocked, but register_startup_integrations() is a direct
        # call, so the slot is populated for real.
        with (
            patch(
                "baldur_pro._validate_and_log_entitlement",
                return_value=EntitlementStatus.ACTIVE,
            ),
            patch("importlib.import_module", return_value=MagicMock()),
        ):
            register_pro_services()

        # Then the six PRO starters are registered, in iteration order.
        assert (
            ProviderRegistry.startup_integrations.list_providers()
            == EXPECTED_STARTER_NAMES
        )

        # And registration started nothing: no starter was invoked-and-cached
        # (the slot is read via get_provider, never get), and the default-ON
        # bulkhead metrics updater spawned no daemon thread.
        assert ProviderRegistry.startup_integrations.instance_count() == 0
        new_threads = {t.name for t in threading.enumerate()} - threads_before
        assert "bulkhead_metrics_updater" not in new_threads
