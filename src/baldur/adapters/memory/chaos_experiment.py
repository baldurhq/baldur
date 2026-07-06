"""
InMemoryChaosExperimentStore — In-memory implementation of ChaosExperimentStore.

Thread-safe using threading.Lock.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from baldur.interfaces.chaos_experiment_store import ChaosExperimentStore

__all__ = ["InMemoryChaosExperimentStore"]


class InMemoryChaosExperimentStore(ChaosExperimentStore):
    """In-memory chaos experiment store."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._experiments: dict[
            str, tuple[dict[str, Any], float]
        ] = {}  # id -> (data, expires_at)

    def save(
        self,
        experiment_id: str,
        data: dict[str, Any],
        ttl_seconds: int,
    ) -> None:
        with self._lock:
            expires_at = time.time() + ttl_seconds
            self._experiments[experiment_id] = (data, expires_at)

    def get(self, experiment_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._experiments.get(experiment_id)
            if entry is None:
                return None
            data, expires_at = entry
            if time.time() > expires_at:
                del self._experiments[experiment_id]
                return None
            return data

    def delete(self, experiment_id: str) -> None:
        with self._lock:
            self._experiments.pop(experiment_id, None)

    def find_active(self) -> list[dict[str, Any]]:
        with self._lock:
            now = time.time()
            results = []
            expired_keys = []
            for exp_id, (data, expires_at) in self._experiments.items():
                if now > expires_at:
                    expired_keys.append(exp_id)
                    continue
                if data.get("status") == "active":
                    results.append(data)
            # Lazy cleanup
            for key in expired_keys:
                del self._experiments[key]
            return results
