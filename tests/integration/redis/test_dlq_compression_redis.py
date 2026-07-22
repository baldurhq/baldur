"""Real-Redis integration tests for the bounded compressed-read surface (721).

What these test that the in-memory / mock round-trip cannot:

- The **reserved-key guard's reason to exist** (D2). Only a real Redis raises
  WRONGTYPE when a ``GET`` lands on a sorted set, and only the real
  ``ResilientStorageBackend`` answers that with ``_switch_to_degraded()``. The
  guard is asserted against the live degrade path, with the counterfactual
  (an unguarded raw read on the index ZSET) shown to actually degrade.
- The **by-id read reaching past the newest-1000 window** (D2/G3) over a real
  sorted-set index that a ``limit=1000`` scan genuinely truncates.
- The **summary's chunked MGET + cap rail** (D4) over real Redis: exactness
  below the cap and newest-``cap`` windowing above it.

Auto-skips when Redis is unavailable via the conftest ``requires_redis`` marker
autoskip hook.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.requires_redis


from baldur.interfaces.repositories import DLQCompressedEntry
from baldur.utils.time import utc_now

_SUMMARY_SCAN_CAP = "baldur.adapters.redis.dlq_compression._summary_scan_cap"


@pytest.fixture(autouse=True)
def _reset_redis_unavailable_flag():
    """Reset the runtime-scoped Redis negative cache so backend can init Redis."""
    from baldur.adapters.redis import _redis_state

    state = _redis_state()
    state.unavailable = False
    state.fail_time = 0.0
    yield
    state.unavailable = False
    state.fail_time = 0.0


def _entry(
    entry_id: str, *, seconds_old: int = 0, count: int = 3, status: str = "active"
) -> DLQCompressedEntry:
    now = utc_now()
    return DLQCompressedEntry(
        id=entry_id,
        domain="payment",
        failure_type="timeout",
        error_code="E001",
        count=count,
        first_seen=now - timedelta(days=7),
        last_seen=now,
        sample_error_message="Connection timeout",
        status=status,
        compressed_at=now - timedelta(seconds=seconds_old),
    )


class TestCompressedReservedKeyGuardOverRedis:
    """The reserved-name guard against a real WRONGTYPE-driven degrade (D2)."""

    def test_reserved_key_name_read_returns_none_and_never_degrades(
        self, redis_dlq_repository
    ):
        """
        Purpose:
            A reserved structural key name supplied as an entry id is read
            through the guard, which returns None without touching Redis.
        Expected:
            - store promotes the backend to REDIS mode (not degraded)
            - get_compressed_entry("index") returns None
            - the backend stays in REDIS mode (the guard prevented the read)
        """
        repo = redis_dlq_repository
        repo.store_compressed_entry(_entry("compressed:payment:timeout:E001:1"))
        backend = repo._backend
        assert backend.is_degraded is False

        assert repo.get_compressed_entry("index") is None

        assert backend.is_degraded is False

    def test_unguarded_read_of_index_zset_degrades_backend(self, redis_dlq_repository):
        """
        Purpose:
            Prove the guard's reason to exist: a raw read of the structural
            index key (bypassing the guard) hits a sorted set and degrades.
        Expected:
            - the backend starts in REDIS mode after a store
            - get_blob on the index ZSET raises WRONGTYPE internally, which the
              backend answers by switching to degraded mode
        """
        repo = redis_dlq_repository
        repo.store_compressed_entry(_entry("compressed:payment:timeout:E001:1"))
        backend = repo._backend
        assert backend.is_degraded is False

        # Bypass the guard: read the index sorted set as if it were a blob.
        backend.get_blob("dlq:compressed:index")

        assert backend.is_degraded is True


class TestCompressedByIdOverRedis:
    """The by-id read reaches entries the newest-1000 window scan misses (G3)."""

    def test_by_id_read_finds_entry_the_1000_window_scan_misses(
        self, redis_dlq_repository
    ):
        """
        Purpose:
            An entry older than the newest 1000 is unreachable by the old
            newest-first ``limit=1000`` scan but is returned by the by-id read.
        Expected:
            - the oldest id is absent from get_compressed_entries(limit=1000)
            - get_compressed_entry(oldest_id) returns it
        """
        repo = redis_dlq_repository
        oldest_id = "compressed:payment:timeout:E001:0000"
        repo.store_compressed_entry(_entry(oldest_id, seconds_old=2000))
        for i in range(1, 1001):
            repo.store_compressed_entry(
                _entry(f"compressed:payment:timeout:E001:{i:04d}", seconds_old=2000 - i)
            )

        window = repo.get_compressed_entries(limit=1000)
        assert oldest_id not in {e.id for e in window}

        fetched = repo.get_compressed_entry(oldest_id)
        assert fetched is not None
        assert fetched.id == oldest_id
        assert fetched.domain == "payment"


class TestCompressedSummaryBoundOverRedis:
    """The chunked-MGET summary and its cap rail over real Redis (D4)."""

    def test_summary_below_cap_is_exact(self, redis_dlq_repository):
        """
        Purpose:
            Below the cap the chunked-MGET walk aggregates every entry exactly.
        Expected:
            - total_summaries == number stored
            - total_compressed_items == the summed counts
            - no summary_truncated flag
        """
        repo = redis_dlq_repository
        for i in range(5):
            repo.store_compressed_entry(
                _entry(f"compressed:payment:timeout:E001:{i}", count=i + 1)
            )

        summary = repo.get_compressed_summary()

        assert summary["total_summaries"] == 5
        assert summary["total_compressed_items"] == 1 + 2 + 3 + 4 + 5
        assert summary["by_status"]["active"] == 5
        assert "summary_truncated" not in summary

    def test_summary_above_cap_windows_to_newest_cap(self, redis_dlq_repository):
        """
        Purpose:
            Above the cap the summary windows to the newest ``cap`` entries and
            flags the response as truncated.
        Expected:
            - total_summaries == full zcard (not the windowed count)
            - summary_truncated is True
            - by_status covers exactly the newest ``cap`` entries
        """
        repo = redis_dlq_repository
        for i in range(150):
            repo.store_compressed_entry(
                _entry(f"compressed:payment:timeout:E001:{i:04d}", seconds_old=150 - i)
            )

        with patch(_SUMMARY_SCAN_CAP, return_value=100):
            summary = repo.get_compressed_summary()

        assert summary["total_summaries"] == 150
        assert summary["summary_truncated"] is True
        assert summary["by_status"]["active"] == 100
