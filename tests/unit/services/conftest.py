"""Fixtures shared by the System Control manager tests in this directory.

``control_env`` gives a test a fresh ``SystemControlManager`` — the process
singleton — on its own ``ScriptedStateBackend``, with its own event bus, its own
process refresher (so no other manager's key rides along in a pass) and a
recorder of every kill-switch event this process's handlers received. Passes
are driven with ``control_env.refresh()`` (the suite keeps the refresher thread
off), exactly as this process's refresher would run them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from baldur.core import control_state as control_state_module
from baldur.core.control_state import ControlStateRefresher
from baldur.core.state_backend import (
    OCC_VERSION_FIELD,
    OCC_WRITER_FIELD,
    MemoryStateBackend,
    configure_state_backend,
    reset_state_backend,
)
from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.event_bus.bus.models import BaldurEvent
from baldur.services.system_control import (
    STATE_KEY,
    SystemControlManager,
    SystemState,
    get_system_control,
    reset_system_control,
)
from tests.factories.state_backend_doubles import ScriptedStateBackend


@dataclass
class ControlEnv:
    """A manager on a private store, bus, refresher and kill-switch event recorder."""

    manager: SystemControlManager
    store: ScriptedStateBackend
    bus: BaldurEventBus
    refresher: ControlStateRefresher
    events: list[BaldurEvent] = field(default_factory=list)

    def seed(self, *, version: int | None = None, token: str = "peer", **fields: Any):
        """Write the store directly — a peer process's committed state.

        Without ``version`` the value carries no stamp: a previous-release
        process's blind write, which reads as version 0.
        """
        value = SystemState(**fields).to_dict()
        if version is not None:
            value[OCC_VERSION_FIELD] = version
            value[OCC_WRITER_FIELD] = token
        MemoryStateBackend.set(self.store, STATE_KEY, value)
        return value

    def stored(self) -> dict[str, Any] | None:
        return MemoryStateBackend.get(self.store, STATE_KEY)

    def refresh(self) -> None:
        """One refresh pass, as this process's refresher runs it."""
        self.refresher.refresh_now()

    def load(self, **fields: Any) -> None:
        """A peer committed ``fields`` and this process's copy has caught up."""
        self.seed(version=1, **fields)
        self.refresh()

    def kill_switch_events(self) -> list[tuple[EventType, str]]:
        return [(e.event_type, e.source) for e in self.events]


@pytest.fixture
def control_env():
    """A fresh process-singleton manager on its own scripted store, bus and refresher."""
    refresher = ControlStateRefresher()
    # A process whose init() already ran: reads never load synchronously.
    refresher._first_load_attempted = True
    SystemControlManager._instance = None
    reset_system_control(cleanup=False)

    with patch.object(control_state_module, "_refresher", refresher):
        store = ScriptedStateBackend()
        configure_state_backend(store)
        manager = get_system_control()
        bus = BaldurEventBus()
        manager._event_bus = bus
        env = ControlEnv(manager=manager, store=store, bus=bus, refresher=refresher)

        def record(event: BaldurEvent) -> None:
            env.events.append(event)

        bus.subscribe(EventType.KILL_SWITCH_ACTIVATED, record)
        bus.subscribe(EventType.KILL_SWITCH_DEACTIVATED, record)

        yield env

        store.fail_reads = None
        store.fail_writes = None
        bus.reset()
        SystemControlManager._instance = None
        reset_system_control(cleanup=False)
        refresher._reset()
        reset_state_backend()
