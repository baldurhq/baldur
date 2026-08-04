"""One collection tick moves the exposition surface — asserted end to end.

The defect this composition closes is invisible from any single seam: every
module in the chain passes its own unit test while the exported series stays
frozen at whatever it held at boot. What is asserted here is the chain itself —

    DomainGaugeUpdater -> collect_all_metrics() -> ProviderRegistry repository
    -> BaldurMetrics recorders -> the process-local Prometheus REGISTRY
    -> the exposition text

— with a real repository, real recorders and the real registry; and separately,
the ``init()`` -> ``_BACKGROUND_WORKER_STARTERS`` -> starter -> thread wiring
that decides whether any of it runs in a given serving process.

Mock-based — no Docker. The repository is the in-memory adapter, which emits
the ``by_status`` key shape (no flat ``pending_count``), so this also exercises
the projection the Redis-shaped unit fixtures cannot.

The tick is driven synchronously through ``_collect_once()`` rather than by
sleeping out an interval: the collection body is the same, and a wall-clock
wait would be both slow and platform-sensitive.

Reference:
    src/baldur/services/metrics/periodic_updater.py
    src/baldur/services/metrics/updaters.py
"""

from __future__ import annotations

import pytest

from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.factory import ProviderRegistry
from baldur.metrics.registry import (
    register_domain,
    reset_registered_domains,
    resolve_domain_label,
)
from baldur.services.metrics.periodic_updater import (
    DomainGaugeUpdater,
    get_domain_gauge_updater,
    reset_domain_gauge_updater,
)

_UNREACHABLE_INTERVAL_SECONDS = 3600.0

# A domain no other suite touches. The Prometheus registry is process-global
# and its children outlive a test, so an assertion that a series is ABSENT is
# only meaningful under a label value nothing else in the run has written.
_PROBE_DOMAIN = "dlq_gauge_probe"


def _exposition() -> str:
    """Current prometheus exposition text from the shared REGISTRY."""
    from prometheus_client import generate_latest

    return generate_latest().decode()


def _series_value(text: str, metric: str, **labels: str) -> float | None:
    """Value of the ``metric`` sample carrying all of ``labels``, or None."""
    needles = [f'{key}="{value}"' for key, value in labels.items()]
    for line in text.splitlines():
        if line.startswith(metric + "{") and all(n in line for n in needles):
            return float(line.rsplit(" ", 1)[-1])
    return None


@pytest.fixture(autouse=True)
def _clean_domain_registry():
    """The per-process domain registry is global; restore it around each test."""
    reset_registered_domains()
    yield
    reset_registered_domains()


@pytest.fixture(autouse=True)
def _reset_updater_singleton():
    """Stop + drop the collector singleton so no daemon thread leaks."""
    reset_domain_gauge_updater()
    yield
    reset_domain_gauge_updater()


@pytest.fixture
def repository():
    """A real in-memory DLQ repository installed as the registry default."""
    repo = InMemoryFailedOperationRepository()
    with ProviderRegistry.failed_op_repo.override(repo):
        yield repo


def _store_pending(repo, domain: str, count: int = 1) -> str:
    """Store ``count`` pending failures under ``domain`` and return its label.

    The domain is registered the way ``protect()`` registers it — the gauge
    loop enumerates the registry, so an unregistered domain has no series.
    """
    register_domain(domain)
    for index in range(count):
        repo.create(
            domain=domain,
            failure_type="PG_TIMEOUT",
            error_message=f"boom {index}",
        )
    return resolve_domain_label(domain)


