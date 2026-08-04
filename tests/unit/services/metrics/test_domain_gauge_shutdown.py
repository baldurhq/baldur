"""Shutdown wiring for the domain-gauge collector.

The collector is the one background-worker starter of this family with drain
accounting: an unstopped daemon keeps issuing repository reads and log lines
throughout DRAINING and TERMINATING. The handler stops it when the coordinator
starts draining and reports drain completion by polling the thread, so an
in-flight tick blocked inside a repository read is a ceiling the coordinator's
own budget resolves — not a guarantee this handler makes.

Driven through ``integrate_domain_gauge_updater_with_shutdown_coordinator()``
and the handler methods directly rather than through a real drain: the
coordinator's own sequencing has its own suite.

Reference:
    src/baldur/services/metrics/shutdown.py
"""

from __future__ import annotations

import threading

import pytest

from baldur.services.metrics.periodic_updater import (
    DomainGaugeUpdater,
    get_domain_gauge_updater,
    reset_domain_gauge_updater,
)
from baldur.services.metrics.shutdown import (
    DomainGaugeUpdaterShutdownHandler,
    integrate_domain_gauge_updater_with_shutdown_coordinator,
)

_UNREACHABLE_INTERVAL_SECONDS = 3600.0


@pytest.fixture(autouse=True)
def _reset_updater_singleton():
    """Stop + drop the collector singleton so no daemon thread leaks."""
    reset_domain_gauge_updater()
    yield
    reset_domain_gauge_updater()


@pytest.fixture
def blocking_collector():
    """A started collector whose first tick blocks until the test releases it.

    Two Events, so the test can observe the thread mid-tick deterministically
    instead of racing a sleep: ``entered`` is set when the tick starts,
    ``release`` lets it finish.
    """
    entered = threading.Event()
    release = threading.Event()

    def _collect() -> None:
        entered.set()
        release.wait(timeout=5.0)

    updater = DomainGaugeUpdater(
        interval=_UNREACHABLE_INTERVAL_SECONDS, collect=_collect
    )
    updater.start()
    assert entered.wait(timeout=5.0), "collector thread never reached its first tick"
    try:
        yield updater, release
    finally:
        release.set()
        updater.stop()


class TestDomainGaugeShutdownHandler:
    """Drain semantics of the collector's shutdown handler."""

    def test_on_shutdown_start_stops_the_collector(self):
        """Draining stops the repository reads instead of letting them run on."""
        updater = DomainGaugeUpdater(interval=_UNREACHABLE_INTERVAL_SECONDS)
        handler = DomainGaugeUpdaterShutdownHandler(updater)

        handler.on_shutdown_start()

        assert updater.is_alive() is False
        assert updater._stop_event.is_set() is True

    def test_is_drain_complete_true_when_no_thread_was_ever_started(self):
        """A collector that never started cannot hold the drain open."""
        handler = DomainGaugeUpdaterShutdownHandler(
            DomainGaugeUpdater(interval=_UNREACHABLE_INTERVAL_SECONDS)
        )

        assert handler.is_drain_complete() is True

    def test_is_drain_complete_false_while_a_tick_is_still_running(
        self, blocking_collector
    ):
        """A tick blocked inside a repository read reports the drain incomplete.

        This is the ceiling the handler documents: the stop Event interrupts
        the interval sleep, not a collection already in flight, so drain
        completion waits on the coordinator's own budget.
        """
        updater, _release = blocking_collector
        handler = DomainGaugeUpdaterShutdownHandler(updater)

        assert handler.is_drain_complete() is False

    def test_is_drain_complete_true_once_the_collector_has_stopped(
        self, blocking_collector
    ):
        """The poll flips as soon as the thread is gone."""
        updater, release = blocking_collector
        handler = DomainGaugeUpdaterShutdownHandler(updater)

        release.set()
        handler.on_shutdown_start()

        assert handler.is_drain_complete() is True

    def test_on_force_shutdown_stops_the_collector_again(self):
        """Force shutdown is a second stop, not a first one."""
        updater = DomainGaugeUpdater(interval=_UNREACHABLE_INTERVAL_SECONDS)
        handler = DomainGaugeUpdaterShutdownHandler(updater)

        handler.on_force_shutdown([])

        assert updater._stop_event.is_set() is True


class TestDomainGaugeShutdownIntegration:
    """The factory the bootstrap shutdown registration appends."""

    def test_integration_factory_does_not_create_the_collector_singleton(self):
        """Building the handler must not fix the collector's cadence.

        ``init()`` registers shutdown handlers BEFORE it runs the
        background-worker starters, and the singleton accessor captures
        interval and jitter at its first call. A factory that resolved the
        collector here would leave the starter's settings-derived values —
        ``BALDUR_METRICS_COLLECTION_INTERVAL_SECONDS`` and the startup jitter —
        silently discarded on every deployment.
        """
        from baldur.services.metrics import periodic_updater

        handler = integrate_domain_gauge_updater_with_shutdown_coordinator()

        assert isinstance(handler, DomainGaugeUpdaterShutdownHandler)
        assert periodic_updater._updater is None

    def test_integration_factory_handler_resolves_the_collector_at_shutdown(self):
        """End to end through the factory: drain start stops the live collector."""
        handler = integrate_domain_gauge_updater_with_shutdown_coordinator()
        updater = get_domain_gauge_updater(_UNREACHABLE_INTERVAL_SECONDS)

        handler.on_shutdown_start()

        assert updater.is_alive() is False
        assert updater._stop_event.is_set() is True
