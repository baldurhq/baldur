"""
Call-outcome ratio window and the shared circuit-breaker trip predicate.

The circuit breaker's rate trigger needs a denominator that contains successes,
not just failures. Repository counters cannot supply one without a write per
successful call, which would put I/O back on the CLOSED-success hot path. This
module holds that evidence in process instead: a bounded ring of recent CLOSED
call outcomes per service name, appended to under one narrow lock.

``evaluate_trip`` is the single trip model. The live service and the config-shadow
evaluator both call it, so a shadow simulation cannot predict a trip the live
breaker would not perform.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import CircuitBreakerConfig

__all__ = [
    "FAILURE_OUTCOME",
    "SUCCESS_OUTCOME",
    "TRIP_REASON_COUNT",
    "TRIP_REASON_RATE",
    "OutcomeWindow",
    "evaluate_trip",
]

# Outcome encoding — the window sums to the failure count.
FAILURE_OUTCOME = 1
SUCCESS_OUTCOME = 0

TRIP_REASON_RATE = "failure_rate_threshold_exceeded"
TRIP_REASON_COUNT = "failure_threshold_exceeded"


class OutcomeWindow:
    """Per-service ring of recent CLOSED-state call outcomes.

    Maps ``service_name`` to a ``deque(maxlen=sliding_window_size)`` of
    outcomes, so ``sum(window) / len(window)`` is the observed failure rate over
    the most recent calls this worker admitted.

    Thread-safe: every operation is a mutation or a consistent multi-field read,
    so all of them take the single lock. The success path — the only hot one —
    holds it for one ``deque.append``.

    Evidence is per worker process by construction. That matches the circuit
    breaker's existing admission model, which reads L1 state only unless cluster
    state propagation is enabled.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[str, deque[int]] = {}

    def record_success(self, service_name: str, window_size: int) -> None:
        """Append a success outcome for ``service_name``."""
        with self._lock:
            self._resolve(service_name, window_size).append(SUCCESS_OUTCOME)

    def record_failure(self, service_name: str, window_size: int) -> None:
        """Append a failure outcome for ``service_name``."""
        with self._lock:
            self._resolve(service_name, window_size).append(FAILURE_OUTCOME)

    def read(self, service_name: str) -> tuple[int, int]:
        """Return ``(failures, total)`` observed for ``service_name``.

        ``(0, 0)`` when nothing has been recorded — the caller treats that as
        "no evidence", never as a 0% failure rate.
        """
        with self._lock:
            window = self._windows.get(service_name)
            if window is None:
                return (0, 0)
            return (sum(window), len(window))

    def read_all(self) -> tuple[int, int]:
        """Return ``(failures, total)`` summed across every tracked service."""
        with self._lock:
            failures = 0
            total = 0
            for window in self._windows.values():
                failures += sum(window)
                total += len(window)
            return (failures, total)

    def clear(self, service_name: str) -> None:
        """Drop the recorded evidence for ``service_name``.

        Called on observed state transitions: outcomes from before a trip or a
        recovery say nothing about the rate after it.
        """
        with self._lock:
            window = self._windows.get(service_name)
            if window is not None:
                window.clear()

    def _resolve(self, service_name: str, window_size: int) -> deque[int]:
        """Return the ring for ``service_name``, sized to ``window_size``.

        Caller MUST hold ``self._lock``.

        On a size change (runtime config or a mesh override) the ring is rebuilt
        from the existing outcomes rather than emptied: ``deque(old, maxlen=new)``
        keeps the rightmost ``new`` entries. Rebuilding empty would blank the
        rate evidence and suspend rate-trigger protection for the next
        ``window_size`` calls.
        """
        maxlen = max(window_size, 0)
        window = self._windows.get(service_name)
        if window is None:
            window = deque(maxlen=maxlen)
            self._windows[service_name] = window
        elif window.maxlen != maxlen:
            window = deque(window, maxlen=maxlen)
            self._windows[service_name] = window
        return window


def evaluate_trip(
    consecutive_failures: int,
    window_failures: int,
    window_total: int,
    config: CircuitBreakerConfig,
) -> str | None:
    """Decide whether a CLOSED circuit should open, and why.

    Two OR'd triggers:

    - **Rate**: the observed failure percentage over the window reaches
      ``failure_rate_threshold``. Evaluated only when that threshold is above
      zero (zero disables the trigger) and the window holds at least
      ``minimum_calls`` observations — low traffic makes a rate estimate noise.
    - **Count**: ``consecutive_failures`` reaches ``failure_threshold``.
      Deliberately not gated by ``minimum_calls``: consecutive-failure evidence
      is traffic-independent, so a low call count is no reason to distrust it.

    Args:
        consecutive_failures: Failure count since the last success, from the
            repository.
        window_failures: Failures recorded in the outcome window.
        window_total: Total calls recorded in the outcome window.
        config: Effective configuration for the service.

    Returns:
        The trip reason, or ``None`` when the circuit should stay closed.
    """
    if (
        config.failure_rate_threshold > 0
        and window_total > 0
        and window_total >= config.minimum_calls
    ):
        failure_rate = window_failures / window_total * 100
        if failure_rate >= config.failure_rate_threshold:
            return TRIP_REASON_RATE

    if consecutive_failures >= config.failure_threshold:
        return TRIP_REASON_COUNT

    return None
