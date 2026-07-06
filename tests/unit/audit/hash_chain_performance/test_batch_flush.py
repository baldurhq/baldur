"""
Tests for BatchFlushWriter.

Batched file writes with reduced fsync.
"""

import tempfile
from pathlib import Path


class TestBatchFlushWriter:
    """Tests for BatchFlushWriter."""

    def test_write_buffers_entries(self):
        """Test that entries are buffered."""
        from baldur.audit.performance import (
            BatchFlushConfig,
            BatchFlushWriter,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            config = BatchFlushConfig(batch_size=10, flush_interval_seconds=60)
            writer = BatchFlushWriter(path, config)

            # Write less than batch size
            for i in range(5):
                writer.write({"id": i})

            # Should still be buffered
            assert len(writer._buffer) == 5
            assert not path.exists()

            writer.close()

    def test_flush_on_batch_size(self):
        """Test automatic flush when batch size reached."""
        from baldur.audit.performance import (
            BatchFlushConfig,
            BatchFlushWriter,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            config = BatchFlushConfig(batch_size=5, sync_on_flush=False)
            writer = BatchFlushWriter(path, config)

            # Write exactly batch size
            for i in range(5):
                writer.write({"id": i})

            # Should have flushed
            assert len(writer._buffer) == 0
            assert path.exists()

            # Verify content
            with open(path) as f:
                lines = f.readlines()
            assert len(lines) == 5

            writer.close()

    def test_force_flush(self):
        """Test force flush."""
        from baldur.audit.performance import (
            BatchFlushConfig,
            BatchFlushWriter,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            writer = BatchFlushWriter(path, BatchFlushConfig(batch_size=100))

            writer.write({"id": 1})
            writer.write({"id": 2})

            assert len(writer._buffer) == 2

            writer.force_flush()

            assert len(writer._buffer) == 0
            assert path.exists()

            writer.close()

    def test_get_stats(self):
        """Test statistics retrieval."""
        from baldur.audit.performance import (
            BatchFlushConfig,
            BatchFlushWriter,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            config = BatchFlushConfig(batch_size=2, sync_on_flush=False)
            writer = BatchFlushWriter(path, config)

            for i in range(5):
                writer.write({"id": i})

            stats = writer.get_stats()

            assert stats["entries_written"] == 4  # 2 flushes of 2
            assert stats["flushes_performed"] == 2
            assert stats["buffer_size"] == 1  # 1 remaining

            writer.close()
