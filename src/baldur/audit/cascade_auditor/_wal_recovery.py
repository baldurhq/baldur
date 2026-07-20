"""
Cascade Auditor - WAL/Load Shedding module.

Owns local WAL storage, Load Shedding, and recovery responsibilities.
Performs thread-safe JSONL WAL writes through JSONLWriter.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.cascade_auditor._helpers import get_index_ids
from baldur.audit.cascade_event import CascadeEvent, ExternalTraceContext
from baldur.audit.wal._jsonl import JSONLWriter
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    import threading

    from baldur.audit.cascade_load_shedding import CascadeLoadShedding

logger = structlog.get_logger()


# WAL path defaults (overridden by BALDUR_CASCADE_WAL_DIR setting)
_DEFAULT_CASCADE_WAL_DIR = "/var/log/baldur/cascade_wal"


def _get_cascade_wal_dir() -> str:
    """Get cascade WAL directory from settings with fallback to default."""
    try:
        from baldur.settings.cascade import get_cascade_settings

        return get_cascade_settings().wal_dir
    except Exception:
        return _DEFAULT_CASCADE_WAL_DIR


LOCAL_CASCADE_WAL_DIR = _DEFAULT_CASCADE_WAL_DIR
LOCAL_CASCADE_WAL_PATH = f"{LOCAL_CASCADE_WAL_DIR}/cascade_audit_wal.jsonl"
LOCAL_CASCADE_FALLBACK_PATH = LOCAL_CASCADE_WAL_PATH

# Lazy-initialized JSONLWriter (fsync=False — Redis is the primary recovery
# path, the local WAL is a best-effort fallback)
# Lazy init prevents file creation during unit test imports where the module is loaded but not used.
_wal_writer: JSONLWriter | None = None


def _get_wal_writer() -> JSONLWriter:
    """Get or create module-level JSONLWriter singleton."""
    global _wal_writer
    if _wal_writer is None:
        wal_dir = _get_cascade_wal_dir()
        wal_path = f"{wal_dir}/cascade_audit_wal.jsonl"
        _wal_writer = JSONLWriter(
            file_path=Path(wal_path),
            fsync=False,
        )
    return _wal_writer


def reset_wal_writer() -> None:
    """Reset WAL writer for test isolation."""
    global _wal_writer
    _wal_writer = None


# Batch recovery constants
_BATCH_SIZE = 1000
_IDEMPOTENCY_TTL = 3600  # 1 hour


def _append_to_wal(data: dict) -> None:
    """
    Append data to the WAL file in JSONL format.

    Writes thread-safely through JSONLWriter.

    Args:
        data: dictionary data to store
    """
    _get_wal_writer().append(data)


class WALRecoveryMixin:
    """WAL / Load Shedding / recovery methods."""

    if TYPE_CHECKING:
        # Host contract — attributes/methods provided via MRO by
        # CascadeEventAuditor and sibling mixins (QueryingMixin,
        # RecordingMixin).
        _lock: threading.RLock
        _load_shedding: CascadeLoadShedding | None
        _max_index_size: int
        CASCADE_INDEX_KEY: str

        def _get_backend(self) -> Any: ...
        def _get_load_shedding(self) -> CascadeLoadShedding | None: ...
        def _save_cascade_event(self, event: Any) -> None: ...
        def record(
            self,
            trigger_type: str,
            trigger_details: dict[str, Any],
            effects: list[dict[str, Any]],
            namespace: str,
            triggered_by: str | None = None,
            external_trace: ExternalTraceContext | None = None,
        ) -> CascadeEvent: ...

    def record_with_load_shedding(
        self,
        trigger_type: str,
        trigger_details: dict[str, Any],
        effects: list[dict[str, Any]],
        namespace: str,
        triggered_by: str | None = None,
        external_trace: ExternalTraceContext | None = None,
    ) -> CascadeEvent | None:
        """
        Record a Cascade Event with Load Shedding applied.

        Drops lower-priority events based on buffer utilization.
        CRITICAL events are never dropped; a local fallback is used
        when necessary.

        Args:
            trigger_type: trigger type
            trigger_details: trigger detail information
            effects: list of cascading effects
            namespace: namespace
            triggered_by: the actor that triggered it
            external_trace: external distributed tracing context

        Returns:
            The created CascadeEvent, or None if dropped
        """
        load_shedding = self._get_load_shedding()

        if not load_shedding:
            # Load Shedding disabled — record normally
            return self.record(
                trigger_type=trigger_type,
                trigger_details=trigger_details,
                effects=effects,
                namespace=namespace,
                triggered_by=triggered_by,
                external_trace=external_trace,
            )

        # Check the buffer state
        backend = self._get_backend()
        index_key = self.CASCADE_INDEX_KEY.format(namespace=namespace)
        buffer_size = len(get_index_ids(backend, index_key))

        # Load Shedding decision
        decision = load_shedding.should_accept(
            trigger_type=trigger_type,
            buffer_size=buffer_size,
            buffer_capacity=self._max_index_size,
        )

        if not decision["accepted"]:
            # Dropped
            logger.warning(
                "cascade_audit.event_dropped_load_shedding",
                trigger_type=trigger_type,
                decision=decision["reason"],
            )

            # Store locally when a fallback is recommended
            if decision.get("use_fallback"):
                self._record_dropped_to_wal(
                    trigger_type=trigger_type,
                    trigger_details=trigger_details,
                    effects=effects,
                    namespace=namespace,
                    reason=decision["reason"],
                )

            return None

        # Normal record
        return self.record(
            trigger_type=trigger_type,
            trigger_details=trigger_details,
            effects=effects,
            namespace=namespace,
            triggered_by=triggered_by,
            external_trace=external_trace,
        )

    def _save_to_local_wal(self, event: CascadeEvent) -> None:
        """
        Save a Cascade Event to the local WAL.

        On Redis failure, stores to the local WAL file in JSONL format.

        Args:
            event: the CascadeEvent to store
        """
        try:
            _append_to_wal(event.to_dict())
            logger.info(
                "cascade_audit.saved_local_wal",
                cascade_event_id=event.id,
            )
        except Exception as e:
            logger.exception(
                "cascade_audit.local_wal_save_failed",
                error=e,
            )

    # Backward compatibility
    _save_to_local_fallback = _save_to_local_wal

    def _record_dropped_to_wal(
        self,
        trigger_type: str,
        trigger_details: dict[str, Any],
        effects: list[dict[str, Any]],
        namespace: str,
        reason: str,
    ) -> None:
        """
        Record information about a dropped event to the WAL.

        Records the minimal information for events dropped by Load Shedding.
        """
        try:
            _append_to_wal(
                {
                    "type": "dropped",
                    "trigger_type": trigger_type,
                    "namespace": namespace,
                    "reason": reason,
                    "effects_count": len(effects),
                    "dropped_at": utc_now().isoformat(),
                }
            )
        except Exception as e:
            logger.debug(
                "cascade_audit.dropped_record_save_failed",
                error=e,
            )

    # Backward compatibility
    _record_dropped_to_fallback = _record_dropped_to_wal

    def recover_from_local_wal(  # noqa: C901, PLR0912
        self,
        namespace: str = "global",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """
        Recover from the local WAL into Redis (batch-optimized).

        After Redis recovers, migrates the events accumulated in the local
        WAL into Redis. Batch idempotency checks prevent duplicate recovery,
        and bulk index updates minimize Redis round trips.

        Args:
            namespace: namespace
            dry_run: when True, only reports the targets without recovering

        Returns:
            Recovery result statistics (including idempotency_skipped)
        """
        wal_path = Path(LOCAL_CASCADE_WAL_PATH)

        if not wal_path.exists():
            return {
                "status": "no_wal_data",
                "namespace": namespace,
                "recovered": 0,
                "failed": 0,
                "idempotency_skipped": 0,
            }

        entries = []

        # Read this namespace's events from the WAL file
        from baldur.audit.wal._jsonl import JSONLReader

        for entry in JSONLReader.iter_entries(wal_path):
            if entry.get("namespace") == namespace and entry.get("type") != "dropped":
                entries.append(entry)

        if dry_run:
            logger.info(
                "cascade_audit.wal_recovery_dry_run",
                count=len(entries),
                namespace=namespace,
            )
            return {
                "status": "dry_run",
                "namespace": namespace,
                "entries_to_recover": len(entries),
                "recovered": 0,
                "idempotency_skipped": 0,
            }

        if not entries:
            return {
                "status": "no_wal_data",
                "namespace": namespace,
                "recovered": 0,
                "failed": 0,
                "idempotency_skipped": 0,
            }

        # Batch idempotency check — skip already-recovered entries
        idempotency_skipped = 0
        entries_to_recover = []

        for i in range(0, len(entries), _BATCH_SIZE):
            batch = entries[i : i + _BATCH_SIZE]
            duplicate_indices = self._batch_check_cascade_idempotency(batch)

            for j, entry in enumerate(batch):
                if j in duplicate_indices:
                    idempotency_skipped += 1
                else:
                    entries_to_recover.append(entry)

        # Recover into Redis
        recovered = 0
        failed = 0
        recovered_ids: list[str] = []
        recovered_entries: list[dict] = []

        for entry in entries_to_recover:
            try:
                event = CascadeEvent.from_dict(entry)
                self._save_cascade_event(event)
                recovered_ids.append(event.id)
                recovered_entries.append(entry)
                recovered += 1
            except Exception as e:
                logger.exception(
                    "watchdog.recovery_failed",
                    error=e,
                )
                failed += 1

        # Batch index update (N × GET/SET → 1 GET + 1 SET)
        index_failed = False
        if recovered_ids:
            try:
                self._batch_add_to_index(namespace, recovered_ids)
            except Exception:
                logger.exception(
                    "cascade_audit.batch_index_update_failed",
                    namespace=namespace,
                    count=len(recovered_ids),
                )
                index_failed = True

        # Batch idempotency marking (only successfully recovered entries).
        # Mark even when the index update failed, to prevent duplicate data
        # being stored on the next recovery.
        if recovered_entries:
            self._batch_mark_cascade_processed(recovered_entries)

        # Remove this namespace's entries once recovery completes.
        # Keep the WAL when the index failed → the next recovery retries
        # the index.
        if (
            failed == 0
            and not index_failed
            and (recovered > 0 or idempotency_skipped > 0)
        ):
            self._remove_namespace_from_wal(namespace)

        logger.info(
            "cascade_audit.wal_recovery_completed",
            recovered=recovered,
            failed=failed,
            idempotency_skipped=idempotency_skipped,
            namespace=namespace,
        )

        return {
            "status": "completed",
            "namespace": namespace,
            "recovered": recovered,
            "failed": failed,
            "idempotency_skipped": idempotency_skipped,
            "index_failed": index_failed,
        }

    def _batch_check_cascade_idempotency(
        self,
        entries: list[dict[str, Any]],
    ) -> set[int]:
        """
        Batch idempotency check (cascade recovery).

        Checks duplicates 1000 at a time via IdempotencyService.batch_check().
        Returns an empty set when the service is unused or failing (proceed
        safely).

        Returns:
            Set of in-batch indices for duplicate entries
        """
        try:
            from baldur.services.idempotency import (
                IdempotencyKey,
                IdempotencyService,
            )

            service = IdempotencyService()
            keys = [
                IdempotencyKey.for_wal_recovery(
                    wal_entry_id=entry.get("id", ""),
                    operation="cascade_recovery",
                )
                for entry in entries
            ]
            results = service.batch_check(keys)
            duplicates = {i for i, result in enumerate(results) if result.is_duplicate}
            logger.debug(
                "cascade_audit.batch_idempotency_checked",
                batch_size=len(entries),
                duplicates_found=len(duplicates),
            )
            return duplicates

        except (ImportError, AttributeError):
            return set()

        except Exception:
            logger.warning(
                "cascade_audit.batch_idempotency_check_failed",
                batch_size=len(entries),
            )
            return set()

    def _batch_mark_cascade_processed(
        self,
        entries: list[dict[str, Any]],
    ) -> None:
        """Batch idempotency marking (cascade recovery)."""
        try:
            from baldur.services.idempotency import (
                IdempotencyKey,
                IdempotencyService,
            )

            service = IdempotencyService()
            keys = [
                IdempotencyKey.for_wal_recovery(
                    wal_entry_id=entry.get("id", ""),
                    operation="cascade_recovery",
                )
                for entry in entries
            ]
            service.batch_mark_as_processed(keys, ttl=_IDEMPOTENCY_TTL)
            logger.debug(
                "cascade_audit.batch_idempotency_marked",
                batch_size=len(entries),
            )

        except (ImportError, AttributeError):
            pass

        except Exception:
            logger.warning(
                "cascade_audit.batch_mark_processed_failed",
                batch_size=len(entries),
            )

    def _batch_add_to_index(
        self,
        namespace: str,
        cascade_ids: list[str],
    ) -> None:
        """
        Bulk-add recovered event IDs to the index.

        Uses 1 GET + 1 SET instead of N × (GET + SET) to minimize Redis
        round trips. Inserts in reverse order to keep the same
        newest-first ordering as _add_to_index.
        """
        backend = self._get_backend()
        key = self.CASCADE_INDEX_KEY.format(namespace=namespace)
        ids = get_index_ids(backend, key)

        # _add_to_index calls insert(0, id) N times → the last ID ends up first.
        # Prepend in reverse order to preserve the same ordering.
        ids = list(reversed(cascade_ids)) + ids

        if len(ids) > self._max_index_size:
            ids = ids[: self._max_index_size]

        backend.set(key, {"ids": ids})

    # Backward compatibility
    recover_from_local_fallback = recover_from_local_wal

    def _remove_namespace_from_wal(self, namespace: str) -> None:
        """Remove a specific namespace's entries from the WAL file."""
        from baldur.audit.wal._cleanup import cleanup_by_namespace

        cleanup_by_namespace(Path(LOCAL_CASCADE_WAL_PATH), namespace)

    # Backward compatibility
    _remove_namespace_from_fallback = _remove_namespace_from_wal

    def get_load_shedding_status(
        self,
        namespace: str = "global",
    ) -> dict[str, Any]:
        """
        Query the Load Shedding status.

        Args:
            namespace: namespace

        Returns:
            Load Shedding status information
        """
        load_shedding = self._get_load_shedding()

        if not load_shedding:
            return {
                "enabled": False,
                "status": "DISABLED",
            }

        # Check the buffer state
        backend = self._get_backend()
        index_key = self.CASCADE_INDEX_KEY.format(namespace=namespace)
        buffer_size = len(get_index_ids(backend, index_key))

        status = load_shedding.get_status(
            buffer_size=buffer_size,
            buffer_capacity=self._max_index_size,
        )
        status["enabled"] = True
        status["namespace"] = namespace

        return status
