"""In-process rate-limiting and cooldown primitives.

Shared, framework-free implementations of the in-process rate-limiting and
cooldown primitives that were otherwise hand-reimplemented across every policy
owner: an exact-timestamp sliding-window counter, a token bucket, and a
single-slot cooldown / debounce gate. Every in-process window / bucket /
cooldown policy composes these instead of forking the prune / refill / cooldown
arithmetic, so a window-boundary, refill, or cooldown-semantics fix lands in
exactly one place.

Both primitives take an injectable ``clock`` (default ``time.time``) so a
consumer can opt into ``time.monotonic`` and tests need not patch the global
clock. ``time.time`` is the wall-clock epoch and is permitted for rate
limiting; it also matches the historical inlined behavior of every consumer.

The primitives are mechanism-only: no logging, metrics, or audit — observability
stays at the policy layers that compose them. Each public method takes the
single instance lock once and never calls another public method on the same
instance (lock symmetry).

Status: Internal
"""

from __future__ import annotations

import bisect
import threading
import time
from collections import defaultdict
from collections.abc import Callable

__all__ = ["CooldownGate", "SlidingWindowCounter", "TokenBucket"]

# Poll interval for TokenBucket.wait_for_token — how often it retries a consume
# while blocking. A named constant rather than an inline literal at the call site.
_WAIT_POLL_INTERVAL_SECONDS = 0.01


