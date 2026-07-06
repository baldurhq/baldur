"""
File-based Audit Log Adapter.

Logs audit entries to JSON files with automatic rotation support.
Non-invasive - does not require database tables or external services.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import structlog

from baldur.interfaces.audit_adapter import (
    AuditAction,
    AuditEntry,
    AuditLogAdapter,
)
from baldur.utils.time import utc_now

logger = structlog.get_logger()


class FileAuditLogAdapter(AuditLogAdapter):
    """
    File-based audit logging adapter.

    Features:
    - JSON Lines format (one JSON object per line)
    - Automatic directory creation
    - Daily rotation support (optional)
    - Thread-safe file writes

    Usage:
        adapter = FileAuditLogAdapter("logs/audit.log")
        adapter.log(AuditEntry(action=AuditAction.CB_FORCE_OPEN, ...))
    """

    def __init__(
        self,
        file_path: str | Path,
        rotate_daily: bool = False,
        max_file_size_mb: int | None = None,
    ):
        """
        Initialize file audit adapter.

        Args:
            file_path: Path to audit log file
            rotate_daily: If True, append date to filename
            max_file_size_mb: Max file size before rotation (not implemented)
        """
        self.base_path = Path(file_path)
        self.rotate_daily = rotate_daily
        self.max_file_size_mb = max_file_size_mb

        # Ensure directory exists
        self.base_path.parent.mkdir(parents=True, exist_ok=True)

    def _get_current_file_path(self) -> Path:
        """Get current file path (with date if rotating)."""
        if self.rotate_daily:
            date_str = utc_now().strftime("%Y-%m-%d")
            name = f"{self.base_path.stem}_{date_str}{self.base_path.suffix}"
            return self.base_path.parent / name
        return self.base_path

    def log(self, entry: AuditEntry) -> None:
        """Log an audit entry to file."""
        file_path = self._get_current_file_path()

        try:
            with open(file_path, "a", encoding="utf-8") as f:
                if isinstance(entry, dict):
                    # The Redis audit-buffer flush (RedisAuditBuffer.
                    # flush_to_external_safe) hands the registered adapter the
                    # raw buffered dict, since the file adapter implements
                    # neither log_batch nor log_raw. Persist it directly rather
                    # than calling AuditEntry.to_json() on a dict.
                    f.write(json.dumps(entry, default=str) + "\n")
                else:
                    f.write(entry.to_json() + "\n")
        except Exception as e:
            logger.exception(
                "file_audit_log_adapter.write_audit_log_failed",
                error=e,
            )

    def _get_log_files(self) -> list[Path]:
        """Get list of log files sorted by modification time (newest first)."""
        if self.rotate_daily:
            pattern = f"{self.base_path.stem}_*{self.base_path.suffix}"
            log_files = list(self.base_path.parent.glob(pattern))
        else:
            log_files = [self.base_path] if self.base_path.exists() else []

        log_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return log_files

    def _entry_matches_filters(
        self,
        entry: AuditEntry,
        action: AuditAction | str | None,
        target_type: str | None,
        target_id: str | None,
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> bool:
        """Check if entry matches all provided filters."""
        if action and self._get_action_value(entry.action) != self._get_action_value(
            action
        ):
            return False
        if target_type and entry.target_type != target_type:
            return False
        if target_id and entry.target_id != target_id:
            return False
        if start_time and entry.timestamp < start_time:
            return False
        return not (end_time and entry.timestamp > end_time)

    def _parse_entries_from_file(
        self,
        log_file: Path,
        action: AuditAction | str | None,
        target_type: str | None,
        target_id: str | None,
        start_time: datetime | None,
        end_time: datetime | None,
        limit: int,
        current_count: int,
    ) -> list[AuditEntry]:
        """Parse and filter entries from a single log file."""
        entries: list[AuditEntry] = []
        try:
            with open(log_file, encoding="utf-8") as f:
                for line in f:
                    if current_count + len(entries) >= limit:
                        break
                    try:
                        data = json.loads(line.strip())
                        entry = self._dict_to_entry(data)
                        if self._entry_matches_filters(
                            entry, action, target_type, target_id, start_time, end_time
                        ):
                            entries.append(entry)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.warning(
                "file_audit_log_adapter.error_reading",
                log_file=log_file,
                error=e,
            )
        return entries

    def query(
        self,
        action: AuditAction | str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        """
        Query audit logs from file.

        Note: This is a simple implementation that reads all files.
        For production use with large volumes, consider a database adapter.
        """
        entries: list[AuditEntry] = []
        log_files = self._get_log_files()

        for log_file in log_files:
            if len(entries) >= limit:
                break
            file_entries = self._parse_entries_from_file(
                log_file,
                action,
                target_type,
                target_id,
                start_time,
                end_time,
                limit,
                len(entries),
            )
            entries.extend(file_entries)

        return entries[:limit]

    def _get_action_value(self, action: AuditAction | str) -> str:
        """Get string value of action."""
        return action.value if isinstance(action, AuditAction) else action

    def _dict_to_entry(self, data: dict) -> AuditEntry:
        """Convert dictionary to AuditEntry."""
        timestamp = data.get("timestamp")
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        elif timestamp is None:
            timestamp = utc_now()

        # Try to parse action as AuditAction enum
        action_str = data.get("action", "")
        try:
            action = AuditAction(action_str)
        except ValueError:
            action = action_str

        return AuditEntry(
            action=action,
            timestamp=timestamp,
            actor_id=data.get("actor_id"),
            actor_type=data.get("actor_type", "system"),
            target_type=data.get("target_type"),
            target_id=data.get("target_id"),
            service_name=data.get("service_name"),
            domain=data.get("domain"),
            reason=data.get("reason"),
            details=data.get("details", {}),
            success=data.get("success", True),
            error_message=data.get("error_message"),
        )
