"""Audit adapter doubles for tests.

The audit destination is behind ``AuditLogAdapter``, whose public surface is
``log()`` / ``query()`` only. A bare ``Mock()`` would accept a call to a method
the contract does not have — the exact shape of the phantom-method regression
this double exists to catch — so it subclasses the real ABC instead.
"""

from __future__ import annotations

from datetime import datetime

from baldur.interfaces.audit_adapter import (
    AuditAction,
    AuditEntry,
    AuditLogAdapter,
)

__all__ = ["CapturingAuditAdapter"]


class CapturingAuditAdapter(AuditLogAdapter):
    """Audit adapter that keeps every delivered ``AuditEntry``.

    Set ``raise_on_log`` to make delivery fail — the fault must actually fire
    for a fail-open assertion to mean anything.
    """

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []
        self.raise_on_log: Exception | None = None

    def log(self, entry: AuditEntry) -> None:
        if self.raise_on_log is not None:
            raise self.raise_on_log
        self.entries.append(entry)

    def query(
        self,
        action: AuditAction | str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        return []

    def actions(self) -> list[AuditAction | str]:
        """Every delivered entry's ``action``, in delivery order."""
        return [e.action for e in self.entries]

    def entries_for(self, action: str) -> list[AuditEntry]:
        return [e for e in self.entries if e.action == action]
