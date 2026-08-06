"""Unit tests for services/circuit_breaker/time_outcome_window.py (746).

The producer behind the comprehensive-metrics payload's five-minute per-service
failure rate. What the payload renders is a projection of this module, so every
way it can lie is a way this module can be wrong:

- an outcome retained too briefly makes a service that failed 291 s ago read
  "not measured" inside its own advertised window;
- a wall-clock step backwards makes stale buckets satisfy the in-window
  predicate, reporting old failures as the last five minutes;
- a key admitted without bound turns a bounded payload into one that grows with
  however many distinct names an application protects;
- an eviction that takes a failing service's slot costs the incident case its
  evidence at the moment an operator polls;
- two names differing only in label-unsafe characters merging into one row whose
  rate matches neither, silently.

Verification techniques applied:
- Contract: window/bucket/cap constants, ``__all__``, the default clock
- Boundary analysis: bucket accumulation and rotation, retention at 291/299/
  300/301/309/310/311 s, the cap at/below/above, headroom arithmetic
- State transition: unstamped -> stamped -> reset ring buckets, the cap epoch
  flag, refused -> admitted after a read
- Side effects: the cap-reached WARNING, the collision WARNING, the batched
  eviction DEBUG (asserted as one event per read, never one per key)
- Negative assertion: a key holding an in-window failure is never evicted; the
  producer never reaches for the registry's lossiness machinery
- Idempotency: two consecutive reads, one spelling recorded N times
- Singleton/lifecycle: ``get_call_outcome_window`` / ``reset_call_outcome_window``
- Concurrency: recorder threads racing readers and a reset (every thread joined)
- Property-based: rates in range, the cap bound, backward clock jumps, and that
  no input raises
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

from baldur.services.circuit_breaker import time_outcome_window
from baldur.services.circuit_breaker.time_outcome_window import (
    _BUCKET_COUNT,
    _BUCKET_SECONDS,
    _HEADROOM_DIVISOR,
    _MAX_OUTCOME_KEYS,
    _MAX_SPELLING_MEMO,
    FAILURE_RATE_WINDOW_SECONDS,
    TimeBucketedOutcomeWindow,
    get_call_outcome_window,
    record_call_outcome,
    reset_call_outcome_window,
    resolve_outcome_key,
)

CAP_REACHED_EVENT = "circuit_breaker.outcome_window_cap_reached"
COLLISION_EVENT = "circuit_breaker.outcome_key_collision"
EVICTED_EVENT = "circuit_breaker.outcome_window_keys_evicted"


class _FakeClock:
    """A monotonic-shaped seconds source the test moves explicitly.

    The window consumes only differences, so a plain float counter is a
    faithful stand-in — and it keeps every time assertion below deterministic
    (no wall-clock comparison, per the suite's timing rules).
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _window(cap: int | None = None, clock: _FakeClock | None = None):
    """Build a window on a fake clock, optionally with an injected cap."""
    clock = clock or _FakeClock()
    provider = (lambda: cap) if cap is not None else None
    return TimeBucketedOutcomeWindow(clock=clock, cap_provider=provider), clock


def _adopt_cap(window: TimeBucketedOutcomeWindow) -> None:
    """Make the injected cap the one in force before any key is admitted.

    The cap lives on the read path by design, so a freshly-built window carries
    the module fallback until the first ``snapshot()``.
    """
    window.snapshot()


def _fill_to_cap(window, cap: int, clock: _FakeClock, *, failure: bool) -> list[str]:
    """Admit exactly ``cap`` keys, one per bucket so their ages differ."""
    keys = []
    for index in range(cap):
        key = f"svc_{index}"
        window.record(key, failure=failure)
        keys.append(key)
        clock.advance(_BUCKET_SECONDS)
    return keys


@pytest.fixture(autouse=True)
def _isolated_window_singleton():
    """Drop the process-wide window around every case.

    It is fed by every protected call in the process, so a neighbouring test's
    traffic would otherwise show up as rows and spelling memo entries here.
    """
    reset_call_outcome_window()
    yield
    reset_call_outcome_window()


# =============================================================================
# Contract — the constants and surface other modules depend on literally
# =============================================================================


class TestTimeOutcomeWindowContract:
    """Design values that are the contract, not a tunable.

    The public field names promise five minutes, so the window length is fixed
    by the wire contract rather than by settings — which is exactly why these
    are asserted literally here instead of read back from the module.
    """

    def test_window_length_is_five_minutes(self):
        """300 s — the span the ``failure_rate_5m`` field name promises."""
        assert FAILURE_RATE_WINDOW_SECONDS == 300

    def test_bucket_length_is_ten_seconds(self):
        """10 s buckets — the granularity the covered span is quantized to."""
        assert _BUCKET_SECONDS == 10

    def test_bucket_count_is_thirty_one(self):
        """31 buckets, not 30: the extra in-flight bucket is the over-cover."""
        assert _BUCKET_COUNT == 31

    def test_bucket_ring_covers_more_than_the_window(self):
        """The ring must over-cover, never under-cover.

        A ring spanning exactly the window evicts a mid-bucket outcome after
        290 s, so a service measured 291 s ago reads "not measured" inside its
        own advertised window. Under-reporting shows an operator a healthier
        service than reality, so the invariant is stated in the safe direction.
        """
        assert _BUCKET_COUNT * _BUCKET_SECONDS >= (
            FAILURE_RATE_WINDOW_SECONDS + _BUCKET_SECONDS
        )

    def test_fallback_cap_matches_the_metric_registry_default(self):
        """50 — so the two cardinality surfaces agree before either reads settings."""
        assert _MAX_OUTCOME_KEYS == 50

    def test_headroom_divisor_is_ten(self):
        """A tenth of the cap is left free at every read."""
        assert _HEADROOM_DIVISOR == 10

    def test_spelling_memo_saturates_at_two_hundred_fifty_six(self):
        """Bounded rather than unbounded by choice, mirroring the registry memo."""
        assert _MAX_SPELLING_MEMO == 256

    def test_default_clock_is_monotonic(self):
        """The wall clock is unsafe here, so the default must be monotonic.

        An NTP step backwards makes the current bucket index smaller than the
        stored ones, so buckets holding old outcomes satisfy the in-window
        predicate and get reported as the last five minutes — fabricated
        freshness, the defect class this producer exists to remove. Reading the
        bound attribute is the cheapest witness that the choice is still wired.
        """
        assert TimeBucketedOutcomeWindow()._clock is time.monotonic

    def test_module_exports_the_public_surface(self):
        """``__all__`` declares the window, its singleton pair and both feeds."""
        assert set(time_outcome_window.__all__) == {
            "FAILURE_RATE_WINDOW_SECONDS",
            "TimeBucketedOutcomeWindow",
            "get_call_outcome_window",
            "record_call_outcome",
            "reset_call_outcome_window",
            "resolve_outcome_key",
        }


# =============================================================================
# Behavior — recording into the bucket ring
# =============================================================================


class TestTimeBucketedOutcomeWindowRecordBehavior:
    """One admission at a time, into the bucket the clock currently names."""

    def test_unrecorded_key_has_no_entry_at_all(self):
        """An unseen service yields no row — absence, not a 0.0 rate.

        The payload turns a missing entry into ``null``; a zero-valued entry
        would render as "measured and healthy" during an incident.
        """
        window, _clock = _window()

        assert window.snapshot() == {}

    def test_single_success_counts_toward_the_denominator_only(self):
        """A success enlarges the total and leaves the numerator at zero."""
        window, _clock = _window()

        window.record("payment", failure=False)

        assert window.snapshot() == {"payment": (0, 1)}

    def test_single_failure_counts_toward_both_members(self):
        """A failure is both a failure and an admission."""
        window, _clock = _window()

        window.record("payment", failure=True)

        assert window.snapshot() == {"payment": (1, 1)}

    def test_outcomes_within_one_bucket_accumulate(self):
        """Several admissions inside the same 10 s bucket sum in place."""
        # Given: a clock that stays inside one bucket
        window, clock = _window()

        # When: three admissions land, one of them failing
        window.record("payment", failure=True)
        clock.advance(_BUCKET_SECONDS - 1)
        window.record("payment", failure=False)
        window.record("payment", failure=False)

        # Then: one bucket carries the whole ratio
        assert window.snapshot() == {"payment": (1, 3)}

    def test_outcomes_across_buckets_are_summed_at_read_time(self):
        """Rotation adds buckets rather than replacing the previous one."""
        window, clock = _window()

        window.record("payment", failure=True)
        clock.advance(_BUCKET_SECONDS)
        window.record("payment", failure=False)
        clock.advance(_BUCKET_SECONDS)
        window.record("payment", failure=False)

        assert window.snapshot() == {"payment": (1, 3)}

    def test_reused_ring_slot_is_reset_rather_than_accumulated(self):
        """A ring index revisited a full lap later starts from zero.

        Given/When/Then: without the epoch stamp check, the second outcome would
        land on top of the first and report two admissions where the window
        holds one — a stale failure re-reported as current traffic.
        """
        # Given: one failure, and a clock advanced exactly one full lap
        window, clock = _window()
        window.record("payment", failure=True)
        clock.advance(_BUCKET_COUNT * _BUCKET_SECONDS)

        # When: a success lands on the same ring index
        window.record("payment", failure=False)

        # Then: only the new outcome is visible
        assert window.snapshot() == {"payment": (0, 1)}

    def test_bucket_boundary_start_accumulates_within_its_own_bucket(self):
        """An outcome recorded exactly on a bucket edge shares that bucket."""
        window, clock = _window(clock=_FakeClock(start=float(_BUCKET_SECONDS)))

        window.record("payment", failure=True)
        clock.advance(_BUCKET_SECONDS - 0.001)
        window.record("payment", failure=True)

        assert window.snapshot() == {"payment": (2, 2)}

    def test_each_key_keeps_its_own_ring(self):
        """One service's failures never enter another's denominator."""
        window, _clock = _window()

        window.record("failing", failure=True)
        window.record("failing", failure=True)
        window.record("healthy", failure=False)

        assert window.snapshot() == {"failing": (2, 2), "healthy": (0, 1)}


# =============================================================================
# Behavior — retention across the window edge
# =============================================================================


class TestOutcomeWindowRetentionBehavior:
    """What the window still counts, and what it has let go.

    The 291 s case is the one that refuted a 30-bucket ring: a service measured
    inside its own advertised five minutes must not read "not measured". The
    309/310 pair is the other edge — the design asserts the covered span is at
    least 300 s and under 310 s, and nothing else pins the upper end.
    """

    @pytest.mark.parametrize(
        ("elapsed", "expected_total"),
        [
            (0, 1),
            (291, 1),
            (299, 1),
            (300, 1),
            (301, 1),
            (309, 1),
            (310, 0),
            (311, 0),
        ],
        ids=[
            "same_instant",
            "inside_window_291s",
            "just_under_window_299s",
            "at_window_300s",
            "just_over_window_301s",
            "upper_cover_edge_309s",
            "past_upper_cover_310s",
            "well_past_311s",
        ],
    )
    def test_outcome_is_counted_until_the_ring_has_fully_lapped(
        self, elapsed, expected_total
    ):
        """Retention holds through 309 s and is gone by 310 s."""
        window, clock = _window()
        window.record("payment", failure=True)

        clock.advance(elapsed)

        observed = window.snapshot()
        assert observed.get("payment", (0, 0))[1] == expected_total

    def test_retention_edges_hold_from_a_bucket_aligned_start(self):
        """The same edges apply when the first outcome lands on a bucket start.

        The index is an integer floor-division, so alignment shifts the edge by
        at most the sub-bucket offset — asserted here rather than assumed.
        """
        window, clock = _window(clock=_FakeClock(start=float(2 * _BUCKET_SECONDS)))
        window.record("payment", failure=True)

        clock.advance(309)
        assert window.snapshot()["payment"] == (1, 1)

        clock.advance(1)
        assert window.snapshot() == {}

    def test_key_whose_every_bucket_aged_out_is_dropped_from_the_read(self):
        """Expiry reclaim removes the key, so its cap slot becomes reusable."""
        window, clock = _window()
        window.record("payment", failure=True)

        clock.advance(_BUCKET_COUNT * _BUCKET_SECONDS)

        assert window.snapshot() == {}

    def test_read_all_also_forgets_out_of_window_outcomes(self):
        """The cross-key total is windowed on the same predicate as the rows."""
        window, clock = _window()
        window.record("payment", failure=True)
        assert window.read_all() == (1, 1)

        clock.advance(_BUCKET_COUNT * _BUCKET_SECONDS)

        assert window.read_all() == (0, 0)

    def test_partially_aged_key_reports_only_its_in_window_buckets(self):
        """An old failure ages out of a key that a later success keeps alive.

        Given/When/Then: both buckets are live at the earlier read, and only the
        newer one survives the later read — so a key does not have to expire
        wholesale for its stale evidence to stop counting.
        """
        # Given: a failure, then a success most of a window later
        window, clock = _window()
        window.record("payment", failure=True)
        clock.advance((_BUCKET_COUNT - 1) * _BUCKET_SECONDS)
        window.record("payment", failure=False)
        assert window.snapshot() == {"payment": (1, 2)}

        # When: the clock crosses the older bucket's edge
        clock.advance(_BUCKET_SECONDS)

        # Then: only the success is still in window
        assert window.snapshot() == {"payment": (0, 1)}


# =============================================================================
# Behavior — aggregate reads and reset
# =============================================================================


class TestOutcomeWindowAggregateBehavior:
    """``read_all()`` and ``reset_all()``."""

    def test_empty_window_aggregates_to_no_evidence(self):
        """(0, 0) is the boot-time honest-absence floor the payload renders null."""
        window, _clock = _window()

        assert window.read_all() == (0, 0)

    def test_read_all_sums_every_key(self):
        """The aggregate is the cross-key total, not a per-key maximum."""
        window, _clock = _window()
        window.record("a", failure=True)
        window.record("b", failure=True)
        window.record("b", failure=False)

        assert window.read_all() == (2, 3)

    def test_reset_all_drops_every_key(self):
        """A reset returns the window to its construction state."""
        window, _clock = _window()
        window.record("payment", failure=True)

        window.reset_all()

        assert window.snapshot() == {}
        assert window.read_all() == (0, 0)

    def test_reset_all_clears_the_spelling_memo(self):
        """A merge already warned about is warnable again after a reset.

        The memo is what makes the collision warning fire once; if a reset left
        it behind, a fresh process-equivalent state would stay silent.
        """
        window, _clock = _window()
        window.note_key_spelling("Payment_API", "payment_api")
        with capture_logs() as first_logs:
            window.note_key_spelling("payment-api", "payment_api")

        window.reset_all()
        window.note_key_spelling("Payment_API", "payment_api")
        with capture_logs() as second_logs:
            window.note_key_spelling("payment-api", "payment_api")

        assert [e["event"] for e in first_logs] == [COLLISION_EVENT]
        assert [e["event"] for e in second_logs] == [COLLISION_EVENT]

    def test_reset_all_on_an_empty_window_does_not_raise(self):
        """The reset chain runs on every settings reset, populated or not."""
        window, _clock = _window()

        window.reset_all()

        assert window.snapshot() == {}


# =============================================================================
# Behavior — the cardinality cap (D14)
# =============================================================================


class TestOutcomeWindowCardinalityBehavior:
    """The key set is bounded, and every cap decision rides the read path.

    The row source this producer replaces was capped and the replacement was
    not: a per-tenant protected name would otherwise grow the response, the
    resident footprint and the read's lock-hold without bound. Refusal is
    invisibility rather than a null — a refused key is in neither row source —
    so it has to be rare and self-correcting, which is what most of these cases
    pin.
    """

    def test_keys_below_the_cap_are_all_admitted(self):
        """Nothing is refused while the window has room."""
        window, clock = _window(cap=4)
        _adopt_cap(window)

        _fill_to_cap(window, 3, clock, failure=True)

        assert len(window.snapshot()) == 3

    def test_key_beyond_the_cap_is_refused_and_has_no_entry(self):
        """The refused name is absent, never folded into another row.

        An overflow row would attribute one service's failures to a label
        naming a different set of services — the fabricated-attribution class
        this producer exists to remove.
        """
        # Given: the cap filled with failing keys, which are never evictable
        window, clock = _window(cap=3)
        _adopt_cap(window)
        _fill_to_cap(window, 3, clock, failure=True)

        # When: a further distinct name records an outcome
        window.record("late_arrival", failure=True)

        # Then: it has no entry, and no existing row absorbed its count
        observed = window.snapshot()
        assert "late_arrival" not in observed
        assert sum(total for _f, total in observed.values()) == 3

    def test_refusal_emits_one_warning_per_cap_epoch(self):
        """One line per time the window fills — not one per refused call."""
        window, clock = _window(cap=2)
        _adopt_cap(window)
        _fill_to_cap(window, 2, clock, failure=True)

        with capture_logs() as logs:
            for _ in range(5):
                window.record("late_arrival", failure=True)

        refusals = [e for e in logs if e["event"] == CAP_REACHED_EVENT]
        assert len(refusals) == 1
        assert refusals[0]["log_level"] == "warning"
        assert refusals[0]["refused_key"] == "late_arrival"
        assert refusals[0]["max_keys"] == 2

    def test_admitting_a_key_reopens_the_warning_for_the_next_fill(self):
        """The epoch flag clears on a successful admission.

        Otherwise an operator gets exactly one warning for the lifetime of the
        process, however many times the window fills afterwards.
        """
        # Given: a window that filled, refused, then had a slot freed
        window, clock = _window(cap=2)
        _adopt_cap(window)
        _fill_to_cap(window, 2, clock, failure=True)
        window.record("first_refused", failure=True)
        clock.advance(_BUCKET_COUNT * _BUCKET_SECONDS)
        window.snapshot()
        window.record("readmitted", failure=True)

        # When: the window fills and refuses again
        window.record("other", failure=True)
        with capture_logs() as logs:
            window.record("second_refused", failure=True)

        # Then: the second fill produces its own warning
        refusals = [e for e in logs if e["event"] == CAP_REACHED_EVENT]
        assert len(refusals) == 1
        assert refusals[0]["refused_key"] == "second_refused"

    def test_expired_key_frees_its_slot_for_a_new_name(self):
        """Slot reclaim: a key with no in-window bucket stops holding its slot."""
        window, clock = _window(cap=2)
        _adopt_cap(window)
        _fill_to_cap(window, 2, clock, failure=True)

        clock.advance(_BUCKET_COUNT * _BUCKET_SECONDS)
        window.snapshot()
        window.record("late_arrival", failure=True)

        assert "late_arrival" in window.snapshot()

    def test_late_failing_service_is_measured_after_one_read(self):
        """Headroom makes refusal self-correcting for a newly-failing service.

        Given/When/Then: with the cap held by healthy keys, a read evicts one of
        them, so the name refused a moment ago is admitted on its next call and
        reported by the following read. Without read-time headroom the arrival
        stays refused forever, made unmeasurable by the operator's own poll.
        """
        # Given: the cap filled by success-only keys, and one refused failure
        window, clock = _window(cap=3)
        _adopt_cap(window)
        _fill_to_cap(window, 3, clock, failure=False)
        window.record("late_arrival", failure=True)
        assert "late_arrival" not in window.snapshot()

        # When: the service fails again after that read
        window.record("late_arrival", failure=True)

        # Then: it is measured, at its real rate
        observed = window.snapshot()
        assert observed["late_arrival"] == (1, 1)

    def test_cap_held_entirely_by_failing_keys_still_refuses(self):
        """Deliberate: every measured service is already failing.

        None of them is a worse answer to the operator's question than the
        arrival, and evicting one would cost the incident case its evidence.
        """
        window, clock = _window(cap=3)
        _adopt_cap(window)
        _fill_to_cap(window, 3, clock, failure=True)

        window.record("late_arrival", failure=True)
        window.snapshot()
        window.record("late_arrival", failure=True)

        assert "late_arrival" not in window.snapshot()

    def test_raising_the_operator_cap_admits_new_names_from_the_next_read(self):
        """The knob works on a running process, without a restart or a reset.

        The metric registry honors a raised cap within its own cache TTL; a cap
        frozen at window construction would refuse at the old value until the
        process restarted, so the two cardinality surfaces would disagree.
        """
        # Given: a window at a low cap, refusing a new name
        cap = {"value": 2}
        clock = _FakeClock()
        window = TimeBucketedOutcomeWindow(
            clock=clock, cap_provider=lambda: cap["value"]
        )
        _adopt_cap(window)
        _fill_to_cap(window, 2, clock, failure=True)
        window.record("late_arrival", failure=True)
        assert "late_arrival" not in window.snapshot()

        # When: the operator raises the cap and the endpoint is polled
        cap["value"] = 10
        window.snapshot()
        window.record("late_arrival", failure=True)

        # Then: the name is admitted
        assert "late_arrival" in window.snapshot()

    def test_two_consecutive_reads_report_the_same_evidence(self):
        """A read is idempotent when the clock has not moved.

        It mutates — expiry reclaim, cap re-resolve, headroom eviction — so
        "reading twice changed the answer" is a real failure mode.
        """
        window, clock = _window(cap=10)
        _adopt_cap(window)
        _fill_to_cap(window, 4, clock, failure=True)

        first = window.snapshot()
        second = window.snapshot()

        assert first == second

    def test_record_path_uses_the_cap_in_force_at_the_last_read(self):
        """No settings read ever reaches a protected call.

        The cap is resolved on the read path only, so a window that has never
        been read enforces the module fallback rather than the injected value.
        This is the deliberate consequence of keeping the recording path free of
        imports and settings merges — pinned so it is not "fixed" by moving the
        resolve into ``record()``.
        """
        window, clock = _window(cap=1)

        for index in range(3):
            window.record(f"svc_{index}", failure=True)
            clock.advance(_BUCKET_SECONDS)

        assert len(window._keys) == 3

    def test_failing_cap_provider_falls_back_to_the_module_constant(self):
        """A broken settings field cannot uncap the window."""
        clock = _FakeClock()
        provider_calls = {"count": 0}

        def _raising_provider() -> int:
            provider_calls["count"] += 1
            raise RuntimeError("settings unavailable")

        window = TimeBucketedOutcomeWindow(clock=clock, cap_provider=_raising_provider)

        window.snapshot()

        assert provider_calls["count"] == 1
        assert window._cap == _MAX_OUTCOME_KEYS

    @pytest.mark.parametrize("value", [0, -1], ids=["zero", "negative"])
    def test_non_positive_cap_falls_back_to_the_module_constant(self, value):
        """A cap of zero would refuse every key and measure nothing at all."""
        window, _clock = _window(cap=value)

        window.snapshot()

        assert window._cap == _MAX_OUTCOME_KEYS


# =============================================================================
# Behavior — read-time eviction (D14)
# =============================================================================


class TestOutcomeWindowEvictionBehavior:
    """Which key loses its slot when a read needs headroom.

    Plain recency would evict a low-traffic failing service in favour of chatty
    healthy ones — optimizing against the incident case. The rule is recency
    restricted to keys holding no in-window failure.
    """

    def test_read_evicts_the_least_recently_active_failure_free_key(self):
        """Oldest-first among the evictable, so the freshest evidence survives."""
        # Given: four healthy keys, each last active one bucket apart
        window, clock = _window(cap=4)
        _adopt_cap(window)
        keys = _fill_to_cap(window, 4, clock, failure=False)

        # When: the read finds no headroom
        observed = window.snapshot()

        # Then: exactly the oldest key lost its slot
        assert keys[0] not in observed
        assert set(observed) == set(keys[1:])

    def test_key_holding_an_in_window_failure_is_never_evicted(self):
        """Negative assertion: an operator's poll cannot cost the incident case.

        The failing key here is the least recently active of all four, so plain
        recency would evict exactly it.
        """
        # Given: the oldest key is the failing one
        window, clock = _window(cap=4)
        _adopt_cap(window)
        window.record("failing_but_stale", failure=True)
        clock.advance(_BUCKET_SECONDS)
        healthy = _fill_to_cap(window, 3, clock, failure=False)

        # When: the read needs headroom
        observed = window.snapshot()

        # Then: the failure survives and a healthy key paid instead
        assert observed["failing_but_stale"] == (1, 1)
        assert healthy[0] not in observed

    def test_eviction_emits_one_batched_debug_per_read(self):
        """One event carrying the key list — never one event per key.

        The window lock is taken by every protected call, and a structlog
        emission here runs a full processor chain plus a process-global
        rate-limit lock, so per-key logging inside the read would be paid by
        the whole process.
        """
        # Given: a cap whose headroom is two slots, entirely filled
        cap = _HEADROOM_DIVISOR * 2
        window, clock = _window(cap=cap)
        _adopt_cap(window)
        _fill_to_cap(window, cap, clock, failure=False)

        # When: the read evicts to restore headroom
        with capture_logs() as logs:
            observed = window.snapshot()

        # Then: exactly one event names both evicted keys
        events = [e for e in logs if e["event"] == EVICTED_EVENT]
        assert len(events) == 1
        assert events[0]["log_level"] == "debug"
        assert events[0]["evicted_count"] == 2
        assert len(events[0]["evicted_keys"]) == 2
        assert not set(events[0]["evicted_keys"]) & set(observed)

    def test_read_with_headroom_available_evicts_nothing(self):
        """No pressure, no eviction, and no event."""
        window, clock = _window(cap=10)
        _adopt_cap(window)
        keys = _fill_to_cap(window, 4, clock, failure=False)

        with capture_logs() as logs:
            observed = window.snapshot()

        assert set(observed) == set(keys)
        assert [e for e in logs if e["event"] == EVICTED_EVENT] == []

    def test_expired_keys_are_reclaimed_before_eviction_is_considered(self):
        """Reclaim comes first, so a live key is not evicted to make room.

        Given/When/Then: three of four keys have aged out entirely, which frees
        enough slots that the survivor keeps its own.
        """
        # Given: three keys aged past the window, one recorded just now
        window, clock = _window(cap=4)
        _adopt_cap(window)
        _fill_to_cap(window, 3, clock, failure=False)
        clock.advance(_BUCKET_COUNT * _BUCKET_SECONDS)
        window.record("still_live", failure=False)

        # When: the read runs its housekeeping
        with capture_logs() as logs:
            observed = window.snapshot()

        # Then: the survivor is intact and nothing was evicted
        assert observed == {"still_live": (0, 1)}
        assert [e for e in logs if e["event"] == EVICTED_EVENT] == []

    def test_evicted_key_is_readmitted_on_its_next_call(self):
        """Losing a slot is not a lockout — a still-live service comes back."""
        window, clock = _window(cap=4)
        _adopt_cap(window)
        keys = _fill_to_cap(window, 4, clock, failure=False)
        evicted = keys[0]
        assert evicted not in window.snapshot()

        window.record(evicted, failure=False)

        assert evicted in window.snapshot()


# =============================================================================
# Behavior — spelling collisions (D4)
# =============================================================================


class TestOutcomeKeyCollisionBehavior:
    """The producer detects its own merges, exactly.

    Two names an operator considers distinct can project onto one key and
    therefore onto one row whose rate matches neither. The warning fires if and
    only if two names actually merge — never on a lone spelling that merely
    could — which is what the metric registry's validated-versus-canonical
    predicate cannot express.
    """

    def test_second_spelling_of_one_key_warns_naming_both(self):
        """The operator gets the key and both spellings that merged into it."""
        window, _clock = _window()
        window.note_key_spelling("Payment_API", "payment_api")

        with capture_logs() as logs:
            window.note_key_spelling("payment-api", "payment_api")

        events = [e for e in logs if e["event"] == COLLISION_EVENT]
        assert len(events) == 1
        assert events[0]["log_level"] == "warning"
        assert events[0]["outcome_key"] == "payment_api"
        assert events[0]["first_spelling"] == "Payment_API"
        assert events[0]["merged_spelling"] == "payment-api"

    def test_one_spelling_recorded_repeatedly_never_warns(self):
        """Idempotent: a policy rebuilt under the same name is not a merge."""
        window, _clock = _window()

        with capture_logs() as logs:
            for _ in range(5):
                window.note_key_spelling("payment_api", "payment_api")

        assert [e for e in logs if e["event"] == COLLISION_EVENT] == []

    def test_further_spellings_of_an_already_warned_key_stay_quiet(self):
        """One line per merged key, so a third name does not re-page."""
        window, _clock = _window()
        window.note_key_spelling("Payment_API", "payment_api")
        window.note_key_spelling("payment-api", "payment_api")

        with capture_logs() as logs:
            window.note_key_spelling("payment.api", "payment_api")

        assert [e for e in logs if e["event"] == COLLISION_EVENT] == []

    def test_distinct_keys_do_not_warn_about_each_other(self):
        """Two services with their own keys are not a collision."""
        window, _clock = _window()

        with capture_logs() as logs:
            window.note_key_spelling("payment", "payment")
            window.note_key_spelling("orders", "orders")

        assert [e for e in logs if e["event"] == COLLISION_EVENT] == []

    def test_non_string_raw_name_is_recorded_as_its_text_form(self):
        """The feed is total on any input, so a non-``str`` name cannot raise."""
        window, _clock = _window()
        window.note_key_spelling(None, "unknown")

        with capture_logs() as logs:
            window.note_key_spelling("unknown", "unknown")

        events = [e for e in logs if e["event"] == COLLISION_EVENT]
        assert len(events) == 1
        assert events[0]["first_spelling"] == "None"

    def test_spelling_memo_stops_recording_past_its_bound(self):
        """Beyond the memo bound a later merge goes unwarned — bounded residue.

        The memo is saturating rather than evicting on purpose: an LRU over a
        rotation of names would re-warn about the same merge forever. The cost
        is disclosed, and this is what pins it as a boundary rather than a bug.
        """
        window, _clock = _window()
        for index in range(_MAX_SPELLING_MEMO):
            window.note_key_spelling(f"svc_{index}", f"svc_{index}")

        window.note_key_spelling("Overflow_Name", "overflow_name")
        with capture_logs() as logs:
            window.note_key_spelling("overflow-name", "overflow_name")

        assert [e for e in logs if e["event"] == COLLISION_EVENT] == []

    def test_memo_bound_leaves_the_last_admitted_key_warnable(self):
        """The boundary is the memo's size, asserted from the inside edge."""
        window, _clock = _window()
        for index in range(_MAX_SPELLING_MEMO - 1):
            window.note_key_spelling(f"svc_{index}", f"svc_{index}")

        window.note_key_spelling("Last_Slot", "last_slot")
        with capture_logs() as logs:
            window.note_key_spelling("last-slot", "last_slot")

        assert len([e for e in logs if e["event"] == COLLISION_EVENT]) == 1


# =============================================================================
# Behavior — the two feed functions (D1 hotness split, D9 fail-open)
# =============================================================================


class TestResolveOutcomeKeyBehavior:
    """The once-per-policy projection from a breaker name onto a window key.

    The key must be the metric registry's own admission form, because that is
    the only vocabulary in which the per-service row join is exact. Base-parsing
    a composite name is deliberately absent: it would split one logical service
    across two rows and merge distinct cells into one.
    """

    @pytest.mark.parametrize(
        ("service_name", "expected"),
        [
            ("payment", "payment"),
            ("Payment_API", "payment_api"),
            ("payment-api", "payment_api"),
            ("orders.charge", "orders_charge"),
            ("payment_api::cell-3", "payment_api__cell_3"),
            ("  spaced name  ", "spaced_name"),
            ("", "unknown"),
            (None, "unknown"),
        ],
        ids=[
            "plain",
            "uppercase",
            "hyphen",
            "dotted",
            "composite_cell_scoped",
            "surrounding_space",
            "empty",
            "non_string",
        ],
    )
    def test_key_is_the_canonical_label_form_of_the_name(self, service_name, expected):
        """The projection rules themselves, including the composite case.

        ``payment_api::cell-3`` keeping its cell suffix is the point: a
        base-parsed key would put two cells of one service on one row and lose
        the ability to tell them apart.
        """
        assert resolve_outcome_key(service_name) == expected

    def test_resolved_name_is_offered_to_the_collision_detector(self):
        """The raw spelling reaches the window, which is what detects merges."""
        resolve_outcome_key("Payment_API")

        with capture_logs() as logs:
            resolve_outcome_key("payment-api")

        events = [e for e in logs if e["event"] == COLLISION_EVENT]
        assert len(events) == 1
        assert events[0]["outcome_key"] == "payment_api"

    def test_empty_projection_result_yields_no_key(self):
        """An empty key would collide with nothing meaningful, so it is dropped."""
        with patch(
            "baldur.metrics.registry.canonicalize_domain_label", return_value=""
        ):
            assert resolve_outcome_key("payment") is None

    def test_projection_failure_yields_no_key_rather_than_raising(self):
        """Fail-open: absence renders null, never a wrong number — and never a
        broken policy construction."""
        with patch(
            "baldur.metrics.registry.canonicalize_domain_label",
            side_effect=RuntimeError("registry unavailable"),
        ) as broken:
            assert resolve_outcome_key("payment") is None

        assert broken.called


class TestRecordCallOutcomeBehavior:
    """The per-call entry point — the only function on the hot path."""

    def test_recorded_outcome_reaches_the_process_window(self):
        """The feed writes to the singleton the payload reads."""
        record_call_outcome("payment", failure=True)
        record_call_outcome("payment", failure=False)

        assert get_call_outcome_window().snapshot() == {"payment": (1, 2)}

    def test_absent_key_records_nothing(self):
        """A policy whose projection failed contributes no evidence at all.

        Negative assertion: recording under a placeholder key would attribute
        one service's outcomes to a row naming something else.
        """
        record_call_outcome(None, failure=True)

        assert get_call_outcome_window().snapshot() == {}

    def test_recorder_failure_never_reaches_the_business_call(self):
        """Fail-open by contract, and the fault is proven to have fired.

        Recording is a side effect: an exception here must not fail — or replace
        the exception of — the call the breaker is protecting.
        """

        class _RaisingWindowGetter:
            def __init__(self) -> None:
                self.touched = False

            def __call__(self):
                self.touched = True
                raise RuntimeError("window unavailable")

        getter = _RaisingWindowGetter()
        with patch.object(time_outcome_window, "get_call_outcome_window", getter):
            record_call_outcome("payment", failure=True)

        assert getter.touched


class TestCallOutcomeWindowSingleton:
    """``get_call_outcome_window()`` / ``reset_call_outcome_window()``."""

    def test_get_returns_the_same_instance(self):
        """One window per process — the payload and the feed must share it."""
        assert get_call_outcome_window() is get_call_outcome_window()

    def test_reset_replaces_the_instance(self):
        """A reset drops the evidence by dropping the holder."""
        first = get_call_outcome_window()

        reset_call_outcome_window()

        assert get_call_outcome_window() is not first

    def test_concurrent_first_use_creates_exactly_one_window(self):
        """The double-checked construction must not hand out two windows.

        Two windows would split one service's outcomes across two rings, so the
        payload would report a fraction of the traffic it actually saw.
        """
        results: list[TimeBucketedOutcomeWindow] = []
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait(timeout=5.0)
            results.append(get_call_outcome_window())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(results) == 8
        assert all(window is results[0] for window in results)


# =============================================================================
# Negative — the producer owns its warning and touches no shared memo (D4 run 5)
# =============================================================================


class TestProducerDoesNotConsumeRegistryLossinessMachinery:
    """The registry's projection memo must stay reachable for its own callers.

    Routing this producer through it was refuted twice: the predicate covers
    neither merge this design can cause, and keying its saturating 256-slot memo
    on caller-controlled raw names would permanently silence a warning that fires
    today. The decoupling is only real if nothing here evaluates it.
    """

    def test_resolving_many_names_leaves_the_registry_memo_untouched(self):
        """Protected-name traffic cannot saturate the registry's memo."""
        from baldur.metrics import registry

        before = len(registry._lossy_projection_seen)

        for index in range(50):
            resolve_outcome_key(f"tenant_{index}_api")

        assert len(registry._lossy_projection_seen) == before


# =============================================================================
# Concurrency — recorders racing readers and a reset
# =============================================================================


class TestOutcomeWindowConcurrency:
    """Bounded contention against the single lock.

    Deadlock freedom is structural — one lock, and no public method calls
    another on the same instance — so this test does not claim to prove it. It
    claims the deterministic invariants survive interleaving, at a thread count
    well below the scale that makes this suite's daemon-thread and worker-crash
    modes a flake source. Every thread is joined before asserting.
    """

    def test_recorders_racing_readers_and_a_reset_hold_the_invariants(self):
        # Given: four recorders, two readers and one reset, on a real clock
        window = TimeBucketedOutcomeWindow(cap_provider=lambda: 32)
        errors: list[BaseException] = []
        observations: list[dict[str, tuple[int, int]]] = []
        stop = threading.Event()

        def recorder(index: int) -> None:
            try:
                for step in range(200):
                    window.record(f"svc_{index}", failure=step % 3 == 0)
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        def reader() -> None:
            try:
                while not stop.is_set():
                    observations.append(window.snapshot())
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        def resetter() -> None:
            try:
                for _ in range(20):
                    window.reset_all()
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        # When: they all run, and every thread is joined
        recorders = [threading.Thread(target=recorder, args=(i,)) for i in range(4)]
        readers = [threading.Thread(target=reader) for _ in range(2)]
        reset_thread = threading.Thread(target=resetter)
        for thread in [*recorders, *readers, reset_thread]:
            thread.start()
        for thread in [*recorders, reset_thread]:
            thread.join()
        stop.set()
        for thread in readers:
            thread.join()

        # Then: nothing raised, and every observation is internally consistent
        assert errors == []
        assert observations, "the reader threads produced no observations"
        for observed in observations:
            assert all(0 <= failures <= total for failures, total in observed.values())
            failing = sum(1 for failures, _ in observed.values() if failures > 0)
            assert len(observed) <= max(32, failing)


# =============================================================================
# Property-based — invariants over arbitrary drive sequences
# =============================================================================

_OPS = st.lists(
    st.one_of(
        st.tuples(
            st.just("record"),
            st.integers(min_value=0, max_value=12),
            st.booleans(),
        ),
        st.tuples(st.just("read"), st.just(0), st.just(False)),
        st.tuples(
            st.just("advance"),
            st.integers(min_value=-2000, max_value=2000),
            st.just(False),
        ),
    ),
    max_size=60,
)


class TestOutcomeWindowProperties:
    """Invariants that must hold however the window is driven.

    The clock is the interesting axis: it is injectable, and the whole reason it
    defaults to a monotonic source is that a backward step would otherwise make
    stale buckets read as fresh. Fuzzing arbitrary jumps is what keeps that a
    regression test rather than an argument.
    """

    @given(operations=_OPS, cap=st.integers(min_value=1, max_value=8))
    @hyp_settings(max_examples=150, deadline=None)
    def test_arbitrary_sequences_keep_every_rate_and_the_bound_honest(
        self, operations, cap
    ):
        """Rates stay in range, and only failing keys can sit above the cap.

        Exceeding the cap is reachable in exactly one way — a key holding an
        in-window failure is never evicted — so the bound is stated against that
        exemption rather than absolutely. A broken admission check shows up as
        healthy keys piling up past the cap.
        """
        clock = _FakeClock()
        window = TimeBucketedOutcomeWindow(clock=clock, cap_provider=lambda: cap)

        for kind, value, failure in operations:
            if kind == "record":
                window.record(f"svc_{value}", failure=failure)
            elif kind == "advance":
                clock.advance(float(value))
            else:
                observed = window.snapshot()
                failing = sum(1 for f, _t in observed.values() if f > 0)
                assert len(observed) <= max(cap, failing)
                for failures, total in observed.values():
                    assert total > 0
                    assert 0 <= failures <= total

    @given(
        recorded_at=st.floats(min_value=0.0, max_value=1e6, allow_nan=False),
        jump_back=st.floats(min_value=0.0, max_value=1e6, allow_nan=False),
    )
    @hyp_settings(max_examples=150, deadline=None)
    def test_backward_clock_jump_never_reports_an_old_outcome_as_fresh(
        self, recorded_at, jump_back
    ):
        """A bucket stamped ahead of "now" is excluded, not counted.

        This is the property the monotonic default protects: under a wall clock
        stepped backwards, an outcome from before the step would satisfy the
        in-window predicate and be reported as the last five minutes.
        """
        clock = _FakeClock(start=recorded_at)
        window = TimeBucketedOutcomeWindow(clock=clock, cap_provider=lambda: 8)
        window.record("payment", failure=True)

        clock.now = recorded_at - jump_back
        observed = window.snapshot()

        same_bucket = int(clock.now // _BUCKET_SECONDS) == int(
            recorded_at // _BUCKET_SECONDS
        )
        assert ("payment" in observed) is same_bucket

    @given(
        names=st.lists(
            st.one_of(
                st.text(max_size=40),
                st.none(),
                st.integers(),
            ),
            max_size=20,
        )
    )
    @hyp_settings(max_examples=100, deadline=None)
    def test_no_service_name_shape_raises_out_of_the_feed(self, names):
        """Both feed functions are total: a policy is never broken by its name."""
        reset_call_outcome_window()
        for name in names:
            key = resolve_outcome_key(name)
            record_call_outcome(key, failure=True)

        for failures, total in get_call_outcome_window().snapshot().values():
            assert 0 <= failures <= total
