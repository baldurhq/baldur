"""
Redis-based Event Journal Repository.

Redis Sorted Set-based implementation, for multi-worker environments.
Uses a monthly partitioned key structure to optimize time-range query
performance.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog

from baldur.interfaces.event_journal import (
    EventJournalRepository,
    JournalEntry,
    JournalQueryFilter,
    JournalQueryResult,
)
from baldur.utils.serialization import fast_dumps_str, fast_loads

logger = structlog.get_logger()


class RedisEventJournalRepository(EventJournalRepository):
    """
    Redis Sorted Set-based implementation, for multi-worker environments.

    Redis Key Structure:
    - baldur:journal:YYYY-MM → Sorted Set (score=sequence, member=JSON)
    - baldur:journal:sequence → atomic sequence counter

    Monthly partitioning means a time-range query only scans the keys for the
    months it covers.
    """

    KEY_PREFIX = "baldur:journal"
    SEQUENCE_KEY = "baldur:journal:sequence"

    def __init__(
        self,
        redis_client: Any,
        ttl_seconds: int = 2592000,
        max_query_limit: int = 10000,
    ):
        """
        Args:
            redis_client: Redis client instance
            ttl_seconds: Partition key TTL (default 30 days = 2592000 seconds)
            max_query_limit: Upper bound on rows returned by query()
        """
        self._redis = redis_client
        self._ttl_seconds = ttl_seconds
        self._max_query_limit = max_query_limit

    def append(self, entry: JournalEntry) -> int:
        seq = self._redis.incr(self.SEQUENCE_KEY)

        key = self._get_key(entry.timestamp)
        data = self._serialize(entry, seq)
        try:
            self._redis.zadd(key, {data: seq})
        except Exception as e:
            logger.warning("redis_journal.zadd_failed", sequence=seq, error=str(e))
            raise

        if self._redis.ttl(key) < 0:
            self._redis.expire(key, self._ttl_seconds)

        return seq

    def query(self, query_filter: JournalQueryFilter) -> JournalQueryResult:
        all_entries: list[JournalEntry] = []
        effective_limit = min(query_filter.limit, self._max_query_limit)

        keys = self._resolve_keys(query_filter)

        for key in keys:
            raw_members = self._redis.zrangebyscore(
                key,
                "-inf",
                "+inf",
                withscores=False,
            )
            for raw in raw_members:
                entry = self._deserialize(raw)
                if entry and self._matches_filter(entry, query_filter):
                    all_entries.append(entry)

        all_entries.sort(key=lambda e: e.sequence)

        total_count = len(all_entries)
        truncated = total_count > effective_limit
        entries = all_entries[:effective_limit]

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
        keys = self._get_all_active_keys()
        results: list[JournalEntry] = []

        for key in keys:
            raw_members = self._redis.zrangebyscore(
                key,
                start_sequence,
                end_sequence - 1,
                withscores=False,
            )
            for raw in raw_members:
                entry = self._deserialize(raw)
                if entry:
                    results.append(entry)

        results.sort(key=lambda e: e.sequence)
        return results

    def get_latest_sequence(self) -> int:
        val = self._redis.get(self.SEQUENCE_KEY)
        if val is None:
            return 0
        return int(val)

    def count(self, query_filter: JournalQueryFilter) -> int:
        keys = self._resolve_keys(query_filter)

        if self._has_no_entry_level_filter(query_filter):
            return sum(self._redis.zcard(key) for key in keys)

        total = 0
        for key in keys:
            raw_members = self._redis.zrangebyscore(
                key,
                "-inf",
                "+inf",
                withscores=False,
            )
            for raw in raw_members:
                entry = self._deserialize(raw)
                if entry and self._matches_filter(entry, query_filter):
                    total += 1

        return total

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _get_key(self, timestamp: datetime) -> str:
        """Return the monthly partition key for a timestamp."""
        return f"{self.KEY_PREFIX}:{timestamp.strftime('%Y-%m')}"

    def _resolve_keys(self, query_filter: JournalQueryFilter) -> list[str]:
        """Return the partition keys to scan for the given filter."""
        if query_filter.start_time and query_filter.end_time:
            return self._resolve_partition_keys(
                query_filter.start_time, query_filter.end_time
            )
        return self._get_all_active_keys()

    def _resolve_partition_keys(
        self,
        start_time: datetime,
        end_time: datetime,
    ) -> list[str]:
        """Return every monthly partition key spanned by the time range."""
        keys = []
        current = start_time.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while current < end_time:
            keys.append(f"{self.KEY_PREFIX}:{current.strftime('%Y-%m')}")
            if current.month == 12:
                current = current.replace(year=current.year + 1, month=1)
            else:
                current = current.replace(month=current.month + 1)
        return keys

    def _get_all_active_keys(self) -> list[str]:
        """Return every journal partition key that currently exists."""
        pattern = f"{self.KEY_PREFIX}:????-??"
        keys: list[str] = []
        for key in self._redis.scan_iter(match=pattern, count=200):
            keys.append(key.decode() if isinstance(key, bytes) else key)
        return keys

    def _has_no_entry_level_filter(self, query_filter: JournalQueryFilter) -> bool:
        """Check whether entry-level filtering can be skipped."""
        return (
            query_filter.event_types is None
            and query_filter.service_name is None
            and query_filter.region is None
            and query_filter.start_time is None
            and query_filter.end_time is None
            and query_filter.context_filters is None
        )

    def _serialize(self, entry: JournalEntry, seq: int) -> str:
        """Serialize a JournalEntry into a JSON string."""
        data = {
            "sequence": seq,
            "event_type": entry.event_type,
            "source": entry.source,
            "timestamp": entry.timestamp.isoformat(),
            "service_name": entry.service_name,
            "context": entry.context,
            "region": entry.region,
            "tier_id": entry.tier_id,
        }
        return fast_dumps_str(data)

    def _deserialize(self, raw: Any) -> JournalEntry | None:
        """Deserialize a JSON string into a JournalEntry."""
        try:
            data = fast_loads(raw)
            return JournalEntry(
                sequence=data["sequence"],
                event_type=data["event_type"],
                source=data["source"],
                timestamp=datetime.fromisoformat(data["timestamp"]),
                service_name=data["service_name"],
                context=data.get("context", {}),
                region=data.get("region", ""),
                tier_id=data.get("tier_id", ""),
            )
        except (ValueError, KeyError, TypeError) as e:
            logger.warning("redis_journal.entry_deserialization_failed", error=str(e))
            return None

    def _matches_filter(
        self, entry: JournalEntry, query_filter: JournalQueryFilter
    ) -> bool:
        """Check whether an entry matches the filter."""
        if (
            query_filter.event_types is not None
            and entry.event_type not in query_filter.event_types
        ):
            return False
        if (
            query_filter.service_name is not None
            and entry.service_name != query_filter.service_name
        ):
            return False
        if (
            query_filter.start_time is not None
            and entry.timestamp < query_filter.start_time
        ):
            return False
        if (
            query_filter.end_time is not None
            and entry.timestamp >= query_filter.end_time
        ):
            return False
        if query_filter.region is not None and entry.region != query_filter.region:
            return False
        if query_filter.context_filters is not None:
            for key, val in query_filter.context_filters.items():
                if str(entry.context.get(key)) != val:
                    return False
        return True
