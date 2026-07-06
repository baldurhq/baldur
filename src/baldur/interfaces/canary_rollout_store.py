"""
CanaryRolloutStore — Domain State Store for Canary Rollouts.

Abstract interface for canary rollout state management including
config lock operations (SET NX PX pattern).

Design:
    - Combines rollout CRUD, active set management, and config locking
    - Config lock uses rollout_id as owner (domain-specific lock)
    - Cannot be replaced by CacheProviderInterface.get_lock()
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Any

__all__ = ["CanaryRolloutStore"]


class CanaryRolloutStore(ABC):
    """Abstract store for canary rollout state and config locks.

    Rollout operations:
    - get_rollout / save_rollout: individual rollout CRUD
    - get_active_ids / add_active / remove_active: active set management
    - find_completed: pattern-based search for terminal rollouts

    Config lock operations:
    - acquire_config_lock / release_config_lock / get_config_lock_owner
    """

    # -- Rollout CRUD ---------------------------------------------------------

    @abstractmethod
    def get_rollout(self, rollout_id: str) -> dict[str, Any] | None:
        """Get rollout data by ID.

        Args:
            rollout_id: Rollout identifier

        Returns:
            Rollout data dict or None
        """

    @abstractmethod
    def save_rollout(
        self,
        rollout_id: str,
        data: dict[str, Any],
        ttl_seconds: int,
        expected_version: int | None = None,
    ) -> bool:
        """Save rollout data with optional optimistic locking.

        Args:
            rollout_id: Rollout identifier
            data: Serialized rollout data
            ttl_seconds: Time-to-live in seconds
            expected_version: If provided, save only if stored version matches.
                              None = unconditional save (backward compatible).

        Returns:
            True if saved, False if version conflict.
        """

    # -- Active rollout set ---------------------------------------------------

    @abstractmethod
    def get_active_ids(self) -> set[str]:
        """Get IDs of all active rollouts.

        Returns:
            Set of active rollout IDs
        """

    @abstractmethod
    def add_active(self, rollout_id: str) -> None:
        """Add a rollout to the active set.

        Args:
            rollout_id: Rollout identifier
        """

    @abstractmethod
    def remove_active(self, rollout_id: str) -> None:
        """Remove a rollout from the active set.

        Args:
            rollout_id: Rollout identifier
        """

    # -- Completed rollout search ---------------------------------------------

    @abstractmethod
    def find_completed(self, pattern: str) -> list[dict[str, Any]]:
        """Find rollouts matching a key pattern.

        Used to discover completed/terminal rollouts via SCAN.

        Args:
            pattern: Key pattern (e.g., '{prefix}canary:rollout:*')

        Returns:
            List of rollout data dicts
        """

    # -- Config lock ----------------------------------------------------------

    @abstractmethod
    def acquire_config_lock(
        self,
        config_type: str,
        rollout_id: str,
        timeout: timedelta | None = None,
    ) -> bool:
        """Acquire a config lock (SET NX PX semantics).

        Only one rollout can hold the lock for a given config_type.
        Lock auto-expires after timeout to prevent zombie locks.

        Args:
            config_type: Configuration type to lock
            rollout_id: Rollout ID as lock owner
            timeout: Lock auto-expire duration (default: implementation-defined)

        Returns:
            True if lock was acquired
        """

    @abstractmethod
    def release_config_lock(self, config_type: str, rollout_id: str) -> bool:
        """Release a config lock (atomic check-and-delete).

        Only the lock owner (matching rollout_id) can release.

        Args:
            config_type: Configuration type
            rollout_id: Expected lock owner

        Returns:
            True if lock was released
        """

    @abstractmethod
    def get_config_lock_owner(self, config_type: str) -> str | None:
        """Get the current lock owner for a config type.

        Args:
            config_type: Configuration type

        Returns:
            Lock owner (rollout_id) or None if unlocked
        """

    @abstractmethod
    def is_config_locked(self, config_type: str) -> bool:
        """Check if a config type is currently locked.

        Args:
            config_type: Configuration type

        Returns:
            True if locked
        """

    @abstractmethod
    def force_release_config_lock(self, config_type: str) -> bool:
        """Force-release a config lock without owner check.

        Admin-only operation for zombie lock cleanup.

        Args:
            config_type: Configuration type

        Returns:
            True if a lock was removed
        """

    @abstractmethod
    def extend_config_lock(
        self,
        config_type: str,
        rollout_id: str,
        additional_time: timedelta | None = None,
    ) -> bool:
        """Extend a config lock's TTL.

        Only the lock owner can extend.

        Args:
            config_type: Configuration type
            rollout_id: Expected lock owner
            additional_time: New TTL measured from now (default:
                implementation-defined). This resets the deadline to
                ``now + additional_time`` (Redis PEXPIRE semantics), it does
                not increment the existing deadline — so repeated renewals do
                not accumulate and the crash-freeze expiry valve is preserved.

        Returns:
            True if TTL was extended
        """
