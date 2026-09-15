"""
Call-outcome ratio window and the shared circuit-breaker trip predicate.

The circuit breaker's rate trigger needs a denominator that contains successes,
not just failures. Repository counters cannot supply one without a write per
successful call, which would put I/O back on the CLOSED-success hot path. This
module holds that evidence in process instead: a bounded ring of recent call
outcomes per service name, appended to under one narrow lock.

The ring is the outcome of every call this breaker answered: admitted CLOSED
calls record their result, and calls the breaker refused because of its own
state are recorded as failures — a dependency that is cut off is a dependency
that is failing, for as long as it stays cut off. The ring is cleared when this
process observes the name back in CLOSED, whoever closed it, so each CLOSED
period starts without evidence from the last one.

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

# The one state whose observation opens a fresh evidence period.
_CLOSED = "closed"


class OutcomeWindow:
    """Per-service ring of recent call outcomes, with transition awareness.

    Maps ``service_name`` to a ``deque(maxlen=sliding_window_size)`` of
    outcomes, so ``sum(window) / len(window)`` is the observed failure rate over
    the most recent calls this worker answered — admitted CLOSED calls by their
    result, refused calls as failures.

    Beside the ring, three small per-name records make transitions observable
    without a repository read:

    - an **epoch**, bumped whenever something happened to the name since an
      admission could have taken a hint — a failure write, a clear, an observed
      state change. A success recorded against a hint whose epoch no longer
      matches is not appended blindly; it takes the slow path's fresh read.
    - an **in-flight marker**, held while a failure's repository write is in
      progress, so a success that records during that write also takes the
      slow path — the epoch alone cannot cover a write that has not landed.
    - the **last state seen**, so a re-entry into CLOSED clears the ring
      exactly once whoever performed the close — this process, a peer through
      the shared store, boot hydration or drift.

    Thread-safe: every mutation and every consistent multi-field read takes the
    single lock. The success hot path holds it for one compare and one
    ``deque.append``; the steady-state admission path reads one dict entry
    without it (a GIL-atomic reference read).

    Evidence is per worker process by construction. That matches the circuit
    breaker's existing admission model, which reads L1 state only unless cluster
    state propagation is enabled.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[str, deque[int]] = {}
        self._epochs: dict[str, int] = {}
        self._writes_in_flight: dict[str, int] = {}
        self._last_seen: dict[str, str] = {}

    # ------------------------------------------------------------------ writes

    def record_success(self, service_name: str, window_size: int) -> None:
        """Append a success outcome for ``service_name``."""
        with self._lock:
            self._resolve(service_name, window_size).append(SUCCESS_OUTCOME)

    def record_success_if_epoch(
        self,
        service_name: str,
        window_size: int,
        hint_epoch: int,
        *,
        require_no_write_in_flight: bool = True,
    ) -> bool:
        """Append a success only if nothing happened to the name since ``hint_epoch``.

        The read-free fast path's append: under one lock hold, the success is
        recorded iff no failure write is in flight for the name and the epoch
        still equals ``hint_epoch``. ``False`` means the caller must fall
        through to its fresh-read path exactly as a stale hint does.

        The slow path uses the same append with
        ``require_no_write_in_flight=False``: it performs the consecutive-count
        reset itself, so a failure write in flight is no reason to withhold the
        entry — but a transition that landed after its fresh read (the epoch
        moved) is, because the name may no longer be CLOSED.
        """
        with self._lock:
            if (
                require_no_write_in_flight
                and self._writes_in_flight.get(service_name, 0) != 0
            ):
                return False
            if self._epochs.get(service_name, 0) != hint_epoch:
                return False
            self._resolve(service_name, window_size).append(SUCCESS_OUTCOME)
            return True

    def record_failure(self, service_name: str, window_size: int) -> None:
        """Append a failure outcome for ``service_name`` and move its epoch."""
        with self._lock:
            self._resolve(service_name, window_size).append(FAILURE_OUTCOME)
            self._epochs[service_name] = self._epochs.get(service_name, 0) + 1

    def record_rejection(self, service_name: str, window_size: int) -> None:
        """Append a failure outcome for a call the breaker refused.

        A refusal is evidence that the dependency is still cut off; it is not
        a transition, so the epoch is left alone — the transition that made the
        name non-CLOSED already moved it.
        """
        with self._lock:
            self._resolve(service_name, window_size).append(FAILURE_OUTCOME)

    def begin_write(self, service_name: str) -> None:
        """Mark a failure's repository write as in flight for ``service_name``."""
        with self._lock:
            self._writes_in_flight[service_name] = (
                self._writes_in_flight.get(service_name, 0) + 1
            )

    def end_write(self, service_name: str) -> None:
        """Release the in-flight marker and move the epoch past the write."""
        with self._lock:
            remaining = self._writes_in_flight.get(service_name, 0) - 1
            if remaining > 0:
                self._writes_in_flight[service_name] = remaining
            else:
                self._writes_in_flight.pop(service_name, None)
            self._epochs[service_name] = self._epochs.get(service_name, 0) + 1

    def bump(self, service_name: str) -> None:
        """Move the epoch for a change the window cannot see on its own.

        For the one operator write that changes a row without clearing the
        window: a success admitted against the pre-change row then falls to
        the slow path and its own checks.
        """
        with self._lock:
            self._epochs[service_name] = self._epochs.get(service_name, 0) + 1

    def observe_state(
        self, service_name: str, state: str, *, as_of: int | None = None
    ) -> bool:
        """Record the state this process just saw for ``service_name``.

        Called wherever the service holds a fresh row. On a change into CLOSED
        the ring is cleared and the epoch moved: the new CLOSED period starts
        without evidence from before the trip, and without the refusals
        recorded while the name was cut off, whoever performed the close. Any
        other change moves the epoch only. The first observation of a name
        records its state without clearing.

        ``as_of`` is the epoch the caller read *before* it fetched the row it
        is reporting. A row read from the repository can be stale by the time
        it is observed — a trip can commit between the read and this call —
        and observing a stale CLOSED row would clear the evidence that trip
        just kept. When the epoch has moved since ``as_of``, the observation is
        dropped: whatever moved it observed the fresher row itself. The result
        of an atomic repository operation is reported without ``as_of``.

        The steady state — the same state as last time — is decided on one
        dict read without the lock, so the CLOSED admission path pays no lock
        acquire here.

        Returns:
            True when the observation cleared the ring.
        """
        if self._last_seen.get(service_name) == state:
            return False
        with self._lock:
            if as_of is not None and self._epochs.get(service_name, 0) != as_of:
                return False
            previous = self._last_seen.get(service_name)
            if previous == state:
                return False
            self._last_seen[service_name] = state
            if previous is None:
                return False
            self._epochs[service_name] = self._epochs.get(service_name, 0) + 1
            if state != _CLOSED:
                return False
            window = self._windows.get(service_name)
            if window is not None:
                window.clear()
            return True

    def clear(self, service_name: str) -> None:
        """Drop the recorded evidence for ``service_name`` and move its epoch.

        Called when this process observes the name re-enter CLOSED (through
        ``observe_state``) and on an operator's force / reset: outcomes from
        before those points say nothing about the rate after them.
        """
        with self._lock:
            window = self._windows.get(service_name)
            if window is not None:
                window.clear()
            self._epochs[service_name] = self._epochs.get(service_name, 0) + 1

    # ------------------------------------------------------------------- reads

    def epoch_of(self, service_name: str) -> int:
        """The current epoch for ``service_name`` — one GIL-atomic dict read."""
        return self._epochs.get(service_name, 0)

    def epochs_snapshot(self) -> dict[str, int]:
        """Every tracked name's epoch, under the lock, for a bulk read's ``as_of``."""
        with self._lock:
            return dict(self._epochs)

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

    def read_each(self) -> dict[str, tuple[int, int]]:
        """Return ``{service_name: (failures, total)}`` for every tracked service.

        One consistent snapshot under the lock, for a reader that combines the
        per-name evidence with per-name repository state.
        """
        with self._lock:
            return {
                name: (sum(window), len(window))
                for name, window in self._windows.items()
            }

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
