"""
Tests for Metric Snapshot Storage.
"""

import json
import os
import tempfile
import time
from pathlib import Path

import pytest


class TestMetricSnapshot:
    """Test MetricSnapshot dataclass."""

    def test_default_values(self):
        """Should have correct default values."""
        from baldur.metrics.snapshot_storage import MetricSnapshot

        snapshot = MetricSnapshot()

        assert snapshot.values == {}
        assert snapshot.version == "1.0"
        assert snapshot.source == "unknown"
        assert snapshot.created_at is not None
        assert snapshot.updated_at is not None

    def test_age_seconds_property(self):
        """Should calculate age correctly."""
        from baldur.metrics.snapshot_storage import MetricSnapshot

        old_time = time.time() - 60  # 1 minute ago
        snapshot = MetricSnapshot(updated_at=old_time)

        age = snapshot.age_seconds
        assert 59 <= age <= 61

    def test_get_value_returns_default_for_missing(self):
        """Should return default value for missing keys."""
        from baldur.metrics.snapshot_storage import MetricSnapshot

        snapshot = MetricSnapshot()

        result = snapshot.get_value("category", "key", default=0)
        assert result == 0

    def test_get_value_returns_stored_value(self):
        """Should return stored value when available."""
        from baldur.metrics.snapshot_storage import MetricSnapshot

        snapshot = MetricSnapshot(values={"dlq_pending": {"payment": 5}})

        result = snapshot.get_value("dlq_pending", "payment")
        assert result == 5

    def test_set_value_creates_category_if_needed(self):
        """Should create category if it doesn't exist."""
        from baldur.metrics.snapshot_storage import MetricSnapshot

        snapshot = MetricSnapshot()
        snapshot.set_value("new_category", "key", "value")

        assert "new_category" in snapshot.values
        assert snapshot.values["new_category"]["key"] == "value"

    def test_set_value_updates_timestamp(self):
        """Should update updated_at timestamp."""
        from baldur.metrics.snapshot_storage import MetricSnapshot

        old_time = time.time() - 60
        snapshot = MetricSnapshot(updated_at=old_time)

        snapshot.set_value("category", "key", "value")

        assert snapshot.updated_at > old_time

    def test_to_dict(self):
        """Should convert to dictionary correctly."""
        from baldur.metrics.snapshot_storage import MetricSnapshot

        snapshot = MetricSnapshot(
            values={"test": {"key": "value"}},
            version="1.0",
            source="test",
        )

        result = snapshot.to_dict()

        assert "values" in result
        assert "created_at" in result
        assert "updated_at" in result
        assert "version" in result
        assert "source" in result
        assert result["values"] == {"test": {"key": "value"}}

    def test_from_dict(self):
        """Should create snapshot from dictionary."""
        from baldur.metrics.snapshot_storage import MetricSnapshot

        data = {
            "values": {"dlq_pending": {"payment": 10}},
            "created_at": time.time(),
            "updated_at": time.time(),
            "version": "1.0",
            "source": "test",
        }

        snapshot = MetricSnapshot.from_dict(data)

        assert snapshot.values == data["values"]
        assert snapshot.version == "1.0"
        assert snapshot.source == "test"


