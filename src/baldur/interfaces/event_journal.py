"""
Event Journal Repository Interface.

An append-only storage interface that records and queries Baldur decision
events in a sequence-guaranteed form.

Used as the simulation data source for the Config Shadow Evaluator (299).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# =============================================================================
# Data Models
# =============================================================================


@dataclass(frozen=True)
class JournalEntry:
    """Event journal entry. Immutable (frozen) data."""

    sequence: int
    event_type: str
    source: str
    timestamp: datetime
    service_name: str
    context: dict[str, Any] = field(default_factory=dict)

    region: str = ""
    tier_id: str = ""


@dataclass
class JournalQueryFilter:
    """Journal query filter."""

    event_types: list[str] | None = None
    service_name: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    region: str | None = None
    limit: int = 1000
    context_filters: dict[str, str] | None = None


@dataclass(frozen=True)
class JournalQueryResult:
    """Journal query result. Includes whether the result was truncated."""

    entries: list[JournalEntry]
    truncated: bool
    total_count: int | None = None


# =============================================================================
# Repository Interface
# =============================================================================


class EventJournalRepository(ABC):
    """
    Baldur event journal storage interface.

    Append-only storage. Recorded entries cannot be modified or deleted.
    Sequence numbers increase monotonically to guarantee order.
    Gaps may exist (e.g., 1, 2, 4, 5); consumers must not assume contiguity.

    Implementations:
    - InMemoryEventJournalRepository: for tests and single process
    - RedisEventJournalRepository: for multi-worker environments
    """

    @abstractmethod
    def append(self, entry: JournalEntry) -> int:
        """
        Append an event to the journal.

        Args:
            entry: Journal entry (the sequence field is assigned by the implementation)

        Returns:
            The assigned sequence number
        """
        ...

    @abstractmethod
    def query(self, query_filter: JournalQueryFilter) -> JournalQueryResult:
        """
        Return entries matching the filter, sorted by sequence (ascending).

        Args:
            query_filter: Query criteria

        Returns:
            JournalQueryResult — entries (sequence ascending), truncated flag, total_count
        """
        ...

    @abstractmethod
    def get_sequence_range(
        self,
        start_sequence: int,
        end_sequence: int,
    ) -> list[JournalEntry]:
        """
        Look up entries by sequence range.

        Used by simulations to replay a precise range.

        Args:
            start_sequence: Start sequence (inclusive)
            end_sequence: End sequence (exclusive)

        Returns:
            List of entries sorted by sequence ascending
        """
        ...

    @abstractmethod
    def get_latest_sequence(self) -> int:
        """Return the current latest sequence number. 0 when empty."""
        ...

    @abstractmethod
    def count(self, query_filter: JournalQueryFilter) -> int:
        """Return the number of entries matching the filter."""
        ...


# =============================================================================
# Lifecycle Interface (not implemented in MVP)
# =============================================================================


class EventJournalLifecycle(ABC):
    """
    Journal data lifecycle management. A separate operations-facing interface.

    Decoupled from the append-only EventJournalRepository so that archive
    and purge responsibilities can be managed independently.

    Not implemented in the MVP; activated when Tiered Storage is introduced.
    """

    @abstractmethod
    def archive_older_than(self, cutoff: datetime) -> int:
        """Move entries older than ``cutoff`` to Cold Storage. Returns the number moved."""
        ...

    @abstractmethod
    def purge_archived(self, before: datetime) -> int:
        """Delete archived entries older than ``before``. Returns the number deleted."""
        ...
