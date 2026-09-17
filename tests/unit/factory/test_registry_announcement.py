"""Unit tests for the registry's per-registration announcement switch.

Under test (795 D7): ``GenericProviderRegistry.register()`` logs
``registry.provider_registered`` at DEBUG for a runtime registration and
stays silent inside ``_unannounced_registrations()`` — the block the registry
module wraps its load-time sweep in, because that sweep runs at import, before
any entry point has configured logging, where structlog's default prints every
level to stdout. The switch restores whatever it found on exit, so a nested or
raising block never leaves runtime registrations silent.
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from baldur.factory.base import GenericProviderRegistry, _unannounced_registrations

_REGISTERED_EVENT = "registry.provider_registered"


class DummyAdapter:
    """Minimal adapter stub for registry tests."""


def _registrations(logs: list[dict]) -> list[dict]:
    return [entry for entry in logs if entry["event"] == _REGISTERED_EVENT]


@pytest.fixture
def registry() -> GenericProviderRegistry[DummyAdapter]:
    """A fresh registry — nothing shared with the process-global slots."""
    return GenericProviderRegistry[DummyAdapter](adapter_type="announce-test")


class TestRegistryAnnouncementBehavior:
    """The DEBUG line follows the switch; the registration itself never does."""

    def test_register_outside_the_block_announces_the_registration(self, registry):
        """A runtime registration is announced with its slot and name.

        Runs after ``baldur.factory.registry``'s load-time sweep, so it also
        pins that the sweep's block handed the switch back.
        """
        with capture_logs() as logs:
            registry.register("memory", DummyAdapter)

        announcements = _registrations(logs)
        assert len(announcements) == 1
        assert announcements[0]["log_level"] == "debug"
        assert announcements[0]["adapter_type"] == "announce-test"
        assert announcements[0]["name"] == "memory"

    def test_register_inside_the_block_announces_nothing(self, registry):
        """Inside the block the line is off; the registration still lands."""
        with capture_logs() as logs, _unannounced_registrations():
            registry.register("memory", DummyAdapter)

        assert _registrations(logs) == []
        assert registry.has_provider("memory") is True
        assert registry.get_default_name() == "memory"

    def test_block_exit_turns_announcing_back_on(self, registry):
        """The switch is scoped to the block, not flipped for the process."""
        with _unannounced_registrations():
            pass

        with capture_logs() as logs:
            registry.register("memory", DummyAdapter)

        assert len(_registrations(logs)) == 1

    def test_nested_block_exit_restores_the_enclosing_block_silence(self, registry):
        """An inner block puts back the value it found — off — not a hardcoded on."""
        with capture_logs() as logs, _unannounced_registrations():
            with _unannounced_registrations():
                pass
            registry.register("memory", DummyAdapter)

        assert _registrations(logs) == []

    def test_block_restores_announcing_when_its_body_raises(self, registry):
        """A failing load-time sweep must not leave every later registration silent."""
        with pytest.raises(RuntimeError, match="sweep failed"):
            with _unannounced_registrations():
                raise RuntimeError("sweep failed")

        with capture_logs() as logs:
            registry.register("memory", DummyAdapter)

        assert len(_registrations(logs)) == 1