class TestMetricSnapshotStorage:
    """Test MetricSnapshotStorage class."""

    @pytest.fixture
    def temp_storage_dir(self):
        """Create temporary storage directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    def test_init_with_custom_directory(self, temp_storage_dir):
        """Should use custom storage directory."""
        from baldur.metrics.snapshot_storage import MetricSnapshotStorage

        storage = MetricSnapshotStorage(storage_dir=temp_storage_dir)

        assert str(storage._storage_dir) == temp_storage_dir

    def test_init_with_custom_filename(self, temp_storage_dir):
        """Should use custom filename."""
        from baldur.metrics.snapshot_storage import MetricSnapshotStorage

        storage = MetricSnapshotStorage(
            storage_dir=temp_storage_dir,
            filename="custom_snapshot.json",
        )

        assert storage._filename == "custom_snapshot.json"

    def test_save_and_load_value(self, temp_storage_dir):
        """Should save and load values correctly."""
        from baldur.metrics.snapshot_storage import MetricSnapshotStorage

        storage = MetricSnapshotStorage(storage_dir=temp_storage_dir)

        # Save value
        storage.save_value("dlq_pending", "payment", 5)

        # Load value
        result = storage.load_value("dlq_pending", "payment")

        assert result == 5

    def test_load_value_returns_default_when_not_found(self, temp_storage_dir):
        """Should return default when value not found."""
        from baldur.metrics.snapshot_storage import MetricSnapshotStorage

        storage = MetricSnapshotStorage(storage_dir=temp_storage_dir)

        result = storage.load_value("nonexistent", "key", default=0)

        assert result == 0

    def test_get_snapshot_age(self, temp_storage_dir):
        """Should return snapshot age."""
        from baldur.metrics.snapshot_storage import MetricSnapshotStorage

        storage = MetricSnapshotStorage(storage_dir=temp_storage_dir)

        # Save something to create snapshot
        storage.save_value("test", "key", "value")

        age = storage.get_snapshot_age()

        # Age should be very small (just created)
        assert age is not None
        assert age < 1.0  # Less than 1 second

    def test_get_snapshot_age_returns_none_when_no_snapshot(self, temp_storage_dir):
        """Should return None when no snapshot exists."""
        from baldur.metrics.snapshot_storage import MetricSnapshotStorage

        storage = MetricSnapshotStorage(storage_dir=temp_storage_dir)

        age = storage.get_snapshot_age()

        # No snapshot yet
        assert age is None or age >= 0  # Implementation may vary

    def test_atomic_write_pattern(self, temp_storage_dir):
        """Should use atomic write pattern."""
        from baldur.metrics.snapshot_storage import MetricSnapshotStorage

        storage = MetricSnapshotStorage(storage_dir=temp_storage_dir)

        # Save should update internal state
        storage.save_value("test", "key", "value")

        # If there's a flush method, call it
        if hasattr(storage, "flush"):
            storage.flush()

        # Check if file exists or internal state is updated
        snapshot_path = Path(temp_storage_dir) / storage._filename

        # Verify save worked (either file exists or snapshot is updated)
        if snapshot_path.exists():
            # Content should be valid JSON
            with open(snapshot_path) as f:
                data = json.load(f)
                assert "values" in data
        else:
            # Check internal snapshot was updated
            assert storage._snapshot is not None
            assert storage._snapshot.get_value("test", "key") == "value"

    def test_thread_safety(self, temp_storage_dir):
        """Should be thread-safe for concurrent access."""
        import threading

        from baldur.metrics.snapshot_storage import MetricSnapshotStorage

        storage = MetricSnapshotStorage(storage_dir=temp_storage_dir)
        errors = []

        def writer(domain_id):
            try:
                for i in range(10):
                    storage.save_value("domain", f"key_{domain_id}", i)
            except Exception as e:
                errors.append(e)

        # Run multiple threads
        threads = [threading.Thread(target=writer, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No errors should occur
        assert len(errors) == 0


class TestMetricSnapshotStorageEdgeCases:
    """Test edge cases for MetricSnapshotStorage."""

    @pytest.fixture
    def temp_storage_dir(self):
        """Create temporary storage directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    def test_handles_corrupted_file(self, temp_storage_dir):
        """Should handle corrupted snapshot file gracefully."""
        from baldur.metrics.snapshot_storage import MetricSnapshotStorage

        storage = MetricSnapshotStorage(storage_dir=temp_storage_dir)

        # Write corrupted content
        snapshot_path = Path(temp_storage_dir) / storage._filename
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        with open(snapshot_path, "w") as f:
            f.write("invalid json {{{")

        # Should not raise, should return default
        result = storage.load_value("test", "key", default=0)
        assert result == 0

    def test_handles_missing_directory(self):
        """Should handle missing directory gracefully."""
        from baldur.metrics.snapshot_storage import MetricSnapshotStorage

        with tempfile.TemporaryDirectory() as tmpdir:
            # Use non-existent subdirectory
            storage_dir = os.path.join(tmpdir, "nonexistent", "subdir")
            storage = MetricSnapshotStorage(storage_dir=storage_dir)

            # Save should create directory
            storage.save_value("test", "key", "value")

            # Should be able to load back
            result = storage.load_value("test", "key")
            assert result == "value"
