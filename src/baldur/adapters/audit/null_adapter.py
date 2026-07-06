"""
Null Audit Log Adapter.

No-op adapter that discards all audit logs.
Useful for testing or when audit logging is disabled.
"""

from __future__ import annotations

from datetime import datetime

from baldur.interfaces.audit_adapter import (
    AuditAction,
    AuditEntry,
    AuditLogAdapter,
)


class NullAuditLogAdapter(AuditLogAdapter):
    """
    No-op audit logging adapter.

    All operations are silent no-ops. Use for:
    - Testing where audit logs are not needed
    - Explicitly disabling audit logging
    - Benchmarking without I/O overhead

    Usage:
        adapter = NullAuditLogAdapter()
        adapter.log(entry)  # Does nothing
    """

    def log(self, entry: AuditEntry) -> None:
        """No-op: discards the entry."""
        pass

    def query(
        self,
        action: AuditAction | str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        """No-op: returns empty list."""
        return []
