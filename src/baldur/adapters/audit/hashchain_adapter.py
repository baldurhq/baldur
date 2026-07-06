"""
Hash-chain-protected file audit adapter.

416 Part 6 (D6): the H2 ``LocalFileBackend`` write path was relocated
into this new ``AuditLogAdapter`` implementation. The class composes
``HashChainManager`` (or ``RedisHashChainManager`` in distributed mode)
and writes the **same dict schema** that ``LocalFileBackend.write()``
previously wrote. This preserves byte-for-byte compatibility with
existing ``audit_{date}.jsonl`` files and ``.hash_chain_state.json``
state — no schema migration, no compliance regression.

D22: cross-process file lock and Redis-backed distributed hash chain
support are wired through constructor parameters.

D23: per-service partitioning splits the JSONL filename and the hash
chain state path so multiple services (web pod, celery worker, cron)
can share the same audit volume without contending on a single chain.

Reference: 416
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from baldur.audit.integrity import (
    DailyHashAnchor,
    HashChainManager,
    HashChainManagerProtocol,
    PendingSequenceManager,
    RedisHashChainManager,
)
from baldur.audit.masking import (
    mask_ip,
    mask_sensitive_fields,
)
from baldur.audit.trace import get_trace_id, get_trace_id_full
from baldur.interfaces.audit_adapter import (
    AuditAction,
    AuditEntry,
    AuditLogAdapter,
    ContextType,
)
from baldur.utils.serialization import fast_dumps_str, fast_loads
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = ["HashChainFileAuditLogAdapter"]


# Default sensitive field names redacted from old_value/new_value payloads.
_DEFAULT_SENSITIVE_FIELDS = [
    "password",
    "secret",
    "token",
    "api_key",
    "private_key",
    "credit_card",
]


class HashChainFileAuditLogAdapter(AuditLogAdapter):
    """File-based audit adapter with hash-chain integrity (D6).

    Owns its own write path — does NOT delegate to ``FileAuditLogAdapter``.
    The dict schema written to disk matches the previous H2
    ``LocalFileBackend`` schema so existing ``.hash_chain_state.json``
    state remains verifiable across the migration.

    Multi-writer safety (D22):
        - When ``distributed_hash_chain=True`` and a Redis client is
          provided, ``RedisHashChainManager`` serializes sequence
          allocation through Redis ``INCR``.
        - Otherwise the local ``HashChainManager`` is used with
          ``use_file_lock=True`` (default), which acquires an exclusive
          cross-process lock on a sibling ``.lock`` file before each
          state update.

    Partitioning (D23):
        When ``partition`` is non-empty, the JSONL file becomes
        ``audit_{date}_{partition}.jsonl`` and the hash chain state
        becomes ``.hash_chain_state.{partition}.json``. Empty partition
        preserves the legacy filenames for backward compatibility.
    """

    DEFAULT_LOG_DIR = "logs/audit"

    def __init__(
        self,
        log_dir: str | None = None,
        filename_pattern: str | None = None,
        enable_hash_chain: bool = True,
        rotate_daily: bool = True,
        distributed_hash_chain: bool = False,
        redis_client: Any | None = None,
        redis_key_prefix: str = "baldur:",
        enable_pending_manager: bool = True,
        enable_anchor_backup: bool = True,
        use_file_lock: bool = True,
        partition: str = "",
        mask_ip_addresses: bool = True,
        sensitive_fields: list[str] | None = None,
    ):
        """Initialize hash-chain file audit adapter.

        Args:
            log_dir: Directory for audit logs. Defaults to ``logs/audit``.
            filename_pattern: Optional override for the filename pattern.
                When ``None``, the partition-aware default is used:
                ``audit_{date}.jsonl`` (empty partition) or
                ``audit_{date}_{partition}.jsonl`` (partitioned).
            enable_hash_chain: Enable hash chain integrity (recommended).
            rotate_daily: Create new file each day.
            distributed_hash_chain: Use Redis-based distributed hash chain.
            redis_client: Redis client for distributed mode.
            redis_key_prefix: Key prefix for Redis keys.
            enable_pending_manager: Enable PENDING state tracking for
                Write-Ahead Checkpoint atomicity.
            enable_anchor_backup: Enable offline anchor backup writes.
            use_file_lock: Enable cross-process file lock (D22).
            partition: Per-service partition identifier (D23). Empty
                string preserves legacy filenames for backward compat.
            mask_ip_addresses: GDPR/CCPA IP masking for events that
                carry an ``ip_address`` field in ``details``.
            sensitive_fields: List of field names to redact from
                ``old_value``/``new_value`` payloads.
        """
        self._log_dir = Path(log_dir or self.DEFAULT_LOG_DIR)
        self._partition = partition
        self._rotate_daily = rotate_daily
        self._enable_hash_chain = enable_hash_chain
        self._enable_anchor_backup = enable_anchor_backup
        self._mask_ip = mask_ip_addresses
        self._sensitive_fields = sensitive_fields or list(_DEFAULT_SENSITIVE_FIELDS)

        self._lock = threading.RLock()
        self._current_file: Path | None = None
        self._file_handle: Any = None
        self._last_anchor_date: str | None = None

        # Resolve filename pattern (D23 partition-aware default).
        if filename_pattern is not None:
            self._filename_pattern = filename_pattern
        elif self._partition:
            self._filename_pattern = "audit_{date}_" + self._partition + ".jsonl"
        else:
            self._filename_pattern = "audit_{date}.jsonl"

        logger.debug(
            "audit.partition_resolved",
            partition=self._partition or "default",
        )

        # Resolve hash chain state file path (D23 partition-aware).
        state_filename = (
            f".hash_chain_state.{self._partition}.json"
            if self._partition
            else ".hash_chain_state.json"
        )
        state_file = self._log_dir / state_filename

        # Hash chain manager — distributed (Redis) or local (file-locked).
        self._hash_chain: HashChainManagerProtocol | None
        if enable_hash_chain:
            if distributed_hash_chain and redis_client is not None:
                local_fallback = HashChainManager(
                    state_file, use_file_lock=use_file_lock
                )
                self._hash_chain = RedisHashChainManager(
                    redis_client=redis_client,
                    key_prefix=(
                        f"{redis_key_prefix}hashchain:{self._partition or 'default'}:"
                    ),
                    fallback_manager=local_fallback,
                )
                logger.info("hash_chain.distributed_mode_enabled")
            else:
                if distributed_hash_chain:
                    logger.warning("hash_chain.distributed_mode_unavailable")
                self._hash_chain = HashChainManager(
                    state_file, use_file_lock=use_file_lock
                )
        else:
            self._hash_chain = None

        # Pending sequence manager — only useful with Redis.
        if (
            enable_pending_manager
            and distributed_hash_chain
            and redis_client is not None
        ):
            self._pending_manager: PendingSequenceManager | None = (
                PendingSequenceManager(
                    redis_client=redis_client,
                    key_prefix=redis_key_prefix,
                )
            )
        else:
            self._pending_manager = None

        # Anchor backup — only available when Redis is reachable.
        if enable_anchor_backup and redis_client is not None:
            self._anchor_manager: DailyHashAnchor | None = DailyHashAnchor(
                redis_client=redis_client,
                key_prefix=redis_key_prefix,
            )
        else:
            self._anchor_manager = None

        # Ensure directory exists.
        self._log_dir.mkdir(parents=True, exist_ok=True)

    # =========================================================================
    # Public AuditLogAdapter contract
    # =========================================================================

    def log(self, entry: AuditEntry) -> None:
        """Log an audit entry by relocating it into the H2 dict schema.

        D6: the dict schema produced here matches the previous
        ``LocalFileBackend.write()`` output, so existing files and
        existing hash chain state remain verifiable.

        A write failure is logged and then **re-raised** so callers on the
        ``AuditLogAdapter`` contract (notably the recovery-replay sync worker)
        can distinguish a real write from a lost one. Swallowing here would
        make a failed write look like success to the sync worker, advancing
        its cursor and deleting the WAL entry — silent audit loss for a
        compliance-grade store.
        """
        with self._lock:
            try:
                event_dict = self._entry_to_event_dict(entry)
                payload = self._build_entry(event_dict)
                self._write_dict(payload)
            except Exception as e:
                logger.exception(
                    "hash_chain_file_audit.log_failed",
                    error=e,
                )
                raise

    def query(
        self,
        action: AuditAction | str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        """Query audit logs (returns H1 ``AuditEntry`` instances per D19).

        Reads JSONL files in the log directory matching this adapter's
        partition, parses the H2 dict schema written by ``log()``, and
        maps each row back to an H1 ``AuditEntry``. The mapping is the
        inverse of ``_build_entry()``.
        """
        results: list[AuditEntry] = []
        try:
            log_files = sorted(self._log_dir.glob(self._glob_pattern()), reverse=True)
            for log_file in log_files:
                if len(results) >= limit:
                    break
                with open(log_file, encoding="utf-8") as f:
                    for line in f:
                        if len(results) >= limit:
                            break
                        row = self._parse_row(line)
                        if row is None:
                            continue
                        entry = self._row_to_entry(row)
                        if not self._entry_matches(
                            entry,
                            action,
                            target_type,
                            target_id,
                            start_time,
                            end_time,
                        ):
                            continue
                        results.append(entry)
        except Exception as e:
            logger.exception(
                "hash_chain_file_audit.query_failed",
                error=e,
            )
        return results[:limit]

    def verify_integrity(self) -> tuple[bool, list[dict[str, Any]]]:
        """Verify integrity of all log files in this adapter's partition."""
        from baldur.audit.integrity import verify_audit_log_integrity

        issues: list[dict[str, Any]] = []
        try:
            for log_file in self._log_dir.glob(self._glob_pattern()):
                is_valid, file_issues = verify_audit_log_integrity(log_file)
                if not is_valid:
                    issues.append(
                        {
                            "file": str(log_file),
                            "issues": file_issues,
                        }
                    )
        except Exception as e:
            logger.exception(
                "hash_chain_file_audit.verify_integrity_failed",
                error=e,
            )
            return False, [{"type": "verify_error", "message": str(e)}]
        return len(issues) == 0, issues

    def close(self) -> None:
        """Close any open file handles and persist hash chain state."""
        with self._lock:
            self._close_file()
            if isinstance(self._hash_chain, HashChainManager):
                self._hash_chain._save_state()

    # =========================================================================
    # Internal write path (relocated from LocalFileBackend.write)
    # =========================================================================

    def _write_dict(self, entry_dict: dict[str, Any]) -> None:
        """Write a pre-built dict via the WAC pattern (D6)."""
        sequence = None
        expected_hash = None
        try:
            if self._hash_chain:
                entry_dict = self._hash_chain.add_integrity(entry_dict)
                integrity = entry_dict.get("integrity", {})
                sequence = integrity.get("sequence")
                expected_hash = integrity.get("current_hash")

            if sequence and expected_hash and self._pending_manager:
                self._pending_manager.reserve_sequence(sequence, expected_hash)

            self._check_anchor_backup()

            if not self._ensure_file_open():
                # A file-open failure (permission denied, ENOSPC, fd
                # exhaustion, read-only mount) is a real write failure: raise
                # rather than silently returning, so a caller on the
                # AuditLogAdapter contract -- notably the recovery-replay sync
                # worker -- surfaces the loss and holds its cursor instead of
                # treating an unwritten entry as a success and deleting its WAL
                # record. The outer except aborts the reserved sequence and
                # re-raises (single abort). _ensure_file_open has already logged
                # the original open failure.
                raise OSError(
                    f"failed to open audit log file for write: {self._current_file}"
                )

            json_line = fast_dumps_str(entry_dict, default=str)
            assert self._file_handle is not None
            self._file_handle.write(json_line + "\n")
            self._file_handle.flush()

            if sequence and self._pending_manager:
                self._pending_manager.commit_sequence(sequence)
        except Exception as e:
            logger.exception(
                "hash_chain_file_audit.write_failed",
                error=e,
            )
            if sequence and self._pending_manager:
                self._pending_manager.abort_sequence(sequence)
            raise

    def _ensure_file_open(self) -> bool:
        """Open the current log file, rotating if the date has changed."""
        try:
            target_file = self._get_current_log_file()
            if self._current_file != target_file:
                self._close_file()
                self._current_file = target_file
                self._file_handle = open(target_file, "a", encoding="utf-8")  # noqa: SIM115
            return True
        except Exception as e:
            logger.exception(
                "hash_chain_file_audit.open_log_file_failed",
                error=e,
            )
            return False

    def _close_file(self) -> None:
        if self._file_handle:
            try:
                self._file_handle.flush()
                self._file_handle.close()
            except Exception:
                pass
            self._file_handle = None

    def _get_current_log_file(self) -> Path:
        date_str = utc_now().strftime("%Y-%m-%d") if self._rotate_daily else "all"
        filename = self._filename_pattern.format(date=date_str)
        return self._log_dir / filename

    def _glob_pattern(self) -> str:
        """Glob pattern for files this adapter manages.

        For empty partition: ``audit_*.jsonl`` (matches legacy files).
        For partitioned: ``audit_*_{partition}.jsonl``.
        """
        if self._partition:
            return f"audit_*_{self._partition}.jsonl"
        return "audit_*.jsonl"

    def _check_anchor_backup(self) -> None:
        """Create anchor backup at day boundary."""
        if not self._anchor_manager or not self._enable_anchor_backup:
            return
        try:
            today = utc_now().strftime("%Y-%m-%d")
            if self._last_anchor_date is not None and self._last_anchor_date != today:
                self._anchor_manager.create_anchor(date=self._last_anchor_date)
            self._last_anchor_date = today
        except Exception as e:
            logger.warning(
                "hash_chain_file_audit.anchor_backup_failed",
                error=e,
            )

    # =========================================================================
    # H1 AuditEntry  ↔  H2 dict schema mapping
    # =========================================================================

    def _entry_to_event_dict(self, entry: AuditEntry) -> dict[str, Any]:
        """Adapt an H1 ``AuditEntry`` into the legacy event_dict shape that
        ``_build_entry()`` consumes.

        Pulls config-change-specific fields out of ``entry.details`` if
        present so the resulting H2 dict schema is identical to the
        ``LocalFileBackend.write()`` output for ``log_config_change()``
        callers.
        """
        details = dict(entry.details or {})
        return {
            "config_type": entry.target_type or details.get("config_type", "unknown"),
            "config_key": entry.target_id or details.get("config_key", ""),
            "action": (
                entry.action.value
                if isinstance(entry.action, AuditAction)
                else entry.action
            ),
            "old_value": details.pop("old_value", None),
            "new_value": details.pop("new_value", None),
            "reason": entry.reason,
            "user": entry.actor_id or "system",
            "ip_address": details.pop("ip_address", None),
            "user_agent": details.pop("user_agent", None),
            "source": details.pop("source", "api"),
            "apply_strategy": details.pop("apply_strategy", None),
            "apply_delay_seconds": details.pop("apply_delay_seconds", None),
            "metadata": details,
            "actor_type": entry.actor_type,
            "actor_roles": list(entry.actor_roles),
            "context_type": (
                entry.context_type.value
                if isinstance(entry.context_type, ContextType)
                else entry.context_type
            ),
            "service_name": entry.service_name,
            "domain": entry.domain,
            "success": entry.success,
            "error_message": entry.error_message,
            "h1_timestamp": entry.timestamp,
        }

    def _build_entry(self, event_dict: dict[str, Any]) -> dict[str, Any]:
        """Build the H2 dict schema (relocated from audit/logger.py:262).

        Applies the existing GDPR/CCPA IP masking pipeline and W3C trace
        correlation. The output dict is byte-compatible with the
        previous ``LocalFileBackend.write()`` schema so existing
        ``audit_{date}.jsonl`` files remain verifiable.
        """
        now = utc_now()

        if self._mask_ip and event_dict.get("ip_address"):
            event_dict["ip_address"] = mask_ip(event_dict["ip_address"])

        if event_dict.get("old_value") is not None:
            event_dict["old_value"] = mask_sensitive_fields(
                event_dict["old_value"],
                self._sensitive_fields,
            )
        if event_dict.get("new_value") is not None:
            event_dict["new_value"] = mask_sensitive_fields(
                event_dict["new_value"],
                self._sensitive_fields,
            )

        trace_id = get_trace_id()
        trace_id_full = get_trace_id_full()

        entry: dict[str, Any] = {
            "timestamp": now.isoformat(),
            "trace_id": trace_id,
            "trace_id_full": trace_id_full,
            "event_type": "config_change",
            "actor": {
                "user": event_dict.get("user") or "system",
                "ip_address": event_dict.get("ip_address"),
                "user_agent": event_dict.get("user_agent"),
                "source": event_dict.get("source", "api"),
            },
            "change": {
                "config_type": event_dict.get("config_type"),
                "config_key": event_dict.get("config_key"),
                "action": event_dict.get("action"),
                "old_value": event_dict.get("old_value"),
                "new_value": event_dict.get("new_value"),
                "reason": event_dict.get("reason"),
            },
            "apply_strategy": (
                {
                    "strategy": event_dict.get("apply_strategy"),
                    "delay_seconds": event_dict.get("apply_delay_seconds"),
                }
                if event_dict.get("apply_strategy")
                else None
            ),
            "metadata": event_dict.get("metadata", {}),
        }

        if self._partition:
            entry["partition"] = self._partition

        # Drop None top-level keys for cleaner output (parity with old logger).
        return {k: v for k, v in entry.items() if v is not None}

    def _parse_row(self, line: str) -> dict[str, Any] | None:
        line = line.strip()
        if not line:
            return None
        try:
            return fast_loads(line)
        except (ValueError, TypeError):
            return None

    def _row_to_entry(self, row: dict[str, Any]) -> AuditEntry:  # noqa: C901
        """Map an H2 dict row back to an H1 ``AuditEntry`` (D19-A field map).

        Mapping table:
            ``change.config_type``  → ``target_type``
            ``change.config_key``   → ``target_id``
            ``change.action``       → ``action``
            ``change.reason``       → ``reason``
            ``actor.user``          → ``actor_id``
            ``change.old_value``    → ``details.old_value``
            ``change.new_value``    → ``details.new_value``
            ``actor.ip_address``    → ``details.ip_address``
            ``metadata.*``          → ``details.*``
            ``integrity``           → ``details.integrity``
            ``trace_id``/``trace_id_full`` → ``details.trace_id*``
        """
        change = row.get("change", {}) or {}
        actor = row.get("actor", {}) or {}
        metadata = row.get("metadata", {}) or {}

        action_str = change.get("action", "")
        try:
            action: AuditAction | str = AuditAction(action_str)
        except ValueError:
            action = action_str or AuditAction.CONFIG_CHANGE

        ts_str = row.get("timestamp")
        if isinstance(ts_str, str):
            try:
                timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                timestamp = utc_now()
        else:
            timestamp = utc_now()

        details: dict[str, Any] = dict(metadata)
        if "old_value" in change:
            details["old_value"] = change.get("old_value")
        if "new_value" in change:
            details["new_value"] = change.get("new_value")
        if actor.get("ip_address"):
            details["ip_address"] = actor.get("ip_address")
        if actor.get("user_agent"):
            details["user_agent"] = actor.get("user_agent")
        if actor.get("source"):
            details["source"] = actor.get("source")
        if row.get("integrity"):
            details["integrity"] = row.get("integrity")
        if row.get("trace_id"):
            details["trace_id"] = row.get("trace_id")
        if row.get("trace_id_full"):
            details["trace_id_full"] = row.get("trace_id_full")

        return AuditEntry(
            action=action,
            timestamp=timestamp,
            actor_id=actor.get("user"),
            actor_type="user" if actor.get("user") else "system",
            target_type=change.get("config_type"),
            target_id=change.get("config_key"),
            reason=change.get("reason"),
            details=details,
        )

    @staticmethod
    def _entry_matches(
        entry: AuditEntry,
        action: AuditAction | str | None,
        target_type: str | None,
        target_id: str | None,
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> bool:
        if action is not None:
            entry_action = (
                entry.action.value
                if isinstance(entry.action, AuditAction)
                else entry.action
            )
            filter_action = action.value if isinstance(action, AuditAction) else action
            if entry_action != filter_action:
                return False
        if target_type and entry.target_type != target_type:
            return False
        if target_id and entry.target_id != target_id:
            return False
        if start_time and entry.timestamp < start_time:
            return False
        return not (end_time and entry.timestamp > end_time)
