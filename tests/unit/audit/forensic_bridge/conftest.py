"""
Forensic Bridge 테스트 공통 설정.

이 패키지의 모든 테스트에서 사용하는 fixtures와 mocks.
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from typing import Any

import pytest

from baldur.interfaces.audit_adapter import (
    AuditAction,
    AuditEntry,
    AuditLogAdapter,
)

# =============================================================================
# Mock Classes
# =============================================================================


class MockAuditAdapter(AuditLogAdapter):
    """Test audit adapter conforming to the real ``AuditLogAdapter`` contract.

    Captures each logged ``AuditEntry`` into the historical dict shape so the
    consuming WAL tests keep filtering on ``["event_type"]`` unchanged
    (WAL meta-events merge their emitting component into ``details["source"]``).
    Because the public surface is ``log()`` / ``query()`` only, a reintroduced
    phantom ``log_event`` call raises ``AttributeError`` — the regression guard.
    """

    def __init__(self):
        self.events: list[dict[str, Any]] = []

    def log(self, entry: AuditEntry) -> None:
        self.events.append(
            {
                "event_type": (
                    entry.action.value
                    if isinstance(entry.action, AuditAction)
                    else entry.action
                ),
                "source": entry.details.get("source"),
                "details": entry.details,
                "service_name": entry.service_name,
            }
        )

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

    def get_events_by_type(self, event_type: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event_type"] == event_type]

    def clear(self) -> None:
        self.events.clear()


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_audit_adapter():
    """Fresh MockAuditAdapter for each test."""
    return MockAuditAdapter()


@pytest.fixture
def temp_wal_dir():
    """임시 WAL 디렉토리."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir
