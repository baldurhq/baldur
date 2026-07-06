"""
InMemoryCanaryRolloutStore — In-memory implementation of CanaryRolloutStore.

Thread-safe using threading.Lock.
Includes config lock simulation with timeout tracking.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from typing import Any

from baldur.interfaces.canary_rollout_store import CanaryRolloutStore

__all__ = ["InMemoryCanaryRolloutStore"]

_DEFAULT_LOCK_TIMEOUT = timedelta(minutes=30)


class InMemoryCanaryRolloutStore(CanaryRolloutStore):
    """In-memory canary rollout store with config lock support."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rollouts: dict[
            str, tuple[dict[str, Any], float]
        ] = {}  # id -> (data, expires_at)
        self._active_ids: set[str] = set()
        self._config_locks: dict[
            str, tuple[str, float]
        ] = {}  # config_type -> (owner, expires_at)

    # -- Rollout CRUD ---------------------------------------------------------

    def get_rollout(self, rollout_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._rollouts.get(rollout_id)
            if entry is None:
                return None
            data, expires_at = entry
            if expires_at and time.time() > expires_at:
                del self._rollouts[rollout_id]
                return None
            return data

    def save_rollout(
        self,
        rollout_id: str,
        data: dict[str, Any],
        ttl_seconds: int,
        expected_version: int | None = None,
    ) -> bool:
        with self._lock:
            if expected_version is not None:
                entry = self._rollouts.get(rollout_id)
                if entry is None:
                    return False
                stored_data, expires_at = entry
                if expires_at and time.time() > expires_at:
                    del self._rollouts[rollout_id]
                    return False
                if stored_data.get("version", 0) != expected_version:
                    return False

            expires_at = time.time() + ttl_seconds
            self._rollouts[rollout_id] = (data, expires_at)
            return True

    # -- Active set -----------------------------------------------------------

    def get_active_ids(self) -> set[str]:
        with self._lock:
            return set(self._active_ids)

    def add_active(self, rollout_id: str) -> None:
        with self._lock:
            self._active_ids.add(rollout_id)

    def remove_active(self, rollout_id: str) -> None:
        with self._lock:
            self._active_ids.discard(rollout_id)

    # -- Completed rollout search ---------------------------------------------

    def find_completed(self, pattern: str) -> list[dict[str, Any]]:
        # InMemory returns all non-expired rollouts regardless of pattern;
        # pattern matching is a Redis key concern — callers filter by state.
        with self._lock:
            now = time.time()
            results = []
            for _rollout_id, (data, expires_at) in list(self._rollouts.items()):
                if expires_at and now > expires_at:
                    continue
                results.append(data)
            return results

    # -- Config lock ----------------------------------------------------------

    def acquire_config_lock(
        self,
        config_type: str,
        rollout_id: str,
        timeout: timedelta | None = None,
    ) -> bool:
        lock_timeout = timeout or _DEFAULT_LOCK_TIMEOUT
        with self._lock:
            # Check existing lock
            if config_type in self._config_locks:
                owner, expires_at = self._config_locks[config_type]
                if time.time() < expires_at:
                    return False  # Still locked
                # Expired — remove stale lock
                del self._config_locks[config_type]

            self._config_locks[config_type] = (
                rollout_id,
                time.time() + lock_timeout.total_seconds(),
            )
            return True

    def release_config_lock(self, config_type: str, rollout_id: str) -> bool:
        with self._lock:
            if config_type not in self._config_locks:
                return False
            owner, _ = self._config_locks[config_type]
            if owner != rollout_id:
                return False
            del self._config_locks[config_type]
            return True

    def get_config_lock_owner(self, config_type: str) -> str | None:
        with self._lock:
            if config_type not in self._config_locks:
                return None
            owner, expires_at = self._config_locks[config_type]
            if time.time() > expires_at:
                del self._config_locks[config_type]
                return None
            return owner

    def is_config_locked(self, config_type: str) -> bool:
        return self.get_config_lock_owner(config_type) is not None

    def force_release_config_lock(self, config_type: str) -> bool:
        with self._lock:
            if config_type in self._config_locks:
                del self._config_locks[config_type]
                return True
            return False

    def extend_config_lock(
        self,
        config_type: str,
        rollout_id: str,
        additional_time: timedelta | None = None,
    ) -> bool:
        extend_time = additional_time or _DEFAULT_LOCK_TIMEOUT
        with self._lock:
            if config_type not in self._config_locks:
                return False
            owner, expires_at = self._config_locks[config_type]
            if owner != rollout_id:
                return False
            if time.time() > expires_at:
                return False
            # Reset the deadline from now (parity with Redis PEXPIRE), not an
            # increment to the existing deadline. Under periodic renewal an
            # additive deadline would drift unboundedly ahead and weaken the
            # crash-freeze valve in memory deployments.
            self._config_locks[config_type] = (
                owner,
                time.time() + extend_time.total_seconds(),
            )
            return True
