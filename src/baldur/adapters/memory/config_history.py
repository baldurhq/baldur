"""
InMemoryConfigHistoryStore — In-memory implementation of ConfigHistoryStore.

Thread-safe using threading.Lock. Suitable for testing and standalone usage.
"""

from __future__ import annotations

import threading
from typing import Any

from baldur.interfaces.config_history_store import ConfigHistoryStore

__all__ = ["InMemoryConfigHistoryStore"]


class InMemoryConfigHistoryStore(ConfigHistoryStore):
    """In-memory config history store with threading.Lock atomicity."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._versions: dict[str, int] = {}  # config_type -> current version
        self._history: dict[str, list[dict[str, Any]]] = {}  # config_type -> [data]
        self._current: dict[str, dict[str, Any]] = {}  # config_type -> data

    def next_version(self, config_type: str) -> int:
        with self._lock:
            current = self._versions.get(config_type, 0)
            new_version = current + 1
            self._versions[config_type] = new_version
            return new_version

    def save_version(
        self,
        config_type: str,
        version_data: dict[str, Any],
        max_entries: int,
    ) -> None:
        with self._lock:
            if config_type not in self._history:
                self._history[config_type] = []
            # Prepend (newest first)
            self._history[config_type].insert(0, version_data)
            # Trim
            self._history[config_type] = self._history[config_type][:max_entries]
            # Update current
            self._current[config_type] = version_data

    def get_current(self, config_type: str) -> dict[str, Any] | None:
        with self._lock:
            return self._current.get(config_type)

    def get_history(self, config_type: str, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            history = self._history.get(config_type, [])
            return list(history[:limit])

    def get_version_count(self, config_type: str) -> int:
        with self._lock:
            return len(self._history.get(config_type, []))

    def clear(self, config_type: str) -> None:
        with self._lock:
            self._versions.pop(config_type, None)
            self._history.pop(config_type, None)
            self._current.pop(config_type, None)
