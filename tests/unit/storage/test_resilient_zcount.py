"""Unit tests for ResilientStorageBackend.zcount() (622 D3).

zcount returns the number of sorted-set members whose score is in the INCLUSIVE
[min_score, max_score] range. It backs the Error Budget DLQ windowed inflow
count (a ZCOUNT over the created_at-scored ``dlq:all`` index).

Coverage:
- Redis mode: delegates to the raw client's zcount on the prefixed key; a
  raw-client failure trips _switch_to_degraded (fail-safe).
- Degraded mode: counts in-memory members within the inclusive score range
  (boundary: members exactly at min/max, empty range, unknown key).
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def temp_wal_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def backend_memory_only(temp_wal_dir):
    """Backend forced into degraded (memory-only) mode."""
    from baldur.adapters.resilient.backend import (
        ResilientStorageBackend,
        reset_storage_backend,
    )
    from baldur.settings.resilient_storage import ResilientStorageSettings

    reset_storage_backend()
    config = ResilientStorageSettings(
        redis_url="redis://nonexistent:6379/0",
        wal_dir=temp_wal_dir,
        allow_memory_only=True,
    )
    with patch("baldur.adapters.cache.RedisCacheAdapter") as MockAdapter:
        MockAdapter.side_effect = Exception("Redis unavailable")
        backend = ResilientStorageBackend(config)
    yield backend
    backend.close()
    reset_storage_backend()


@pytest.fixture
def mock_redis():
    mock = MagicMock()
    mock.ping.return_value = True
    return mock


@contextmanager
def redis_backend(temp_wal_dir, mock_redis):
    """Yield a backend whose lazy Redis probe resolves to ``mock_redis``."""
    import baldur.adapters.redis as _redis_mod
    from baldur.adapters.resilient.backend import (
        ResilientStorageBackend,
        reset_storage_backend,
    )
    from baldur.settings.resilient_storage import ResilientStorageSettings

    reset_storage_backend()
    config = ResilientStorageSettings(wal_dir=temp_wal_dir)
    _redis_mod._redis_state().unavailable = False

    with patch("baldur.adapters.cache.RedisCacheAdapter") as MockAdapter:
        mock_instance = MagicMock()
        mock_instance._redis = mock_redis
        mock_instance.raw_client = mock_redis
        MockAdapter.return_value = mock_instance
        backend = ResilientStorageBackend(config)
        try:
            yield backend
        finally:
            backend.close()
            reset_storage_backend()


class TestResilientBackendZcountRedisMode:
    """REDIS-branch delegation + degraded fall-through."""

    def test_redis_mode_delegates_on_prefixed_key(self, temp_wal_dir, mock_redis):
        """REDIS branch calls raw zcount on the full (prefixed) key."""
        from baldur.adapters.resilient.backend import ResilientStorageMode

        mock_redis.zcount.return_value = 4
        with redis_backend(temp_wal_dir, mock_redis) as backend:
            result = backend.zcount("idx", 100.0, 200.0)

            assert result == 4
            assert backend.mode == ResilientStorageMode.REDIS
            # key_prefix default is "baldur:" — full key passed to raw client.
            mock_redis.zcount.assert_called_once_with("baldur:idx", 100.0, 200.0)

    def test_redis_error_switches_to_degraded(self, temp_wal_dir, mock_redis):
        """A raw-client failure trips _switch_to_degraded (fail-safe)."""
        mock_redis.zcount.side_effect = Exception("connection reset")
        with redis_backend(temp_wal_dir, mock_redis) as backend:
            # No in-memory data, so the degraded fallback returns 0.
            result = backend.zcount("idx", 0.0, 1.0)

            assert result == 0
            assert backend.is_degraded is True


class TestResilientBackendZcountDegradedMode:
    """Degraded in-memory inclusive-range counting + boundaries."""

    def test_counts_members_within_inclusive_range(self, backend_memory_only):
        """Members with min <= score <= max are counted."""
        backend = backend_memory_only
        backend.zadd("idx", {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 5.0})

        # Scores 2, 3, 4 fall in [2, 4].
        assert backend.zcount("idx", 2.0, 4.0) == 3

    def test_includes_members_exactly_at_boundaries(self, backend_memory_only):
        """Both the min and max boundaries are inclusive."""
        backend = backend_memory_only
        backend.zadd("idx", {"lo": 10.0, "mid": 15.0, "hi": 20.0})

        # [10, 20] includes lo (==min) and hi (==max).
        assert backend.zcount("idx", 10.0, 20.0) == 3

    def test_excludes_members_outside_range(self, backend_memory_only):
        """Members just outside the range are not counted."""
        backend = backend_memory_only
        backend.zadd("idx", {"a": 1.0, "b": 2.0, "c": 3.0})

        # Only score 2.0 lies within (1.0, 3.0) exclusive bounds [1.5, 2.5].
        assert backend.zcount("idx", 1.5, 2.5) == 1

    def test_empty_range_returns_zero(self, backend_memory_only):
        """A range containing no member scores returns zero."""
        backend = backend_memory_only
        backend.zadd("idx", {"a": 1.0, "b": 2.0})

        assert backend.zcount("idx", 100.0, 200.0) == 0

    def test_unknown_key_returns_zero(self, backend_memory_only):
        """An unknown key returns zero, not an error."""
        assert backend_memory_only.zcount("missing", 0.0, 100.0) == 0

    def test_single_point_range_counts_exact_score(self, backend_memory_only):
        """A degenerate [s, s] range counts members at exactly that score."""
        backend = backend_memory_only
        backend.zadd("idx", {"a": 1.0, "b": 2.0, "c": 2.0})

        assert backend.zcount("idx", 2.0, 2.0) == 2
