"""Time-bucketed call-outcome rate window — the failure-rate producer.

The comprehensive-metrics payload advertises a five-minute per-service failure
rate. Nothing measured one: the field was a literal. This module is the
producer, and the three primitives already in the tree each answer a different
question:

- ``OutcomeWindow`` (this package) holds *count*-based, CLOSED-only evidence and
  is cleared on every breaker transition — it exists to decide a trip, and it
  reads empty at the exact moment an operator asks what just happened.
- ``SlidingWindowCounter`` (``core.rate_limiting``) stores an exact timestamp
  per event, which is O(events): at 500 RPS over 300 s that is 150 000 floats
  per service per worker.
- This window stores a fixed ring of 10-second buckets per key, so its memory
  is O(keys) regardless of traffic and a read is a bounded integer walk.

Evidence is per worker process, matching the breaker's own admission model.
Absence is never a zero: a key with no in-window outcome yields nothing, and the
payload renders ``null``.

Design constraints that are load-bearing rather than stylistic:

- **The clock is monotonic.** The window consumes only time differences. A
  wall clock stepped backwards by NTP makes the current bucket index smaller
  than the stored ones, so buckets holding old outcomes satisfy the in-window
  predicate and get reported as "the last five minutes" — fabricated freshness,
  the defect class this module exists to remove. A bucket stores its index and
  nothing else, so no read-side check can recover from that; the only defence
  is a clock that cannot step back. Treat the default as load-bearing.
- **Buckets are pre-allocated mutable integer arrays.** A tuple triple would
  allocate and rebind on every recorded outcome, putting one object allocation
  on every protected call.
- **No log call is ever made while the lock is held.** ``structlog`` here is a
  non-filtering ``BoundLogger`` whose rate-limit processor takes a
  process-global lock, so an emission under this lock would nest that global
  lock beneath the one every protected call takes. Every warning is decided
  inside the critical section as plain data and emitted after release.
"""

from __future__ import annotations

import array
import threading
from collections.abc import Callable

import structlog

logger = structlog.get_logger()

__all__ = [
    "FAILURE_RATE_WINDOW_SECONDS",
    "TimeBucketedOutcomeWindow",
    "get_call_outcome_window",
    "record_call_outcome",
    "reset_call_outcome_window",
    "resolve_outcome_key",
]

# The public field names (``failure_rate_5m``, ``last_5m_*``) promise five
# minutes, so the window length is a contract constant rather than a tunable —
# a configurable window would contradict the name it is rendered under.
FAILURE_RATE_WINDOW_SECONDS = 300
_BUCKET_SECONDS = 10

# 31, not 30, and derived so the "+ 1" survives an edit to either operand above.
# With 30 buckets and index ``floor(t / 10) % 30`` an outcome recorded mid-bucket
# is evicted after only 290 s, so a service measured 291 s ago would render "not
# measured" inside its own advertised window. The extra in-flight bucket makes
# the covered span >= 300 s and < 310 s: the window can carry up to one bucket
# of extra age but can never under-report, which is the safe direction — under-
# reporting shows an operator a healthier service than reality.
_BUCKET_COUNT = FAILURE_RATE_WINDOW_SECONDS // _BUCKET_SECONDS + 1

# Fallback cardinality cap, used only when the shared resolver is unreachable.
# Mirrors the metric registry's own default so the two surfaces agree before
# either has read settings.
_MAX_OUTCOME_KEYS = 50

# Fraction of the cap a read leaves free. Without headroom, "at cap" is a
# permanent state for whoever arrives late: a slow-cadence service whose slot is
# reclaimed at a read is immediately taken by another name and refused forever
# after, made unmeasurable by the operator's own poll.
_HEADROOM_DIVISOR = 10

# Distinct keys whose first raw spelling is remembered for collision detection.
# Saturating rather than LRU, mirroring the registry's refusal memo: a rotation
# of more than this many names through an LRU would re-warn forever.
_MAX_SPELLING_MEMO = 256

# An unstamped bucket. Distinguishable from a real index because a monotonic
# clock never yields a negative reading.
_EMPTY_EPOCH = -1

