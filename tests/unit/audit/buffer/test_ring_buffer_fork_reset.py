"""``RingBuffer.reset_after_fork()`` — re-owning a buffer inherited across fork.

Two halves, both load-bearing:

- The lock is renewed. The forking process holds it for the duration of every
  ``put``/``get_batch``, so a fork taken at that instant hands the child a lock
  whose owner does not exist here and which is therefore never released.
- The contents are abandoned. They belong to the forking process, whose own
  drainer is still delivering them; draining the copies here would write one
  duplicate downstream per child, with nothing to dedup them.

The reset works in place — object identity is preserved so producers and the
drainer that already hold a reference keep pointing at the buffer this process
now owns.
"""

from __future__ import annotations

import threading

from baldur.audit.ring_buffer import RingBuffer, RingBufferStats


class TestRingBufferForkResetBehavior:
    """The child's buffer restarts empty, unlocked, and with its own counters."""

    def test_inherited_contents_are_abandoned(self):
        """The forking process's drainer owns those entries; this one must not."""
        buffer: RingBuffer[int] = RingBuffer(capacity=100)
        for item in range(10):
            buffer.put(item)
        assert buffer.size == 10

        buffer.reset_after_fork()

        assert buffer.size == 0
        assert buffer.is_empty is True

    def test_counters_restart_with_the_contents(self):
        """A rate accumulated before the fork does not describe this process."""
        buffer: RingBuffer[int] = RingBuffer(capacity=5)
        for item in range(50):
            buffer.put(item)
        assert buffer.get_stats().total_enqueued == 50
        assert buffer.get_stats().total_dropped > 0

        buffer.reset_after_fork()

        stats = buffer.get_stats()
        assert stats.total_enqueued == 0
        assert stats.total_dropped == 0
        assert stats.drop_rate == 0.0

    def test_capacity_survives_the_reset(self):
        """The child gets its own contents, not its own configuration."""
        buffer: RingBuffer[int] = RingBuffer(capacity=7)
        for item in range(20):
            buffer.put(item)

        buffer.reset_after_fork()

        assert buffer.get_stats().capacity == 7
        for item in range(20):
            buffer.put(item)
        assert buffer.size == 7

    def test_lock_is_replaced_rather_than_reused(self):
        """A lock inherited *held* is never released — acquiring it first would
        be exactly the deadlock this repairs."""
        buffer: RingBuffer[int] = RingBuffer(capacity=10)
        inherited_lock = buffer._lock

        buffer.reset_after_fork()

        assert buffer._lock is not inherited_lock

    def test_reset_succeeds_while_the_inherited_lock_is_held(self):
        """The reset is deliberately not lock-guarded, and this is why.

        A fork taken mid-``put`` hands the child a locked lock. The repair runs
        single-threaded in a fresh child, so it replaces the lock instead of
        waiting on one nothing will release.
        """
        # Given — the inherited lock is held, as a mid-``put`` fork leaves it
        buffer: RingBuffer[int] = RingBuffer(capacity=10)
        buffer.put(1)
        buffer._lock.acquire()

        # When / Then — the reset returns instead of deadlocking
        buffer.reset_after_fork()

        assert buffer.size == 0
        assert buffer._lock.acquire(blocking=False) is True
        buffer._lock.release()

    def test_buffer_is_usable_through_the_same_reference_after_the_reset(self):
        """Identity is preserved: producers already hold this object."""
        buffer: RingBuffer[int] = RingBuffer(capacity=10)
        producer_reference = buffer
        for item in range(10):
            buffer.put(item)

        buffer.reset_after_fork()
        producer_reference.put(99)

        assert buffer.size == 1
        assert buffer.get_batch(max_size=10) == [99]

    def test_drop_rate_alert_can_fire_again_in_the_child(self):
        """The alert latch belongs to the parent's traffic, so it restarts too.

        Left set, a child that starts dropping would never report it — the one
        operational signal the buffer has.
        """
        alerts: list[RingBufferStats] = []
        buffer: RingBuffer[int] = RingBuffer(
            capacity=10, on_drop_threshold=alerts.append, drop_rate_threshold=0.1
        )
        for item in range(150):
            buffer.put(item)
        assert len(alerts) == 1

        buffer.reset_after_fork()
        for item in range(150):
            buffer.put(item)

        assert len(alerts) == 2

    def test_reset_is_safe_to_repeat(self):
        """Both outbox halves reach it; a second call must not corrupt state."""
        buffer: RingBuffer[int] = RingBuffer(capacity=10)
        buffer.put(1)

        buffer.reset_after_fork()
        buffer.put(2)
        buffer.reset_after_fork()

        assert buffer.size == 0
        assert buffer.get_stats().total_enqueued == 0

    def test_renewed_lock_still_serializes_concurrent_producers(self):
        """The replacement is a real lock, not a discarded guard."""
        buffer: RingBuffer[int] = RingBuffer(capacity=1000)
        buffer.reset_after_fork()

        def _produce(base: int) -> None:
            for offset in range(50):
                buffer.put(base + offset)

        threads = [
            threading.Thread(target=_produce, args=(worker * 100,))
            for worker in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)

        assert buffer.get_stats().total_enqueued == 200
        assert buffer.size == 200
