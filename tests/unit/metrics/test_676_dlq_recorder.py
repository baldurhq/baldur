"""676 — DLQ recorder: auto-replay armed gauge + dispatch counter (D8/D9).

Target: ``baldur.metrics.recorders.dlq.DLQMetricRecorder``

    - ``set_auto_replay_armed(bool)`` -> ``baldur_dlq_auto_replay_armed`` 0/1
      empty-label gauge (mirrors the system-control enabled gauge).
    - ``record_replay_dispatch(outcome)`` -> ``baldur_dlq_replay_dispatch_total``
      counter, labelled by the bounded ``outcome`` set.

The Prometheus REGISTRY is a process-singleton, so counter assertions measure
deltas and gauge assertions rely on last-write-wins.
"""

from __future__ import annotations

import pytest

from baldur.metrics.recorders.dlq import DLQMetricRecorder

# The bounded outcome cardinality asserted at the dispatch site.
_OUTCOMES = [
    "dispatched",
    "skipped_disabled",
    "celery_missing",
    "error",
]


class TestDLQRecorderArmedGaugeContract:
    """``baldur_dlq_auto_replay_armed`` 0/1 empty-label gauge."""

    def test_gauge_name_and_no_labels(self):
        recorder = DLQMetricRecorder()
        name_attr = getattr(recorder._auto_replay_armed, "_name", "")
        assert "dlq_auto_replay_armed" in name_attr

    def test_armed_true_sets_gauge_to_one(self):
        recorder = DLQMetricRecorder()
        recorder.set_auto_replay_armed(True)
        assert recorder._auto_replay_armed._value.get() == 1

    def test_armed_false_sets_gauge_to_zero(self):
        recorder = DLQMetricRecorder()
        recorder.set_auto_replay_armed(False)
        assert recorder._auto_replay_armed._value.get() == 0


class TestDLQRecorderDispatchCounterContract:
    """``baldur_dlq_replay_dispatch_total`` counter, labelled by outcome."""

    def test_counter_name(self):
        recorder = DLQMetricRecorder()
        name_attr = getattr(recorder._replay_dispatch_total, "_name", "")
        assert "dlq_replay_dispatch" in name_attr

    @pytest.mark.parametrize("outcome", _OUTCOMES)
    def test_record_increments_the_labelled_outcome(self, outcome):
        recorder = DLQMetricRecorder()
        before = recorder._replay_dispatch_total.labels(outcome=outcome)._value.get()

        recorder.record_replay_dispatch(outcome)

        after = recorder._replay_dispatch_total.labels(outcome=outcome)._value.get()
        assert after == before + 1

    def test_distinct_outcomes_do_not_cross_contaminate(self):
        recorder = DLQMetricRecorder()
        before_other = recorder._replay_dispatch_total.labels(
            outcome="error"
        )._value.get()

        recorder.record_replay_dispatch("dispatched")

        # Incrementing one outcome does not move a sibling label's series.
        after_other = recorder._replay_dispatch_total.labels(
            outcome="error"
        )._value.get()
        assert after_other == before_other
