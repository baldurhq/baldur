"""Unit tests for the shipped self-healing demo's bookkeeping.

The demo's closing line ("lost N", and the exit code behind it) is computed
from what the demo itself counted, so the counting has to agree with what the
framework captures. Every charge that failed on the way out is parked — the
ones that exhausted their retries AND the ones the OPEN breaker rejected before
they ran — and every parked charge is replayed on recovery. A tally that
measured "lost" against the retry-exhausted charges alone ended a healthy run
at ``lost -2`` with exit code 2, which is the regression pinned here.

A larger outage (``--outage-charges``) replays its backlog in passes, one batch
event each, and the passes keep landing after the first. A tally taken at the
first batch reported a 500-charge outage as ``400/400 ... lost 100`` while the
last pass was still running; the drain check below is what the tally now waits
on.
"""

from __future__ import annotations

import pytest

from baldur.scripts.demo_self_healing import (
    _batch_totals,
    _parse_args,
    _replay_drained,
    _Tally,
)


def _outage_tally() -> _Tally:
    """Five charges exhaust their retries, then two are fast-rejected."""
    tally = _Tally()
    for _ in range(3):
        tally.record_ok()
    for order in range(104, 109):
        tally.record_failed(order)
    for order in (109, 110):
        tally.record_rejected(order)
    return tally


class TestParkedWork:
    def test_rejected_charges_are_parked_alongside_failed_ones(self):
        tally = _outage_tally()

        assert tally.failed == 5
        assert tally.rejected == 2
        assert tally.parked == 7
        assert tally.span() == "#104-110"

    def test_lost_is_zero_when_every_parked_charge_came_back(self):
        tally = _outage_tally()

        assert tally.lost(replayed_ok=7) == 0

    def test_lost_counts_parked_charges_that_did_not_come_back(self):
        tally = _outage_tally()

        assert tally.lost(replayed_ok=5) == 2

    def test_replayed_orders_are_the_parked_ones_charged_again(self):
        tally = _outage_tally()
        charged = [101, 102, 103, 111, 112, 104, 105, 106, 107, 108, 109, 110]

        assert tally.replayed(charged) == list(range(104, 111))

    def test_empty_tally_has_no_span_and_nothing_lost(self):
        tally = _Tally()

        assert tally.parked == 0
        assert tally.span() == "none"
        assert tally.lost(replayed_ok=0) == 0
        assert tally.replayed([101, 102]) == []


def _passes(count: int, size: int = 100) -> list[dict]:
    """Batch events of ``count`` full replay passes, every entry re-executed."""
    return [{"total": size, "success_count": size} for _ in range(count)]


class TestReplayDrain:
    def test_first_passes_of_a_backlog_are_not_the_whole_drain(self):
        assert _replay_drained(_passes(4), expected=500) is False

    def test_drain_completes_once_the_passes_cover_every_parked_charge(self):
        assert _replay_drained(_passes(5), expected=500) is True

    def test_no_batch_yet_is_not_a_drain(self):
        assert _replay_drained([], expected=0) is False

    def test_a_failed_re_execution_still_counts_as_attempted(self):
        batches = [{"total": 7, "success_count": 6}]

        assert _replay_drained(batches, expected=7) is True
        assert _batch_totals(batches) == (6, 7)

    def test_totals_sum_every_pass(self):
        batches = _passes(4) + [{"total": 7, "success_count": 7}]

        assert _batch_totals(batches) == (407, 407)


class TestOutageSize:
    def test_default_is_the_seven_charge_story(self):
        assert _parse_args([]).outage_charges == 7

    def test_accepts_a_backlog_sized_outage(self):
        assert _parse_args(["--outage-charges", "500"]).outage_charges == 500

    def test_accepts_the_ceiling(self):
        assert _parse_args(["--outage-charges", "5000"]).outage_charges == 5000

    @pytest.mark.parametrize("value", ["0", "-3", "5001", "many"])
    def test_rejects_sizes_outside_one_to_five_thousand(self, value):
        with pytest.raises(SystemExit):
            _parse_args(["--outage-charges", value])
