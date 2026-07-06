"""
InMemoryCrossClusterStore — In-memory implementation of CrossClusterStore.

Thread-safe using threading.Lock.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from baldur.interfaces.cross_cluster_store import CrossClusterStore

__all__ = ["InMemoryCrossClusterStore"]


class InMemoryCrossClusterStore(CrossClusterStore):
    """In-memory cross-cluster state store."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[
            str, tuple[dict[str, Any], float]
        ] = {}  # id -> (data, expires_at)
        self._pending: dict[str, set[str]] = {}  # cluster -> {request_ids}
        self._policies: dict[str, dict[str, Any]] = {}  # policy_id -> data

    # -- Propagation requests -------------------------------------------------

    def save_request(
        self,
        request_id: str,
        data: dict[str, Any],
        ttl_seconds: int,
    ) -> None:
        with self._lock:
            expires_at = time.time() + ttl_seconds
            self._requests[request_id] = (data, expires_at)

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._requests.get(request_id)
            if entry is None:
                return None
            data, expires_at = entry
            if time.time() > expires_at:
                del self._requests[request_id]
                return None
            return data

    def add_pending(self, cluster: str, request_id: str) -> None:
        with self._lock:
            if cluster not in self._pending:
                self._pending[cluster] = set()
            self._pending[cluster].add(request_id)

    def remove_pending(self, cluster: str, request_id: str) -> None:
        with self._lock:
            if cluster in self._pending:
                self._pending[cluster].discard(request_id)

    # -- Governance policies --------------------------------------------------

    def save_policy(self, policy_id: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._policies[policy_id] = data

    def get_policy(self, policy_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._policies.get(policy_id)
