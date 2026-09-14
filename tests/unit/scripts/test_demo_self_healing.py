"""Unit tests for the shipped self-healing demo's bookkeeping.

The demo's closing line ("lost N", and the exit code behind it) is computed
from what the demo itself counted, so the counting has to agree with what the
framework captures. Every charge that failed on the way out is parked — the
ones that exhausted their retries AND the ones the OPEN breaker rejected before
they ran — and every parked charge is replayed on recovery. A tally that
measured "lost" against the retry-exhausted charges alone ended a healthy run
at ``lost -2`` with exit code 2, which is the regression pinned here.
"""

from __future__ import annotations

from baldur.scripts.demo_self_healing import _Tally


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
