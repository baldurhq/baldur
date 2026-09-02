"""``RingBuffer.drain_all()`` — the atomic read-and-empty the shutdown drain needs.

``get_all()`` followed by ``clear()`` releases the buffer lock between the two
calls. An item landing in that window is cleared without ever being read, which
on a shutdown drain means a captured DLQ entry is discarded without reaching its
emergency destination — the buffer has no WAL, so "discarded" is "gone". This
method exists to close that window, so the tests here are about the window and
not about the return value.
"""

from __future__ import annotations

import threading

from baldur.audit.ring_buffer import (
    BackpressureStrategy,
    RingBuffer,
)

# Waits are on threading primitives, so a healthy run blocks only as long as the
# handoff needs. These bound the failure mode, which is a hang.
_HANDOFF_TIMEOUT_SECONDS = 5.0
_JOIN_TIMEOUT_SECONDS = 10.0
# Long enough that a drain which does NOT take the lock has returned by now.
_STILL_BLOCKED_PROBE_SECONDS = 0.2


class TestRingBufferDrainAllBehavior:
    """Read + removal in one lock scope, and the emptiness that follows."""

    def test_drain_all_returns_every_item_and_empties_the_buffer(self):
        # Given
        buffer: RingBuffer[int] = RingBuffer(capacity=10)
        for i in range(5):
            buffer.put(i)

        # When
        drained = buffer.drain_all()

        # Then — everything handed over, in enqueue order, and nothing left
        assert drained == [0, 1, 2, 3, 4]
        assert buffer.size == 0
        assert buffer.is_empty is True

    def test_drain_all_on_an_empty_buffer_returns_an_empty_list(self):
        """Boundary: the teardown calls this unconditionally, including on a
        process that captured nothing."""
        buffer: RingBuffer[int] = RingBuffer(capacity=10)

        assert buffer.drain_all() == []
        assert buffer.size == 0

    def test_drain_all_is_idempotent_across_repeat_calls(self):
        """The teardown's once-guard is not the only caller: a second drain
        must report an empty buffer rather than re-hand the same entries."""
        # Given
        buffer: RingBuffer[str] = RingBuffer(capacity=10)
        buffer.put("a")
        buffer.put("b")

        # When
        first = buffer.drain_all()
        second = buffer.drain_all()

        # Then
        assert first == ["a", "b"]
        assert second == []

    def test_drain_all_returns_a_list_the_caller_owns(self):
        """The dump mutates nothing, but a returned view over the live deque
        would let a concurrent put appear in a batch already handed over."""
        # Given
        buffer: RingBuffer[int] = RingBuffer(capacity=10)
        buffer.put(1)

        # When
        drained = buffer.drain_all()
        drained.append(99)
        buffer.put(2)

        # Then — the caller's mutation and the later put are independent
        assert drained == [1, 99]
        assert buffer.get_all() == [2]

    def test_drain_all_holds_the_buffer_lock_across_read_and_clear(self):
        """The atomicity claim, asserted where it is decidable.

        A ``get_all()`` + ``clear()`` implementation releases the lock between
        the read and the removal. Holding the lock from the test thread and
        observing that the drain cannot proceed is what pins the read and the
        removal into ONE scope: an implementation whose read ran outside the
        lock would have returned by the probe.
        """
        # Given
        buffer: RingBuffer[int] = RingBuffer(capacity=10)
        for i in range(3):
            buffer.put(i)

        result: list[list[int]] = []
        released = threading.Event()

        def _drain() -> None:
            result.append(buffer.drain_all())

        # When — the lock is held while a drain is attempted
        buffer._lock.acquire()
        drainer = threading.Thread(target=_drain)
        drainer.start()
        try:
            drainer.join(timeout=_STILL_BLOCKED_PROBE_SECONDS)

            # Then — the drain is still waiting on the lock
            assert drainer.is_alive(), (
                "drain_all completed while the buffer lock was held — the read "
                "and the removal are not in one lock scope"
            )
        finally:
            buffer._lock.release()
            released.set()

        drainer.join(timeout=_JOIN_TIMEOUT_SECONDS)
        assert not drainer.is_alive()
        assert result == [[0, 1, 2]]
        assert buffer.size == 0

    def test_a_put_racing_a_drain_is_drained_or_retained_never_lost(self):
        """Conservation under concurrent producers.

        Every item a producer reports as accepted must be returned by some
        drain or still be in the buffer at the end. A correct implementation
        can never lose one; an implementation that clears outside the read's
        lock scope loses exactly the items that land in the window.
        """
        # Given — capacity well above the item count so no backpressure drop
        # can explain a missing item
        buffer: RingBuffer[int] = RingBuffer(
            capacity=10_000, strategy=BackpressureStrategy.DROP_OLDEST
        )
        n_producers = 4
        per_producer = 250
        accepted: list[list[int]] = [[] for _ in range(n_producers)]
        drained: list[int] = []
        stop = threading.Event()
        start = threading.Barrier(n_producers + 1)

        def _produce(worker_id: int) -> None:
            start.wait(timeout=_HANDOFF_TIMEOUT_SECONDS)
            for i in range(per_producer):
                item = worker_id * per_producer + i
                if buffer.put(item):
                    accepted[worker_id].append(item)

        def _drain() -> None:
            start.wait(timeout=_HANDOFF_TIMEOUT_SECONDS)
            while not stop.is_set():
                drained.extend(buffer.drain_all())

        producers = [
            threading.Thread(target=_produce, args=(w,)) for w in range(n_producers)
        ]
        drainer = threading.Thread(target=_drain)

        # When
        drainer.start()
        for p in producers:
            p.start()
        for p in producers:
            p.join(timeout=_JOIN_TIMEOUT_SECONDS)
        stop.set()
        drainer.join(timeout=_JOIN_TIMEOUT_SECONDS)
        drained.extend(buffer.drain_all())

        # Then — every accepted item was handed to a drain exactly once
        expected = {item for items in accepted for item in items}
        assert len(drained) == len(set(drained)), "an item was handed over twice"
        assert set(drained) == expected
