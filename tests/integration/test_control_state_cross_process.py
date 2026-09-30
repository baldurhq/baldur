"""A kill-switch flip and a dry-run toggle reach every process sharing the store.

Target: the control-state delivery chain across process boundaries —
``SystemControlManager`` writing the shared store in one process, and in
another process ``baldur.core.control_state``'s refresher assigning what it
reads to that process's copy, which every protected call asks through the
execution-mode resolver (802 D2, D5, D6, D12).

This test process is process A: it flips the switch in a store it shares with a
real peer process B (``_control_state_peer.py``). B loads the switch the way a
real process does and serves "requests" in a loop, reporting each change it
observes. B never restarts and never serves a status read, so the refresher is
the only way A's flip can reach it. The delivery bound under test is B's
refresh interval (shortened to one second before B builds its manager) plus
the slack the success criterion allows: 6 seconds end to end.

Covered peers: a worker-style load; a ``baldur.init()`` process that reads
itself as a pre-fork master (``SERVER_SOFTWARE=gunicorn``, no hook) — no
fork-source gate; a process whose refresher thread died (the next read restarts
it); a process started after the flip (its first pass announces the brake to its
own subscribers). In every case B's request thread performs no store read.

The file-store variants need only the local filesystem; the Redis-store variant
runs under ``requires_redis``.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from baldur.core import control_state as control_state_module
from baldur.core.control_state import ControlStateRefresher
from baldur.core.state_backend import (
    FileStateBackend,
    RedisStateBackend,
    StateBackend,
    configure_state_backend,
    reset_state_backend,
)
from baldur.services.system_control import (
    SystemControlManager,
    get_system_control,
    reset_system_control,
)
from tests.factories.constants import RedisTestConfig
from tests.factories.peer_process import PeerProcess

_PEER = Path(__file__).with_name("_control_state_peer.py")
#: B's refresh interval, set before B builds its manager.
_PEER_INTERVAL_SECONDS = 1.0
#: The success criterion's end-to-end bound for a flip to reach B.
_REACH_BOUND_SECONDS = 6.0
#: B's startup (imports, and ``init()`` when asked) fits comfortably in this.
_READY_TIMEOUT_SECONDS = 60.0
_PEER_MAX_SERVE_SECONDS = 90.0
_KILL_SWITCH_ACTIVATED = "kill_switch_activated"
_KILL_SWITCH_DEACTIVATED = "kill_switch_deactivated"


class _SharedStore:
    """Process A's side: a manager on the store it shares with B."""

    def __init__(self, backend: StateBackend, peer_env: dict[str, str], tmp: Path):
        self.backend = backend
        self._peer_env = peer_env
        self._tmp = tmp
        self._peers: list[PeerProcess] = []

    @property
    def manager(self) -> SystemControlManager:
        return get_system_control()

    def start_peer(self, **env: str) -> PeerProcess:
        base = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
        peer = PeerProcess(
            _PEER,
            {
                **base,
                "BALDUR_CONTROL_STATE_REFRESHER_AUTOSTART": "1",
                "PEER_INTERVAL": str(_PEER_INTERVAL_SECONDS),
                "PEER_DURATION": str(_PEER_MAX_SERVE_SECONDS),
                **self._peer_env,
                **env,
            },
            self._tmp / f"stop-{uuid.uuid4().hex}",
            shutdown_timeout=_PEER_MAX_SERVE_SECONDS,
        )
        self._peers.append(peer)
        return peer

    def close(self) -> None:
        for peer in self._peers:
            peer.kill()


@pytest.fixture
def isolated_manager() -> Iterator[None]:
    """A fresh process-A manager with its own refresher (no thread)."""
    refresher = ControlStateRefresher()
    refresher._first_load_attempted = True
    SystemControlManager._instance = None
    reset_system_control(cleanup=False)
    with patch.object(control_state_module, "_refresher", refresher):
        yield
    SystemControlManager._instance = None
    reset_system_control(cleanup=False)
    refresher._reset()
    reset_state_backend()


@pytest.fixture
def shared_file_store(tmp_path, isolated_manager) -> Iterator[_SharedStore]:
    directory = tmp_path / "shared_state"
    configure_state_backend(FileStateBackend(directory))
    shared = _SharedStore(
        FileStateBackend(directory),
        {
            "BALDUR_SYSTEM_CONTROL_BACKEND": "file",
            "BALDUR_SYSTEM_CONTROL_DIR": str(directory),
        },
        tmp_path,
    )
    yield shared
    shared.close()


def _flip_and_time_reach(store: _SharedStore, peer: PeerProcess, flip, **observed):
    """Flip in A, return how long B took to act on it."""
    flipped_at = time.time()
    change = flip()
    assert change.persisted is True
    seen = peer.next_event("observed", timeout=_REACH_BOUND_SECONDS + 5.0, **observed)
    return seen["t"] - flipped_at


