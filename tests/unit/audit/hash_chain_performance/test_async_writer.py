"""
Tests for AsyncAuditWriter.

Non-blocking async write queue.
"""

import time


class TestAsyncAuditWriter:
    """Tests for AsyncAuditWriter."""

    def test_async_write_queues_entry(self):
        """Test that entries are queued."""
        from baldur.audit.performance import AsyncAuditWriter

        written = []

        def sync_writer(entry):
            written.append(entry)
            return True

        writer = AsyncAuditWriter(sync_writer, batch_size=1)
        writer.start()

        try:
            writer.write_async({"id": 1})
            writer.write_async({"id": 2})

            # Give time for async processing
            time.sleep(0.5)

            assert len(written) >= 1
        finally:
            writer.stop()

    def test_queue_full_handling(self):
        """Test handling when queue is full."""
        from baldur.audit.performance import AsyncAuditWriter

        def slow_writer(entry):
            time.sleep(0.1)
            return True

        writer = AsyncAuditWriter(slow_writer, max_queue_size=2)
        # Don't start - queue will fill up

        # Fill queue
        assert writer.write_async({"id": 1}) is True
        assert writer.write_async({"id": 2}) is True

        # Should fail (queue full, not blocking)
        result = writer.write_async({"id": 3}, block=False)
        assert result is False

        stats = writer.get_stats()
        assert stats["dropped"] >= 1

    def test_stats_tracking(self):
        """Test that statistics are tracked."""
        from baldur.audit.performance import AsyncAuditWriter

        written_count = 0

        def sync_writer(entry):
            nonlocal written_count
            written_count += 1
            return True

        writer = AsyncAuditWriter(sync_writer, batch_size=1)
        writer.start()

        try:
            for i in range(5):
                writer.write_async({"id": i})

            time.sleep(0.5)

            stats = writer.get_stats()
            assert stats["queued"] == 5
            assert stats["written"] >= 1
            assert stats["is_running"] is True
        finally:
            writer.stop()
