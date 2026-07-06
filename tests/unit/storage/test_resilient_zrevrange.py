"""
Unit tests for ResilientStorageBackend.zrevrange() (541 D9).

zrevrange mirrors zrange but in descending score order. It backs every
newest-first DLQ find() read (global / by_domain / per-status indexes are all
created_at-scored, so a DESC range == created_at DESC).

Coverage:
- Redis mode: delegates to the raw client's zrevrange + bytes-decode; falls to
  degraded mode on error.
- Degraded mode: reverses the score-ascending in-memory list, then applies the
  same start:end slice convention as zrange (boundary: end=-1 full range,
  partial windows).
- Parity: degraded zrevrange order is the exact reverse of zrange order.
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
    """Yield a backend whose lazy Redis probe resolves to ``mock_redis``.

    Redis connection is lazy (first op triggers ``_ensure_redis``), so the
    RedisCacheAdapter patch must stay active for the whole test body — not
    just construction.
    """
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


class TestZrevrangeBehavior:
    """Redis-mode delegation + degraded reverse-slice."""

    def test_redis_mode_delegates_and_decodes_bytes(self, temp_wal_dir, mock_redis):
        """REDIS branch calls raw zrevrange on the full key and decodes bytes."""
        from baldur.adapters.resilient.backend import ResilientStorageMode

        mock_redis.zrevrange.return_value = [b"3", b"2", b"1"]
        with redis_backend(temp_wal_dir, mock_redis) as backend:
            result = backend.zrevrange("idx", 0, -1)

            assert result == ["3", "2", "1"]
            assert backend.mode == ResilientStorageMode.REDIS
            # key_prefix default is "baldur:" — full key passed to raw client.
            mock_redis.zrevrange.assert_called_once_with("baldur:idx", 0, -1)

    def test_redis_mode_passes_already_str_members_through(
        self, temp_wal_dir, mock_redis
    ):
        """Non-bytes members from the raw client are returned unchanged."""
        mock_redis.zrevrange.return_value = ["b", "a"]
        with redis_backend(temp_wal_dir, mock_redis) as backend:
            assert backend.zrevrange("idx", 0, 1) == ["b", "a"]

    def test_redis_error_switches_to_degraded(self, temp_wal_dir, mock_redis):
        """A raw-client failure trips _switch_to_degraded (fail-safe)."""
        mock_redis.zrevrange.side_effect = Exception("connection reset")
        with redis_backend(temp_wal_dir, mock_redis) as backend:
            # No in-memory data, so the degraded fallback returns [].
            result = backend.zrevrange("idx", 0, -1)

            assert result == []
            assert backend.is_degraded is True

    def test_degraded_returns_descending_score_order(self, backend_memory_only):
        """Degraded zrevrange yields highest-score-first."""
        backend = backend_memory_only
        backend.zadd("idx", {"low": 1.0, "mid": 2.0, "high": 3.0})

        assert backend.zrevrange("idx", 0, -1) == ["high", "mid", "low"]

    def test_degraded_is_exact_reverse_of_zrange(self, backend_memory_only):
        """Parity: zrevrange full range == reversed zrange full range."""
        backend = backend_memory_only
        backend.zadd("idx", {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0})

        ascending = backend.zrange("idx", 0, -1)
        descending = backend.zrevrange("idx", 0, -1)

        assert descending == list(reversed(ascending))

    def test_degraded_partial_window_slice(self, backend_memory_only):
        """start/end index a window of the descending order."""
        backend = backend_memory_only
        backend.zadd("idx", {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 5.0})

        # Descending: e, d, c, b, a — window [1, 2] is d, c.
        assert backend.zrevrange("idx", 1, 2) == ["d", "c"]

    def test_degraded_empty_key_returns_empty(self, backend_memory_only):
        """Unknown key returns an empty list, not an error."""
        assert backend_memory_only.zrevrange("missing", 0, -1) == []
