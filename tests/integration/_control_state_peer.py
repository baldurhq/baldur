"""A peer process for the control-state cross-process tests.

Run as a script by ``test_control_state_cross_process.py``; never collected as a
test. The peer loads the switch state the way a real process does — through
``baldur.init()`` or the per-worker load — then serves "requests" in a loop: each
iteration asks the execution-mode resolver every protected call asks, and the
peer reports every change it observes. It never reads the switch store on its
own request thread; which threads did read the store is reported at the end.

Protocol: every line the peer prints for the test starts with ``PEER `` and
carries one JSON object — ``ready``, then one ``observed`` per change and one
``heard`` per kill-switch event its own subscriber received, then ``done``.
Configuration arrives through environment variables:

- ``PEER_INTERVAL``: the switch refresh interval, set before the manager exists.
- ``PEER_DURATION``: the longest the peer serves after ``ready``.
- ``PEER_STOP_FILE``: the peer stops serving once this file exists.
- ``PEER_STARTUP``: ``init`` (``baldur.init()``) or ``load`` (the worker load).
- ``PEER_KILL_THREAD``: ``1`` to end the refresher thread right after startup.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

REFRESHER_THREAD = "control_state_refresher"
REQUEST_THREAD = threading.main_thread().name


def _emit(event: str, **fields: object) -> None:
    print("PEER " + json.dumps({"event": event, **fields}), flush=True)


def _record_store_reads(phase: dict[str, str]) -> dict[str, list[str]]:
    """Wrap every shipped backend's strict read with a per-phase thread record."""
    from baldur.core import state_backend

    reads: dict[str, list[str]] = {"startup": [], "serving": []}
    for cls in (state_backend.FileStateBackend, state_backend.RedisStateBackend):
        original = cls.get_strict

        def recorded(self, key, _original=original):
            reads[phase["name"]].append(threading.current_thread().name)
            return _original(self, key)

        cls.get_strict = recorded
    return reads


def _record_kill_switch_events() -> list[list[str]]:
    from baldur.services.event_bus import get_event_bus
    from baldur.services.event_bus.bus.event_types import EventType

    heard: list[list[str]] = []

    def record(event) -> None:
        heard.append([event.event_type.value, event.source])
        _emit("heard", event_type=event.event_type.value, source=event.source)

    bus = get_event_bus()
    bus.subscribe(EventType.KILL_SWITCH_ACTIVATED, record)
    bus.subscribe(EventType.KILL_SWITCH_DEACTIVATED, record)
    return heard


def _end_refresher_thread() -> None:
    """Make the refresher thread exit, as a crash would, leaving it restartable."""
    from baldur.core.control_state import get_control_state_refresher

    state = get_control_state_refresher()._state
    thread = state.thread
    state.stopped = True
    state.wake.set()
    if thread is not None:
        thread.join(5.0)
    state.stopped = False


def main() -> int:
    interval = float(os.environ["PEER_INTERVAL"])
    duration = float(os.environ["PEER_DURATION"])
    startup = os.environ.get("PEER_STARTUP", "load")

    from baldur.services import system_control

    system_control.SYSTEM_CONTROL_REFRESH_INTERVAL_SECONDS = interval
    phase = {"name": "startup"}
    reads = _record_store_reads(phase)
    heard = _record_kill_switch_events()

    if startup == "init":
        import baldur

        baldur.init()
    else:
        from baldur.bootstrap import _start_control_state_refresher

        _start_control_state_refresher()

    from baldur.core.control_state import get_control_state_refresher
    from baldur.core.execution_mode import resolve_execution_mode
    from baldur.core.process_utils import is_fork_source_process

    if os.environ.get("PEER_KILL_THREAD") == "1":
        _end_refresher_thread()
    refresher = get_control_state_refresher()
    manager = system_control.get_system_control()
    phase["name"] = "serving"
    _emit(
        "ready",
        refresher_running=refresher.is_running,
        fork_source=is_fork_source_process(),
        enabled=manager.is_enabled(),
    )

    stop_file = os.environ.get("PEER_STOP_FILE")
    pace = threading.Event()
    last = None
    most_refresher_threads = 0
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        if stop_file and os.path.exists(stop_file):
            break
        mode, source = resolve_execution_mode()
        enabled, dry_run = manager.switches()
        current = (enabled, dry_run, source)
        if current != last:
            _emit(
                "observed",
                t=time.time(),
                enabled=enabled,
                dry_run=dry_run,
                source=source,
                should_execute=mode.should_execute,
            )
            last = current
        alive = sum(
            1
            for t in threading.enumerate()
            if t.name == REFRESHER_THREAD and t.is_alive()
        )
        most_refresher_threads = max(most_refresher_threads, alive)
        pace.wait(0.02)

    _emit(
        "done",
        # The serving loop runs on the main thread: it is the request thread.
        request_thread_store_reads=[
            name for name in reads["serving"] if name == REQUEST_THREAD
        ],
        refresher_store_reads=reads["serving"].count(REFRESHER_THREAD),
        most_refresher_threads=most_refresher_threads,
        heard=heard,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
