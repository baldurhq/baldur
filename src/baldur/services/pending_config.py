"""
Pending Configuration Change Service.

Manages scheduled/pending configuration changes that are waiting to be applied.

Features:
- Store pending changes with scheduled apply time
- Cancel pending changes before they's applied
- Apply changes when the scheduled time arrives
- Track change history

Audit:
- cancel_pending_change: log_config_apply_audit(status="cancelled")
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

import structlog

from baldur.audit.helpers import log_config_apply_audit
from baldur.core.apply_strategy import ApplyOptions, ApplyStrategy
from baldur.core.serializable import SerializableMixin
from baldur.core.state_backend import ListCapableBackend, get_state_backend
from baldur.settings.audit import get_audit_settings
from baldur.utils.time import utc_now

logger = structlog.get_logger()


class PendingStatus(str, Enum):
    """Status of a pending configuration change."""

    PENDING = "pending"  # Waiting to be applied
    APPLIED = "applied"  # Successfully applied
    CANCELLED = "cancelled"  # Cancelled by user
    FAILED = "failed"  # Failed to apply
    EXPIRED = "expired"  # Expired without being applied


@dataclass
class PendingConfigChange(SerializableMixin):
    """A pending configuration change."""

    id: str
    config_type: str
    changes: dict[str, Any]
    strategy: str  # ApplyStrategy value
    status: str = PendingStatus.PENDING.value
    created_at: str = ""
    scheduled_at: str = ""
    applied_at: str | None = None
    cancelled_at: str | None = None
    cancelled_by: str | None = None
    error_message: str | None = None
    previous_values: dict[str, Any] = field(default_factory=dict)
    # Operator who requested the change, preserved through deferred apply so the
    # CONFIG_CHANGE audit attributes the human requester (not the worker).
    # Symmetric to ``cancelled_by``; defaulted so legacy serialized blobs (and
    # system-initiated changes) load as "system" via SerializableMixin.from_dict.
    requested_by: str = "system"

    def __post_init__(self):
        if not self.created_at:
            self.created_at = utc_now().isoformat()


# Singleton
_pending_config_service: PendingConfigService | None = None
_service_lock = threading.Lock()


class PendingConfigService:
    """
    Service for managing pending configuration changes.

    Thread-safe singleton that tracks scheduled config changes.

    Configuration:
        AuditSettings (baldur.settings.audit)
    """

    # Legacy single-blob key — migration source only (see _migrate_legacy_blob).
    STORAGE_KEY = "pending_config_changes"
    # Per-key storage: one key per pending change, so create = set(one key) and
    # apply/cancel = delete(one key). This eliminates the shared-blob
    # read-modify-write that silently lost a change created on another process
    # during an in-flight apply cycle. A change another process created is a key
    # this process never touches → it survives.
    _CHANGE_KEY_PREFIX = "pending_config:change:"
    _CHANGE_KEY_PATTERN = "pending_config:change:*"
    _HISTORY_KEY = "pending_config:history"

    def __init__(self):
        """Initialize PendingConfigService."""
        self._lock = threading.RLock()
        self._backend = get_state_backend()
        self._pending: dict[str, PendingConfigChange] = {}
        self._settings = get_audit_settings()
        self._load_state()

    @property
    def MAX_HISTORY(self) -> int:
        """Get max history from settings."""
        return self._settings.max_history

    def _change_key(self, change_id: str) -> str:
        return f"{self._CHANGE_KEY_PREFIX}{change_id}"

    def _load_state(self) -> None:
        """Hydrate the in-memory mirror from per-key backend state at startup.

        Migrates a legacy single-blob first (one-time), then populates the
        mirror from the per-key entries.
        """
        with self._lock:
            self._migrate_legacy_blob()
            self._reload_state()

    def _migrate_legacy_blob(self) -> None:
        """One-time migration of the legacy single-blob to per-key storage.

        Pending changes are transient (they apply within ``delay_seconds``), so
        even without the shim the loss on upgrade is negligible; the shim is
        cheap insurance. Reads the legacy ``pending_config_changes`` blob once,
        hydrates each still-pending entry to its own key (terminal entries go
        straight to history), then deletes the legacy key.
        """
        legacy = self._backend.get(self.STORAGE_KEY)
        if not legacy:
            return
        migrated = 0
        for item in legacy.get("pending", []):
            try:
                change = PendingConfigChange.from_dict(item)
            except Exception as e:
                logger.warning("pending_config.migrate_pending_failed", error=e)
                continue
            if change.status == PendingStatus.PENDING.value:
                self._backend.set(self._change_key(change.id), change.to_dict())
                migrated += 1
            else:
                self._append_history(change)
        for item in legacy.get("history", []):
            try:
                self._append_history(PendingConfigChange.from_dict(item))
            except Exception as e:
                logger.warning("pending_config.migrate_history_failed", error=e)
        self._backend.delete(self.STORAGE_KEY)
        logger.info("pending_config.migrated_legacy_blob", migrated_pending=migrated)

    def _reload_state(self) -> None:
        """Re-hydrate the in-memory mirror from per-key backend state.

        The backend is authoritative: each pending change lives at its own key
        (``pending_config:change:{id}``), so enumerating ``get_all`` reflects
        changes created on any worker/pod. This refreshes the *mirror* (not just
        a returned list) because the apply path reads ``_pending`` again via
        ``get_pending_change`` / ``mark_applied``. The per-key ``get_all`` IS the
        reload (supersedes the former full-blob reload).
        """
        with self._lock:
            entries = self._backend.get_all(self._CHANGE_KEY_PATTERN)
            mirror: dict[str, PendingConfigChange] = {}
            for raw in entries.values():
                try:
                    change = PendingConfigChange.from_dict(raw)
                except Exception as e:
                    logger.warning("pending_config.load_change_failed", error=e)
                    continue
                mirror[change.id] = change
            self._pending = mirror

    def _find_change(self, change_id: str) -> PendingConfigChange | None:
        """Locate a change backend-first (authoritative), mirror as fallback.

        Caller holds ``self._lock``. The fresh per-key read avoids acting on a
        stale mirror; the mirror fallback covers a same-process change not yet
        flushed (and mock backends in unit tests).
        """
        raw = self._backend.get(self._change_key(change_id))
        if raw:
            try:
                return PendingConfigChange.from_dict(raw)
            except Exception as e:
                logger.warning("pending_config.load_change_failed", error=e)
        return self._pending.get(change_id)

    def _delete_change(self, change_id: str) -> None:
        """Delete a change's per-key entry + drop it from the mirror. Caller holds lock."""
        self._backend.delete(self._change_key(change_id))
        self._pending.pop(change_id, None)

    def _append_history(self, change: PendingConfigChange) -> None:
        """Append a terminal record to the capped history list. Caller holds lock.

        Follows the corruption_shield precedent: a ``ListCapableBackend``
        (Redis/Memory) uses the atomic ``push_limit``; ``FileStateBackend`` (the
        OSS default, which does NOT implement ``ListCapableBackend``) falls back
        to a capped get/set list — an unguarded ``push_limit`` would
        ``AttributeError`` on a default deployment.
        """
        record = change.to_dict()
        backend = self._backend
        if isinstance(backend, ListCapableBackend):
            try:
                backend.push_limit(self._HISTORY_KEY, record, self.MAX_HISTORY)
            except Exception as e:
                logger.warning("pending_config.history_push_failed", error=e)
            return
        existing = backend.get(self._HISTORY_KEY)
        items = existing.get("items", []) if isinstance(existing, dict) else []
        items.append(record)
        items = items[-self.MAX_HISTORY :]
        backend.set(self._HISTORY_KEY, {"items": items})

    def _read_history(self) -> list[PendingConfigChange]:
        """Read the terminal-record history from the backend (cross-process)."""
        backend = self._backend
        if isinstance(backend, ListCapableBackend):
            try:
                records = backend.list_range(self._HISTORY_KEY, 0, -1)
            except Exception as e:
                logger.warning("pending_config.history_read_failed", error=e)
                records = []
        else:
            existing = backend.get(self._HISTORY_KEY)
            records = existing.get("items", []) if isinstance(existing, dict) else []
        result: list[PendingConfigChange] = []
        for raw in records:
            if not raw:
                continue
            try:
                result.append(PendingConfigChange.from_dict(raw))
            except Exception as e:
                logger.warning("pending_config.load_history_failed", error=e)
        return result

    # =========================================================================
    # Public API
    # =========================================================================

    def create_pending_change(
        self,
        config_type: str,
        changes: dict[str, Any],
        apply_options: ApplyOptions,
        previous_values: dict[str, Any] | None = None,
        requested_by: str = "system",
    ) -> PendingConfigChange:
        """
        Create a new pending configuration change.

        Args:
            config_type: Type of configuration (e.g., "circuit_breaker")
            changes: The configuration changes to apply
            apply_options: How and when to apply the changes
            previous_values: Current values before change (for rollback)
            requested_by: Operator/user requesting the change, preserved through
                the deferred-apply audit trail (defaults to "system")

        Returns:
            The created PendingConfigChange
        """
        with self._lock:
            change_id = str(uuid.uuid4())[:8]

            # Calculate scheduled time
            if apply_options.strategy == ApplyStrategy.DELAYED:
                scheduled_time = utc_now() + timedelta(
                    seconds=apply_options.delay_seconds
                )
            else:
                scheduled_time = utc_now()

            change = PendingConfigChange(
                id=change_id,
                config_type=config_type,
                changes=changes,
                strategy=apply_options.strategy.value,
                scheduled_at=scheduled_time.isoformat(),
                previous_values=previous_values or {},
                requested_by=requested_by,
            )

            self._backend.set(self._change_key(change_id), change.to_dict())
            self._pending[change_id] = change

            logger.info(
                "pending_config.created_pending_change_scheduled",
                change_id=change_id,
                config_type=config_type,
                scheduled_time=scheduled_time,
            )

            return change

    def get_pending_change(self, change_id: str) -> PendingConfigChange | None:
        """Get a pending change by ID (backend-authoritative)."""
        with self._lock:
            return self._find_change(change_id)

    def get_pending_changes_for_config(
        self, config_type: str
    ) -> list[PendingConfigChange]:
        """Get all pending changes for a config type."""
        with self._lock:
            self._reload_state()
            return [
                c
                for c in self._pending.values()
                if c.config_type == config_type
                and c.status == PendingStatus.PENDING.value
            ]

    def get_all_pending_changes(self) -> list[PendingConfigChange]:
        """Get all pending changes."""
        with self._lock:
            self._reload_state()
            return [
                c
                for c in self._pending.values()
                if c.status == PendingStatus.PENDING.value
            ]

    def get_due_changes(self) -> list[PendingConfigChange]:
        """Get all changes that are due to be applied.

        Reloads the in-memory mirror from the shared backend first so the leader
        applier sees pending changes created on other workers/pods (cost: one
        backend read per poll, ≤ once per 30s scheduler tick). Single-host with
        exactly one applier process: the reload is the only read path that runs.
        """
        with self._lock:
            self._reload_state()
            now = utc_now()
            due = []
            for change in self._pending.values():
                if change.status != PendingStatus.PENDING.value:
                    continue
                scheduled = datetime.fromisoformat(change.scheduled_at)
                if scheduled <= now:
                    due.append(change)
            return due

    def cancel_pending_change(
        self,
        change_id: str,
        cancelled_by: str | None = None,
    ) -> PendingConfigChange | None:
        """
        Cancel a pending change.

        Args:
            change_id: ID of the change to cancel
            cancelled_by: Who cancelled (user/system)

        Returns:
            The cancelled change, or None if not found
        """
        with self._lock:
            change = self._find_change(change_id)
            if not change:
                return None

            if change.status != PendingStatus.PENDING.value:
                logger.warning(
                    "pending_config.cannot_cancel_status",
                    change_id=change_id,
                    change=change.status,
                )
                return None

            change.status = PendingStatus.CANCELLED.value
            change.cancelled_at = utc_now().isoformat()
            change.cancelled_by = cancelled_by

            self._delete_change(change_id)
            self._append_history(change)

            logger.info(
                "pending_config.cancelled_pending_change",
                change_id=change_id,
            )

            # === Audit record: scheduled config change cancelled ===
            log_config_apply_audit(
                pending_id=change_id,
                config_key=change.config_type,
                old_value=change.previous_values,
                new_value=change.changes,
                status="cancelled",
                details={
                    "cancelled_by": cancelled_by,
                    "scheduled_at": change.scheduled_at,
                    "strategy": change.strategy,
                },
            )

            return change

    def mark_applied(
        self,
        change_id: str,
    ) -> PendingConfigChange | None:
        """Mark a pending change as applied."""
        with self._lock:
            change = self._find_change(change_id)
            if not change:
                return None

            change.status = PendingStatus.APPLIED.value
            change.applied_at = utc_now().isoformat()

            self._delete_change(change_id)
            self._append_history(change)

            logger.info(
                "pending_config.applied_pending_change",
                change_id=change_id,
            )
            return change

    def mark_failed(
        self,
        change_id: str,
        error_message: str,
    ) -> PendingConfigChange | None:
        """Mark a pending change as failed."""
        with self._lock:
            change = self._find_change(change_id)
            if not change:
                return None

            change.status = PendingStatus.FAILED.value
            change.error_message = error_message

            self._delete_change(change_id)
            self._append_history(change)

            logger.error(
                "pending_config.apply_failed",
                change_id=change_id,
                error_message=error_message,
            )
            return change

    def get_history(
        self,
        config_type: str | None = None,
        limit: int = 50,
    ) -> list[PendingConfigChange]:
        """Get change history."""
        with self._lock:
            history = self._read_history()
            if config_type:
                history = [c for c in history if c.config_type == config_type]
            return list(reversed(history[-limit:]))

    def cleanup_expired(self, max_age_hours: int = 24) -> int:
        """
        Cleanup old pending changes that were never applied.

        Returns:
            Number of expired changes cleaned up
        """
        with self._lock:
            self._reload_state()
            now = utc_now()
            cutoff = now - timedelta(hours=max_age_hours)
            expired = []

            for change in list(self._pending.values()):
                if change.status != PendingStatus.PENDING.value:
                    continue
                created = datetime.fromisoformat(change.created_at)
                if created < cutoff:
                    change.status = PendingStatus.EXPIRED.value
                    self._delete_change(change.id)
                    self._append_history(change)
                    expired.append(change)

            if expired:
                logger.info(
                    "pending_config.cleaned_up_expired_changes",
                    expired_count=len(expired),
                )

            return len(expired)


def get_pending_config_service() -> PendingConfigService:
    """Get singleton PendingConfigService instance."""
    global _pending_config_service

    if _pending_config_service is None:
        with _service_lock:
            if _pending_config_service is None:
                _pending_config_service = PendingConfigService()

    return _pending_config_service


def reset_pending_config_service() -> None:
    """Reset singleton instance (for testing)."""
    global _pending_config_service
    with _service_lock:
        _pending_config_service = None