class TestControlStateCrossProcessBehavior:
    """A flip made in A is honored by B's protected calls within the bound (SC4)."""

    def test_kill_switch_and_dry_run_reach_a_running_peer_within_the_bound(
        self, shared_file_store
    ):
        """No restart and no status read in B: the refresher delivers both flips."""
        # Given: B serving, switch up
        peer = shared_file_store.start_peer(PEER_STARTUP="load")
        ready = peer.next_event("ready", timeout=_READY_TIMEOUT_SECONDS)
        manager = shared_file_store.manager

        # When: A pulls the brake, releases it, then turns dry-run on
        brake = _flip_and_time_reach(
            shared_file_store,
            peer,
            lambda: manager.disable(actor="operator-a", reason="incident"),
            enabled=False,
            source="kill_switch",
            should_execute=False,
        )
        manager.enable(actor="operator-a", reason="resolved")
        peer.next_event("observed", timeout=_REACH_BOUND_SECONDS + 5.0, enabled=True)
        dry_run = _flip_and_time_reach(
            shared_file_store,
            peer,
            lambda: manager.enable_dry_run(actor="operator-a"),
            dry_run=True,
            source="runtime_toggle",
        )
        done = peer.finish()

        # Then
        assert ready["refresher_running"] is True
        assert brake < _REACH_BOUND_SECONDS
        assert dry_run < _REACH_BOUND_SECONDS
        assert done["request_thread_store_reads"] == []
        assert done["refresher_store_reads"] > 0

    def test_kill_switch_reaches_a_peer_that_reads_itself_as_a_prefork_master(
        self, shared_file_store
    ):
        """``baldur.init()`` under gunicorn without the hook: no fork-source gate."""
        peer = shared_file_store.start_peer(
            PEER_STARTUP="init", SERVER_SOFTWARE="gunicorn/23.0.0"
        )
        ready = peer.next_event("ready", timeout=_READY_TIMEOUT_SECONDS)

        reach = _flip_and_time_reach(
            shared_file_store,
            peer,
            lambda: shared_file_store.manager.disable(
                actor="operator-a", reason="incident"
            ),
            enabled=False,
            source="kill_switch",
        )
        done = peer.finish()

        assert (ready["fork_source"], ready["refresher_running"]) == (True, True)
        assert reach < _REACH_BOUND_SECONDS
        assert done["request_thread_store_reads"] == []

    def test_kill_switch_reaches_a_peer_whose_refresher_thread_died(
        self, shared_file_store
    ):
        """The next read restarts a dead refresher; still one thread at a time."""
        peer = shared_file_store.start_peer(PEER_STARTUP="load", PEER_KILL_THREAD="1")
        ready = peer.next_event("ready", timeout=_READY_TIMEOUT_SECONDS)

        reach = _flip_and_time_reach(
            shared_file_store,
            peer,
            lambda: shared_file_store.manager.disable(
                actor="operator-a", reason="incident"
            ),
            enabled=False,
            source="kill_switch",
        )
        done = peer.finish()

        assert ready["refresher_running"] is False
        assert reach < _REACH_BOUND_SECONDS
        assert done["most_refresher_threads"] == 1
        assert done["request_thread_store_reads"] == []

    def test_peer_started_after_the_flip_announces_the_brake_to_its_own_subscribers(
        self, shared_file_store
    ):
        """No event from A reaches B; B's own first pass tells B's subscribers."""
        # Given: the brake was pulled before B existed
        shared_file_store.manager.disable(actor="operator-a", reason="incident")

        # When
        peer = shared_file_store.start_peer(PEER_STARTUP="load")
        ready = peer.next_event("ready", timeout=_READY_TIMEOUT_SECONDS)
        done = peer.finish()

        # Then
        assert ready["enabled"] is False
        assert [_KILL_SWITCH_ACTIVATED, "system_control"] in done["heard"]
        assert [_KILL_SWITCH_DEACTIVATED, "system_control"] not in done["heard"]
        assert done["request_thread_store_reads"] == []


@pytest.fixture
def shared_redis_store(tmp_path, isolated_manager) -> Iterator[_SharedStore]:
    url = os.environ.get("REDIS_URL", RedisTestConfig().test_redis_url)
    prefix = f"test:baldur:state:{uuid.uuid4().hex}:"
    backend = RedisStateBackend(redis_url=url, key_prefix=prefix)
    configure_state_backend(backend)
    shared = _SharedStore(
        backend,
        {
            "BALDUR_SYSTEM_CONTROL_BACKEND": "redis",
            "BALDUR_SYSTEM_CONTROL_REDIS_URL": url,
            "BALDUR_SYSTEM_CONTROL_REDIS_KEY_PREFIX": prefix,
        },
        tmp_path,
    )
    yield shared
    shared.close()
    backend._client.delete(f"{prefix}system_control")
    backend.close()


@pytest.mark.requires_redis
class TestControlStateCrossProcessRedisBehavior:
    """The same reach over the Redis store (multi-host deployments)."""

    def test_kill_switch_reaches_a_running_peer_over_the_redis_store(
        self, shared_redis_store
    ):
        """A flip written to Redis in A is honored by B within the bound."""
        peer = shared_redis_store.start_peer(PEER_STARTUP="load")
        peer.next_event("ready", timeout=_READY_TIMEOUT_SECONDS)

        reach = _flip_and_time_reach(
            shared_redis_store,
            peer,
            lambda: shared_redis_store.manager.disable(
                actor="operator-a", reason="incident"
            ),
            enabled=False,
            source="kill_switch",
        )
        done = peer.finish()

        assert reach < _REACH_BOUND_SECONDS
        assert done["request_thread_store_reads"] == []
