"""One repository snapshot per tick, and the heartbeat gate that guards it.

``collect_all_metrics()`` is the body of every collection driver — the
per-process daemon thread, the optional Celery task, and the public export. Two
properties of that body are load-bearing and neither is visible from the
individual updaters:

* the repository is read **once** per tick, not once per updater. On Redis that
  read is the O(pending) breakdown scan, and the tick now runs per process per
  interval instead of once a day.
* the ``metric_collection`` heartbeat advances **iff the DLQ status family was
  written**. That family carries the pending total the bundled backlog alerts
  page on, so the dead-man's switch must track the paged gauge and not the
  diagnostic per-domain one — in both directions. The decisive cases are the
  crossed ones, which is why they are asserted as a pair.

Reference:
    src/baldur/services/metrics/updaters.py
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur.interfaces.repositories import FailedOperationRepository
from baldur.metrics.recorders.dlq import DLQMetricRecorder
from baldur.metrics.recorders.retry import RetryMetricRecorder
from baldur.services.metrics import baldur_heartbeat_count
from baldur.services.metrics.updaters import (
    METRIC_COLLECTION_HEARTBEAT_COMPONENT,
    collect_all_metrics,
)

# Status family writable, per-domain breakdown missing: the Redis incident
# shape, where the O(pending) scan fails open and the O(1) counts survive.
_STATUS_ONLY_SNAPSHOT = {"total": 5, "pending_count": 120}

# Per-domain breakdown present, no key shape the status family can read: the
# mirror case, where the paged gauge is the one that was skipped.
_PENDING_ONLY_SNAPSHOT = {"total": 5, "pending_by_domain": {"payment": 3}}

_HEALTHY_SNAPSHOT = {
    "total": 5,
    "pending_count": 3,
    "reviewing_count": 0,
    "resolved_count": 2,
    "rejected_count": 0,
    "pending_by_domain": {"payment": 3},
}


def _heartbeats() -> float:
    """Current value of the collection heartbeat counter for this process.

    The counter, not the timestamp gauge: it moves by exactly one per emit,
    where a timestamp can repeat under a coarse platform clock.
    """
    return baldur_heartbeat_count.labels(
        component=METRIC_COLLECTION_HEARTBEAT_COMPONENT
    )._value.get()


@pytest.fixture
def repository():
    """A spy repository whose ``get_statistics()`` calls can be counted."""
    repo = MagicMock(spec=FailedOperationRepository)
    repo.get_statistics.return_value = dict(_HEALTHY_SNAPSHOT)
    return repo


@pytest.fixture
def collection_env(repository):
    """Wire the collection body to the spy repository and spy recorders.

    The circuit-breaker updater reads a different repository entirely and is
    stubbed out so a tick's DLQ behaviour is what these tests observe. The
    arming probe's broker seam is stubbed for the same reason and one more: a
    tick now refreshes the armed gauge, and an unpatched probe would dial the
    default broker URL for real.
    """
    facade = SimpleNamespace(
        dlq=MagicMock(spec=DLQMetricRecorder),
        retry=MagicMock(spec=RetryMetricRecorder),
    )
    with (
        patch(
            "baldur.factory.ProviderRegistry.get_failed_operation_repo",
            return_value=repository,
        ),
        patch("baldur.metrics.prometheus.get_metrics", return_value=facade),
        patch(
            "baldur.services.metrics.updaters.get_registered_domains",
            return_value=["payment"],
        ),
        patch(
            "baldur.services.metrics.updaters.update_circuit_breaker_gauges",
            return_value={},
        ),
        patch(
            "baldur.services.replay_service.arming._probe_dlq_worker", return_value="ok"
        ),
    ):
        yield facade


class TestCollectAllSingleSnapshot:
    """The tick pays the repository read once, whatever it drives with it."""

    def test_single_snapshot_reads_the_repository_exactly_once(
        self, collection_env, repository
    ):
        """A call count, not a value — the assertion survives new updaters.

        Three DLQ-family updaters run per tick and each used to fetch its own
        statistics dict, tripling the most expensive read in the tick and the
        warning it logs on failure.
        """
        collect_all_metrics()

        assert repository.get_statistics.call_count == 1

    def test_single_snapshot_feeds_every_dlq_family_updater(
        self, collection_env, repository
    ):
        """One read, three families written — the read is shared, not skipped."""
        report = collect_all_metrics()

        assert report["dlq_pending_by_domain"] == {"payment": 3}
        assert report["dlq_by_status"]["pending"] == 3
        assert collection_env.dlq.set_status_count.called

    def test_snapshot_fetch_failure_writes_no_gauge_and_holds_the_heartbeat(
        self, collection_env, repository
    ):
        """A raising repository leaves every DLQ family exactly as it was."""
        repository.get_statistics.side_effect = RuntimeError("backend down")
        before = _heartbeats()

        report = collect_all_metrics()

        assert report["dlq_pending_by_domain"] == {}
        assert report["dlq_by_status"] == {}
        collection_env.dlq.set_status_count.assert_not_called()
        collection_env.dlq.set_pending_count.assert_not_called()
        assert _heartbeats() == before


class TestCollectAllHeartbeatGate:
    """The dead-man's switch follows the paged family, not the diagnostic one."""

    def test_heartbeat_advances_on_a_tick_whose_breakdown_absent(
        self, collection_env, repository
    ):
        """The paged gauge is fresh, so no staleness page during the incident.

        The Redis adapter fails the O(pending) breakdown open at outage scale
        while the O(1) counts survive. Gating the heartbeat on the per-domain
        family would page ``BaldurMetricCollectionStale`` for the whole
        incident, stacked on the real DLQ page.
        """
        repository.get_statistics.return_value = dict(_STATUS_ONLY_SNAPSHOT)
        before = _heartbeats()

        report = collect_all_metrics()

        assert report["dlq_by_status"]["pending"] == 120
        assert report["dlq_pending_by_domain"] == {}
        assert _heartbeats() == before + 1

    def test_heartbeat_holds_on_a_tick_whose_status_skipped(
        self, collection_env, repository
    ):
        """A stalled paged write must not leave the switch green.

        The mirror of the case above: the per-domain family wrote, the status
        family had no source. The paged series is frozen, so the heartbeat must
        not advance over it.
        """
        repository.get_statistics.return_value = dict(_PENDING_ONLY_SNAPSHOT)
        before = _heartbeats()

        report = collect_all_metrics()

        assert report["dlq_pending_by_domain"] == {"payment": 3}
        assert report["dlq_by_status"] == {}
        assert _heartbeats() == before

    def test_heartbeat_advances_once_per_healthy_tick(self, collection_env, repository):
        """The alive-signal is emitted per successful collection, not per family."""
        before = _heartbeats()

        collect_all_metrics()

        assert _heartbeats() == before + 1