class TestDomainGaugeCollectionEndToEnd:
    """A tick turns repository state into exported samples."""

    def test_domain_gauge_tick_publishes_the_stored_backlog(self, repository):
        """The per-domain series moves from a real DLQ entry, through a real tick."""
        label = _store_pending(repository, _PROBE_DOMAIN, count=3)
        updater = DomainGaugeUpdater(interval=_UNREACHABLE_INTERVAL_SECONDS)

        updater._collect_once()

        assert (
            _series_value(_exposition(), "baldur_dlq_pending_count", domain=label) == 3
        )

    def test_domain_gauge_tick_publishes_the_paged_status_total(self, repository):
        """The series the bundled backlog alerts read is the one that must move.

        The in-memory adapter emits ``by_status`` and no flat ``pending_count``,
        so this is the projection path — a regression there would leave the
        alerts evaluating a constant on two of the three shipped backends.
        """
        _store_pending(repository, _PROBE_DOMAIN, count=3)
        updater = DomainGaugeUpdater(interval=_UNREACHABLE_INTERVAL_SECONDS)

        updater._collect_once()

        assert (
            _series_value(_exposition(), "baldur_dlq_items_by_status", status="pending")
            == 3
        )

    def test_domain_gauge_series_tracks_a_growing_backlog(self, repository):
        """A second tick reports the new value — the point of a periodic updater.

        A single tick would pass even with the pre-change once-at-boot
        hydration; only a second tick over changed state can tell the two
        apart.
        """
        label = _store_pending(repository, _PROBE_DOMAIN, count=3)
        updater = DomainGaugeUpdater(interval=_UNREACHABLE_INTERVAL_SECONDS)
        updater._collect_once()

        _store_pending(repository, _PROBE_DOMAIN, count=2)
        updater._collect_once()

        text = _exposition()
        assert _series_value(text, "baldur_dlq_pending_count", domain=label) == 5
        assert _series_value(text, "baldur_dlq_items_by_status", status="pending") == 5

    def test_domain_gauge_tick_emits_the_collection_heartbeat(self, repository):
        """The dead-man's switch advances on the tick that wrote the paged gauge."""
        _store_pending(repository, _PROBE_DOMAIN)
        updater = DomainGaugeUpdater(interval=_UNREACHABLE_INTERVAL_SECONDS)

        updater._collect_once()

        assert (
            _series_value(
                _exposition(),
                "baldur_heartbeat_timestamp_seconds",
                component="metric_collection",
            )
            is not None
        )

    def test_domain_gauge_tick_publishes_no_retry_success_rate(self, repository):
        """No producer exists, so the rate stays absent rather than reading 100%.

        The family is declared at import, so its HELP/TYPE lines are always in
        the exposition — what must not appear is a *sample* for the domain the
        tick just collected.
        """
        label = _store_pending(repository, _PROBE_DOMAIN)
        updater = DomainGaugeUpdater(interval=_UNREACHABLE_INTERVAL_SECONDS)

        updater._collect_once()

        assert (
            _series_value(_exposition(), "baldur_retry_success_rate", domain=label)
            is None
        )


class TestDomainGaugeCollectorInitWiring:
    """``init()`` is what puts the collector in a serving process."""

    @pytest.fixture(autouse=True)
    def _isolated_init_state(self):
        """Each test starts and ends with a clean bootstrap state."""
        from baldur import bootstrap

        bootstrap.reset_init_state()
        yield
        bootstrap.reset_init_state()

    def test_init_starts_the_domain_gauge_collector(self, monkeypatch, repository):
        """The plain-Python, Celery-less shape gets a live collector thread.

        The starter lives in ``_BACKGROUND_WORKER_STARTERS``, so this is the
        same path the gunicorn ``post_worker_init`` hook drives per worker.
        """
        import baldur

        monkeypatch.setenv("BALDUR_DOMAIN_GAUGE_UPDATER_AUTOSTART", "1")
        monkeypatch.setenv("BALDUR_METRICS_JITTER_ENABLED", "false")
        monkeypatch.setenv("BALDUR_METRICS_COLLECTION_INTERVAL_SECONDS", "3600")

        from baldur.settings.metrics import reset_metrics_settings

        reset_metrics_settings()
        try:
            baldur.init()
            updater = get_domain_gauge_updater()

            assert updater.is_alive() is True
            assert updater._interval == 3600.0
            assert updater._jitter_seconds == 0.0
        finally:
            reset_domain_gauge_updater()
            reset_metrics_settings()

    def test_init_starts_no_collector_when_the_hatch_is_off(
        self, monkeypatch, repository
    ):
        """The hatch is what keeps the unit suite free of this daemon thread."""
        import baldur

        monkeypatch.setenv("BALDUR_DOMAIN_GAUGE_UPDATER_AUTOSTART", "0")

        baldur.init()

        assert get_domain_gauge_updater().is_alive() is False
