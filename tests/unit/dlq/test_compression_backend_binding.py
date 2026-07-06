"""Real-backend binding coverage for RedisDLQCompression (586).

The compression write/lifecycle path was written against a raw-Redis-client
mental model (``pipeline()`` / ``exists()`` / ``expire()``), none of which
exist on ``ResilientStorageBackend`` — the backend ``RedisDLQRepository`` is
actually bound to in production. Every prior unit test injected a ``MagicMock``
backend (every method auto-succeeds), so the ``AttributeError`` crashes were
invisible to CI. These tests exercise the real method surface over a concrete
``ResilientStorageBackend`` in memory-only (degraded) mode — the absent
methods would raise ``AttributeError`` in any mode, so memory-only is
sufficient and needs no Redis.

OSS-pure: ``DLQCompressedEntry`` objects are constructed directly and the
compression methods are called directly (never ``compress_and_evict_oldest``,
which would pull in the PRO ``compress_entries``), so the import-graph SUT is
OSS and the file runs in OSS-only CI where the crash matters.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.adapters.redis.dlq_compression import _AUDIT_SOURCE_IDS_CAP
from baldur.adapters.resilient.backend import ResilientStorageBackend
from baldur.interfaces.repositories import DLQCompressedEntry, DLQCompressedStatus
from baldur.settings.resilient_storage import ResilientStorageSettings


@pytest.fixture
def repo_factory():
    """Yield a factory building ``RedisDLQRepository`` over a real
    memory-only ``ResilientStorageBackend`` (tmpdir WAL).

    ``use_dynamic_prefix=False`` pins a static key_prefix so full keys are
    deterministic; the first-init Redis probe is pushed far out so the hot-path
    ``_ensure_redis()`` never flips to a real local-dev Redis. Backends are
    closed + their dirs removed on teardown (Windows-safe).
    """
    created: list[ResilientStorageBackend] = []
    dirs: list[str] = []

    def make() -> RedisDLQRepository:
        wal_dir = tempfile.mkdtemp()
        dirs.append(wal_dir)
        settings = ResilientStorageSettings(
            wal_dir=wal_dir,
            allow_memory_only=True,
            use_dynamic_prefix=False,
        )
        backend = ResilientStorageBackend(settings)
        backend._next_redis_probe = time.monotonic() + 9999.0
        created.append(backend)
        return RedisDLQRepository(backend, pod_id="test-pod", pid=1, run_nonce="nonce0")

    yield make

    for backend in created:
        try:
            backend.close()
        except Exception:
            pass
    for wal_dir in dirs:
        shutil.rmtree(wal_dir, ignore_errors=True)


def _make_entry(
    entry_id: str,
    *,
    domain: str = "payment",
    count: int = 10,
    status: str = DLQCompressedStatus.ACTIVE.value,
) -> DLQCompressedEntry:
    """Construct a DLQCompressedEntry directly (no PRO compression call)."""
    now = datetime.now(UTC)
    return DLQCompressedEntry(
        id=entry_id,
        domain=domain,
        failure_type="timeout",
        error_code="E_TIMEOUT",
        count=count,
        first_seen=now,
        last_seen=now,
        sample_error_message="boom",
        sample_context={"trace": "abc", "n": 1},
        status=status,
        compressed_at=now,
    )


# =============================================================================
# G1 — store_compressed_entry / read-path round-trip (D1)
# =============================================================================


class TestStoreCompressedEntryRealBackendBehavior:
    """store_compressed_entry writes via batch_write_ops without AttributeError."""

    def test_store_does_not_raise_attribute_error(self, repo_factory):
        """Storing against a real backend must not hit a backend-absent method."""
        repo = repo_factory()
        entry = _make_entry("compressed:payment:timeout:E_TIMEOUT:1")
        assert repo.compression.store_compressed_entry(entry) is True

    def test_store_then_get_round_trip(self, repo_factory):
        """get_compressed_entries reads back the entry written by store."""
        repo = repo_factory()
        entry = _make_entry("compressed:payment:timeout:E_TIMEOUT:1", count=42)
        repo.compression.store_compressed_entry(entry)

        results = repo.compression.get_compressed_entries()
        assert len(results) == 1
        got = results[0]
        assert got.id == entry.id
        assert got.domain == "payment"
        assert got.count == 42
        # Inner sample_context round-trips through its JSON-string form.
        assert got.sample_context == {"trace": "abc", "n": 1}
        assert got.status == DLQCompressedStatus.ACTIVE.value

    def test_get_filters_by_domain(self, repo_factory):
        """The by_domain index serves a domain-scoped read."""
        repo = repo_factory()
        repo.compression.store_compressed_entry(
            _make_entry("c:payment:1", domain="payment")
        )
        repo.compression.store_compressed_entry(_make_entry("c:auth:1", domain="auth"))

        payment = repo.compression.get_compressed_entries(domain="payment")
        assert len(payment) == 1
        assert payment[0].domain == "payment"
        assert (
            repo.compression.get_compressed_entries(domain="auth")[0].domain == "auth"
        )

    def test_get_filters_by_status(self, repo_factory):
        """Status filter reads status from the decoded blob and filters in Python."""
        repo = repo_factory()
        repo.compression.store_compressed_entry(
            _make_entry("c:1", status=DLQCompressedStatus.ACTIVE.value)
        )
        repo.compression.store_compressed_entry(
            _make_entry("c:2", status=DLQCompressedStatus.ACTIVE.value)
        )
        repo.compression.update_compressed_status(
            "c:2", DLQCompressedStatus.STALE.value
        )

        active = repo.compression.get_compressed_entries(
            status=DLQCompressedStatus.ACTIVE.value
        )
        stale = repo.compression.get_compressed_entries(
            status=DLQCompressedStatus.STALE.value
        )
        assert len(active) == 1
        assert len(stale) == 1
        assert stale[0].id == "c:2"

    def test_get_summary_aggregates(self, repo_factory):
        """get_compressed_summary reads each blob and aggregates counts."""
        repo = repo_factory()
        repo.compression.store_compressed_entry(_make_entry("c:1", count=5))
        repo.compression.store_compressed_entry(_make_entry("c:2", count=7))

        summary = repo.compression.get_compressed_summary()
        assert summary["total_summaries"] == 2
        assert summary["total_compressed_items"] == 12
        assert summary["by_status"][DLQCompressedStatus.ACTIVE.value] == 2

    def test_get_summary_seeds_all_statuses(self, repo_factory):
        """by_status is seeded with every DLQCompressedStatus, not only seen ones."""
        repo = repo_factory()
        repo.compression.store_compressed_entry(_make_entry("c:1"))

        by_status = repo.compression.get_compressed_summary()["by_status"]
        # Every enum member is present (zero for the unused statuses).
        for member in DLQCompressedStatus:
            assert member.value in by_status
        assert by_status[DLQCompressedStatus.ARCHIVED.value] == 0

    def test_get_respects_limit(self, repo_factory):
        """limit caps the returned count via zrevrange(key, 0, limit - 1)."""
        repo = repo_factory()
        for i in range(5):
            repo.compression.store_compressed_entry(_make_entry(f"c:{i}"))

        assert len(repo.compression.get_compressed_entries(limit=3)) == 3
        assert len(repo.compression.get_compressed_entries(limit=100)) == 5


# =============================================================================
# G2 — update_compressed_status lifecycle (D2)
# =============================================================================


class TestUpdateCompressedStatusRealBackendBehavior:
    """update_compressed_status existence-checks via get_blob, rewrites the blob."""

    def test_nonexistent_returns_false(self, repo_factory):
        """Missing blob → get_blob is None → False (no exists() call)."""
        repo = repo_factory()
        assert repo.compression.update_compressed_status("absent", "stale") is False

    @pytest.mark.parametrize(
        ("new_status", "timestamp_attr"),
        [
            (DLQCompressedStatus.STALE.value, "stale_at"),
            (DLQCompressedStatus.ARCHIVED.value, "archived_at"),
        ],
    )
    def test_transition_sets_timestamp(self, repo_factory, new_status, timestamp_attr):
        """STALE/ARCHIVED transitions set the matching timestamp field."""
        repo = repo_factory()
        repo.compression.store_compressed_entry(_make_entry("c:1"))

        assert repo.compression.update_compressed_status("c:1", new_status) is True

        got = repo.compression.get_compressed_entries(status=new_status)
        assert len(got) == 1
        assert got[0].status == new_status
        assert getattr(got[0], timestamp_attr) is not None

    def test_transition_preserves_other_fields(self, repo_factory):
        """The GET→mutate→SET rewrite keeps the rest of the entry intact."""
        repo = repo_factory()
        repo.compression.store_compressed_entry(_make_entry("c:1", count=99))
        repo.compression.update_compressed_status(
            "c:1", DLQCompressedStatus.STALE.value
        )

        got = repo.compression.get_compressed_entries(
            status=DLQCompressedStatus.STALE.value
        )[0]
        assert got.count == 99
        assert got.sample_context == {"trace": "abc", "n": 1}


# =============================================================================
# G3 — _record_compression_audit embed + cap truncation (D3)
# =============================================================================


class TestRecordCompressionAuditRealBackendBehavior:
    """_record_compression_audit embeds source_ids inline (no Redis artifact)."""

    def test_small_list_full_embed(self, repo_factory):
        """A small source_ids list is embedded in full with no truncation marker."""
        repo = repo_factory()
        source_ids = [f"id-{i}" for i in range(5)]
        summaries = [_make_entry("c:1")]

        with patch(
            "baldur.adapters.redis.dlq_compression.log_dlq_compress_audit"
        ) as mock_log:
            repo.compression._record_compression_audit(
                source_ids=source_ids, summaries=summaries
            )

        details = mock_log.call_args.kwargs["details"]
        assert details["source_ids"] == sorted(source_ids)
        assert details["source_count"] == 5
        assert "source_ids_truncated" not in details
        assert details["source_ids_hash"].startswith("sha256:")

    def test_large_list_truncates_to_cap(self, repo_factory):
        """A > cap source_ids list truncates the list but keeps set identity full."""
        repo = repo_factory()
        source_ids = [f"id-{i:07d}" for i in range(_AUDIT_SOURCE_IDS_CAP + 250)]
        summaries = [_make_entry("c:1")]

        with patch(
            "baldur.adapters.redis.dlq_compression.log_dlq_compress_audit"
        ) as mock_log:
            repo.compression._record_compression_audit(
                source_ids=source_ids, summaries=summaries
            )

        details = mock_log.call_args.kwargs["details"]
        assert len(details["source_ids"]) == _AUDIT_SOURCE_IDS_CAP
        assert details["source_ids_truncated"] is True
        # Authoritative set identity is always emitted in full.
        assert details["source_count"] == _AUDIT_SOURCE_IDS_CAP + 250
        assert details["source_ids_hash"].startswith("sha256:")

    def test_audit_does_not_raise(self, repo_factory):
        """The audit path touches no backend-absent method (no set/expire)."""
        repo = repo_factory()
        repo.compression._record_compression_audit(
            source_ids=["a", "b"], summaries=[_make_entry("c:1")]
        )

    def test_exactly_at_cap_does_not_truncate(self, repo_factory):
        """Boundary just-at: a list of exactly the cap size is embedded in full.

        The condition is ``len(...) > _AUDIT_SOURCE_IDS_CAP``, so at exactly the
        cap it is False — no truncation, no marker.
        """
        repo = repo_factory()
        source_ids = [f"id-{i:07d}" for i in range(_AUDIT_SOURCE_IDS_CAP)]

        with patch(
            "baldur.adapters.redis.dlq_compression.log_dlq_compress_audit"
        ) as mock_log:
            repo.compression._record_compression_audit(
                source_ids=source_ids, summaries=[_make_entry("c:1")]
            )

        details = mock_log.call_args.kwargs["details"]
        assert len(details["source_ids"]) == _AUDIT_SOURCE_IDS_CAP
        assert "source_ids_truncated" not in details

    def test_one_over_cap_truncates(self, repo_factory):
        """Boundary just-over: one id past the cap trips truncation."""
        repo = repo_factory()
        source_ids = [f"id-{i:07d}" for i in range(_AUDIT_SOURCE_IDS_CAP + 1)]

        with patch(
            "baldur.adapters.redis.dlq_compression.log_dlq_compress_audit"
        ) as mock_log:
            repo.compression._record_compression_audit(
                source_ids=source_ids, summaries=[_make_entry("c:1")]
            )

        details = mock_log.call_args.kwargs["details"]
        assert len(details["source_ids"]) == _AUDIT_SOURCE_IDS_CAP
        assert details["source_ids_truncated"] is True
        assert details["source_count"] == _AUDIT_SOURCE_IDS_CAP + 1

    def test_truncation_hash_stays_authoritative_over_full_set(self, repo_factory):
        """source_ids_hash fingerprints the COMPLETE set even when the embedded
        list is truncated — the authoritative-fields-always-full invariant.

        Behavior test: the expected hash is recomputed from source
        (``fast_canonical_dumps`` of the sorted full set) rather than hardcoded,
        and is asserted distinct from the hash of just the truncated 5000.
        """
        import hashlib

        from baldur.utils.serialization import fast_canonical_dumps

        repo = repo_factory()
        source_ids = [f"id-{i:07d}" for i in range(_AUDIT_SOURCE_IDS_CAP + 250)]
        full_sorted = sorted(source_ids)
        expected_full = (
            "sha256:" + hashlib.sha256(fast_canonical_dumps(full_sorted)).hexdigest()
        )
        hash_of_truncated = (
            "sha256:"
            + hashlib.sha256(
                fast_canonical_dumps(full_sorted[:_AUDIT_SOURCE_IDS_CAP])
            ).hexdigest()
        )

        with patch(
            "baldur.adapters.redis.dlq_compression.log_dlq_compress_audit"
        ) as mock_log:
            repo.compression._record_compression_audit(
                source_ids=source_ids, summaries=[_make_entry("c:1")]
            )

        details = mock_log.call_args.kwargs["details"]
        assert details["source_ids_hash"] == expected_full
        # The fingerprint is NOT the hash of the truncated embedded list.
        assert details["source_ids_hash"] != hash_of_truncated

    def test_hash_and_bounds_are_order_independent(self, repo_factory):
        """Same id set in different input orders → identical hash + first/last.

        source_ids_hash is an order-independent set fingerprint, and
        first/last_source_id are min/max (not positional), so input ordering
        does not change any of the authoritative fields.
        """
        repo = repo_factory()
        ids = [f"id-{i:03d}" for i in range(20)]

        def record(order: list[str]) -> dict:
            with patch(
                "baldur.adapters.redis.dlq_compression.log_dlq_compress_audit"
            ) as mock_log:
                repo.compression._record_compression_audit(
                    source_ids=order, summaries=[_make_entry("c:1")]
                )
            return mock_log.call_args.kwargs["details"]

        forward = record(list(ids))
        reverse = record(list(reversed(ids)))

        assert forward["source_ids_hash"] == reverse["source_ids_hash"]
        assert forward["first_source_id"] == reverse["first_source_id"] == ids[0]
        assert forward["last_source_id"] == reverse["last_source_id"] == ids[-1]
