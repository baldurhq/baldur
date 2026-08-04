"""Statistics-dict projection and fail direction of the DLQ gauge updaters.

Each updater is a pure function over one ``get_statistics()`` snapshot, and the
shipped adapters disagree on the key shape for the same quantity: Redis emits
flat ``*_count`` keys plus an O(pending) ``pending_by_domain`` scan, memory and
SQL emit ``by_status`` and no flat keys at all. Two things are asserted here.

**The projection** — which source key reaches which gauge label. The by-status
fixtures deliberately carry BOTH ``reviewing`` and ``requires_review`` with
different values: both are live statuses that memory and SQL emit, so a stub
carrying only one of them passes under the correct explicit projection and
under a colliding name-identity map alike.

**The fail direction** — a snapshot that cannot be trusted must leave the
previously exported gauge values standing. Writing zeros would resolve the DLQ
backlog alerts during exactly the incident that produced the backlog, so the
updater returns ``None`` and writes nothing instead.

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
from baldur.services.metrics.updaters import (
    _STATUS_GAUGE_SOURCE_KEYS,
    _resolve_pending_total,
    update_dlq_pending_gauges,
    update_dlq_status_gauges,
    update_retry_success_rates,
)

# The memory/SQL shape: `by_status` keyed by the raw status values, and NO flat
# `pending_count`. Both review statuses are present with different values —
# see the module docstring.
_BY_STATUS_SNAPSHOT = {
    "total": 30,
    "by_status": {
        "pending": 11,
        "requires_review": 4,
        "reviewing": 7,
        "resolved": 5,
        "rejected": 3,
        "archived": 2,
    },
    "pending_by_domain": {"payment": 11},
}

# The Redis shape: flat `*_count` keys, where `reviewing_count` already carries
# the requires-review count.
_FLAT_SNAPSHOT = {
    "total": 30,
    "pending_count": 11,
    "reviewing_count": 4,
    "resolved_count": 5,
    "rejected_count": 3,
    "archived_count": 2,
    "pending_by_domain": {"payment": 11},
}


@pytest.fixture
def dlq_recorder():
    """A spy standing in for the process-global DLQ gauge recorder."""
    return MagicMock(spec=DLQMetricRecorder)


@pytest.fixture
def retry_recorder():
    """A spy standing in for the process-global retry gauge recorder."""
    return MagicMock(spec=RetryMetricRecorder)


@pytest.fixture
def published(dlq_recorder, retry_recorder):
    """Patch the metrics facade and expose the recorders the updaters reach.

    ``get_metrics()`` is a module-level singleton accessor, so it is patched
    with a with-form context manager rather than autospec (guidelines §6.5.2).
    """
    facade = SimpleNamespace(dlq=dlq_recorder, retry=retry_recorder)
    with patch(
        "baldur.metrics.prometheus.get_metrics",
        return_value=facade,
    ):
        yield facade


@pytest.fixture
def registered_domains():
    """Pin the per-process domain registry the pending updater enumerates."""
    with patch(
        "baldur.services.metrics.updaters.get_registered_domains",
        return_value=["payment", "inventory"],
    ):
        yield ["payment", "inventory"]


def _status_writes(recorder) -> dict[str, int]:
    """Collapse the recorded ``set_status_count`` calls into label -> value."""
    return {
        call.args[0]: call.args[1] for call in recorder.set_status_count.call_args_list
    }


def _pending_writes(recorder) -> dict[str, int]:
    """Collapse the recorded ``set_pending_count`` calls into domain -> value."""
    return {
        call.args[0]: call.args[1] for call in recorder.set_pending_count.call_args_list
    }


class TestResolvePendingTotalBehavior:
    """The shared "pending total, whichever shape this adapter uses" projection."""

    def test_flat_pending_count_is_read_when_the_adapter_emits_it(self):
        """The Redis shape resolves through the O(1) flat key."""
        assert _resolve_pending_total(_FLAT_SNAPSHOT) == 11

    def test_by_status_pending_is_read_when_there_is_no_flat_key(self):
        """The memory/SQL shape resolves through ``by_status["pending"]``."""
        assert _resolve_pending_total(_BY_STATUS_SNAPSHOT) == 11

    def test_flat_key_wins_over_by_status_when_both_are_present(self):
        """One adapter emitting both shapes must not depend on lookup order."""
        both = {"pending_count": 11, "by_status": {"pending": 99}}

        assert _resolve_pending_total(both) == 11

    def test_zero_pending_count_resolves_to_zero_not_none(self):
        """A measured empty backlog is 0 — distinct from "not measured"."""
        assert _resolve_pending_total({"pending_count": 0}) == 0

    def test_by_status_without_a_pending_bucket_resolves_to_zero(self):
        """An adapter that measured the statuses and found no pending reports 0."""
        assert _resolve_pending_total({"by_status": {"resolved": 5}}) == 0

    def test_neither_shape_present_returns_none(self):
        """Nothing measured the total — callers must be able to tell.

        Returning 0 here would print a fabricated empty backlog on the payload
        surfaces that render this number.
        """
        assert _resolve_pending_total({"total": 30}) is None

    @pytest.mark.parametrize(
        "snapshot",
        [
            {"pending_count": "many"},
            {"pending_count": None},
            {"by_status": {"pending": "many"}},
            {"by_status": {"pending": None}},
        ],
    )
    def test_non_numeric_source_returns_none(self, snapshot):
        """A non-numeric count is unmeasured, not zero."""
        assert _resolve_pending_total(snapshot) is None


class TestStatusGaugeProjectionContract:
    """The four shipped status gauge labels and the source key behind each."""

    def test_projection_maps_exactly_the_four_gauged_labels(self):
        """Spec values, hardcoded: the gauge vocabulary is a shipped contract."""
        assert _STATUS_GAUGE_SOURCE_KEYS == {
            "pending": "pending",
            "reviewing": "requires_review",
            "resolved": "resolved",
            "rejected": "rejected",
        }

    def test_reviewing_label_has_exactly_one_source_key(self):
        """``reviewing`` and ``requires_review`` must not collide on one label.

        Both are live statuses that memory and SQL emit in ``by_status``. A
        name-identity map with a single alias would land both on the
        ``reviewing`` gauge, making its value depend on iteration order.
        """
        sources = [
            source
            for source in _STATUS_GAUGE_SOURCE_KEYS.values()
            if source in {"reviewing", "requires_review"}
        ]

        assert sources == ["requires_review"]


class TestDLQStatusGaugeSourceProjection:
    """The status family reads whichever key shape the adapter supplies."""

    def test_flat_keys_publish_each_shipped_status_count(self, published, dlq_recorder):
        """The Redis shape maps one flat key per gauge label."""
        result = update_dlq_status_gauges(stats=_FLAT_SNAPSHOT)

        assert _status_writes(dlq_recorder) == {
            "pending": 11,
            "reviewing": 4,
            "resolved": 5,
            "rejected": 3,
        }
        assert result == {"pending": 11, "reviewing": 4, "resolved": 5, "rejected": 3}

    def test_by_status_projection_maps_requires_review_to_the_reviewing_label(
        self, published, dlq_recorder
    ):
        """The memory/SQL shape publishes the requires-review count, not ``reviewing``.

        The snapshot carries both keys with different values, so a colliding
        projection would publish 7 (or a sum) instead of 4.
        """
        update_dlq_status_gauges(stats=_BY_STATUS_SNAPSHOT)

        assert _status_writes(dlq_recorder)["reviewing"] == 4

    def test_by_status_projection_publishes_only_the_four_gauged_labels(
        self, published, dlq_recorder
    ):
        """``archived`` and the raw ``reviewing`` bucket stay ungauged.

        Same set the flat path publishes — the two paths must not disagree on
        which statuses have a series.
        """
        update_dlq_status_gauges(stats=_BY_STATUS_SNAPSHOT)

        assert set(_status_writes(dlq_recorder)) == {
            "pending",
            "reviewing",
            "resolved",
            "rejected",
        }

    def test_by_status_missing_bucket_publishes_zero(self, published, dlq_recorder):
        """A status the adapter measured and did not find is a real zero."""
        update_dlq_status_gauges(stats={"by_status": {"pending": 2}})

        assert _status_writes(dlq_recorder) == {
            "pending": 2,
            "reviewing": 0,
            "resolved": 0,
            "rejected": 0,
        }

    def test_neither_key_shape_writes_nothing_and_returns_none(
        self, published, dlq_recorder
    ):
        """An out-of-tree repository supplying neither shape holds the gauges."""
        result = update_dlq_status_gauges(stats={"total": 30})

        assert result is None
        dlq_recorder.set_status_count.assert_not_called()

    def test_by_status_of_the_wrong_type_writes_nothing_and_returns_none(
        self, published, dlq_recorder
    ):
        """A non-dict ``by_status`` is no source at all, not an empty one."""
        result = update_dlq_status_gauges(stats={"by_status": []})

        assert result is None
        dlq_recorder.set_status_count.assert_not_called()


class TestDLQPendingGaugeFailDirection:
    """An untrustworthy breakdown holds the gauges instead of zeroing them."""

    def test_healthy_breakdown_publishes_every_registered_domain(
        self, published, registered_domains, dlq_recorder
    ):
        """The write loop enumerates the registry, not the breakdown."""
        result = update_dlq_pending_gauges(
            stats={"pending_count": 11, "pending_by_domain": {"payment": 11}}
        )

        assert _pending_writes(dlq_recorder) == {"payment": 11, "inventory": 0}
        assert result == {"payment": 11}

    def test_breakdown_absent_writes_no_gauge_and_returns_none(
        self, published, registered_domains, dlq_recorder
    ):
        """The adapter's documented fail-open drops the key; nothing is written.

        This is the incident shape: the O(pending) scan times out at
        outage-scale backlog while the baseline counts survive.
        """
        result = update_dlq_pending_gauges(stats={"pending_count": 120})

        assert result is None
        dlq_recorder.set_pending_count.assert_not_called()

    def test_breakdown_absent_retains_the_previously_exported_values(
        self, published, registered_domains, dlq_recorder
    ):
        """No domain is written to 0 by the failing call — the last write stands."""
        # Given a healthy tick that exported a live backlog
        update_dlq_pending_gauges(
            stats={"pending_count": 120, "pending_by_domain": {"payment": 120}}
        )
        writes_after_healthy_tick = list(dlq_recorder.set_pending_count.call_args_list)

        # When the next tick's breakdown is unavailable
        update_dlq_pending_gauges(stats={"pending_count": 120})

        # Then it added no write at all, so the exported 120 is still standing
        assert (
            dlq_recorder.set_pending_count.call_args_list == writes_after_healthy_tick
        )

    def test_inconsistent_breakdown_against_a_live_baseline_returns_none(
        self, published, registered_domains, dlq_recorder
    ):
        """Present-but-empty against a non-zero baseline is a failure, not a drain.

        A mid-call backend degradation flips the breakdown collector onto its
        empty in-memory fallback, which returns ``{}`` without raising — key
        present, values empty, baseline live.
        """
        result = update_dlq_pending_gauges(
            stats={"pending_count": 120, "pending_by_domain": {}}
        )

        assert result is None
        dlq_recorder.set_pending_count.assert_not_called()

    def test_empty_breakdown_with_a_zero_baseline_publishes_zeros(
        self, published, registered_domains, dlq_recorder
    ):
        """A genuinely drained DLQ is consistent with its own baseline."""
        result = update_dlq_pending_gauges(
            stats={"pending_count": 0, "pending_by_domain": {}}
        )

        assert result == {}
        assert _pending_writes(dlq_recorder) == {"payment": 0, "inventory": 0}

    def test_empty_breakdown_on_an_adapter_without_a_baseline_publishes_zeros(
        self, published, registered_domains, dlq_recorder
    ):
        """The consistency guard is inert where no baseline key exists.

        Memory and SQL emit no ``pending_count`` and raise on backend error
        rather than degrading silently, so an empty breakdown there is real.
        """
        result = update_dlq_pending_gauges(stats={"pending_by_domain": {}})

        assert result == {}
        assert _pending_writes(dlq_recorder) == {"payment": 0, "inventory": 0}

    def test_repository_read_exception_writes_nothing_and_returns_none(
        self, published, registered_domains, dlq_recorder
    ):
        """A raising repository holds the gauges too."""
        repository = MagicMock(spec=FailedOperationRepository)
        repository.get_statistics.side_effect = RuntimeError("backend down")

        result = update_dlq_pending_gauges(repository=repository)

        assert result is None
        dlq_recorder.set_pending_count.assert_not_called()

    def test_prefetched_snapshot_is_used_without_touching_the_repository(
        self, published, registered_domains
    ):
        """The per-tick snapshot parameter replaces the updater's own read."""
        repository = MagicMock(spec=FailedOperationRepository)

        update_dlq_pending_gauges(
            repository=repository,
            stats={"pending_count": 1, "pending_by_domain": {"payment": 1}},
        )

        repository.get_statistics.assert_not_called()


