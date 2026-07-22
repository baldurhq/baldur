"""
Redis compressed-entry cutoff query + status-stamp unit tests.

Test targets:
    - baldur.adapters.redis.dlq_compression.RedisDLQCompression
      (get_compressed_entries_before, update_compressed_status,
      backfill_compressed_status_index, the walk's chunk/epoch gate and the
      post-walk index repair)

Test Categories:
    A. Contract: get_compressed_entries_before ordering, cutoff, status, paging
    B. Behavior: the ascending walk stops at the first entry past the cutoff
    C. Contract: update_compressed_status stamps the transition's timestamp
    D. Contract: the console listing query still returns newest-first
    E. Behavior: the walk's per-chunk degrade epoch gates transitions
    F. Behavior: post-walk index repair — relocation and unreadable eviction
    G. Behavior: the backfill's scan modes and gap healing
    H. Behavior: the verified-scan invariant that gates progress state
    I. Behavior: completion stamping across walks
    J. Contract: the stability-chain helper

The backend is a fake that keeps real sorted-set ordering rather than a
MagicMock (``tests.factories.redis.FakeSortedSetBackend``, wired by the
directory conftest). The defect these methods exist to fix was an ordering one
— the lifecycle sweep read the newest page and so transitioned nothing — and a
MagicMock returns whatever the test hands it, passing against either ordering.
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.redis.dlq_compression import (
    _BACKFILL_RESCAN_SLACK_SECONDS,
    _BACKFILL_STABILITY_MIN_SECONDS,
    _COMPRESSED_BY_DOMAIN_PREFIX,
    _COMPRESSED_INDEX_KEY,
    _COMPRESSED_MARKER_KEY,
    _COMPRESSED_STATUS_DOMAIN_PREFIX,
    _COMPRESSED_STATUS_PREFIX,
    _COMPRESSED_WATERMARK_KEY,
    _SUMMARY_MGET_CHUNK,
    RedisDLQCompression,
    _stability_chain_matured,
)
from baldur.utils.time import utc_now

_UTC_NOW = "baldur.adapters.redis.dlq_compression.utc_now"


def _suffixes(entries) -> list[str]:
    return [e.id.split(":")[-1] for e in entries]


def _status_key(status: str) -> str:
    return f"{_COMPRESSED_STATUS_PREFIX}{status}"


def _composite_key(status: str, domain: str = "payment") -> str:
    return f"{_COMPRESSED_STATUS_DOMAIN_PREFIX}{status}:{domain}"


def _strip_per_status_membership(backend, entry) -> None:
    """Reduce a stored entry to its pre-migration shape.

    Entries written before the per-status family existed live only in the
    all-statuses ``index`` / ``by_domain`` sets. Stripping the newer
    memberships from a normally-stored entry reproduces exactly that state,
    which is the population the backfill exists to reach.
    """
    backend.zrem(_status_key(entry.status), entry.id)
    backend.zrem(_composite_key(entry.status, entry.domain), entry.id)


def _read_watermark(backend) -> dict:
    from baldur.utils.serialization import fast_loads

    blob = backend.blobs.get(_COMPRESSED_WATERMARK_KEY)
    return fast_loads(blob) if blob else {}


# =============================================================================
# A. Contract — ordering, cutoff, status filter, paging
# =============================================================================


class TestRedisCompressedCutoffQueryContract:
    """get_compressed_entries_before is oldest-first, cutoff-bounded and paged."""

    def test_returns_oldest_first(self, compression, store_compressed):
        store_compressed("a", days_ago=10)
        store_compressed("b", days_ago=50)
        store_compressed("c", days_ago=30)

        rows = compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=5)
        )

        assert _suffixes(rows) == ["b", "c", "a"]

    def test_excludes_entries_at_or_after_the_cutoff(
        self, compression, store_compressed
    ):
        store_compressed("old", days_ago=40)
        store_compressed("young", days_ago=10)

        rows = compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=30)
        )

        assert _suffixes(rows) == ["old"]

    def test_filters_by_status(self, compression, store_compressed):
        store_compressed("active-one", days_ago=40)
        store_compressed("stale-one", days_ago=41, status="stale")

        rows = compression.get_compressed_entries_before(
            status="stale", before=utc_now() - timedelta(days=5)
        )

        assert _suffixes(rows) == ["stale-one"]

    def test_limit_bounds_the_page(self, compression, store_compressed):
        for i in range(5):
            store_compressed(f"e{i}", days_ago=40 - i)

        rows = compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=5), limit=2
        )

        assert _suffixes(rows) == ["e0", "e1"]

    def test_offset_skips_matching_entries(self, compression, store_compressed):
        for i in range(5):
            store_compressed(f"e{i}", days_ago=40 - i)

        rows = compression.get_compressed_entries_before(
            status="active",
            before=utc_now() - timedelta(days=5),
            limit=2,
            offset=2,
        )

        assert _suffixes(rows) == ["e2", "e3"]

    def test_offset_counts_matching_entries_not_index_positions(
        self, compression, store_compressed
    ):
        """A non-matching entry between two matches does not consume the offset.

        The index holds every status, so if ``offset`` stepped over index
        positions instead of matches the caller's cursor would drift by the
        number of interleaved non-matching entries.
        """
        store_compressed("m0", days_ago=50)
        store_compressed("other", days_ago=45, status="archived")
        store_compressed("m1", days_ago=40)

        rows = compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=5), offset=1
        )

        assert _suffixes(rows) == ["m1"]


# =============================================================================
# B. Behavior — the ascending walk stops at the cutoff
# =============================================================================


class TestRedisCompressedCutoffScanBehavior:
    """The score window bounds the scan on both ends."""

    def test_entries_past_the_cutoff_are_never_read(
        self, compression, backend, store_compressed
    ):
        store_compressed("old0", days_ago=50)
        store_compressed("old1", days_ago=49)
        for i in range(20):
            store_compressed(f"young{i:02d}", days_ago=1)

        backend.blob_reads = 0
        rows = compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=30)
        )

        assert _suffixes(rows) == ["old0", "old1"]
        # The upper score bound keeps the 20 young entries out of the range
        # entirely, so only the two eligible blobs are fetched.
        assert backend.blob_reads == 2

    def test_after_keeps_the_scan_off_the_processed_prefix(
        self, compression, backend, store_compressed
    ):
        """The lower bound is what makes a paged drain linear, not quadratic.

        Pre-migration lane (no completion marker): the walk is over the
        all-statuses index, so a transitioned entry stays in the walked key
        and the status filter runs off its blob. Without a lower bound every
        page would re-read the whole transitioned prefix at one round trip
        per entry.
        """
        # Ages stay well clear of the cutoff: an entry landing exactly on it
        # is correctly excluded (``before`` is exclusive) and would make the
        # counts below ambiguous on a coarse clock.
        for i in range(100):
            store_compressed(f"e{i:03d}", days_ago=200 - i)

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

    def test_per_status_lane_stops_reading_the_transitioned_cursor_entry(
        self, compression, backend, store_compressed, mark_index_ready
    ):
        """Post-migration twin: the cursor entry left the walked key entirely.

        The transitioned entry is ``zrem``d from ``status:active``, so unlike
        the pre-migration lane above it is not re-read to have its blob status
        checked — 50 reads, not 51.
        """
        for i in range(100):
            store_compressed(f"e{i:03d}", days_ago=200 - i)
        mark_index_ready()

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
        assert backend.blob_reads == 50

    def test_stale_lane_never_reads_the_archived_prefix(
        self, compression, backend, store_compressed, mark_index_ready
    ):
        """The terminal ARCHIVED prefix is not in the walked key (SB-040).

        The unbounded term in the pre-migration sweep was the archive: every
        run re-read one blob per already-archived entry before reaching any
        work, so the cost grew with lifetime volume forever. Archived entries
        are not in ``status:stale``, so the walk's cost is the live STALE
        population and nothing else.
        """
        for i in range(5000):
            store_compressed(f"arch{i:04d}", days_ago=300, status="archived")
        for i in range(50):
            store_compressed(f"stale{i:02d}", days_ago=200, status="stale")
        mark_index_ready()

        backend.blob_reads = 0
        rows = compression.get_compressed_entries_before(
            status="stale", before=utc_now() - timedelta(days=100), limit=100
        )

        assert len(rows) == 50
        assert backend.blob_reads == 50

    @pytest.mark.parametrize("marker_set", [False, True], ids=["legacy", "per_status"])
    def test_walk_fetches_one_blob_batch_per_chunk_not_one_per_member(
        self, compression, backend, store_compressed, mark_index_ready, marker_set
    ):
        """Blobs come one chunk per round trip, on both lanes.

        A chunk-sized window costs a single batched fetch; the per-member
        primitive would cost one round trip per walked member. The per-key
        read accounting is unchanged, so the walk-cost assertions elsewhere in
        this suite keep their meaning.
        """
        for i in range(_SUMMARY_MGET_CHUNK):
            store_compressed(f"e{i:04d}", days_ago=200)
        if marker_set:
            mark_index_ready()

        backend.blob_reads = 0
        backend.get_blobs_calls = 0
        rows = compression.get_compressed_entries_before(
            status="active",
            before=utc_now() - timedelta(days=100),
            limit=_SUMMARY_MGET_CHUNK,
        )

        assert len(rows) == _SUMMARY_MGET_CHUNK
        assert backend.get_blobs_calls == 1
        assert backend.blob_reads == _SUMMARY_MGET_CHUNK

    def test_legacy_lane_still_drains_an_entry_that_predates_the_status_keys(
        self, compression, backend, store_compressed
    ):
        """A migration that never runs must cost visibility, not transitions.

        An entry compressed before the per-status family existed lives only in
        the all-statuses index. With no marker the walk reads that index, so
        the entry is still found and still transitions — which is what makes
        the switch safe to ship ahead of the migration rather than with it.
        """
        legacy = store_compressed("legacy", days_ago=50)
        _strip_per_status_membership(backend, legacy)

        rows = compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=5)
        )

        assert _suffixes(rows) == ["legacy"]
        assert compression.update_compressed_status(legacy.id, "stale") is True
        assert (
            compression.get_compressed_entries_before(
                status="active", before=utc_now() - timedelta(days=5)
            )
            == []
        )

    def test_per_status_lane_offset_counts_matches_and_skips_a_stray(
        self,
        compression,
        backend,
        store_compressed,
        mark_index_ready,
        rewrite_blob_status,
    ):
        """A crash-window stray consumes no offset and is not transitioned.

        The stray sits in ``status:active`` while its blob says ``archived``
        (a transition that stopped after its blob write). It must be skipped
        for transition purposes exactly like a non-matching member on the
        pre-migration lane — and repaired afterwards rather than served.
        """
        store_compressed("m0", days_ago=50)
        stray = store_compressed("stray", days_ago=45)
        store_compressed("m1", days_ago=40)
        rewrite_blob_status(stray, "archived")
        mark_index_ready()

        rows = compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=5), offset=1
        )

        assert _suffixes(rows) == ["m1"]
        # The stray was relocated to the key its blob names, not transitioned.
        assert stray.id in backend.zsets[_status_key("archived")]
        assert stray.id not in backend.zsets[_status_key("active")]


# =============================================================================
# C. Contract — status stamps
# =============================================================================


class TestRedisCompressedStatusStampParity:
    """update_compressed_status stamps the timestamp its transition implies."""

    def test_stale_transition_stamps_stale_at(self, compression, store_compressed):
        entry = store_compressed("s1", days_ago=40)

        compression.update_compressed_status(entry.id, "stale")

        row = compression.get_compressed_entries(status="stale")[0]
        assert row.stale_at is not None
        assert row.archived_at is None

    def test_archived_transition_stamps_archived_at(
        self, compression, store_compressed
    ):
        entry = store_compressed("s2", days_ago=100, status="stale")

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

    def test_listing_is_newest_first(self, compression, store_compressed):
        store_compressed("a", days_ago=10)
        store_compressed("b", days_ago=50)
        store_compressed("c", days_ago=30)

        rows = compression.get_compressed_entries()

        assert _suffixes(rows) == ["a", "c", "b"]


# =============================================================================
# E. Behavior — the walk's per-chunk degrade epoch
# =============================================================================


class TestRedisCompressedWalkEpochGate:
    """A chunk fetched across a degrade contributes nothing to the walk.

    The batched fetch widens a failed read's blast radius from one member to
    a whole chunk, and the caller *acts* on what the walk returns: a stale
    payload can archive an entry that should not have been archived, and
    ARCHIVED is terminal. So a chunk whose fetch coincided with the backend
    leaving Redis yields no entries at all.
    """

    def test_degraded_chunk_yields_no_entries_and_removes_nothing(
        self, compression, backend, store_compressed, mark_index_ready
    ):
        for i in range(3):
            store_compressed(f"e{i}", days_ago=50)
        mark_index_ready()
        backend.blob_fetch_degrades_on_call = {1}

        rows = compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=5)
        )

        # No entries to transition, and the all-None chunk is not read as
        # "these members are unreadable" — they keep their membership.
        assert rows == []
        assert len(backend.zsets[_status_key("active")]) == 3

    def test_chunk_served_from_a_stale_local_view_yields_no_entries(
        self, compression, backend, store_compressed, mark_index_ready
    ):
        """Plausible payloads from a degraded fetch are still not acted on.

        This is the shape no result inspection can catch: the chunk answers
        with content, not with ``None``. Acting on it archives entries off a
        stale payload, and ARCHIVED is terminal — no lane walks it again.
        Only the epoch around the fetch separates it from a healthy chunk.
        """
        for i in range(3):
            store_compressed(f"e{i}", days_ago=50)
        mark_index_ready()
        backend.blob_fetch_serves_stale_on_call = {1}

        rows = compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=5)
        )

        assert rows == []

    def test_stale_chunk_collects_no_relocation_repair(
        self,
        compression,
        backend,
        store_compressed,
        mark_index_ready,
        rewrite_blob_status,
    ):
        """A repair computed from a degraded chunk is not issued.

        Relocation is not gated by the walk-wide epoch — it is add-forward and
        idempotent — so the per-chunk gate is the only thing standing between
        a stale payload and an index rewritten to match it.
        """
        stray = store_compressed("stray", days_ago=50)
        rewrite_blob_status(stray, "stale")
        mark_index_ready()
        backend.blob_fetch_serves_stale_on_call = {1}

        compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=5)
        )

        assert stray.id in backend.zsets[_status_key("active")]
        assert stray.id not in backend.zsets.get(_status_key("stale"), {})

    def test_walk_continues_past_a_degraded_chunk_to_the_next_one(
        self, compression, backend, store_compressed, mark_index_ready
    ):
        """The skipped chunk does not end the walk — the next one still yields."""
        total = _SUMMARY_MGET_CHUNK + 100
        for i in range(total):
            store_compressed(f"e{i:04d}", days_ago=200)
        mark_index_ready()
        backend.blob_fetch_degrades_on_call = {1}

        rows = compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=100), limit=total
        )

        assert len(rows) == 100
        assert _suffixes(rows) == [
            f"e{i:04d}" for i in range(_SUMMARY_MGET_CHUNK, total)
        ]

    def test_members_skipped_by_a_degraded_chunk_are_returned_by_the_next_run(
        self, compression, backend, store_compressed, mark_index_ready
    ):
        """Skipping is a delay, never a loss: nothing about the member changed."""
        for i in range(3):
            store_compressed(f"e{i}", days_ago=50)
        mark_index_ready()
        backend.blob_fetch_degrades_on_call = {1}
        assert (
            compression.get_compressed_entries_before(
                status="active", before=utc_now() - timedelta(days=5)
            )
            == []
        )

        backend.blob_fetch_degrades_on_call = set()
        rows = compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=5)
        )

        assert _suffixes(rows) == ["e0", "e1", "e2"]


# =============================================================================
# F. Behavior — post-walk index repair
# =============================================================================


class TestRedisCompressedIndexRepair:
    """Strays are repaired after the paging loop, never inside it."""

    # The transition's op list (723 D9), blob first then add-before-remove.
    _TRANSITION_OPS = [
        "set_blob",
        "zadd new-status",
        "zadd new-composite",
        "zrem old-status",
        "zrem old-composite",
    ]

    @staticmethod
    def _apply_transition_prefix(
        backend, rewrite_blob_status, entry, new_status: str, applied: int
    ) -> None:
        """Apply the first ``applied`` ops of a blob-first transition."""
        if applied >= 1:
            rewrite_blob_status(entry, new_status)
        if applied >= 2:
            backend.zadd(_status_key(new_status), {entry.id: 1.0})
        if applied >= 3:
            backend.zadd(_composite_key(new_status, entry.domain), {entry.id: 1.0})
        if applied >= 4:
            backend.zrem(_status_key(entry.status), entry.id)
        if applied >= 5:
            backend.zrem(_composite_key(entry.status, entry.domain), entry.id)

    @pytest.mark.parametrize(
        "applied",
        range(1, len(_TRANSITION_OPS) + 1),
        ids=[f"prefix_{n}" for n in range(1, len(_TRANSITION_OPS) + 1)],
    )
    def test_every_transition_crash_prefix_converges_to_the_blob_status(
        self,
        compression,
        backend,
        store_compressed,
        mark_index_ready,
        rewrite_blob_status,
        applied,
    ):
        """A partially applied transition ends up filed under its blob's status.

        With the blob written first, an interrupted transition always leaves
        the payload ahead of the indexes — so the repair completes it forward
        rather than undoing it. Prefixes that already removed the old
        membership are past the sweep's reach and must already be converged.
        """
        entry = store_compressed("c1", days_ago=50)
        self._apply_transition_prefix(
            backend, rewrite_blob_status, entry, "stale", applied
        )
        mark_index_ready()

        compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=5)
        )

        assert entry.id in backend.zsets[_status_key("stale")]
        assert entry.id in backend.zsets[_composite_key("stale")]
        assert entry.id not in backend.zsets.get(_status_key("active"), {})

    def test_repair_is_idempotent_across_repeated_walks(
        self,
        compression,
        backend,
        store_compressed,
        mark_index_ready,
        rewrite_blob_status,
    ):
        """The second walk finds nothing left to repair."""
        entry = store_compressed("c1", days_ago=50)
        rewrite_blob_status(entry, "stale")
        mark_index_ready()

        with capture_logs() as logs:
            compression.get_compressed_entries_before(
                status="active", before=utc_now() - timedelta(days=5)
            )
            after_first = {k: dict(v) for k, v in backend.zsets.items()}
            compression.get_compressed_entries_before(
                status="active", before=utc_now() - timedelta(days=5)
            )

        assert backend.zsets == after_first
        repaired = [e for e in logs if e["event"] == "dlq.compressed_index_repaired"]
        assert len(repaired) == 1
        assert repaired[0]["relocated"] == 1

    def test_repair_flushes_when_the_walk_ends_on_its_limit(
        self,
        compression,
        backend,
        store_compressed,
        mark_index_ready,
        rewrite_blob_status,
    ):
        """A full page ends the walk from inside the chunk — the flush still runs.

        The repair is issued from a ``finally``, so every exit path carries it.
        This pins the one that returns from inside the member loop.
        """
        stray = store_compressed("stray", days_ago=60)
        rewrite_blob_status(stray, "stale")
        store_compressed("m0", days_ago=50)
        store_compressed("m1", days_ago=40)
        mark_index_ready()

        rows = compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=5), limit=1
        )

        assert _suffixes(rows) == ["m0"]
        assert stray.id in backend.zsets[_status_key("stale")]
        assert stray.id not in backend.zsets[_status_key("active")]

    def test_unreadable_member_leaves_the_per_status_keys_but_not_the_index(
        self, compression, backend, store_compressed, mark_index_ready
    ):
        """An unreadable member stops occupying page slots — it is not deleted.

        The permanent all-statuses family is untouched, so the id stays
        recoverable by walking the index; only the per-status membership that
        no reader can act on goes away.
        """
        entry = store_compressed("ghost", days_ago=50)
        del backend.blobs[f"dlq:compressed:{entry.id}"]
        mark_index_ready()

        rows = compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=5)
        )

        assert rows == []
        assert entry.id not in backend.zsets[_status_key("active")]
        assert entry.id in backend.zsets[_COMPRESSED_INDEX_KEY]
        assert entry.id in backend.zsets[f"{_COMPRESSED_BY_DOMAIN_PREFIX}payment"]

    def test_unreadable_removal_is_suppressed_when_a_later_chunk_degrades(
        self, compression, backend, store_compressed, mark_index_ready
    ):
        """One bad chunk vetoes the whole walk's removals, not just its own.

        The member here is collected by a *clean* chunk, so the per-chunk gate
        has already let it through; only the walk-wide epoch check stops the
        eviction. A degrade anywhere in the walk means the process-local view
        was in play, and a member that reads as absent under it may be alive.
        """
        ghost = store_compressed("ghost", days_ago=200)
        del backend.blobs[f"dlq:compressed:{ghost.id}"]
        for i in range(_SUMMARY_MGET_CHUNK):
            store_compressed(f"e{i:04d}", days_ago=100)
        mark_index_ready()
        backend.blob_fetch_serves_stale_on_call = {2}

        compression.get_compressed_entries_before(
            status="active",
            before=utc_now() - timedelta(days=5),
            limit=_SUMMARY_MGET_CHUNK + 1,
        )

        assert ghost.id in backend.zsets[_status_key("active")]

    def test_degraded_chunk_never_evicts_a_live_member(
        self, compression, backend, store_compressed, mark_index_ready
    ):
        """A blip that recovers before the results are inspected removes nothing.

        The degraded fetch answers all-``None`` for members that are alive,
        and background recovery can re-promote the backend before the walk
        looks at the results — so a point-in-time availability check would
        read those live members as unreadable and evict a whole chunk of them
        permanently. The epoch is what makes the difference decidable.
        """
        entry = store_compressed("alive", days_ago=50)
        mark_index_ready()
        backend.blob_fetch_degrades_on_call = {1}
        zrem_calls = []
        real_zrem = backend.zrem
        backend.zrem = lambda key, members: (
            zrem_calls.append((key, members)),
            real_zrem(key, members),
        )[1]

        compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=5)
        )

        assert backend.is_redis_available is True  # recovery re-promoted
        assert zrem_calls == []
        assert entry.id in backend.zsets[_status_key("active")]


# =============================================================================
# G. Behavior — backfill scan modes
# =============================================================================


class TestCompressedBackfillModes:
    """Full walk before the marker, watermark-anchored tail scan after it."""

    def test_full_walk_files_pre_migration_entries_under_their_status(
        self, compression, backend, store_compressed
    ):
        legacy = [
            store_compressed(f"e{i}", days_ago=50, status="stale") for i in range(3)
        ]
        for entry in legacy:
            _strip_per_status_membership(backend, entry)

        result = compression.backfill_compressed_status_index()

        assert result["mode"] == "full"
        assert result["walked"] == 3
        assert result["added"] == 6  # per-status + composite for each member
        assert set(backend.zsets[_status_key("stale")]) == {e.id for e in legacy}
        assert set(backend.zsets[_composite_key("stale")]) == {e.id for e in legacy}

    def test_second_full_walk_over_the_same_index_adds_nothing(
        self, compression, backend, store_compressed
    ):
        """Re-adding a member with its own score is a no-op — the walk is safe
        to repeat, and the zero it reports is the coverage property itself."""
        entry = store_compressed("e0", days_ago=50)
        _strip_per_status_membership(backend, entry)
        compression.backfill_compressed_status_index()

        result = compression.backfill_compressed_status_index()

        assert result["added"] == 0
        assert result["walked"] == 1

    def test_marker_present_runs_a_tail_scan_from_the_watermark_minus_slack(
        self, compression, backend, store_compressed, mark_index_ready
    ):
        """The window starts a slack allowance below the reconciled score."""
        from baldur.utils.serialization import fast_dumps

        inside = store_compressed("inside", days_ago=5)
        outside = store_compressed("outside", days_ago=40)
        for entry in (inside, outside):
            _strip_per_status_membership(backend, entry)
        mark_index_ready()
        backend.set_blob(
            _COMPRESSED_WATERMARK_KEY,
            fast_dumps(
                {
                    "reconciled_through_score": (
                        utc_now() - timedelta(days=10)
                    ).timestamp()
                }
            ),
        )

        result = compression.backfill_compressed_status_index()

        assert result["mode"] == "tail"
        assert result["walked"] == 1
        assert inside.id in backend.zsets[_status_key("active")]
        assert outside.id not in backend.zsets.get(_status_key("active"), {})

    def test_tail_scan_heals_a_gap_the_frozen_watermark_predates_by_weeks(
        self, compression, backend, store_compressed, mark_index_ready
    ):
        """A rollback freezes the watermark, so the next scan spans the window.

        Anchoring the window to wall clock instead would age this gap out of
        it permanently: the entry landed while old code was running, weeks
        before the scan that has to find it.
        """
        from baldur.utils.serialization import fast_dumps

        gap = store_compressed("rolled-back", days_ago=25)
        _strip_per_status_membership(backend, gap)
        mark_index_ready()
        backend.set_blob(
            _COMPRESSED_WATERMARK_KEY,
            fast_dumps(
                {
                    "reconciled_through_score": (
                        utc_now() - timedelta(days=30)
                    ).timestamp()
                }
            ),
        )

        result = compression.backfill_compressed_status_index()

        assert result["mode"] == "tail"
        assert gap.id in backend.zsets[_status_key("active")]

    def test_absent_watermark_widens_the_tail_scan_to_the_whole_index(
        self, compression, backend, store_compressed, mark_index_ready
    ):
        """Losing the watermark costs one wide re-scan, never coverage."""
        old = store_compressed("ancient", days_ago=900)
        _strip_per_status_membership(backend, old)
        mark_index_ready()

        result = compression.backfill_compressed_status_index()

        assert result["mode"] == "tail"
        assert old.id in backend.zsets[_status_key("active")]

    def test_operator_run_walks_the_full_index_even_with_the_marker_set(
        self, compression, backend, store_compressed, mark_index_ready
    ):
        entry = store_compressed("old", days_ago=400)
        _strip_per_status_membership(backend, entry)
        mark_index_ready()

        result = compression.backfill_compressed_status_index(operator_initiated=True)

        assert result["mode"] == "full"
        assert entry.id in backend.zsets[_status_key("active")]

    def test_unreadable_members_are_skipped_and_counted(
        self, compression, backend, store_compressed
    ):
        """A member no reader can see loses nothing by staying unfiled."""
        entry = store_compressed("ghost", days_ago=50)
        _strip_per_status_membership(backend, entry)
        del backend.blobs[f"dlq:compressed:{entry.id}"]

        result = compression.backfill_compressed_status_index()

        assert result["walked"] == 1
        assert result["skipped_unreadable"] == 1
        assert result["added"] == 0

    def test_empty_domain_member_is_filed_globally_only(
        self, compression, backend, store_compressed
    ):
        """The composite key is skipped for an empty domain, as the writes are."""
        entry = store_compressed("nodomain", days_ago=50, domain="")
        backend.zrem(_status_key("active"), entry.id)

        result = compression.backfill_compressed_status_index()

        assert result["added"] == 1
        assert entry.id in backend.zsets[_status_key("active")]

    def test_verified_full_walk_moves_this_process_onto_the_per_status_lane(
        self, compression, backend, store_compressed
    ):
        """A run that just proved coverage does not also pay the legacy walk.

        The flag is process-local: the sweep honours it, the marker is what
        every other reader waits for.
        """
        entry = store_compressed("e0", days_ago=50)
        _strip_per_status_membership(backend, entry)
        compression.backfill_compressed_status_index()
        # An artificial post-backfill stray: present in the index with an
        # ACTIVE payload, absent from status:active. The pre-migration lane
        # walks the index and would return it; the per-status lane cannot.
        backend.zrem(_status_key("active"), entry.id)

        rows = compression.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=5)
        )

        assert rows == []


# =============================================================================
# H. Behavior — the verified-scan invariant
# =============================================================================


class TestCompressedBackfillVerification:
    """Progress state is written only for a scan Redis served end to end."""

    def test_scan_that_starts_degraded_records_no_progress(
        self, compression, backend, store_compressed
    ):
        entry = store_compressed("e0", days_ago=50)
        _strip_per_status_membership(backend, entry)
        backend.is_redis_available = False

        result = compression.backfill_compressed_status_index()

        assert result["verified"] is False
        assert _COMPRESSED_WATERMARK_KEY not in backend.blobs
        assert _COMPRESSED_MARKER_KEY not in backend.blobs

    def test_mid_scan_degrade_vetoes_progress_even_after_recovery(
        self, compression, backend, store_compressed
    ):
        """Availability is True at both ends; only the epoch shows the gap."""
        entry = store_compressed("e0", days_ago=50)
        _strip_per_status_membership(backend, entry)
        backend.blob_fetch_degrades_on_call = {1}

        result = compression.backfill_compressed_status_index()

        assert backend.is_redis_available is True
        assert result["verified"] is False
        assert _COMPRESSED_WATERMARK_KEY not in backend.blobs

    def test_peer_degrade_right_after_the_availability_read_is_still_caught(
        self, compression, backend, store_compressed
    ):
        """The sampling-order hole: a peer thread degrades between the reads.

        Nothing bumps the counter again once the mode is already degraded and
        background recovery re-promotes before scan end — so a sample taken
        *after* the availability read is already post-bump, matches at scan
        end, and reports a wholly memory-served scan as verified. The sample
        is taken first, so it predates the bump and the end comparison fails.
        """
        entry = store_compressed("e0", days_ago=50)
        _strip_per_status_membership(backend, entry)
        backend.peer_degrade_after_availability_read = True

        result = compression.backfill_compressed_status_index()

        assert result["walked"] == 0  # the memory view reported an empty index
        assert result["added"] == 0
        assert result["verified"] is False
        assert _COMPRESSED_WATERMARK_KEY not in backend.blobs
        assert _COMPRESSED_MARKER_KEY not in backend.blobs

    def test_two_wholly_degraded_scans_hours_apart_never_stamp_the_marker(
        self, compression, backend, store_compressed
    ):
        """The zero-add stability rule cannot conclude on unverified scans.

        Both scans report ``added == 0`` — from an empty view, not from
        coverage — so a rule that read the count without the invariant would
        stamp the marker on an install where nothing was reconciled.
        """
        entry = store_compressed("e0", days_ago=50)
        _strip_per_status_membership(backend, entry)
        start = utc_now()

        for offset_hours in (0, 12):
            backend.peer_degrade_after_availability_read = True
            with patch(_UTC_NOW, return_value=start + timedelta(hours=offset_hours)):
                result = compression.backfill_compressed_status_index()
            assert result["marker_set"] is False

        assert _COMPRESSED_MARKER_KEY not in backend.blobs

    def test_epoch_is_sampled_before_the_availability_check(self):
        """Source-order pin: reversing the two reads reopens the hole above.

        Both reads are cheap and adjacent, so nothing but their order stops a
        degrade that lands between them from passing every check.
        """
        source = inspect.getsource(RedisDLQCompression.backfill_compressed_status_index)

        assert source.index("_degrade_epoch()") < source.index("_redis_available()")

    def test_verified_scan_records_the_highest_reconciled_score(
        self, compression, backend, store_compressed
    ):
        newest = store_compressed("newest", days_ago=1)
        older = store_compressed("older", days_ago=40)
        for entry in (newest, older):
            _strip_per_status_membership(backend, entry)

        result = compression.backfill_compressed_status_index()

        assert result["verified"] is True
        assert _read_watermark(backend)["reconciled_through_score"] == pytest.approx(
            newest.compressed_at.timestamp()
        )

    def test_scan_failure_reports_unverified_and_logs_the_degrade_delta(
        self, compression, backend
    ):
        backend.zrange = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))

        with capture_logs() as logs:
            result = compression.backfill_compressed_status_index()

        assert result["verified"] is False
        assert result["complete"] is False
        failures = [e for e in logs if e["event"] == "dlq.compressed_backfill_failed"]
        assert len(failures) == 1
        assert failures[0]["log_level"] == "warning"


# =============================================================================
# I. Behavior — completion stamping
# =============================================================================


class TestCompressedBackfillStamping:
    """Two quiet walks, hours apart, conclude the migration."""

    @staticmethod
    def _seed_chain(backend, *, added: int, walk_time) -> None:
        from baldur.utils.serialization import fast_dumps

        backend.set_blob(
            _COMPRESSED_WATERMARK_KEY,
            fast_dumps({"added": added, "walk_time": walk_time.isoformat()}),
        )

    @pytest.mark.parametrize(
        ("chain_added", "chain_age_hours", "has_unfiled_member", "expected"),
        [
            (0, _BACKFILL_STABILITY_MIN_SECONDS / 3600 + 1, False, True),
            (0, _BACKFILL_STABILITY_MIN_SECONDS / 3600 - 1, False, False),
            (0, _BACKFILL_STABILITY_MIN_SECONDS / 3600 + 1, True, False),
            (5, _BACKFILL_STABILITY_MIN_SECONDS / 3600 + 1, False, False),
        ],
        ids=["quiet_chain_matured", "chain_too_young", "walk_added", "chain_reset"],
    )
    def test_marker_is_stamped_only_by_a_matured_quiet_chain(
        self,
        compression,
        backend,
        store_compressed,
        chain_added,
        chain_age_hours,
        has_unfiled_member,
        expected,
    ):
        now = utc_now()
        entry = store_compressed("e0", days_ago=50)
        if has_unfiled_member:
            _strip_per_status_membership(backend, entry)
        self._seed_chain(
            backend,
            added=chain_added,
            walk_time=now - timedelta(hours=chain_age_hours),
        )

        with patch(_UTC_NOW, return_value=now):
            result = compression.backfill_compressed_status_index()

        assert result["marker_set"] is expected
        assert (_COMPRESSED_MARKER_KEY in backend.blobs) is expected

    def test_unverified_walk_records_nothing_even_on_a_matured_chain(
        self, compression, backend, store_compressed
    ):
        now = utc_now()
        store_compressed("e0", days_ago=50)
        self._seed_chain(
            backend,
            added=0,
            walk_time=now - timedelta(seconds=_BACKFILL_STABILITY_MIN_SECONDS * 2),
        )
        watermark_before = dict(_read_watermark(backend))
        backend.is_redis_available = False

        with patch(_UTC_NOW, return_value=now):
            result = compression.backfill_compressed_status_index()

        assert result["marker_set"] is False
        assert _COMPRESSED_MARKER_KEY not in backend.blobs
        assert _read_watermark(backend) == watermark_before

    def test_marker_is_stamped_while_entries_keep_being_compressed(
        self, compression, backend, store_compressed
    ):
        """Write volume must not block completion.

        An install only reaches the volume that makes the migration necessary
        by compressing continuously, so a criterion keyed on the index
        standing still would never fire there. New entries file themselves,
        so they add nothing to the walk — the quiet chain survives them.
        """
        now = utc_now()
        store_compressed("before", days_ago=50)

        with patch(_UTC_NOW, return_value=now):
            first = compression.backfill_compressed_status_index()
        store_compressed("during", days_ago=1)
        with patch(_UTC_NOW, return_value=now + timedelta(hours=7)):
            second = compression.backfill_compressed_status_index()

        assert first["added"] == 0
        assert first["marker_set"] is False
        assert backend.zcard(_COMPRESSED_INDEX_KEY) == 2  # the index grew between walks
        assert second["added"] == 0
        assert second["marker_set"] is True

    def test_quiet_walk_keeps_the_chain_start_time(
        self, compression, backend, store_compressed
    ):
        """Maturity is measured from the chain's start, not the previous run.

        Otherwise a hand-triggered pair of sweeps could walk its way to a
        conclusion inside a live rolling upgrade.
        """
        now = utc_now()
        store_compressed("e0", days_ago=50)

        with patch(_UTC_NOW, return_value=now):
            compression.backfill_compressed_status_index()
        chain_start = _read_watermark(backend)["walk_time"]
        with patch(_UTC_NOW, return_value=now + timedelta(hours=1)):
            compression.backfill_compressed_status_index()

        assert _read_watermark(backend)["walk_time"] == chain_start

    def test_operator_run_stamps_on_one_pass_that_added_members(
        self, compression, backend, store_compressed
    ):
        """The wait substitutes for operator judgement; here it was made."""
        entry = store_compressed("e0", days_ago=50)
        _strip_per_status_membership(backend, entry)

        with capture_logs() as logs:
            result = compression.backfill_compressed_status_index(
                operator_initiated=True
            )

        assert result["added"] > 0
        assert result["marker_set"] is True
        assert result["complete"] is True
        ready = [e for e in logs if e["event"] == "dlq.compressed_index_ready"]
        assert len(ready) == 1
        assert ready[0]["source"] == "cli"

    def test_stamped_marker_switches_the_status_filtered_listing_over(
        self, compression, backend, store_compressed
    ):
        """The end the migration exists for: the archived view becomes reachable.

        The archive is the oldest population, so a newest-first slice of the
        all-statuses index reaches none of it once the newer entries fill the
        page.
        """
        archived = [
            store_compressed(f"a{i}", days_ago=300 - i, status="archived")
            for i in range(3)
        ]
        for entry in archived:
            _strip_per_status_membership(entry=entry, backend=backend)
        for i in range(10):
            store_compressed(f"n{i}", days_ago=10)

        before = compression.get_compressed_entries(status="archived", limit=5)
        compression.backfill_compressed_status_index(operator_initiated=True)
        after = compression.get_compressed_entries(status="archived", limit=5)

        assert before == []
        assert {e.id for e in after} == {e.id for e in archived}


# =============================================================================
# J. Contract — the stability chain helper
# =============================================================================


class TestStabilityChainContract:
    """_stability_chain_matured gates the marker on an aged quiet chain."""

    def test_stability_floor_is_six_hours(self):
        """The floor is a design value (723 D12), not an incidental one."""
        assert _BACKFILL_STABILITY_MIN_SECONDS == 21600

    def test_rescan_slack_is_one_day(self):
        """The tail window's lower slack absorbs a day of writer clock skew."""
        assert _BACKFILL_RESCAN_SLACK_SECONDS == 86400

    @pytest.mark.parametrize(
        ("age_seconds", "expected"),
        [
            (_BACKFILL_STABILITY_MIN_SECONDS - 1, False),
            (_BACKFILL_STABILITY_MIN_SECONDS, True),
            (_BACKFILL_STABILITY_MIN_SECONDS + 1, True),
        ],
        ids=["below_floor", "at_floor", "above_floor"],
    )
    def test_chain_matures_at_the_floor(self, age_seconds, expected):
        now = utc_now()
        watermark = {
            "added": 0,
            "walk_time": (now - timedelta(seconds=age_seconds)).isoformat(),
        }

        assert _stability_chain_matured(watermark, now) is expected

    @pytest.mark.parametrize(
        "watermark",
        [
            {},
            {"added": 3, "walk_time": "2026-01-01T00:00:00+00:00"},
            {"added": 0},
            {"added": 0, "walk_time": None},
            {"added": 0, "walk_time": "not-a-timestamp"},
        ],
        ids=["empty", "chain_reset", "no_walk_time", "null_walk_time", "unparseable"],
    )
    def test_absent_or_malformed_record_does_not_mature(self, watermark):
        """Anything the record cannot prove is read as "not yet"."""
        assert _stability_chain_matured(watermark, utc_now()) is False
