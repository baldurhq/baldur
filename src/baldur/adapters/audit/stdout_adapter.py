"""
Stdout Audit Log Adapter.

Logs audit entries to stdout in JSON format.
Ideal for containerized environments where logs are collected from stdout.
"""

from __future__ import annotations

import sys
from datetime import datetime

import structlog

from baldur.interfaces.audit_adapter import (
    AuditAction,
    AuditEntry,
    AuditLogAdapter,
)

logger = structlog.get_logger()


class StdoutAuditLogAdapter(AuditLogAdapter):
    """
    Stdout-based audit logging adapter.

    Features:
    - JSON output to stdout
    - Optional pretty printing
    - Works with log aggregators (Fluentd, Filebeat, etc.)

    Usage:
        adapter = StdoutAuditLogAdapter()
        adapter.log(AuditEntry(action=AuditAction.CB_FORCE_OPEN, ...))
    """

    def __init__(self, pretty: bool = False, prefix: str = "[AUDIT] "):
        """
        Initialize stdout audit adapter.

        Args:
            pretty: If True, format JSON with indentation
            prefix: Prefix to add before each log line
        """
        self.pretty = pretty
        self.prefix = prefix

    def log(self, entry: AuditEntry) -> None:
        """Log an audit entry to stdout."""
        if self.pretty:
            from baldur.utils.serialization import fast_dumps_pretty

            output = fast_dumps_pretty(entry.to_dict(), default=str)
        else:
            output = entry.to_json()

        print(f"{self.prefix}{output}", file=sys.stdout, flush=True)

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
        Query is not supported for stdout adapter.

        Returns empty list. Use FileAuditLogAdapter or database adapter
        if you need query capabilities.
        """
        logger.warning("stdout_audit_adapter.query_not_supported")
        return []
