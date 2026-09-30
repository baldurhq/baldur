"""A switch store that cannot be reached or built neither slows calls nor hides the switch.

Target: ``baldur.services.system_control`` with ``baldur.core.control_state``
and ``baldur.core.state_backend`` — during a store outage every protected call
answers from this process's copy with no store access (the only store read a
never-loaded process makes is its first reader's one load); ``is_baldur_enabled``
never raises; the status fields report an unreachable store and an age that
keeps growing from the last good read; one refresher thread survives a long
outage under heavy reads; a flip reports honestly where it applies; and the
Redis store is admitted through the bounded ``probe()`` instead of a data-path
connect followed by ``ping()``.

Verification techniques applied (§8):
  - §8.2 Exception/edge cases — an unbuildable store, a store that blocks, a
    store that refuses reads
  - §8.11 Time dependency — the copy's age grows on a controlled clock
  - §8.7 Concurrency — one refresher thread through the outage
  - §8.5 Dependency interaction — the probe precedes the data client, which is
    never pinged
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest

from baldur import protect_facade
from baldur.adapters.redis.connection_factory import RedisConnectionFactory
from baldur.core import control_state as control_state_module
from baldur.core.control_state import DAEMON_WORKER_NAME
from baldur.core.exceptions import SystemControlStoreError
from baldur.core.execution_mode import (
    clear_execution_mode_override,
    get_execution_mode,
)
from baldur.core.state_backend import RedisStateBackend
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.system_control import is_baldur_enabled, is_dry_run
from baldur.settings.protect import reset_protect_settings

_GET_BACKEND = "baldur.core.state_backend.get_state_backend"
_FACTORY = "baldur.adapters.redis.connection_factory.get_redis_connection_factory"

#: The blocking store of the success criterion: every read takes 2 s.
_BLOCKING_READ_SECONDS = 2.0
#: 50 protected calls after the first must finish well inside one store read.
_CALLS_AFTER_FIRST = 50
_CALLS_BUDGET_SECONDS = 0.5


class _Clock:
    def __init__(self, start: float = 5000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock():
    fake = _Clock()
    with patch.object(
        control_state_module, "time", SimpleNamespace(monotonic=fake.monotonic)
    ):
        yield fake


@pytest.fixture
def protect_state():
    clear_execution_mode_override()
    reset_protect_settings()
    yield
    reset_protect_settings()
    clear_execution_mode_override()


def _failing_protected_call(calls: list[int]) -> None:
    """A protected call with a retry stage — the path that asks the resolver."""

    def fn() -> str:
        calls.append(1)
        raise ConnectionError("downstream refused")

    with pytest.raises(ConnectionError):
        protect_facade.protect(
            "svc.outage",
            fn,
            retry=RetryPolicyConfig(
                max_attempts=2,
                backoff_base=0,
                backoff_max=0,
                jitter_percent=0,
                enable_dlq=False,
                domain="svc.outage",
            ),
            circuit_breaker=False,
            dlq=False,
            timeout=None,
        )


class TestSystemControlStoreOutageBehavior:
    """Readers answer from the copy; the outage is visible, never slow (D5, D6, D9)."""

    def test_protected_calls_pay_no_store_access_during_a_blocking_outage(
        self, control_env, protect_state
    ):
        """After the first reader's one load, 50 calls touch the store zero times."""
        # Given: a never-loaded process whose store takes 2 s per read
        control_env.refresher._first_load_attempted = False
        control_env.store.read_gate = threading.Event()
        control_env.store.read_gate_timeout = _BLOCKING_READ_SECONDS
        calls: list[int] = []
        _failing_protected_call(calls)
        reads_after_first = control_env.store.strict_reads

        # When
        started = time.perf_counter()
        for _ in range(_CALLS_AFTER_FIRST):
            _failing_protected_call(calls)
        elapsed = time.perf_counter() - started

        # Then
        assert reads_after_first == 1
        assert control_env.store.strict_reads == reads_after_first
        assert elapsed < _CALLS_BUDGET_SECONDS

    def test_unbuildable_store_is_attempted_at_most_once_across_calls(
        self, control_env
    ):
        """A construction failure never reaches the call path again."""
        # Given: a never-loaded process whose store cannot be built
        control_env.refresher._first_load_attempted = False

        # When
        with patch(
            _GET_BACKEND, side_effect=ConnectionError("redis unreachable")
        ) as get_backend:
            modes = [get_execution_mode() for _ in range(_CALLS_AFTER_FIRST)]
            enabled = is_baldur_enabled()

        # Then
        assert get_backend.call_count == 1
        assert all(mode.should_execute for mode in modes)
        assert enabled is True

    def test_quick_checks_never_raise_while_the_store_refuses_reads(self, control_env):
        """The last known state (here the default) answers; nothing propagates."""
        control_env.refresher._first_load_attempted = False
        control_env.store.fail_reads = ConnectionError("store down")

        assert (is_baldur_enabled(), is_dry_run()) == (True, False)
        assert control_env.manager.is_state_known() is False

    def test_status_reports_an_unreachable_store_and_a_growing_age(
        self, control_env, clock
    ):
        """``store_reachable: false``; the age counts from the last good read."""
        # Given: a good read, then a failing one 10 s later
        control_env.load(enabled=False)
        clock.now += 10.0
        control_env.store.fail_reads = ConnectionError("store down")
        control_env.refresh()

        # When
        first = control_env.manager.get_refresh_status()
        clock.now += 15.0
        second = control_env.manager.get_refresh_status()

        # Then
        assert first["store_reachable"] is False
        assert first["last_store_error"] == "ConnectionError: store down"
        assert (first["state_age_seconds"], second["state_age_seconds"]) == (
            10.0,
            25.0,
        )
        assert second["state_refreshed_at"] is not None
        assert control_env.manager.is_enabled() is False

    def test_status_before_the_first_attempt_reports_unknown(self, control_env):
        """``store_reachable`` is ``null`` until this process tried once."""
        status = control_env.manager.get_refresh_status()

        assert status == {
            "store_reachable": None,
            "state_refreshed_at": None,
            "state_age_seconds": None,
            "last_store_error": None,
            "refresher_running": False,
        }

    def test_one_refresher_thread_through_a_long_outage_under_many_reads(
        self, control_env, clock, monkeypatch
    ):
        """No age-based replacement: a 60 s outage and 1,000 reads keep one thread."""
        # Given: a running refresher, then an outage that lasts 60 s
        monkeypatch.setenv("BALDUR_CONTROL_STATE_REFRESHER_AUTOSTART", "1")
        control_env.load(enabled=True)
        control_env.refresher.start()
        running = control_env.refresher._state.thread
        control_env.store.fail_reads = ConnectionError("store down")
        clock.now += 60.0

        # When
        try:
            for _ in range(1000):
                control_env.manager.is_enabled()
            alive = [
                t
                for t in threading.enumerate()
                if t.name == DAEMON_WORKER_NAME and t.is_alive()
            ]
        finally:
            control_env.refresher.stop()

        # Then
        assert alive == [running]

    def test_flips_against_an_unbuildable_store_report_where_they_apply(
        self, control_env
    ):
        """A pulled brake holds here (``this_process``); a release raises."""
        control_env.load(enabled=True, dry_run=True)

        with patch(
            "baldur.services.system_control.get_state_backend",
            side_effect=ConnectionError("redis unreachable"),
        ):
            held = control_env.manager.disable(actor="oncall", reason="incident")
            with pytest.raises(SystemControlStoreError) as raised:
                control_env.manager.disable_dry_run(actor="oncall")

        assert (held.persisted, held.applies) == (False, "this_process")
        assert control_env.manager.is_enabled() is False
        assert (raised.value.persisted, raised.value.applies) == (False, "none")
        assert control_env.manager.is_dry_run() is True


class TestStateStoreConstructionOutageBehavior:
    """The Redis store is admitted on the bounded probe budget (D14)."""

    def test_construction_admits_through_probe_before_building_the_client(self):
        """``probe()`` answers first; the data client is never pinged afterwards."""
        # Given
        factory = create_autospec(RedisConnectionFactory, instance=True)
        client = factory.create.return_value

        # When
        with patch(_FACTORY, return_value=factory):
            backend = RedisStateBackend(redis_url="redis://state-store:6379/2")

        # Then
        factory.probe.assert_called_once_with("redis://state-store:6379/2")
        factory.create.assert_called_once_with(
            "redis://state-store:6379/2", decode_responses=True
        )
        client.ping.assert_not_called()
        assert backend._client is client

    def test_construction_fails_on_a_non_answering_store_without_a_client(self):
        """A blackholed store fails construction inside the probe; no client is built."""
        factory = create_autospec(RedisConnectionFactory, instance=True)
        factory.probe.side_effect = ConnectionError("probe budget exhausted")

        with patch(_FACTORY, return_value=factory), pytest.raises(ConnectionError):
            RedisStateBackend(redis_url="redis://blackholed:6379/0")

        factory.create.assert_not_called()
