"""
Redis compressed-entry cutoff query + status-stamp unit tests.

Test targets:
    - baldur.adapters.redis.dlq_compression.RedisDLQCompression
      (get_compressed_entries_before, update_compressed_status)

Test Categories:
    A. Contract: get_compressed_entries_before ordering, cutoff, status, paging
    B. Behavior: the ascending walk stops at the first entry past the cutoff
    C. Contract: update_compressed_status stamps the transition's timestamp
    D. Contract: the console listing query still returns newest-first

The backend is a fake that keeps real sorted-set ordering rather than a
MagicMock. The defect these methods exist to fix was an ordering one — the
lifecycle sweep read the newest page and so transitioned nothing — and a
MagicMock returns whatever the test hands it, passing against either
ordering.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.interfaces.repositories import DLQCompressedEntry
from baldur.utils.time import utc_now


class FakeSortedSetBackend:
    """In-process stand-in that preserves real score ordering.

    Only the operations the compression module reaches for are implemented.
    ``blob_reads`` counts *entry* fetches so a test can assert the ascending
    walk stops early instead of reading the whole index. The namespace's
    structural blobs — the migration marker and its watermark — are excluded:
    they are control-flow reads of fixed cost, and counting them would make
    every walk-cost assertion depend on how often the marker is consulted.

    ``degrade_count`` and ``is_redis_available`` mirror the real backend's
    degradation seam so a test can express "the backend dropped out mid-walk"
    without threads.
    """

    _CONTROL_BLOB_KEYS = frozenset(
        {
            "dlq:compressed:status_index_ready",
            "dlq:compressed:backfill_watermark",
        }
    )

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.blob_reads = 0
        self.get_blobs_calls = 0
        self.degrade_count = 0
        self.is_redis_available = True

    def set_blob(self, key: str, value: bytes) -> None:
        self.blobs[key] = value

    def get_blob(self, key: str) -> bytes | None:
        if key not in self._CONTROL_BLOB_KEYS:
            self.blob_reads += 1
        return self.blobs.get(key)

    def get_blobs(self, keys: list[str]) -> list[bytes | None]:
        # Batched read must charge the same per-key cost as get_blob, so a
        # cost assertion cannot be passed by accident (see D8).
        self.get_blobs_calls += 1
        return [self.get_blob(k) for k in keys]

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        """Add members, returning the count that were new — as Redis does.

        The backfill's completion criterion sums exactly this value, so a
        fake that always reported zero would let a migration conclude on a
        walk that had just filed thousands of entries.
        """
        zset = self.zsets.setdefault(key, {})
        new = sum(1 for member in mapping if member not in zset)
        zset.update(mapping)
        return new

    def zrem(self, key: str, members) -> int:
        """Remove members from a sorted set (list or single-str, as the batch
        ops emit them). Returns the count actually removed, like Redis."""
        zset = self.zsets.get(key)
        if not zset:
            return 0
        if isinstance(members, str):
            members = [members]
        removed = 0
        for m in members:
            if zset.pop(m, None) is not None:
                removed += 1
        return removed

    def _sorted_items(self, key: str) -> list[tuple[str, float]]:
        members = self.zsets.get(key, {})
        return sorted(members.items(), key=lambda kv: (kv[1], kv[0]))

    def _ordered(self, key: str) -> list[str]:
        return [m for m, _ in self._sorted_items(key)]

    def zrange(self, key: str, start: int, end: int) -> list[str]:
        ids = self._ordered(key)
        return ids[start:] if end < 0 else ids[start : end + 1]

    def zrangebyscore(
        self,
        key: str,
        min_score: float,
        max_score: float,
        *,
        offset: int = 0,
        count: int | None = None,
    ) -> list[str]:
        selected = [
            m for m, s in self._sorted_items(key) if min_score <= s <= max_score
        ]
        if count is None:
            return selected[offset:]
        return selected[offset : offset + count]

    def zrevrange(self, key: str, start: int, end: int) -> list[str]:
        ids = list(reversed(self._ordered(key)))
        return ids[start:] if end < 0 else ids[start : end + 1]

    def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    def batch_write_ops(self, ops: list[tuple]) -> None:
        for op in ops:
            getattr(self, op[0])(*op[1:])


def _make_repo(backend: FakeSortedSetBackend) -> RedisDLQRepository:
    """Build a RedisDLQRepository around the fake backend."""
    from baldur.adapters.redis.dlq_compression import RedisDLQCompression

    with patch.object(RedisDLQRepository, "__init__", lambda self, **kw: None):
        repo = RedisDLQRepository.__new__(RedisDLQRepository)
    repo._backend = backend
    repo.compression = RedisDLQCompression(repo)
    return repo


@pytest.fixture
def backend() -> FakeSortedSetBackend:
    return FakeSortedSetBackend()


@pytest.fixture
def compression(backend):
    """The repository, not the compression mixin it delegates to.

    The lifecycle sweep holds a repository, so the delegating wrapper is
    part of the call path under test. Driving the mixin directly would let
    a wrapper whose signature has drifted from the mixin's pass here and
    fail in production.
    """
    return _make_repo(backend)


def _store(compression, suffix, *, days_ago, status="active"):
    """Store a compressed entry aged ``days_ago`` through the real adapter."""
    now = utc_now()
    entry = DLQCompressedEntry(
        id=f"compressed:payment:timeout:E_X:{suffix}",
        domain="payment",
        failure_type="timeout",
        error_code="E_X",
        count=1,
        first_seen=now,
        last_seen=now,
        sample_error_message="x",
        status=status,
        compressed_at=now - timedelta(days=days_ago),
    )
    compression.store_compressed_entry(entry)
    return entry


def _suffixes(entries) -> list[str]:
    return [e.id.split(":")[-1] for e in entries]


# =============================================================================
# A. Contract — ordering, cutoff, status filter, paging
# =============================================================================


class TestRedisCompressedCutoffQueryContract:
    """get_compressed_entries_before is oldest-first, cutoff-bounded and paged."""

    def test_returns_oldest_first(self, compression):
        _store(compression, "a", days_ago=10)
        _store(compression, "b", days_ago=50)
        _store(compression, "c", days_ago=30)

        rows = compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=5)
        )

        assert _suffixes(rows) == ["b", "c", "a"]

    def test_excludes_entries_at_or_after_the_cutoff(self, compression):
        _store(compression, "old", days_ago=40)
        _store(compression, "young", days_ago=10)

        rows = compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=30)
        )

        assert _suffixes(rows) == ["old"]

    def test_filters_by_status(self, compression):
        _store(compression, "active-one", days_ago=40)
        _store(compression, "stale-one", days_ago=41, status="stale")

        rows = compression.get_compressed_entries_before(
            status="stale", before=utc_now() - timedelta(days=5)
        )

        assert _suffixes(rows) == ["stale-one"]

    def test_limit_bounds_the_page(self, compression):
        for i in range(5):
            _store(compression, f"e{i}", days_ago=40 - i)

        rows = compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=5), limit=2
        )

        assert _suffixes(rows) == ["e0", "e1"]

    def test_offset_skips_matching_entries(self, compression):
        for i in range(5):
            _store(compression, f"e{i}", days_ago=40 - i)

        rows = compression.get_compressed_entries_before(
            status="active",
            before=utc_now() - timedelta(days=5),
            limit=2,
            offset=2,
        )

        assert _suffixes(rows) == ["e2", "e3"]

    def test_offset_counts_matching_entries_not_index_positions(self, compression):
        """A non-matching entry between two matches does not consume the offset.

        The index holds every status, so if ``offset`` stepped over index
        positions instead of matches the caller's cursor would drift by the
        number of interleaved non-matching entries.
        """
        _store(compression, "m0", days_ago=50)
        _store(compression, "other", days_ago=45, status="archived")
        _store(compression, "m1", days_ago=40)

        rows = compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=5), offset=1
        )

        assert _suffixes(rows) == ["m1"]


# =============================================================================
# B. Behavior — the ascending walk stops at the cutoff
# =============================================================================


class TestRedisCompressedCutoffScanBehavior:
    """The score window bounds the scan on both ends."""

    def test_entries_past_the_cutoff_are_never_read(self, compression, backend):
        _store(compression, "old0", days_ago=50)
        _store(compression, "old1", days_ago=49)
        for i in range(20):
            _store(compression, f"young{i:02d}", days_ago=1)

        backend.blob_reads = 0
        rows = compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=30)
        )

        assert _suffixes(rows) == ["old0", "old1"]
        # The upper score bound keeps the 20 young entries out of the range
        # entirely, so only the two eligible blobs are fetched.
        assert backend.blob_reads == 2

    def test_after_keeps_the_scan_off_the_processed_prefix(self, compression, backend):
        """The lower bound is what makes a paged drain linear, not quadratic.

        A status transition leaves the entry in the index — the index is
        scored by compressed_at and is deliberately not rewritten — and the
        status lives in the blob. Without a lower bound every page would
        re-read the whole transitioned prefix at one round trip per entry.
        """
        # Ages stay well clear of the cutoff: an entry landing exactly on it
        # is correctly excluded (``before`` is exclusive) and would make the
        # counts below ambiguous on a coarse clock.
        for i in range(100):
            _store(compression, f"e{i:03d}", days_ago=200 - i)

        cutoff = utc_now() - timedelta(days=1)
        first = compression.get_compressed_entries_before(
            status="active", before=cutoff, limit=50
        )
        assert len(first) == 50
        for entry in first:
            compression.update_compressed_status(entry.id, "stale")

        backend.blob_reads = 0
        second = compression.get_compressed_entries_before(
            status="active",
            before=cutoff,
            limit=50,
            after=first[-1].compressed_at,
        )

        assert _suffixes(second) == [f"e{i:03d}" for i in range(50, 100)]
        # 50 fresh entries plus the transitioned one sitting on the cursor.
        # A head-anchored scan would read all 100.
        assert backend.blob_reads == 51


# =============================================================================
# C. Contract — status stamps
# =============================================================================


class TestRedisCompressedStatusStampParity:
    """update_compressed_status stamps the timestamp its transition implies."""

    def test_stale_transition_stamps_stale_at(self, compression):
        entry = _store(compression, "s1", days_ago=40)

        compression.update_compressed_status(entry.id, "stale")

        row = compression.get_compressed_entries(status="stale")[0]
        assert row.stale_at is not None
        assert row.archived_at is None

    def test_archived_transition_stamps_archived_at(self, compression):
        entry = _store(compression, "s2", days_ago=100, status="stale")

        compression.update_compressed_status(entry.id, "archived")

        row = compression.get_compressed_entries(status="archived")[0]
        assert row.archived_at is not None

    def test_missing_entry_returns_false(self, compression):
        assert compression.update_compressed_status("compressed:nope", "stale") is False


# =============================================================================
# D. Contract — the console listing query is unchanged
# =============================================================================


class TestRedisCompressedListingOrderContract:
    """get_compressed_entries still returns newest-first for the console."""

    def test_listing_is_newest_first(self, compression):
        _store(compression, "a", days_ago=10)
        _store(compression, "b", days_ago=50)
        _store(compression, "c", days_ago=30)

        rows = compression.get_compressed_entries()

        assert _suffixes(rows) == ["a", "c", "b"]
