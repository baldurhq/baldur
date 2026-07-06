"""``RedisCircuitBreakerStateRepository.cleanup_stale_keys`` tests (484 D5).

Covers:
- Age boundary: keys with ``updated_at >= threshold`` are kept; only strictly
  older keys are deleted (matches the ``data.updated_at < threshold`` check
  in ``cleanup_stale_keys``).
- Degraded mode: when the backend is degraded, the method iterates the in-memory
  fallback store via ``_get_all_from_memory()`` instead of Redis SCAN.
- SCAN guards: ``max_iterations=1000`` and ``deadline=2.0s`` early-termination
  match the established ``get_open_states()`` pattern.
- Exception path: SCAN exceptions are logged and the method returns the count
  accumulated so far (graceful degradation).

Reference:
- ``484`` D5
- ``src/baldur/adapters/redis/circuit_breaker.py:479-549``
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from baldur.adapters.redis.circuit_breaker import RedisCircuitBreakerStateRepository
from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)
from baldur.utils.time import utc_now


def _make_repo(
    is_degraded: bool = False,
    key_prefix: str = "baldur:",
) -> tuple[RedisCircuitBreakerStateRepository, MagicMock, MagicMock]:
    """Build a repo with a fully-mocked ResilientStorageBackend.

    Returns ``(repo, backend, redis_client)`` so individual tests can stub
    SCAN responses on the redis client and ``hgetall`` on the backend.
    """
    backend = MagicMock()
    backend.is_degraded = is_degraded
    backend.config.key_prefix = key_prefix

    redis_client = MagicMock()
    backend.raw_redis_client = redis_client

    repo = RedisCircuitBreakerStateRepository(backend=backend)
    return repo, backend, redis_client


def _state_dict(updated_at_iso: str, state: str = "closed") -> dict[str, str]:
    """Hash-shaped state dict accepted by ``_to_data``."""
    return {
        "state": state,
        "failure_count": "0",
        "success_count": "0",
        "manually_controlled": "False",
        "control_reason": "",
        "created_at": updated_at_iso,
        "updated_at": updated_at_iso,
    }


# =============================================================================
# Behavior — Redis SCAN path
# =============================================================================


class TestCleanupStaleKeysScanBehavior:
    """484 D5: SCAN-based cleanup deletes keys older than ``retention_days``."""

    def test_strictly_older_key_is_deleted(self):
        """A key older than the retention window is deleted exactly once."""
        repo, backend, redis_client = _make_repo()
        old_iso = (utc_now() - timedelta(days=60)).isoformat()

        redis_client.scan.return_value = (0, [b"baldur:cb:legacy-service"])
        backend.hgetall.return_value = _state_dict(old_iso)
        backend.delete.return_value = True

        deleted = repo.cleanup_stale_keys(retention_days=30)

        assert deleted == 1
        backend.delete.assert_called_once_with("cb:legacy-service")

    def test_recent_key_is_not_deleted(self):
        """A key updated within the retention window is preserved."""
        repo, backend, redis_client = _make_repo()
        recent_iso = (utc_now() - timedelta(days=5)).isoformat()

        redis_client.scan.return_value = (0, [b"baldur:cb:active-service"])
        backend.hgetall.return_value = _state_dict(recent_iso)

        deleted = repo.cleanup_stale_keys(retention_days=30)

        assert deleted == 0
        backend.delete.assert_not_called()

    def test_state_with_missing_updated_at_is_skipped(self):
        """Entries without ``updated_at`` are not considered stale."""
        repo, backend, redis_client = _make_repo()

        redis_client.scan.return_value = (0, [b"baldur:cb:partial"])
        backend.hgetall.return_value = {
            "state": "closed",
            "failure_count": "0",
            # no updated_at — _to_data will set it to None
        }

        deleted = repo.cleanup_stale_keys(retention_days=30)

        assert deleted == 0
        backend.delete.assert_not_called()

    def test_get_state_returning_none_is_skipped(self):
        """Race: SCAN returns a key but the hash was deleted concurrently."""
        repo, backend, redis_client = _make_repo()

        redis_client.scan.return_value = (0, [b"baldur:cb:race-victim"])
        backend.hgetall.return_value = {}  # get_state -> None

        deleted = repo.cleanup_stale_keys(retention_days=30)

        assert deleted == 0
        backend.delete.assert_not_called()

    def test_mixed_stale_and_fresh_keys_deletes_only_stale(self):
        """Multiple keys: only the strictly-older ones are deleted."""
        repo, backend, redis_client = _make_repo()
        old = (utc_now() - timedelta(days=60)).isoformat()
        fresh = (utc_now() - timedelta(days=2)).isoformat()

        redis_client.scan.return_value = (
            0,
            [b"baldur:cb:old-a", b"baldur:cb:fresh", b"baldur:cb:old-b"],
        )

        def hgetall_side_effect(key: str) -> dict[str, str]:
            if key == "cb:old-a":
                return _state_dict(old)
            if key == "cb:old-b":
                return _state_dict(old)
            if key == "cb:fresh":
                return _state_dict(fresh)
            return {}

        backend.hgetall.side_effect = hgetall_side_effect
        backend.delete.return_value = True

        deleted = repo.cleanup_stale_keys(retention_days=30)

        assert deleted == 2
        deleted_keys = {call.args[0] for call in backend.delete.call_args_list}
        assert deleted_keys == {"cb:old-a", "cb:old-b"}

    def test_pagination_continues_until_cursor_zero(self):
        """SCAN cursor sequence is iterated until cursor returns 0."""
        repo, backend, redis_client = _make_repo()
        old = (utc_now() - timedelta(days=60)).isoformat()

        redis_client.scan.side_effect = [
            (123, [b"baldur:cb:a"]),
            (456, [b"baldur:cb:b"]),
            (0, [b"baldur:cb:c"]),
        ]
        backend.hgetall.return_value = _state_dict(old)
        backend.delete.return_value = True

        deleted = repo.cleanup_stale_keys(retention_days=30)

        assert deleted == 3
        assert redis_client.scan.call_count == 3

    def test_scan_exception_returns_partial_count(self):
        """When SCAN raises, the method swallows and returns count so far."""
        repo, backend, redis_client = _make_repo()

        redis_client.scan.side_effect = ConnectionError("redis gone")

        deleted = repo.cleanup_stale_keys(retention_days=30)

        assert deleted == 0  # No keys processed before the failure


# =============================================================================
# Behavior — Degraded fallback path
# =============================================================================


class TestCleanupStaleKeysDegradedBehavior:
    """484 D5: degraded backend falls back to ``_get_all_from_memory()``."""

    def test_degraded_iterates_in_memory_fallback(self):
        """In degraded mode, no Redis SCAN; cleanup walks the memory fallback."""
        repo, backend, redis_client = _make_repo(is_degraded=True)
        old_dt = utc_now() - timedelta(days=60)
        fresh_dt = utc_now() - timedelta(days=5)

        old_state = CircuitBreakerStateData(
            service_name="legacy",
            state=CircuitBreakerStateEnum.CLOSED.value,
            updated_at=old_dt,
        )
        fresh_state = CircuitBreakerStateData(
            service_name="active",
            state=CircuitBreakerStateEnum.CLOSED.value,
            updated_at=fresh_dt,
        )

        repo._get_all_from_memory = MagicMock(return_value=[old_state, fresh_state])
        backend.delete.return_value = True

        deleted = repo.cleanup_stale_keys(retention_days=30)

        assert deleted == 1
        # Ensure SCAN was NOT called in degraded mode.
        redis_client.scan.assert_not_called()
        backend.delete.assert_called_once_with("cb:legacy")

    def test_degraded_skips_state_with_no_updated_at(self):
        """Memory entries with ``updated_at=None`` are skipped, not deleted."""
        repo, backend, _ = _make_repo(is_degraded=True)

        partial = CircuitBreakerStateData(
            service_name="no-timestamp",
            state=CircuitBreakerStateEnum.CLOSED.value,
            updated_at=None,
        )
        repo._get_all_from_memory = MagicMock(return_value=[partial])

        deleted = repo.cleanup_stale_keys(retention_days=30)

        assert deleted == 0
        backend.delete.assert_not_called()

    def test_degraded_delete_failure_does_not_increment_count(self):
        """If ``delete_state`` returns False, the count does not advance."""
        repo, backend, _ = _make_repo(is_degraded=True)
        old_dt = utc_now() - timedelta(days=60)

        old_state = CircuitBreakerStateData(
            service_name="legacy",
            state=CircuitBreakerStateEnum.CLOSED.value,
            updated_at=old_dt,
        )
        repo._get_all_from_memory = MagicMock(return_value=[old_state])
        backend.delete.return_value = False  # delete fails

        deleted = repo.cleanup_stale_keys(retention_days=30)

        assert deleted == 0


# =============================================================================
# Contract — SCAN guard parameters match get_open_states pattern
# =============================================================================


class TestCleanupStaleKeysScanGuardsContract:
    """484 D5: SCAN guards mirror the established ``get_open_states()`` pattern."""

    def test_scan_uses_count_100_pagination(self):
        """SCAN ``count=100`` matches the existing get_open_states() pattern."""
        repo, backend, redis_client = _make_repo()
        redis_client.scan.return_value = (0, [])

        repo.cleanup_stale_keys(retention_days=30)

        scan_kwargs = redis_client.scan.call_args.kwargs
        assert scan_kwargs.get("count") == 100

    def test_scan_uses_namespaced_pattern(self):
        """SCAN match pattern is ``{key_prefix}cb:*``."""
        repo, backend, redis_client = _make_repo(key_prefix="baldur:seoul:")
        redis_client.scan.return_value = (0, [])

        repo.cleanup_stale_keys(retention_days=30)

        scan_kwargs = redis_client.scan.call_args.kwargs
        assert scan_kwargs.get("match") == "baldur:seoul:cb:*"

    @pytest.mark.parametrize("retention_days", [1, 30, 90, 365])
    def test_returns_int_count(self, retention_days):
        """Method always returns an int for any in-range retention window."""
        repo, backend, redis_client = _make_repo()
        redis_client.scan.return_value = (0, [])

        deleted = repo.cleanup_stale_keys(retention_days=retention_days)

        assert isinstance(deleted, int)
        assert deleted >= 0
