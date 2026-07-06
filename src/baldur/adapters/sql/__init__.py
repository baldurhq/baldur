"""
Framework-free SQL adapter (DB-API 2.0).

Provides a generic repository base plus priority-1 SQL-backed
implementations of Baldur's core repositories. Works with any
DB-API 2.0 driver — psycopg2, mysql-connector-python, or the stdlib
sqlite3 — selected by DSN scheme.

Status: Public
"""

# Reference: 429 Part 4.

from __future__ import annotations

from baldur.adapters.sql.base import (
    GenericSQLRepository,
    SchemaVersionManager,
    sql_transaction,
)
from baldur.adapters.sql.cascade_event import SQLCascadeEventArchiveRepository
from baldur.adapters.sql.circuit_breaker import SQLCircuitBreakerStateRepository
from baldur.adapters.sql.event_journal import SQLEventJournalRepository
from baldur.adapters.sql.failed_operation import SQLFailedOperationRepository
from baldur.adapters.sql.postmortem import SQLPostmortemRepository
from baldur.adapters.sql.recovery_session import SQLRecoverySessionArchiveRepository
from baldur.adapters.sql.security_incident import SQLSecurityIncidentRepository
from baldur.adapters.sql.statistics import SQLStatisticsRepository

__all__ = [
    "GenericSQLRepository",
    "SchemaVersionManager",
    "sql_transaction",
    "SQLCascadeEventArchiveRepository",
    "SQLCircuitBreakerStateRepository",
    "SQLEventJournalRepository",
    "SQLFailedOperationRepository",
    "SQLPostmortemRepository",
    "SQLRecoverySessionArchiveRepository",
    "SQLSecurityIncidentRepository",
    "SQLStatisticsRepository",
]
