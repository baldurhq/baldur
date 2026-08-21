"""615 D1 — startup-integration registration/start decoupling (integration).

The ACTIVE-entitlement registration flow
(``register_pro_services`` → ``_register_all_pro_services`` →
``register_startup_integrations``) must populate
``ProviderRegistry.startup_integrations`` with the PRO starters while
spawning NO daemon thread and creating NO EventBus subscription — the start
happens only later, in ``start_background_workers()``.

Proving the two are genuinely decoupled requires the real registration flow
plus thread enumeration before/after, not a single function's behavior — hence
an integration test rather than a unit test. The PRO service-import loop is
mocked via ``importlib.import_module`` (no infra dependency, xdist-safe);
``register_startup_integrations()`` is a direct call, so the slot is populated
for real.

The assertions are properties of whatever the slot holds — never a hand-written
roster of starter names. A roster drifts silently every time a starter joins or
leaves the slot, and the drifted test then fails for a reason unrelated to the
decoupling it exists to protect.

Mock-based — no infra. The slot registrations are cleared by the root conftest
``auto_reset_all_state`` autouse reset.
"""

from __future__ import annotations

import inspect
import threading
from unittest.mock import MagicMock, patch

import pytest

from baldur.core.entitlement import reset_entitlement_status


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
        threads_before = {t.ident for t in threading.enumerate()}

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

        # Then the flow reached the slot, and everything it left there satisfies
        # the module charter: a zero-arg callable that start_background_workers()
        # can invoke as get_provider(name)().
        registered = ProviderRegistry.startup_integrations.list_providers()
        assert registered
        for name in registered:
            starter = ProviderRegistry.startup_integrations.get_provider(name)
            assert callable(starter), name
            assert not inspect.signature(starter).parameters, name

        # And registration started nothing. instance_count() is the assertion
        # that cannot go vacuous: the slot is read via get_provider, never get,
        # so a single invoke-and-cache at registration time shows up here.
        assert ProviderRegistry.startup_integrations.instance_count() == 0

        # Thread enumeration is defense-in-depth for the starters that spawn
        # daemons — no starter ran, so no thread may appear.
        new_threads = [
            t.name for t in threading.enumerate() if t.ident not in threads_before
        ]
        assert new_threads == []