class SlidingWindowCounter:
    """Thread-safe, multi-key, exact-timestamp sliding-window event counter.

    Records event timestamps per key and answers "how many events fell in the
    last ``window_seconds``". The window and limit are per-call parameters so a
    single instance serves callers whose window changes on a settings reload,
    and multiple keys stay isolated. Per-key window consistency is the caller's
    contract; the primitive does not detect a mismatched window.

    Memory is bounded two ways, depending on how a consumer records events:

    * ``try_acquire`` / ``record_and_count`` prune the per-key series to the
      call's ``window_seconds`` as they mutate, so a series is bounded by the
      window (enforcement and check-and-record consumers).
    * ``record`` is append-only unless ``retention_seconds`` is set, in which
      case it front-trims the series to ``clock() - retention_seconds`` on every
      write. A consumer that reads with ``count`` (non-destructive) sets
      ``retention_seconds`` to bound the series; reads with any
      ``window_seconds <= retention_seconds`` stay exact.

    ``cleanup_interval`` enables an additional periodic stale-key sweep during
    ``try_acquire`` (removing emptied keys); ``None`` disables it.
    """

    def __init__(
        self,
        *,
        cleanup_interval: float | None = None,
        retention_seconds: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._events: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()
        # None => wall-clock ``time.time`` resolved at call time, so the default
        # preserves the historical inlined ``time.time()`` behavior exactly
        # (including test patchability). Inject a callable to opt into e.g.
        # ``time.monotonic`` or a deterministic test clock.
        self._clock = clock
        self._cleanup_interval = cleanup_interval
        self._retention_seconds = retention_seconds
        self._last_cleanup: float = self._now()

    def _now(self) -> float:
        """Return the current time from the injected clock or wall clock."""
        clock = self._clock
        return clock() if clock is not None else time.time()

    def try_acquire(
        self,
        key: str,
        max_events: int,
        window_seconds: float,
    ) -> tuple[bool, int]:
        """Atomically decide + conditionally record one event under one lock.

        Prunes the key's series to the window, compares the count to
        ``max_events``, and records the event ONLY when allowed (so memory is
        bounded to ~``max_events`` per key). Returns ``(allowed, count)`` where
        ``count`` includes the new event when allowed.

        Note:
            For a single-slot cooldown with a rollback token (reserve, act,
            release-on-failure), use :class:`CooldownGate` — ``try_acquire``
            returns ``(allowed, count)`` and has no per-reservation rollback.
        """
        with self._lock:
            now = self._now()
            window_start = now - window_seconds
            self._maybe_cleanup(now, window_seconds)
            kept = [ts for ts in self._events[key] if ts > window_start]
            count = len(kept)
            if count >= max_events:
                self._events[key] = kept
                return False, count
            kept.append(now)
            self._events[key] = kept
            return True, count + 1

    def record(self, key: str) -> None:
        """Append the current timestamp for ``key`` (front-trim if retention set)."""
        with self._lock:
            now = self._now()
            events = self._events[key]
            events.append(now)
            if self._retention_seconds is not None:
                self._trim_retention(events, now)

    def record_and_count(self, key: str, window_seconds: float) -> int:
        """Atomically append the current timestamp, prune to the window, and count."""
        with self._lock:
            now = self._now()
            window_start = now - window_seconds
            events = self._events[key]
            events.append(now)
            kept = [ts for ts in events if ts > window_start]
            self._events[key] = kept
            return len(kept)

    def count(self, key: str, window_seconds: float) -> int:
        """Return the event count within the window without mutating state."""
        with self._lock:
            window_start = self._now() - window_seconds
            return sum(1 for ts in self._events.get(key, ()) if ts > window_start)

    def snapshot(self, key: str, window_seconds: float) -> list[float]:
        """Return a pruned read-only copy of the key's in-window timestamps."""
        with self._lock:
            window_start = self._now() - window_seconds
            return [ts for ts in self._events.get(key, ()) if ts > window_start]

    def restore(self, key: str, timestamps: list[float]) -> None:
        """Replace the stored series for ``key`` (the inverse of ``snapshot``).

        A persistence-backed consumer reloads a previously stored series after a
        restart. The input is copied and sorted ascending so the window
        arithmetic stays correct.
        """
        with self._lock:
            self._events[key] = sorted(float(ts) for ts in timestamps)

    def keys(self) -> list[str]:
        """Return a snapshot of the currently tracked keys."""
        with self._lock:
            return list(self._events.keys())

    def reset(self, key: str) -> bool:
        """Drop all state for ``key``; return whether it existed."""
        with self._lock:
            if key in self._events:
                del self._events[key]
                return True
            return False

    def reset_all(self) -> None:
        """Drop all state for every key."""
        with self._lock:
            self._events.clear()

    def _maybe_cleanup(self, now: float, window_seconds: float) -> None:
        """Periodic stale-key sweep — prune every key and drop emptied ones.

        Runs only when ``cleanup_interval`` is set and the interval has elapsed.
        Must be called with the lock held.
        """
        if self._cleanup_interval is None:
            return
        if now - self._last_cleanup <= self._cleanup_interval:
            return
        self._last_cleanup = now
        window_start = now - window_seconds
        empty: list[str] = []
        for key, events in self._events.items():
            kept = [ts for ts in events if ts > window_start]
            if kept:
                self._events[key] = kept
            else:
                empty.append(key)
        for key in empty:
            del self._events[key]

    def _trim_retention(self, events: list[float], now: float) -> None:
        """Front-trim entries at or older than ``now - retention_seconds``.

        Assumes ``events`` is ascending (append order). Must be called with the
        lock held and only when ``retention_seconds`` is set.
        """
        assert self._retention_seconds is not None
        cutoff = now - self._retention_seconds
        drop = bisect.bisect_right(events, cutoff)
        if drop:
            del events[:drop]


class TokenBucket:
    """Token-bucket rate limiter.

    Tokens refill at ``rate`` per second up to ``capacity`` and are consumed one
    (or more) per request. Thread-safe for single-process use.

    Note:
        This is a synchronous primitive. ``wait_for_token`` uses ``time.sleep``
        and blocks the event loop under asyncio.
    """

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ):
        """Initialise the bucket.

        Args:
            rate: Tokens generated per second.
            capacity: Maximum token count (defaults to ``rate``).
            clock: Time source. ``None`` (default) uses wall-clock
                ``time.time`` resolved at call time (patchable in tests);
                inject a callable to opt into e.g. ``time.monotonic``.
        """
        self._rate = rate
        self._capacity = capacity or rate
        self._tokens = self._capacity
        self._clock = clock
        self._last_update = self._now()
        self._lock = threading.Lock()

    def _now(self) -> float:
        """Return the current time from the injected clock or wall clock."""
        clock = self._clock
        return clock() if clock is not None else time.time()

    def set_rate(self, rate: float) -> None:
        """Change the refill rate."""
        with self._lock:
            self._rate = rate

    def get_rate(self) -> float:
        """Return the current refill rate."""
        with self._lock:
            return self._rate

    def consume(self, tokens: int = 1) -> bool:
        """Try to consume ``tokens``; return whether enough were available."""
        with self._lock:
            now = self._now()
            elapsed = now - self._last_update
            self._last_update = now

            # Refill in proportion to elapsed time.
            self._tokens = min(
                self._capacity,
                self._tokens + elapsed * self._rate,
            )

            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def get_token_ratio(self) -> float:
        """Return the current fill ratio (0.0–1.0).

        Reflects refill but does not advance ``_last_update`` (read-only).
        """
        with self._lock:
            now = self._now()
            elapsed = now - self._last_update
            current = min(self._capacity, self._tokens + elapsed * self._rate)
            return current / self._capacity if self._capacity > 0 else 0.0

    def wait_for_token(self, timeout: float = 1.0) -> bool:
        """Block up to ``timeout`` seconds trying to consume one token.

        Note:
            Uses ``time.sleep`` and blocks the event loop under asyncio.

        Args:
            timeout: Maximum time to wait, in seconds.

        Returns:
            Whether a token was acquired before the timeout.
        """
        start = self._now()
        while self._now() - start < timeout:
            if self.consume():
                return True
            time.sleep(_WAIT_POLL_INTERVAL_SECONDS)
        return False


