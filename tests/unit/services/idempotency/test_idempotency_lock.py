"""
IdempotencyService Lock Implementation Tests (390 DD-5).

Tests for:
A. IdempotencyService.acquire_lock() — lock acquisition behavior
B. IdempotencyService.release_lock() — lock release behavior

Note (532 D1): The historical ``_NoopCache`` / ``_NoopLock`` classes were
deleted when the service-layer ``_get_cache()`` silent-NoopCache fallback
was replaced by the shared ``InMemoryCacheAdapter`` resolver. The lock
behavior at the service layer is unchanged — it still delegates to
``cache.get_lock()``; only the fallback adapter identity changed. These
tests now exercise the service-layer contract directly via injected mocks.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from baldur.services.idempotency.models import (
    IdempotencyDomain,
    IdempotencyKey,
)
from baldur.services.idempotency.service import IdempotencyService


def _make_key(key: str = "zombie_rollback:exp-001") -> IdempotencyKey:
    return IdempotencyKey(
        domain=IdempotencyDomain.CHAOS_ZOMBIE_HUNTER,
        key=key,
        components={"experiment_id": "exp-001"},
    )


def _make_service_with_succeeding_lock_cache() -> tuple[
    IdempotencyService, MagicMock, MagicMock
]:
    """Inject a MagicMock cache whose ``get_lock()`` returns an always-acquire
    lock — emulates the previous _NoopCache/_NoopLock fixture without
    depending on classes deleted by 532 D1."""
    mock_cache = MagicMock()
    mock_lock = MagicMock()
    mock_lock.acquire.return_value = True
    mock_cache.get_lock.return_value = mock_lock

    service = IdempotencyService()
    service._cache = mock_cache
    return service, mock_cache, mock_lock


# =============================================================================
# A. IdempotencyService.acquire_lock()
# =============================================================================


class TestAcquireLockBehavior:
    """IdempotencyService.acquire_lock() behavior verification."""

    def test_acquire_lock_returns_true_when_lock_acquires(self):
        """acquire_lock() returns True when the underlying lock acquires."""
        service, _, _ = _make_service_with_succeeding_lock_cache()
        key = _make_key()

        assert service.acquire_lock(key, ttl_seconds=120) is True

    def test_acquire_lock_stores_lock_in_held_locks(self):
        """acquire_lock() stores the lock instance in _held_locks."""
        service, _, _ = _make_service_with_succeeding_lock_cache()
        key = _make_key()

        service.acquire_lock(key, ttl_seconds=120)

        lock_key_str = f"{key.domain}:{key.key}"
        assert lock_key_str in service._held_locks

    def test_acquire_lock_delegates_to_cache_get_lock(self):
        """acquire_lock() calls cache.get_lock() with correct parameters."""
        service, mock_cache, mock_lock = _make_service_with_succeeding_lock_cache()
        key = _make_key()

        result = service.acquire_lock(key, ttl_seconds=120)

        assert result is True
        mock_cache.get_lock.assert_called_once_with(
            f"idempotency:lock:{key.domain}:{key.key}",
            timeout=timedelta(seconds=120),
        )
        mock_lock.acquire.assert_called_once_with(blocking=False)

    def test_acquire_lock_returns_false_when_already_held(self):
        """acquire_lock() returns False when lock is already held by another."""
        mock_cache = MagicMock()
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = False
        mock_cache.get_lock.return_value = mock_lock

        service = IdempotencyService()
        service._cache = mock_cache
        key = _make_key()

        result = service.acquire_lock(key, ttl_seconds=120)

        assert result is False
        assert f"{key.domain}:{key.key}" not in service._held_locks


# =============================================================================
# B. IdempotencyService.release_lock()
# =============================================================================


class TestReleaseLockBehavior:
    """IdempotencyService.release_lock() behavior verification."""

    def test_release_lock_returns_true_for_held_lock(self):
        """release_lock() returns True and removes from _held_locks."""
        service, _, _ = _make_service_with_succeeding_lock_cache()
        key = _make_key()

        service.acquire_lock(key, ttl_seconds=120)
        assert f"{key.domain}:{key.key}" in service._held_locks

        result = service.release_lock(key)
        assert result is True
        assert f"{key.domain}:{key.key}" not in service._held_locks

    def test_release_lock_returns_false_for_unheld_lock(self):
        """release_lock() returns False when lock is not held."""
        service, _, _ = _make_service_with_succeeding_lock_cache()
        key = _make_key()

        result = service.release_lock(key)

        assert result is False

    def test_release_lock_calls_lock_release(self):
        """release_lock() calls the underlying lock.release()."""
        service, _, mock_lock = _make_service_with_succeeding_lock_cache()
        key = _make_key()

        service.acquire_lock(key, ttl_seconds=120)
        service.release_lock(key)

        mock_lock.release.assert_called_once()

    def test_release_lock_swallows_release_exception(self):
        """release_lock() swallows exceptions from lock.release() (best-effort)."""
        mock_cache = MagicMock()
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.release.side_effect = ConnectionError("Redis gone")
        mock_cache.get_lock.return_value = mock_lock

        service = IdempotencyService()
        service._cache = mock_cache
        key = _make_key()

        service.acquire_lock(key, ttl_seconds=120)
        result = service.release_lock(key)

        # Should not raise, return True (lock was held)
        assert result is True
        assert f"{key.domain}:{key.key}" not in service._held_locks
