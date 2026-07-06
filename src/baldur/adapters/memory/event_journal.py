"""
In-Memory Event Journal Repository.

Thread-safe in-memory implementation. For tests and single-process use.
Follows the InMemoryCircuitBreakerStateRepository (adapters/memory/circuit_breaker.py) pattern.
"""

from __future__ import annotations

import threading

import structlog

from baldur.interfaces.event_journal import (
    EventJournalRepository,
    JournalEntry,
    JournalQueryFilter,
    JournalQueryResult,
)

logger = structlog.get_logger()


class InMemoryEventJournalRepository(EventJournalRepository):
    """Thread-safe in-memory implementation. For tests and single-process use."""

    def __init__(
        self,
        max_entries: int = 10000,
        max_query_limit: int = 10000,
    ):
        self._entries: list[JournalEntry] = []
        self._lock = threading.RLock()
        self._next_sequence = 1
        self._max_entries = max_entries
        self._max_query_limit = max_query_limit

    def append(self, entry: JournalEntry) -> int:
        with self._lock:
            seq = self._next_sequence
            self._next_sequence += 1
            stored = JournalEntry(
                sequence=seq,
                event_type=entry.event_type,
                source=entry.source,
                timestamp=entry.timestamp,
                service_name=entry.service_name,
                context=entry.context,
                region=entry.region,
                tier_id=entry.tier_id,
            )
            self._entries.append(stored)
            if len(self._entries) > self._max_entries:
                self._entries = self._entries[-self._max_entries :]
            return seq

    def query(self, query_filter: JournalQueryFilter) -> JournalQueryResult:
        with self._lock:
            matched = self._apply_filter(query_filter)
            total_count = len(matched)
            effective_limit = min(query_filter.limit, self._max_query_limit)
            truncated = total_count > effective_limit
            entries = matched[:effective_limit]
            return JournalQueryResult(
                entries=entries,
                truncated=truncated,
                total_count=total_count,
            )

    def get_sequence_range(
        self,
        start_sequence: int,
        end_sequence: int,
    ) -> list[JournalEntry]:
        with self._lock:
            return [
                e for e in self._entries if start_sequence <= e.sequence < end_sequence
            ]

    def get_latest_sequence(self) -> int:
        with self._lock:
            if not self._entries:
                return 0
            return self._entries[-1].sequence

    def count(self, query_filter: JournalQueryFilter) -> int:
        with self._lock:
            return len(self._apply_filter(query_filter))

    def _apply_filter(self, query_filter: JournalQueryFilter) -> list[JournalEntry]:  # noqa: C901
        """Return entries matching the filter, sorted by sequence ascending."""
        results: list[JournalEntry] = []
        for entry in self._entries:
            if (
                query_filter.event_types is not None
                and entry.event_type not in query_filter.event_types
            ):
                continue
            if (
                query_filter.service_name is not None
                and entry.service_name != query_filter.service_name
            ):
                continue
            if (
                query_filter.start_time is not None
                and entry.timestamp < query_filter.start_time
            ):
                continue
            if (
                query_filter.end_time is not None
                and entry.timestamp >= query_filter.end_time
            ):
                continue
            if query_filter.region is not None and entry.region != query_filter.region:
                continue
            if query_filter.context_filters is not None:
                match = True
                for key, val in query_filter.context_filters.items():
                    if str(entry.context.get(key)) != val:
                        match = False
                        break
                if not match:
                    continue
            results.append(entry)
        return results