# ``(epochs, failure_counts, total_counts)`` — three parallel int64 arrays,
# mutated in place.
_BucketRing = tuple["array.array[int]", "array.array[int]", "array.array[int]"]


def _default_cap_provider() -> int:
    """Return the shared cardinality cap, or the module fallback.

    Reuses the metric registry's memoized resolver rather than reading settings
    directly. That is what makes "an operator who raises the cap sees it honored
    within seconds" exact instead of approximate: sharing one resolver makes the
    two cardinality surfaces incapable of disagreeing about the cap at any
    instant, and it keeps a layered-settings merge plus a model construction off
    every metrics poll.
    """
    try:
        from baldur.metrics.registry import _resolve_max_domains_cached

        return int(_resolve_max_domains_cached())
    except Exception:
        return _MAX_OUTCOME_KEYS


class TimeBucketedOutcomeWindow:
    """Per-key ring of 10-second call-outcome buckets over a 300-second window.

    Each key owns three pre-allocated int64 arrays — bucket epoch, failure
    count, total count — mutated in place. Recording is O(1); reading is
    O(keys x buckets) integer comparisons.

    Thread-safe under a single lock. Every public method takes that lock exactly
    once and never calls another public method on this instance, so no
    re-entrancy is possible. The lock is process-global across services rather
    than per-breaker, which is acceptable only because the critical section is a
    handful of integer operations with no allocation, no I/O and no callbacks.

    Args:
        clock: Seconds source. Defaults to ``time.monotonic`` — see the module
            docstring for why the wall clock is unsafe here.
        cap_provider: Returns the maximum number of distinct keys the window may
            hold. Re-read on every ``snapshot()``, never on the recording path.
    """

    def __init__(
        self,
        clock: Callable[[], float] | None = None,
        cap_provider: Callable[[], int] | None = None,
    ) -> None:
        if clock is None:
            import time

            clock = time.monotonic
        self._clock = clock
        self._cap_provider = cap_provider or _default_cap_provider

        self._lock = threading.Lock()
        self._keys: dict[str, _BucketRing] = {}
        self._cap = _MAX_OUTCOME_KEYS
        self._cap_epoch_warned = False

        # First raw spelling seen per key, plus the keys already reported as
        # merged. Both bounded by ``_MAX_SPELLING_MEMO``.
        self._first_spelling: dict[str, str] = {}
        self._collisions_warned: set[str] = set()

    # -- recording ---------------------------------------------------------

    def record(self, key: str, *, failure: bool) -> None:
        """Count one classified circuit-breaker admission against ``key``.

        Refuses silently once the window holds ``cap`` distinct keys, emitting
        one warning per cap epoch. Refusal drops the outcome rather than folding
        it into a collapse label: an overflow row would attribute one service's
        failures to a label naming a different set of services.
        """
        epoch = int(self._clock() // _BUCKET_SECONDS)
        index = epoch % _BUCKET_COUNT

        refused_key: str | None = None
        cap_at_refusal = 0

        with self._lock:
            ring = self._keys.get(key)
            if ring is None:
                if len(self._keys) >= self._cap:
                    if not self._cap_epoch_warned:
                        self._cap_epoch_warned = True
                        refused_key = key
                        cap_at_refusal = self._cap
                    ring = None
                else:
                    ring = self._new_ring()
                    self._keys[key] = ring
                    # A successful admission closes the cap epoch, so the
                    # operator gets one line per time the window fills.
                    self._cap_epoch_warned = False
            if ring is not None:
                epochs, failures, totals = ring
                if epochs[index] != epoch:
                    epochs[index] = epoch
                    failures[index] = 0
                    totals[index] = 0
                totals[index] += 1
                if failure:
                    failures[index] += 1

        if refused_key is not None:
            logger.warning(
                "circuit_breaker.outcome_window_cap_reached",
                max_keys=cap_at_refusal,
                refused_key=refused_key,
            )

    def note_key_spelling(self, raw_name: object, key: str) -> None:
        """Record the first raw spelling for ``key``, warning on a second one.

        Two names an operator considers distinct — ``orders.charge`` and
        ``orders-charge``, or any pair differing only in label-unsafe characters
        — project onto one key and therefore onto one row whose rate matches
        neither. This fires if and only if two names actually merge, never on a
        lone spelling that merely could.

        Called once per protected name at policy construction, never per call.
        """
        spelling = raw_name if isinstance(raw_name, str) else str(raw_name)
        collision: tuple[str, str] | None = None

        with self._lock:
            first = self._first_spelling.get(key)
            if first is None:
                if len(self._first_spelling) < _MAX_SPELLING_MEMO:
                    self._first_spelling[key] = spelling
            elif first != spelling and key not in self._collisions_warned:
                self._collisions_warned.add(key)
                collision = (first, spelling)

        if collision is not None:
            logger.warning(
                "circuit_breaker.outcome_key_collision",
                outcome_key=key,
                first_spelling=collision[0],
                merged_spelling=collision[1],
            )

    # -- reading -----------------------------------------------------------

    def snapshot(self) -> dict[str, tuple[int, int]]:
        """Return ``{key: (failures, total)}`` over the in-window buckets.

        A read that mutates. Three pieces of housekeeping ride here rather than
        on the recording path, so that path stays a pure O(1) refuse:

        1. keys whose every bucket has aged out are dropped;
        2. the cardinality cap is re-resolved, so raising it takes effect on a
           running process instead of at the next restart;
        3. if fewer than one tenth of the cap is free, least-recently-active
           keys **that hold no in-window failure** are evicted until it is.

        A key holding a failure is never evicted, so an operator's own poll can
        never cost the incident case its evidence. A purely-healthy key that
        loses its slot renders no row — which is what it renders today anyway —
        and a still-live service re-admits itself on its next call.
        """
        cap = self._resolve_cap()
        now_epoch = int(self._clock() // _BUCKET_SECONDS)

        evicted: list[str] = []
        with self._lock:
            self._cap = cap
            observed: dict[str, tuple[int, int]] = {}
            # ``(last_active_epoch, key)`` for keys with no in-window failure —
            # the only eviction candidates, ordered oldest-first below.
            evictable: list[tuple[int, str]] = []
            stale: list[str] = []

            for key, ring in self._keys.items():
                failures, total, last_epoch = self._walk(ring, now_epoch)
                if total == 0:
                    stale.append(key)
                    continue
                observed[key] = (failures, total)
                if failures == 0:
                    evictable.append((last_epoch, key))

            for key in stale:
                del self._keys[key]

            free = cap - len(self._keys)
            headroom = max(1, cap // _HEADROOM_DIVISOR)
            if free < headroom and evictable:
                evictable.sort()
                for _, key in evictable[: headroom - free]:
                    del self._keys[key]
                    del observed[key]
                    evicted.append(key)

        if evicted:
            logger.debug(
                "circuit_breaker.outcome_window_keys_evicted",
                evicted_count=len(evicted),
                evicted_keys=evicted,
            )
        return observed

    def read_all(self) -> tuple[int, int]:
        """Return ``(failures, total)`` summed across every key.

        A convenience for callers that want the cross-key total alone. The
        metrics payload deliberately does NOT use it: summing a second read
        would let a call landing between the two reads make the aggregate
        contradict the rows rendered beside it in the same response.
        """
        now_epoch = int(self._clock() // _BUCKET_SECONDS)
        with self._lock:
            total_failures = 0
            total_calls = 0
            for ring in self._keys.values():
                failures, total, _ = self._walk(ring, now_epoch)
                total_failures += failures
                total_calls += total
            return (total_failures, total_calls)

    def reset_all(self) -> None:
        """Drop every key, every spelling memo and the cap epoch flag."""
        with self._lock:
            self._keys.clear()
            self._first_spelling.clear()
            self._collisions_warned.clear()
            self._cap_epoch_warned = False
            self._cap = _MAX_OUTCOME_KEYS

    # -- internals ---------------------------------------------------------

    def _resolve_cap(self) -> int:
        """Resolve the cap OUTSIDE the lock, falling back to the constant.

        A broken settings field cannot uncap the window, and a slow settings
        read can never reach a protected call: this runs on the metrics-read
        path only, before the lock is taken.
        """
        try:
            cap = int(self._cap_provider())
        except Exception:
            return _MAX_OUTCOME_KEYS
        return cap if cap > 0 else _MAX_OUTCOME_KEYS

    @staticmethod
    def _new_ring() -> _BucketRing:
        """Allocate one key's three parallel bucket arrays."""
        return (
            array.array("q", [_EMPTY_EPOCH]) * _BUCKET_COUNT,
            array.array("q", [0]) * _BUCKET_COUNT,
            array.array("q", [0]) * _BUCKET_COUNT,
        )

    @staticmethod
    def _walk(ring: _BucketRing, now_epoch: int) -> tuple[int, int, int]:
        """Return ``(failures, total, last_active_epoch)`` over in-window buckets.

        A bucket is in window when its epoch is within ``_BUCKET_COUNT`` steps
        behind ``now_epoch``, so the covered span is >= 300 s and < 310 s. The
        exact edge inside that band is bucket-phase-relative, not
        elapsed-relative: an outcome is counted iff the read's bucket index is
        at most 30 ahead of the record's. Every phase counts everything up to
        300 s and drops everything from 310 s; only the 10 s between them
        depends on where in its bucket the outcome landed.

        A bucket stamped *ahead* of now is excluded. That is NOT a general
        defence against a clock moving backwards, and must not be read as one:
        a clock that runs forward past the window and only then steps back can
        leave an aged-out bucket inside the in-window band again, republishing
        old failures as the last five minutes. Nothing here can detect that,
        because a bucket stores only its index. The defence is the monotonic
        default clock, which never moves backwards at all — which is why that
        default is a contract of this module rather than a convenience (see the
        module docstring).

        Caller MUST hold the lock.
        """
        epochs, failures, totals = ring
        window_failures = 0
        window_total = 0
        last_epoch = _EMPTY_EPOCH
        for index in range(_BUCKET_COUNT):
            epoch = epochs[index]
            if epoch < 0:
                continue
            age = now_epoch - epoch
            if age < 0 or age >= _BUCKET_COUNT:
                continue
            total = totals[index]
            if total == 0:
                continue
            window_failures += failures[index]
            window_total += total
            if epoch > last_epoch:
                last_epoch = epoch
        return (window_failures, window_total, last_epoch)


# =============================================================================
# Module singleton
# =============================================================================

_window: TimeBucketedOutcomeWindow | None = None
_window_lock = threading.Lock()


def get_call_outcome_window() -> TimeBucketedOutcomeWindow:
    """Return the process-wide call-outcome window, creating it on first use."""
    global _window
    if _window is None:
        with _window_lock:
            if _window is None:
                _window = TimeBucketedOutcomeWindow()
    return _window


def reset_call_outcome_window() -> None:
    """Drop the process-wide window so the next read starts empty.

    Wired into the protect-cache reset chain. No production caller exists, so
    live evidence is never wiped.
    """
    global _window
    with _window_lock:
        _window = None


# =============================================================================
# Feed functions — split by hotness
# =============================================================================


def resolve_outcome_key(service_name: str) -> str | None:
    """Project a circuit-breaker service name onto its window key, or None.

    Runs **once per protected name**, at policy construction, so the projection,
    its lazy import and its collision check are paid once rather than on every
    call.

    The key is the canonical domain-label form, which is exactly the form the
    metric registry admits — that is what makes the per-service row join exact
    instead of missing every name carrying a dot, a hyphen or an uppercase
    letter. Base-parsing a composite name is deliberately NOT applied: it would
    split one logical service across two rows and merge distinct cells.

    Returns ``None`` on any failure. Absence renders null, never a wrong number.
    """
    try:
        from baldur.metrics.registry import canonicalize_domain_label

        key = canonicalize_domain_label(service_name)
        if not key:
            return None
        get_call_outcome_window().note_key_spelling(service_name, key)
        return key
    except Exception:
        return None


def record_call_outcome(key: str | None, *, failure: bool) -> None:
    """Count one classified admission — the per-call entry point.

    Deliberately does no import, no regex and no string work: one lock, one dict
    get, three integer operations. Folding the key projection in here would put
    a substitution pass and a module-table lookup on every protected call.

    Fail-open by contract: recording is a side effect, so an exception here can
    never fail (or replace the exception of) the business call.
    """
    if key is None:
        return
    try:
        get_call_outcome_window().record(key, failure=failure)
    except Exception:
        pass
