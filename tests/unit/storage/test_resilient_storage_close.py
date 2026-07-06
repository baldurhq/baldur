"""Unit tests for ``ResilientStorageBackend.close()`` (463 D16).

Source: ``src/baldur/adapters/resilient/backend.py``

Covers:

- ``close()`` invokes ``_wal.close()`` AND ``_redis.close()`` so the
  cache adapter's connection pool drains during the
  ``reset_storage_backend(cleanup=True)`` test-fixture chain.
- Order: WAL close runs before Redis close. WAL is the higher-priority
  resource (forensic durability); Redis is the I/O cleanup.
- Idempotency: ``close()`` works when ``_redis is None`` (memory-only
  mode entered via Redis init failure).
- ``close()`` works when ``_wal is None`` (WAL init failed and adapter
  fell through to memory).

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.5 Dependency interaction (call counts on _wal.close / _redis.close).
- §8.2 Exception/edge cases (None resources, no AttributeError).
"""

from __future__ import annotations

import tempfile
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def temp_wal_dir():
    """Per-test isolated WAL directory."""
    with tempfile.TemporaryDirectory() as d:
        yield d


def _build_backend(temp_wal_dir, *, wal=None, redis=None):
    """Construct a ``ResilientStorageBackend`` with controllable internals.

    Bypasses the real WAL / Redis init by pre-populating the slots after
    construction. The constructor still runs ``_init_components`` against
    ``temp_wal_dir`` (a writable path), but we overwrite ``_wal`` /
    ``_redis`` with mocks so the close path is exercised against
    deterministic stand-ins.
    """
    from baldur.adapters.resilient.backend import ResilientStorageBackend
    from baldur.settings.resilient_storage import ResilientStorageSettings

    config = ResilientStorageSettings(
        redis_url="redis://test-only:6379/0",
        wal_dir=temp_wal_dir,
    )
    backend = ResilientStorageBackend(config)
    backend._wal = wal
    backend._redis = redis
    return backend


class TestResilientStorageBackendCloseBehavior:
    """``close()`` chains WAL close then Redis cache adapter close."""

    def test_close_invokes_both_wal_and_redis_close(self, temp_wal_dir):
        """Both ``_wal.close()`` and ``_redis.close()`` run on close."""
        wal = MagicMock()
        redis = MagicMock()
        backend = _build_backend(temp_wal_dir, wal=wal, redis=redis)

        backend.close()

        wal.close.assert_called_once_with()
        redis.close.assert_called_once_with()

    def test_close_runs_wal_before_redis(self, temp_wal_dir):
        """Order matters: WAL must drain before the Redis pool tear-down."""
        order: list[str] = []

        wal = MagicMock()
        wal.close.side_effect = lambda: order.append("wal")

        redis = MagicMock()
        redis.close.side_effect = lambda: order.append("redis")

        backend = _build_backend(temp_wal_dir, wal=wal, redis=redis)
        backend.close()

        assert order == ["wal", "redis"]

    def test_close_skips_redis_when_none(self, temp_wal_dir):
        """``_redis is None`` (memory-only mode) → WAL closes, no AttributeError."""
        wal = MagicMock()
        backend = _build_backend(temp_wal_dir, wal=wal, redis=None)

        # Must not raise.
        backend.close()

        wal.close.assert_called_once_with()

    def test_close_skips_wal_when_none(self, temp_wal_dir):
        """``_wal is None`` → Redis still closes, no AttributeError."""
        redis = MagicMock()
        backend = _build_backend(temp_wal_dir, wal=None, redis=redis)

        # Must not raise.
        backend.close()

        redis.close.assert_called_once_with()

    def test_close_with_both_none_is_noop(self, temp_wal_dir):
        """Both resources absent → close is a silent no-op (no crash)."""
        backend = _build_backend(temp_wal_dir, wal=None, redis=None)

        # Must not raise.
        backend.close()
