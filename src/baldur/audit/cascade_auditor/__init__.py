"""
Cascade Event Auditor - cascading event auditor.

Creates, stores, and queries cascading events, and verifies hash chain
integrity.

Features:
- Cascade Event creation and storage
- Hash Chain linking
- Integrity verification
- Causation queries

Usage:
    from baldur.audit.cascade_auditor import get_cascade_event_auditor

    auditor = get_cascade_event_auditor()

    cascade_event = auditor.record(
        trigger_type="EMERGENCY_LEVEL_CHANGED",
        trigger_details={"old_level": "NORMAL", "new_level": "LEVEL_3"},
        effects=[
            {"action_type": "GOVERNANCE_STRICT", "success": True},
            {"action_type": "CANARY_ROLLBACK", "success": True, "target": "rollout-123"},
        ],
        namespace="seoul",
        triggered_by="system",
    )

    # Query
    event = auditor.get_cascade_event("cascade-abc123", "seoul")
    events = auditor.get_recent_events("seoul", limit=100)

    # Integrity verification
    result = auditor.verify_chain_integrity("seoul")
    if result["valid"]:
        print("Hash chain is valid")
"""

from __future__ import annotations

import threading
from typing import Any

import structlog

from baldur.audit.cascade_auditor._helpers import get_index_ids
from baldur.audit.cascade_auditor._querying import QueryingMixin
from baldur.audit.cascade_auditor._recording import RecordingMixin
from baldur.audit.cascade_auditor._verification import VerificationMixin
from baldur.audit.cascade_auditor._wal_recovery import (
    LOCAL_CASCADE_FALLBACK_PATH,
    LOCAL_CASCADE_WAL_DIR,
    LOCAL_CASCADE_WAL_PATH,
    WALRecoveryMixin,
)
from baldur.settings.cascade_retention import get_cascade_retention_settings

logger = structlog.get_logger()


def _get_max_cascade_index_size() -> int:
    """Get max cascade index size from settings."""
    return get_cascade_retention_settings().max_cascade_index_size


class CascadeEventAuditor(
    RecordingMixin,
    QueryingMixin,
    VerificationMixin,
    WALRecoveryMixin,
):
    """
    Cascade Event auditor.

    Creates, stores, and queries cascading events, and verifies hash chain
    integrity.

    Features:
    - Cascade Event creation and storage
    - Hash Chain linking
    - Integrity verification
    - Causation queries
    - Load Shedding (Phase 5)
    - Fail-Soft local fallback (Phase 5)
    """

    # Redis key patterns
    CASCADE_KEY = "baldur:{namespace}:audit:cascade:{cascade_id}"
    CASCADE_INDEX_KEY = "baldur:{namespace}:audit:cascade_index"
    LAST_HASH_KEY = "baldur:{namespace}:audit:cascade_last_hash"

    # Legacy constant for backward compatibility
    MAX_INDEX_SIZE = 10000

    def __init__(
        self,
        enable_load_shedding: bool = True,
        max_index_size: int | None = None,
    ) -> None:
        """
        Args:
            enable_load_shedding: Whether Load Shedding is enabled
            max_index_size: Max index size (default from CascadeRetentionSettings)
        """
        self._lock = threading.RLock()
        self._enable_load_shedding = enable_load_shedding
        self._load_shedding = None  # Lazy init
        self._max_index_size = (
            max_index_size
            if max_index_size is not None
            else _get_max_cascade_index_size()
        )

    def _get_backend(self):
        """Acquire the state backend."""
        from baldur.core.state_backend import get_state_backend

        return get_state_backend()

    def _get_load_shedding(self):
        """Acquire the Load Shedding manager (lazy init)."""
        if self._load_shedding is None and self._enable_load_shedding:
            from baldur.audit.cascade_load_shedding import (
                get_cascade_load_shedding,
            )

            self._load_shedding = get_cascade_load_shedding()
        return self._load_shedding

    # =========================================================================
    # Private Methods (Storage)
    # =========================================================================

    def _get_last_hash(self, namespace: str) -> str | None:
        """Look up the last hash."""
        backend = self._get_backend()
        key = self.LAST_HASH_KEY.format(namespace=namespace)
        data = backend.get(key)
        if data:
            return data.get("hash") if isinstance(data, dict) else data
        return None

    def _update_last_hash(self, namespace: str, hash_value: str) -> None:
        """Update the last hash."""
        backend = self._get_backend()
        key = self.LAST_HASH_KEY.format(namespace=namespace)
        backend.set(key, {"hash": hash_value})

    def _save_cascade_event(self, event: Any) -> None:
        """Store a Cascade Event.

        Applies CascadeRetentionSettings.hot_retention_days as the TTL to
        prevent Redis memory leaks. Uses the ttl_seconds parameter of
        StateBackend.set(); RedisStateBackend sets atomic expiry via
        redis.setex().
        """
        backend = self._get_backend()
        key = self.CASCADE_KEY.format(
            namespace=event.namespace,
            cascade_id=event.id,
        )
        retention = get_cascade_retention_settings()
        ttl_seconds = retention.hot_retention_days * 86400
        backend.set(key, event.to_dict(), ttl_seconds=ttl_seconds)

    def _add_to_index(self, namespace: str, cascade_id: str) -> None:
        """Add to the index (newest first)."""
        backend = self._get_backend()
        key = self.CASCADE_INDEX_KEY.format(namespace=namespace)
        ids = get_index_ids(backend, key)

        # Prepend
        ids.insert(0, cascade_id)

        # Keep within the max size
        if len(ids) > self._max_index_size:
            ids = ids[: self._max_index_size]

        backend.set(key, {"ids": ids})


# =============================================================================
# Singleton
# =============================================================================

from baldur.utils.singleton import make_singleton_factory  # noqa: E402

get_cascade_event_auditor, configure_cascade_event_auditor, reset_cascade_auditor = (
    make_singleton_factory("cascade_event_auditor", CascadeEventAuditor)
)

__all__ = [
    "CascadeEventAuditor",
    "get_cascade_event_auditor",
    "configure_cascade_event_auditor",
    "reset_cascade_auditor",
    "LOCAL_CASCADE_WAL_DIR",
    "LOCAL_CASCADE_WAL_PATH",
    "LOCAL_CASCADE_FALLBACK_PATH",
]