class TestRetrySuccessRateHonestAbsence:
    """No producer computes per-domain success rates — the gauge stays absent."""

    def test_success_rate_source_absent_returns_none_and_writes_nothing(
        self, published, registered_domains, retry_recorder
    ):
        """The shipped adapters emit no ``success_rates_by_domain`` key at all."""
        result = update_retry_success_rates(stats=_FLAT_SNAPSHOT)

        assert result is None
        retry_recorder.set_success_rate.assert_not_called()

    def test_success_rate_is_never_defaulted_to_a_fabricated_hundred(
        self, published, registered_domains, retry_recorder
    ):
        """A domain missing from a present map is skipped, not defaulted.

        The pre-change code wrote 100.0 for every registered domain, which is
        the fabricated constant this posture exists to remove.
        """
        result = update_retry_success_rates(
            stats={"success_rates_by_domain": {"payment": 95.0}}
        )

        assert result == {"payment": 95.0}
        retry_recorder.set_success_rate.assert_called_once_with("payment", 95.0)

    def test_present_source_publishes_the_measured_rate(
        self, published, registered_domains, retry_recorder
    ):
        """When a producer does exist, every registered domain it covers is written."""
        update_retry_success_rates(
            stats={"success_rates_by_domain": {"payment": 95.0, "inventory": 80.0}}
        )

        assert retry_recorder.set_success_rate.call_count == 2

    def test_non_dict_source_returns_none_and_writes_nothing(
        self, published, registered_domains, retry_recorder
    ):
        """A malformed source is no source."""
        result = update_retry_success_rates(stats={"success_rates_by_domain": []})

        assert result is None
        retry_recorder.set_success_rate.assert_not_called()
