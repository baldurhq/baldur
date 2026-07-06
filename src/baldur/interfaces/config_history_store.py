"""
ConfigHistoryStore — Domain State Store for Configuration History.

Abstract interface for config version history management.
Decouples ConfigHistoryService from Redis implementation details.

Design:
    - Domain language only (no Redis commands in interface)
    - Atomicity guaranteed by each implementation
      (Redis: MULTI/EXEC pipeline, InMemory: threading.Lock)
    - Follows CircuitBreakerStateRepository pattern
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

__all__ = ["ConfigHistoryStore"]


class ConfigHistoryStore(ABC):
    """Abstract store for configuration version history.

    Each method maps to a domain operation:
    - next_version: atomic version counter increment
    - save_version: atomic version save (history + current)
    - get_current / get_history / get_version_count: read operations
    - clear: cleanup (testing / admin)
    """

    @abstractmethod
    def next_version(self, config_type: str) -> int:
        """Atomically increment and return the next version number.

        Args:
            config_type: Configuration type (e.g., 'circuit_breaker', 'dlq')

        Returns:
            New version number (starts from 1)
        """

    @abstractmethod
    def save_version(
        self,
        config_type: str,
        version_data: dict[str, Any],
        max_entries: int,
    ) -> None:
        """Atomically save a version to history and update current pointer.

        This operation MUST be atomic:
        - Prepend to history list
        - Trim history to max_entries
        - Update current version pointer

        Args:
            config_type: Configuration type
            version_data: Serialized version data dict
            max_entries: Maximum history entries to retain
        """

    @abstractmethod
    def get_current(self, config_type: str) -> dict[str, Any] | None:
        """Get the current (latest) version data.

        Args:
            config_type: Configuration type

        Returns:
            Version data dict or None if no versions exist
        """

    @abstractmethod
    def get_history(self, config_type: str, limit: int) -> list[dict[str, Any]]:
        """Get version history (newest first).

        Args:
            config_type: Configuration type
            limit: Maximum number of entries to return

        Returns:
            List of version data dicts, newest first
        """

    @abstractmethod
    def get_version_count(self, config_type: str) -> int:
        """Get the number of stored versions.

        Args:
            config_type: Configuration type

        Returns:
            Version count
        """

    @abstractmethod
    def clear(self, config_type: str) -> None:
        """Clear all history for a config type.

        WARNING: Destructive operation — use for testing/admin only.

        Args:
            config_type: Configuration type
        """
