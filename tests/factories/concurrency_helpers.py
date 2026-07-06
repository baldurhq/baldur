"""
Concurrency helpers for deterministic Singleflight waiter tests.

The naive "Barrier + slow compute" pattern leaves a race window: a thread
that arrives at ``Singleflight.run()`` after the winner popped the
in-flight entry becomes a SECOND winner, flaking exactly-once assertions.
``make_observable_singleflight`` closes that window by signaling when all
expected callers have passed the in-flight map section, so the winner's
event-gated compute can be released only after every caller is committed
to either the winner or waiter role.

Usage:
    from tests.factories.concurrency_helpers import make_observable_singleflight

    sf, all_entered = make_observable_singleflight(expected_entries=8)

    def gated_fn():
        calls.append(1)
        all_entered.wait(timeout=5.0)  # release only when all 8 entered
        return "value"

    # 8 threads call sf.run("key", gated_fn) -> exactly 1 compute,
    # 7 waiters share the result. Deterministic, no sleeps.
"""

from __future__ import annotations

import threading

from baldur.core.singleflight import Singleflight

__all__ = ["make_observable_singleflight"]


class _CountingEntryLock:
    """Lock wrapper counting context-manager exits, signaling a threshold.

    ``Singleflight.run()`` acquires its internal lock exactly once per
    caller at entry (future registration/lookup) and once more in the
    winner's ``finally`` (map pop). When the winner's compute is gated on
    the ``all_entered`` event, the ``finally`` acquisition cannot happen
    before the threshold fires - so the first ``expected`` exits are
    exactly the N entry sections, and ``all_entered`` being set guarantees
    every caller has committed to the winner or waiter role.
    """

    def __init__(self, expected: int, all_entered: threading.Event) -> None:
        self._inner = threading.Lock()
        self._exits = 0
        self._expected = expected
        self._all_entered = all_entered

    def __enter__(self) -> _CountingEntryLock:
        self._inner.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._exits += 1  # still holding the inner lock - no race
        if self._exits >= self._expected:
            self._all_entered.set()
        self._inner.release()


def make_observable_singleflight(
    expected_entries: int,
) -> tuple[Singleflight, threading.Event]:
    """Build a Singleflight whose entry lock signals when N callers entered.

    Args:
        expected_entries: Number of concurrent ``run()`` callers the test
            will launch for the same key.

    Returns:
        ``(singleflight, all_entered)`` - gate the winner's compute
        callable on ``all_entered.wait(timeout)`` so it completes only
        after every concurrent caller has passed the in-flight map
        section, making exactly-once / shared-result / waiter-label
        assertions deterministic instead of timing-dependent.
    """
    all_entered = threading.Event()
    sf: Singleflight = Singleflight()
    # White-box swap of the internal entry lock; run() only uses it as a
    # context manager, which _CountingEntryLock implements.
    sf._lock = _CountingEntryLock(expected_entries, all_entered)  # type: ignore[assignment]
    return sf, all_entered
