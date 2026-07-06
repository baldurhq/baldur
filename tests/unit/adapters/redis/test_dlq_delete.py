"""Unit tests for the DLQ single-RTT delete path (544 D6).

``RedisDLQRepository.delete`` collapses to a single ``batch_write_ops`` call
covering the blob delete + every index removal in one round trip:

    [delete blob, zrem domain, zrem status, zrem ALL, zrem composite,
     zrem PENDING (conditional)]

The ordering is blob-first so a mid-batch failure cannot leave an orphaned
blob with stale index entries -- the partial prefix is index-only and
zrem-recoverable. The per-status zrem branches between ``dlq:pending`` (when
status == PENDING, the dedicated key family) and ``dlq:status:{status}``
(every other indexed status); the composite zrem is conditional on a
non-empty domain. The blob-only no-entry case (already absent) routes
through ``backend.delete`` for idempotency.

Test classes:
    TestDeleteSingleRTT -- single ``batch_write_ops`` call shape across the
        (status indexed / PENDING / not indexed) x (with domain / without
        domain) matrix; blob-first ordering; no separate backend.delete.
    TestDeleteIdempotency -- second delete of an already-absent entry takes
        the backend.delete fast path without a second batch.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.interfaces.repositories import FailedOperationStatus

_PENDING = FailedOperationStatus.PENDING.value
_RESOLVED = FailedOperationStatus.RESOLVED.value
_EXPIRED = FailedOperationStatus.EXPIRED.value


def _make_repo() -> RedisDLQRepository:
    backend = MagicMock()
    backend.config.key_prefix = ""
    repo = RedisDLQRepository(backend, pod_id="pod-a", pid=100, run_nonce="nonce0")
    repo._compression_enabled = MagicMock(return_value=False)
    return repo, backend


def _wire_existing_entry(repo, *, entry_id: str, status: str, domain: str):
    """Make _load_blob/_decode_entry return a synthetic entry blob so the
    delete path runs the indexed branch instead of the empty fast path."""
    fake = {"id": entry_id, "status": status, "domain": domain}
    repo._load_blob = MagicMock(return_value=json.dumps(fake).encode())
    repo._decode_entry = MagicMock(return_value=fake)


# =============================================================================
# Single-RTT batch shape
# =============================================================================


class TestDeleteSingleRTTBehavior:
    """delete() issues exactly one batch_write_ops call covering blob + all
    index removals -- 1 RTT total, no separate backend.delete call."""

    @pytest.mark.parametrize(
        ("status", "domain", "expected_keys"),
        [
            (
                _PENDING,
                "payment",
                {
                    "dlq:entry:e1",
                    "dlq:by_domain:payment",
                    "dlq:pending",
                    "dlq:all",
                    "dlq:status_domain:pending:payment",
                },
            ),
            (
                _RESOLVED,
                "payment",
                {
                    "dlq:entry:e1",
                    "dlq:by_domain:payment",
                    "dlq:status:resolved",
                    "dlq:all",
                    "dlq:status_domain:resolved:payment",
                },
            ),
            (
                _EXPIRED,
                "auth",
                {
                    "dlq:entry:e1",
                    "dlq:by_domain:auth",
                    "dlq:status:expired",
                    "dlq:all",
                    "dlq:status_domain:expired:auth",
                },
            ),
        ],
    )
    def test_delete_indexed_status_with_domain_issues_single_batch(
        self, status, domain, expected_keys
    ):
        repo, backend = _make_repo()
        _wire_existing_entry(repo, entry_id="e1", status=status, domain=domain)

        result = repo.delete("e1")

        assert result is True
        # Exactly one batch_write_ops call.
        backend.batch_write_ops.assert_called_once()
        # No separate backend.delete call on the indexed-entry path.
        backend.delete.assert_not_called()

        ops = backend.batch_write_ops.call_args.args[0]
        keys = {op[1] for op in ops}
        assert keys == expected_keys

    def test_delete_uses_blob_first_ordering(self):
        """Blob delete is op[0] -- if the batch fails mid-pipeline, the
        orphan state is index-only (zrem-recoverable), never an orphaned
        blob with no index."""
        repo, backend = _make_repo()
        _wire_existing_entry(repo, entry_id="e1", status=_RESOLVED, domain="payment")

        repo.delete("e1")

        ops = backend.batch_write_ops.call_args.args[0]
        assert ops[0][0] == "delete"
        assert ops[0][1] == "dlq:entry:e1"

    def test_delete_without_domain_skips_domain_and_composite_zrems(self):
        """An entry with an empty domain has no by_domain index entry and
        no composite key -- both ops drop from the batch."""
        repo, backend = _make_repo()
        _wire_existing_entry(repo, entry_id="e1", status=_RESOLVED, domain="")

        repo.delete("e1")

        ops = backend.batch_write_ops.call_args.args[0]
        keys = {op[1] for op in ops}
        assert "dlq:by_domain:" not in keys
        assert not any("status_domain" in k for k in keys)
        # Still has blob + per-status + ALL.
        assert "dlq:entry:e1" in keys
        assert "dlq:status:resolved" in keys
        assert "dlq:all" in keys

    def test_delete_unindexed_status_skips_per_status_zrem(self):
        """A status that is neither PENDING nor _STATUS_INDEXED has no
        per-status ZSET, so the per-status zrem drops from the batch.
        (REVIEWING is included in _STATUS_INDEXED today; an empty-string
        status is the canonical "no index" probe.)"""
        repo, backend = _make_repo()
        _wire_existing_entry(repo, entry_id="e1", status="", domain="payment")

        repo.delete("e1")

        ops = backend.batch_write_ops.call_args.args[0]
        keys = [op[1] for op in ops]
        # No per-status or PENDING zrem because status is empty.
        assert "dlq:pending" not in keys
        assert not any(k.startswith("dlq:status:") for k in keys)

    def test_delete_op_count_matches_keys_one_to_one(self):
        """Each op targets a distinct key -- the batch carries no duplicate
        ops (a duplicate would silently double-zrem the same key)."""
        repo, backend = _make_repo()
        _wire_existing_entry(repo, entry_id="e1", status=_RESOLVED, domain="payment")

        repo.delete("e1")

        ops = backend.batch_write_ops.call_args.args[0]
        keys = [op[1] for op in ops]
        assert len(keys) == len(set(keys))


# =============================================================================
# Idempotency on absent entry
# =============================================================================


class TestDeleteIdempotencyBehavior:
    """delete() of an already-absent entry routes through backend.delete
    (returns False) without issuing a batch -- second delete is a no-op."""

    def test_absent_entry_uses_backend_delete_fast_path(self):
        repo, backend = _make_repo()
        # _load_blob returns None for an absent entry; _decode_entry returns {}.
        repo._load_blob = MagicMock(return_value=None)
        repo._decode_entry = MagicMock(return_value={})
        backend.delete.return_value = False

        result = repo.delete("missing")

        assert result is False
        # No batch issued -- the fast path is backend.delete only.
        backend.batch_write_ops.assert_not_called()
        backend.delete.assert_called_once_with("dlq:entry:missing")
