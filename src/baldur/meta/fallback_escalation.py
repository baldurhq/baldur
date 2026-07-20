"""
Fallback Escalation Handler.

Records escalations on local disk when Slack/PagerDuty delivery fails, so they
can be drained later or handled manually.

Capabilities:
- Records failed escalations to a JSONL file
- Memory buffer fallback (when the disk write also fails)
- Lookup and count of pending escalations
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import structlog

from baldur.core.file_utils import safe_unlink
from baldur.utils.serialization import fast_dumps_str, fast_loads
from baldur.utils.time import utc_now

logger = structlog.get_logger()

# Fallback log path
DEFAULT_ESCALATION_LOG_PATH = Path(
    os.environ.get(
        "BALDUR_EMERGENCY_ESCALATION_LOG",
        "/var/log/baldur/emergency_escalation.jsonl",
    )
)


class FallbackEscalationHandler:
    """
    Fallback escalation handler.

    Records to local disk when the Slack/PagerDuty API is unavailable. The
    records can be drained later or handled manually.

    Storage layout:
    - File: JSONL (one JSON object per line)
    - Memory buffer: fallback when the disk write fails

    Example:
        handler = FallbackEscalationHandler()

        handler.record_failed_escalation(
            component="dlq",
            title="DLQ Stuck",
            description="DLQ consumer stopped",
            level="critical",
            details={"pending_count": 1500},
            failed_channels=["pagerduty", "slack"],
            error_message="Connection timeout",
        )

        # Inspect the pending escalations
        count = handler.get_pending_count()
        entries = handler.get_pending_escalations()
    """

    def __init__(
        self,
        log_path: Path | str | None = None,
        max_buffer_size: int = 1000,
    ):
        """
        Initialize.

        Args:
            log_path: log file path (default when None)
            max_buffer_size: maximum memory buffer size
        """
        self._log_path = Path(log_path) if log_path else DEFAULT_ESCALATION_LOG_PATH
        self._lock = (
            threading.RLock()
        )  # reentrant: drain_to_file calls _write_to_file while holding it
        self._memory_buffer: list[dict[str, Any]] = []
        self._max_buffer_size = max_buffer_size

    def _ensure_directory(self) -> bool:
        """
        Create the log directory.

        Returns:
            Whether the directory is available
        """
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            return True
        except Exception as e:
            logger.exception(
                "fallback_escalation.cannot_create_directory",
                error=e,
            )
            return False

    def record_failed_escalation(
        self,
        component: str,
        title: str,
        description: str,
        level: str,
        details: dict[str, Any],
        failed_channels: list[str],
        error_message: str,
    ) -> bool:
        """
        Record a failed escalation.

        Args:
            component: component name
            title: escalation title
            description: description
            level: severity level
            details: detailed information
            failed_channels: channels that failed
            error_message: error message

        Returns:
            Whether the record was stored
        """
        entry = {
            "timestamp": utc_now().isoformat(),
            "type": "EMERGENCY_ESCALATION_FAILED",
            "component": component,
            "title": title,
            "description": description,
            "level": level,
            "details": details,
            "failed_channels": failed_channels,
            "error": error_message,
            "requires_manual_review": True,
        }

        # 1. Try writing to the file
        if self._write_to_file(entry):
            return True

        # 2. Store in the memory buffer (fallback)
        return self._write_to_memory(entry)

    def _write_to_file(self, entry: dict[str, Any]) -> bool:
        """
        Write to the file.

        Args:
            entry: entry to write

        Returns:
            Whether the write succeeded
        """
        if not self._ensure_directory():
            return False

        try:
            with self._lock, open(self._log_path, "a", encoding="utf-8") as f:
                f.write(fast_dumps_str(entry) + "\n")

            logger.warning(
                "emergency_escalation_log.recorded",
                component_name=entry["component"],
                title=entry["title"],
            )
            return True
        except Exception as e:
            logger.exception(
                "fallback_escalation.file_write_failed",
                error=e,
            )
            return False

    def _write_to_memory(self, entry: dict[str, Any]) -> bool:
        """
        Store in the memory buffer.

        Args:
            entry: entry to store

        Returns:
            Whether the entry was stored
        """
        with self._lock:
            self._memory_buffer.append(entry)
            # Cap the buffer size
            if len(self._memory_buffer) > self._max_buffer_size:
                self._memory_buffer = self._memory_buffer[-self._max_buffer_size :]

        logger.warning(
            "fallback_escalation.stored_memory_buffer_size",
            component_name=entry["component"],
            memory_buffer_count=len(self._memory_buffer),
        )
        return True

    def get_pending_escalations(self) -> list[dict[str, Any]]:
        """
        Read the pending escalations.

        Reads from both the file and the memory buffer.

        Returns:
            Pending escalations
        """
        entries: list[dict[str, Any]] = []

        # Read from the file
        if self._log_path.exists():
            try:
                with open(self._log_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            entries.append(fast_loads(line))
            except Exception as e:
                logger.exception(
                    "fallback_escalation.file_read_failed",
                    error=e,
                )

        # Append the memory buffer
        with self._lock:
            entries.extend(self._memory_buffer)

        return entries

    def get_pending_count(self) -> int:
        """
        Number of pending escalations.

        Returns:
            Pending escalation count
        """
        count = 0

        # Count the lines in the file
        if self._log_path.exists():
            try:
                with open(self._log_path, encoding="utf-8") as f:
                    count = sum(1 for line in f if line.strip())
            except Exception:
                pass

        # Count the memory buffer
        with self._lock:
            count += len(self._memory_buffer)

        return count

    def drain_to_file(self) -> int:
        """
        Drain the memory buffer to the file.

        Returns:
            Number of entries drained
        """
        with self._lock:
            if not self._memory_buffer:
                return 0

            drained = 0
            for entry in self._memory_buffer:
                if self._write_to_file(entry):
                    drained += 1

            self._memory_buffer.clear()
            return drained

    def clear_file(self) -> None:
        """Clear the file (after the entries have been handled)."""
        if safe_unlink(self._log_path):
            logger.info("fallback_escalation.file_cleared")

    def clear_memory(self) -> None:
        """Clear the memory buffer."""
        with self._lock:
            self._memory_buffer.clear()

    def clear_all(self) -> None:
        """Clear both the file and the memory buffer."""
        self.clear_file()
        self.clear_memory()


# =============================================================================
# Singleton
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

(
    get_fallback_escalation_handler,
    configure_fallback_escalation_handler,
    reset_fallback_escalation_handler,
) = make_singleton_factory("fallback_escalation_handler", FallbackEscalationHandler)
