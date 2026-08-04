"""DomainGaugeUpdater lifecycle — the per-process collector thread.

The collector is a daemon thread whose whole job is that the repository-backed
gauge families keep moving in *this* process. Three lifecycle properties carry
the design:

* the labelled heartbeat is **seeded at start**, before any tick. A labelled
  gauge exports no sample until first touched, so a process whose collection
  never succeeds would otherwise export no heartbeat series at all and the
  staleness rule would evaluate an empty vector — silent on exactly the
  never-worked case.
* the startup jitter delays only the **first** tick; a respawn after a crash
  collects immediately rather than waiting the jitter out again.
* ``stop()`` interrupts the interval sleep through the stop Event, so it
  returns in milliseconds rather than up to a whole interval.

The loop is driven directly with the stop Event pre-set wherever possible: with
the event already set, ``_update_loop()`` performs exactly the collections the
design claims and returns, so the assertions need no sleeping and no wall-clock
bound.

Reference:
    src/baldur/services/metrics/periodic_updater.py
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.services.metrics import baldur_heartbeat_count
from baldur.services.metrics.periodic_updater import (
    DAEMON_WORKER_NAME,
    DomainGaugeUpdater,
    get_domain_gauge_updater,
    reset_domain_gauge_updater,
    start_domain_gauge_updater,
)
from baldur.services.metrics.updaters import (
    METRIC_COLLECTION_HEARTBEAT_COMPONENT,
    collect_all_metrics,
)

# Long enough that any tick beyond the first would have to come from a bug,
# not from the test waiting.
_UNREACHABLE_INTERVAL_SECONDS = 3600.0


def _collect_stub(side_effect=None) -> MagicMock:
    """A stand-in for the collection callable, spec'd to the real body."""
    return MagicMock(spec=collect_all_metrics, side_effect=side_effect)


def _heartbeats() -> float:
    """Current value of the collection heartbeat counter for this process."""
    return baldur_heartbeat_count.labels(
        component=METRIC_COLLECTION_HEARTBEAT_COMPONENT
    )._value.get()


@pytest.fixture(autouse=True)
def _reset_updater_singleton():
    """Stop + drop the collector singleton so no daemon thread leaks."""
    reset_domain_gauge_updater()
    yield
    reset_domain_gauge_updater()


@pytest.fixture
def updaters():
    """Track directly-constructed updaters and stop them in teardown."""
    created: list[DomainGaugeUpdater] = []

    def _make(**kwargs) -> DomainGaugeUpdater:
        kwargs.setdefault("interval", _UNREACHABLE_INTERVAL_SECONDS)
        updater = DomainGaugeUpdater(**kwargs)
        created.append(updater)
        return updater

    yield _make
    for updater in created:
        updater.stop()


class TestDomainGaugeUpdaterLifecycle:
    """Start, stop and restart semantics of the collector thread."""

    def test_start_seeds_the_heartbeat_before_the_first_tick(self, updaters):
        """A collector whose collection always fails still exports the series.

        Without the seed the dead-man's switch has nothing to evaluate on the
        one process it most needs to describe.
        """
        collect = _collect_stub(RuntimeError("repository unresolvable"))
        updater = updaters(collect=collect)
        before = _heartbeats()

        updater.start()

        # The seed is emitted synchronously by start(), so it is observable
        # without waiting on the thread.
        assert _heartbeats() == before + 1

    def test_start_registers_the_thread_as_a_named_daemon_worker(self, updaters):
        """The liveness family (`baldur_daemon_worker_*`) covers thread death."""
        updater = updaters(collect=_collect_stub())

        updater.start()

        assert updater.is_alive() is True
        assert updater._thread.name == DAEMON_WORKER_NAME

    def test_start_is_idempotent(self, updaters):
        """A double invocation (framework init + post-fork hook) spawns one thread."""
        updater = updaters(collect=_collect_stub())

        updater.start()
        first_thread = updater._thread
        updater.start()

        assert updater._thread is first_thread

    def test_stop_joins_without_a_join_timeout_critical(self, updaters):
        """The Event interrupt returns in ms — the 5s join ceiling never trips."""
        updater = updaters(collect=_collect_stub())
        updater.start()

        with patch("baldur.services.metrics.periodic_updater.logger") as mock_logger:
            updater.stop()

        assert updater.is_alive() is False
        mock_logger.critical.assert_not_called()

    def test_stop_before_start_is_a_no_op(self, updaters):
        """Teardown ordering must not depend on whether the thread ever ran."""
        updater = updaters(collect=_collect_stub())

        updater.stop()

        assert updater.is_alive() is False


