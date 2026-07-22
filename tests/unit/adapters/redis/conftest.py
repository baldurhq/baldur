"""Shared in-process backend fake for the compressed-DLQ adapter suites.

``FakeSortedSetBackend`` preserves real sorted-set ordering and models the
resilient backend's degradation seam. Both compressed-entry suites in this
directory drive the production read paths through it: the defects those paths
exist to fix are ordering and routing ones, and a MagicMock returns whatever
the test hands it — it would pass against either ordering (§6.4).

The degradation seam is the part a mock cannot express at all. A failed read
on the real backend does not raise: it bumps ``degrade_count``, flips the mode
and answers from the process-local view, so the caller sees a plausible result
that came from nowhere. The fake reproduces that shape — an unavailable
backend answers sorted-set reads with an empty view and batched blob reads
with all-``None`` — which is what lets the verified-scan tests express
"the backend dropped out mid-walk" without threads.
"""

from __future__ import annotations

from datetime import timedelta

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
    without threads. Two knobs drive the sequences the design's verified-scan
    invariant exists for:

    ``blob_fetch_degrades_on_call``
        The listed ``get_blobs`` calls (1-based) answer all-``None`` and bump
        the counter, while availability reads keep reporting True — the
        recovery-daemon re-promotion race, where a point-in-time mode check
        after the fetch cannot tell the failed chunk from an empty one.

    ``blob_fetch_serves_stale_on_call``
        The same degrade, but the process-local store still holds the
        payloads: the call answers with content rather than ``None`` and bumps
        the counter. This is the shape a caller cannot detect at all by
        looking at the result — the payloads are plausible and possibly
        stale, and acting on them transitions entries that should not have
        transitioned.

    ``peer_degrade_after_availability_read``
        A peer thread degrades immediately after the first availability read
        reports True: the counter bumps once, every subsequent read is served
        from an empty process-local view, and background recovery re-promotes
        before the closing availability read. Nothing bumps the counter while
        already degraded, so a caller that samples the epoch *after* its
        availability read gets a post-bump value that still matches at the end
        — the whole scan looks clean. Only a sample taken first sees the
        pre-bump value.
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
        self.blob_fetch_degrades_on_call: set[int] = set()
        self.blob_fetch_serves_stale_on_call: set[int] = set()
        self.peer_degrade_after_availability_read = False
        self._is_redis_available = True
        self._serving_memory_view = False
        self._degrade_count = 0

    # -- degradation seam --------------------------------------------------

    @property
    def degrade_count(self) -> int:
        return self._degrade_count

    @degrade_count.setter
    def degrade_count(self, value: int) -> None:
        self._degrade_count = value

    @property
    def is_redis_available(self) -> bool:
        """Report availability, optionally letting a peer thread degrade first.

        The first read reports True and then applies the pending degrade, so
        the transition lands strictly *after* that read — the position from
        which a later epoch sample can no longer detect it. Reads after that
        one report True again: the recovery daemon re-promotes the mode while
        the walk is still running, which is what makes an end-of-scan mode
        check insufficient on its own.
        """
        if self.peer_degrade_after_availability_read:
            self.peer_degrade_after_availability_read = False
            self._degrade_count += 1
            self._serving_memory_view = True
        elif self._serving_memory_view:
            self._serving_memory_view = False
        return self._is_redis_available

    @is_redis_available.setter
    def is_redis_available(self, value: bool) -> None:
        self._is_redis_available = value

    def _reads_served_locally(self) -> bool:
        """True while reads come from the process-local view, not Redis."""
        return self._serving_memory_view or not self._is_redis_available

    # -- blob store --------------------------------------------------------

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
        if self.get_blobs_calls in self.blob_fetch_degrades_on_call:
            # Degrade + serve the process-local view, then let background
            # recovery re-promote before the caller inspects the results.
            self._degrade_count += 1
            return [None for _ in keys]
        if self.get_blobs_calls in self.blob_fetch_serves_stale_on_call:
            # Same degrade, but the local store still holds the payloads, so
            # the response is indistinguishable from a healthy one.
            self._degrade_count += 1
            return [self.get_blob(k) for k in keys]
        if self._reads_served_locally():
            return [None for _ in keys]
        return [self.get_blob(k) for k in keys]

    # -- sorted sets -------------------------------------------------------

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
        if self._reads_served_locally():
            return []
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
        if self._reads_served_locally():
            return []
        selected = [
            m for m, s in self._sorted_items(key) if min_score <= s <= max_score
        ]
        if count is None:
            return selected[offset:]
        return selected[offset : offset + count]

    def zrevrange(self, key: str, start: int, end: int) -> list[str]:
        if self._reads_served_locally():
            return []
        ids = list(reversed(self._ordered(key)))
        return ids[start:] if end < 0 else ids[start : end + 1]

    def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    def batch_write_ops(self, ops: list[tuple]) -> None:
        for op in ops:
            getattr(self, op[0])(*op[1:])


def _build_repo(backend: FakeSortedSetBackend) -> RedisDLQRepository:
    """Build a RedisDLQRepository around the fake backend."""
    from unittest.mock import patch

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
    return _build_repo(backend)


@pytest.fixture
def store_compressed(compression):
    """Store a compressed entry through the real adapter write path."""

    def _store(suffix, *, days_ago, status="active", domain="payment"):
        now = utc_now()
        entry = DLQCompressedEntry(
            id=f"compressed:{domain}:timeout:E_X:{suffix}",
            domain=domain,
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

    return _store


@pytest.fixture
def rewrite_blob_status(backend):
    """Apply only the payload write of a transition (its first op).

    A transition writes the payload first, then relocates the index
    membership, so a crash between the two leaves exactly this state: a
    payload naming the new status while the indexes still name the old one.
    """

    def _rewrite(entry, new_status: str):
        from baldur.utils.serialization import fast_dumps, fast_loads

        key = f"dlq:compressed:{entry.id}"
        data = fast_loads(backend.blobs[key])
        data["status"] = new_status
        backend.set_blob(key, fast_dumps(data))

    return _rewrite


@pytest.fixture
def mark_index_ready(backend):
    """Stamp the migration-completion marker the status routes are gated on."""

    def _mark():
        from baldur.adapters.redis.dlq_compression import _COMPRESSED_MARKER_KEY
        from baldur.utils.serialization import fast_dumps

        backend.set_blob(
            _COMPRESSED_MARKER_KEY,
            fast_dumps({"stamped_at": utc_now().isoformat(), "source": "test"}),
        )

    return _mark
