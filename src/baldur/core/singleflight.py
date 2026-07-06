"""In-process per-key singleflight - deduplicate concurrent computations.

When N threads miss the same cache key simultaneously, only one (the
winner) runs the compute callable; the others (waiters) block on the
winner's Future and share its result - or its exception. This turns an
N-way cache-miss dogpile into exactly one computation per process.

Design notes:
- No logging or metrics inside the primitive (hot path; consumers own
  observability).
- No knobs: a waiter's upper bound is the winner's ``fn`` runtime,
  identical to an inline compute without deduplication.
- Context propagation caveat: ``fn`` runs in the winner's thread and
  execution context. Waiters' ``contextvars`` (trace/request IDs) do
  not flow into the compute, and logs/spans inside ``fn`` attribute to
  the winner. This is inherent to deduplication - the backend call
  happens once, in one context. Consumers needing per-caller dedup
  visibility should label at their own layer.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable
from concurrent.futures import Future
from typing import Generic, TypeVar

V = TypeVar("V")

__all__ = ["Singleflight"]


class Singleflight(Generic[V]):
    """Per-key in-flight deduplication of concurrent computations.

    The first caller for a key (winner) registers a Future, runs ``fn``
    with no locks held, publishes the result or exception, and removes
    the entry. Concurrent callers for the same key (waiters) block on
    the Future and receive the winner's value - or its exception
    (``concurrent.futures`` shares the same exception instance across
    all ``result()`` callers).

    Thread-safe: the in-flight map is guarded by a plain lock that is
    never held while ``fn`` runs, and the map self-cleans in the
    winner's ``finally`` (bounded by concurrent computes).
    """

    # verified-by: tests/unit/core/test_singleflight.py

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: dict[Hashable, Future[V]] = {}
        # Keys the CURRENT thread is computing as winner - used to
        # fast-fail re-entrant calls instead of self-deadlocking on
        # the thread's own Future. Cross-thread key cycles (A->B/B->A)
        # are NOT detected - equivalent to ordinary lock-ordering
        # deadlocks, out of scope.
        self._computing = threading.local()

    def run(self, key: Hashable, fn: Callable[[], V]) -> V:
        """Run ``fn`` for ``key``, deduplicating concurrent callers.

        Args:
            key: Deduplication key (hashable).
            fn: Zero-arg compute callable. Executed only by the winner.

        Returns:
            The winner's return value (shared by all concurrent callers).

        Raises:
            RuntimeError: If ``fn`` re-enters ``run()`` for the same key
                on the same thread (would otherwise deadlock on its own
                Future).
            BaseException: Whatever the winner's ``fn`` raised - the
                same exception instance propagates to the winner and
                all current waiters.
        """
        owned: set[Hashable] | None = getattr(self._computing, "keys", None)
        if owned is not None and key in owned:
            raise RuntimeError(
                f"Singleflight re-entrancy detected: compute for key {key!r} "
                f"re-entered run() for its own key on the same thread"
            )

        with self._lock:
            existing = self._inflight.get(key)
            if existing is None:
                future: Future[V] = Future()
                self._inflight[key] = future
            else:
                future = existing

        if existing is not None:
            # Waiter: share the winner's result or exception.
            return future.result()

        # Winner: run fn with NO locks held.
        if owned is None:
            owned = set()
            self._computing.keys = owned
        owned.add(key)
        try:
            try:
                result = fn()
            except BaseException as exc:
                # Publish the exception to all current waiters, then
                # re-raise it for the winner.
                future.set_exception(exc)
                raise
            future.set_result(result)
            return result
        finally:
            owned.discard(key)
            with self._lock:
                self._inflight.pop(key, None)