class TestDomainGaugeUpdaterCollectionLoop:
    """What one pass of the loop collects, driven without sleeping."""

    def test_first_tick_jitter_delays_collection(self, updaters):
        """A stop during the jitter window collects nothing at all."""
        collect = _collect_stub()
        updater = updaters(collect=collect, jitter_seconds=30.0)
        updater._running = True
        updater._stop_event.set()

        updater._update_loop()

        collect.assert_not_called()
        assert updater._jitter_pending is False

    def test_respawn_after_the_jitter_collects_immediately(self, updaters):
        """The jitter is consumed once; a crash-restart does not re-serve it."""
        collect = _collect_stub()
        updater = updaters(collect=collect, jitter_seconds=30.0)
        updater._jitter_pending = False
        updater._running = True
        updater._stop_event.set()

        updater._update_loop()

        collect.assert_called_once()

    def test_collection_failure_does_not_end_the_loop(self, updaters):
        """One bad tick is not a dead updater — the exception is caught per tick."""
        collect = _collect_stub(RuntimeError("repository down"))
        updater = updaters(collect=collect)
        updater._running = True
        updater._stop_event.set()

        updater._update_loop()

        collect.assert_called_once()

    def test_collect_once_defaults_to_the_shared_collection_body(self, updaters):
        """No injected callable means the real ``collect_all_metrics()`` body."""
        updater = updaters()

        with patch(
            "baldur.services.metrics.updaters.collect_all_metrics"
        ) as collect_all:
            updater._collect_once()

        collect_all.assert_called_once_with()


class TestDomainGaugeUpdaterSingleton:
    """The module-level singleton captures its cadence at the first call."""

    def test_get_returns_the_same_instance(self):
        """Every caller drives one collector per process."""
        assert get_domain_gauge_updater() is get_domain_gauge_updater()

    def test_first_call_captures_the_interval_and_jitter(self):
        """The bootstrap starter is the production first-caller."""
        updater = get_domain_gauge_updater(12.0, jitter_seconds=3.0)

        assert updater._interval == 12.0
        assert updater._jitter_seconds == 3.0

    def test_later_calls_do_not_reconfigure_the_running_collector(self):
        """A second caller's arguments must not silently change the cadence."""
        get_domain_gauge_updater(12.0)

        assert get_domain_gauge_updater(999.0)._interval == 12.0

    def test_start_convenience_starts_the_singleton(self):
        """``start_domain_gauge_updater()`` is what the starter calls."""
        with patch("baldur.services.metrics.updaters.collect_all_metrics"):
            updater = start_domain_gauge_updater(_UNREACHABLE_INTERVAL_SECONDS)

        assert updater is get_domain_gauge_updater()
        assert updater.is_alive() is True

    def test_reset_stops_and_drops_the_singleton(self):
        """Test-facing lifecycle: no daemon thread survives a reset."""
        with patch("baldur.services.metrics.updaters.collect_all_metrics"):
            updater = start_domain_gauge_updater(_UNREACHABLE_INTERVAL_SECONDS)

        reset_domain_gauge_updater()

        assert updater.is_alive() is False
        assert get_domain_gauge_updater() is not updater