class CooldownGate:
    """Thread-safe, multi-key single-slot cooldown / debounce gate.

    Suppresses a repeated action for ``cooldown_seconds`` after it last fired,
    per key. Where :class:`SlidingWindowCounter` counts many events in a window,
    ``CooldownGate`` tracks exactly one reservation per key and returns a
    rollback token, so a failed action can release its slot without leaving a
    cooldown that suppresses the retry — the "reserve, act, release-on-failure"
    pattern every hand-rolled alert / debounce fork re-implemented.

    Each key maps to ``(reserved_ts, window_at_reserve)``. Eviction judges every
    entry against its OWN stored window, so a short-cooldown caller can never
    strip a longer-cooldown caller's still-in-window reservation. The cooldown
    decision itself uses the call-time ``cooldown_seconds``, so a settings
    reload shortens or extends suppression immediately.

    ``cooldown_seconds <= 0`` disables the gate: every reserve succeeds and the
    cooldown check is skipped (matching the notification hub's ``cooldown <= 0``
    untracked precedent).

    The gate is mechanism-only (no logging / metrics / audit). Each public
    method takes the single instance lock once and never calls another public
    method on the same instance (lock symmetry); :meth:`is_suppressed` is a
    lock-free GIL-atomic read.
    """

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        # key -> (reserved_ts, window_seconds_at_reserve)
        self._entries: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
        # None => wall-clock ``time.time`` resolved at call time (patchable in
        # tests); inject a callable to opt into a deterministic clock.
        self._clock = clock

    def _now(self) -> float:
        """Return the current time from the injected clock or wall clock."""
        clock = self._clock
        return clock() if clock is not None else time.time()

    def try_reserve(
        self, key: str, cooldown_seconds: float
    ) -> tuple[bool, float | None]:
        """Atomically reserve the cooldown slot for ``key`` under one lock.

        Evicts every entry past its own stored window, then — when
        ``cooldown_seconds > 0`` and ``key`` is still within the CALL's cooldown
        — returns ``(False, None)``. Otherwise writes ``(now, cooldown_seconds)``
        and returns ``(True, now)``. The returned timestamp is the rollback
        token for :meth:`release`.
        """
        with self._lock:
            now = self._now()
            self._evict_expired(now)
            if cooldown_seconds > 0:
                entry = self._entries.get(key)
                if entry is not None and now - entry[0] < cooldown_seconds:
                    return False, None
            self._entries[key] = (now, cooldown_seconds)
            return True, now

    def release(self, key: str, token: float) -> bool:
        """Release a reservation iff its stored timestamp equals ``token``.

        A successor's live reservation (a different timestamp) is never
        clobbered, so a slow failed action cannot delete a rival's fresh slot.
        Returns whether an entry was removed.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[0] == token:
                del self._entries[key]
                return True
            return False

    def is_suppressed(self, key: str, cooldown_seconds: float) -> bool:
        """Lock-free read: is ``key`` within its cooldown right now?

        Reads a single GIL-atomic ``dict.get`` reference (the lock-symmetry
        read-only exemption); the authoritative gate is :meth:`try_reserve`.
        ``cooldown_seconds <= 0`` is never suppressed.
        """
        if cooldown_seconds <= 0:
            return False
        entry = self._entries.get(key)
        if entry is None:
            return False
        return self._now() - entry[0] < cooldown_seconds

    def snapshot(self) -> dict[str, float]:
        """Return a locked copy mapping key -> reserved timestamp."""
        with self._lock:
            return {key: ts for key, (ts, _window) in self._entries.items()}

    def keys(self) -> list[str]:
        """Return a snapshot of the currently reserved keys."""
        with self._lock:
            return list(self._entries.keys())

    def reset(self, key: str) -> bool:
        """Drop the reservation for ``key``; return whether it existed."""
        with self._lock:
            if key in self._entries:
                del self._entries[key]
                return True
            return False

    def reset_all(self) -> None:
        """Drop every reservation."""
        with self._lock:
            self._entries.clear()

    def _evict_expired(self, now: float) -> None:
        """Drop entries past their own stored window (caller holds the lock).

        Per-entry window comparison: a short-cooldown reserve cannot evict a
        longer-cooldown caller's still-in-window entry, so keys with different
        cooldowns sharing one gate never shorten one another.
        """
        expired = [
            key for key, (ts, window) in self._entries.items() if now - ts >= window
        ]
        for key in expired:
            del self._entries[key]
