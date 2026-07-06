"""
Unit tests for composite_storage.py fix(356) — threading lock for save/load.

Tests:
E. Concurrent save/load operations are serialized by _lock.
F. CheckpointStrategyRegistry.set_default() is protected by _lock.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from baldur.audit.checkpoint.composite_storage import CompositeCheckpointStorage
from baldur.audit.checkpoint.strategy import (
    CheckpointStorageStrategy,
    UnifiedCheckpointData,
)


def _make_data(seq: int = 1) -> UnifiedCheckpointData:
    """Create a test UnifiedCheckpointData."""
    return UnifiedCheckpointData(wal_sequence=seq)


class _CountingStorage(CheckpointStorageStrategy):
    """Storage that counts operations for concurrency verification."""

    def __init__(self):
        super().__init__()
        self._data: dict[str, UnifiedCheckpointData] = {}
        self.save_count = 0
        self._save_lock = threading.Lock()

    def save(self, namespace: str, data: UnifiedCheckpointData) -> None:
        with self._save_lock:
            self._data[namespace] = data
            self.save_count += 1

    def load(self, namespace: str) -> UnifiedCheckpointData | None:
        return self._data.get(namespace)

    def commit(self, namespace: str) -> None:
        pass

    def delete(self, namespace: str) -> bool:
        return self._data.pop(namespace, None) is not None

    def exists(self, namespace: str) -> bool:
        return namespace in self._data


class TestCompositeStorageConcurrentSaveBehavior:
    """Concurrent save operations must be serialized to prevent stats corruption."""

    def test_concurrent_saves_produce_correct_stats(self) -> None:
        """Stats counters remain consistent under concurrent save."""
        primary = _CountingStorage()
        composite = CompositeCheckpointStorage(primary=primary)

        num_threads = 20
        barrier = threading.Barrier(num_threads)
        errors: list[Exception] = []

        def save_worker(idx: int):
            try:
                barrier.wait(timeout=5)
                composite.save(f"ns-{idx}", _make_data(idx))
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=save_worker, args=(i,)) for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Threads raised errors: {errors}"
        stats = composite.get_stats()
        assert stats["primary_writes"] == num_threads
        assert primary.save_count == num_threads

    def test_concurrent_save_load_does_not_corrupt(self) -> None:
        """Interleaved save and load calls do not corrupt data."""
        primary = _CountingStorage()
        composite = CompositeCheckpointStorage(primary=primary)

        # Pre-populate some data
        for i in range(5):
            composite.save(f"ns-{i}", _make_data(i))

        errors: list[Exception] = []
        barrier = threading.Barrier(10)

        def writer(idx: int):
            try:
                barrier.wait(timeout=5)
                composite.save(f"ns-{idx}", _make_data(idx + 100))
            except Exception as e:
                errors.append(e)

        def reader(idx: int):
            try:
                barrier.wait(timeout=5)
                composite.load(f"ns-{idx}")
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(5):
            threads.append(threading.Thread(target=writer, args=(i,)))
            threads.append(threading.Thread(target=reader, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors

    def test_fallback_stats_consistent_under_concurrency(self) -> None:
        """Fallback event counter is correct when primary fails concurrently."""
        failing_primary = MagicMock(spec=CheckpointStorageStrategy)
        failing_primary.save.side_effect = ConnectionError("redis down")

        secondary = _CountingStorage()
        composite = CompositeCheckpointStorage(
            primary=failing_primary,
            secondary=secondary,
        )

        num_threads = 10
        barrier = threading.Barrier(num_threads)
        errors: list[Exception] = []

        def save_worker(idx: int):
            try:
                barrier.wait(timeout=5)
                composite.save(f"ns-{idx}", _make_data(idx))
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=save_worker, args=(i,)) for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors
        stats = composite.get_stats()
        assert stats["fallback_events"] == num_threads
        assert stats["secondary_writes"] == num_threads


class TestCheckpointRegistrySetDefaultBehavior:
    """CheckpointStrategyRegistry.set_default() uses lock for thread safety."""

    def test_concurrent_set_default_does_not_corrupt(self) -> None:
        """Multiple threads setting default do not corrupt the value."""
        from baldur.audit.checkpoint import CheckpointStrategyRegistry

        original_default = CheckpointStrategyRegistry._default

        try:
            errors: list[Exception] = []
            barrier = threading.Barrier(10)

            def set_worker(name: str):
                try:
                    barrier.wait(timeout=5)
                    CheckpointStrategyRegistry.set_default(name)
                except Exception as e:
                    errors.append(e)

            threads = [
                threading.Thread(target=set_worker, args=(f"strategy-{i}",))
                for i in range(10)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            assert not errors
            # After all threads, _default should be one of the set values
            assert CheckpointStrategyRegistry._default.startswith("strategy-")
        finally:
            CheckpointStrategyRegistry._default = original_default
