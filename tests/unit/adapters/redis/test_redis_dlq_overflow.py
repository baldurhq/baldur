"""
RedisDLQRepository Overflow Operations Unit Tests (329_DLQ_SIZE_LIMIT).

Test targets:
    - baldur.adapters.redis.dlq.RedisDLQRepository
      (count_all, count_by_domain, get_oldest_ids, evict_oldest, delete)

Test Categories:
    A. Contract: count_all / count_by_domain delegate to ZCARD
    B. Behavior: get_oldest_ids key selection and result parsing
    C. Behavior: evict_oldest 3-key consistency via delete()
    D. Behavior: delete() cleans up all 3 keys (hash, pending ZSET, domain ZSET)
"""

import json
from unittest.mock import MagicMock, call, patch

from baldur.adapters.redis.dlq import RedisDLQRepository


def _blob(data: dict) -> bytes:
    """Encode dict as the orjson-style JSON bytes a real entry blob would hold."""
    return json.dumps(data).encode("utf-8")


def _make_repo(mock_backend=None):
    """Create RedisDLQRepository with mock backend."""
    from baldur.adapters.redis.dlq_compression import RedisDLQCompression
    from baldur.adapters.redis.dlq_lifecycle import RedisDLQLifecycle
    from baldur.adapters.redis.dlq_maintenance import RedisDLQMaintenance
    from baldur.adapters.redis.dlq_query import RedisDLQQuery

    backend = mock_backend or MagicMock()
    with patch.object(RedisDLQRepository, "__init__", lambda self, **kw: None):
        repo = RedisDLQRepository.__new__(RedisDLQRepository)
    repo._backend = backend
    repo._key_prefix = "dlq:"
    repo._pending_key = "dlq:pending"
    repo._entry_prefix = "dlq:entry:"
    repo._by_domain_prefix = "dlq:by_domain:"
    repo._status_prefix = "dlq:status:"
    repo._status_domain_prefix = "dlq:status_domain:"
    repo._all_key = "dlq:all"
    repo._domains_key = "dlq:domains"
    repo._known_domains = set()
    repo.query = RedisDLQQuery(repo)
    repo.lifecycle = RedisDLQLifecycle(repo)
    repo.maintenance = RedisDLQMaintenance(repo)
    repo.compression = RedisDLQCompression(repo)
    return repo


# =============================================================================
# A. Contract Tests — count_all / count_by_domain ZCARD delegation
# =============================================================================


class TestCountAllContract:
    """count_all delegates to ZCARD on PENDING_KEY."""

    def test_count_all_calls_zcard_on_pending_key(self):
        """count_all calls backend.zcard with PENDING_KEY."""
        backend = MagicMock()
        backend.zcard.return_value = 42
        repo = _make_repo(backend)

        result = repo.count_all()

        backend.zcard.assert_called_once_with("dlq:pending")
        assert result == 42

    def test_count_all_returns_zero_when_empty(self):
        """count_all returns 0 when ZCARD returns 0."""
        backend = MagicMock()
        backend.zcard.return_value = 0
        repo = _make_repo(backend)

        assert repo.count_all() == 0


class TestCountByDomainContract:
    """count_by_domain delegates to ZCARD on domain key."""

    def test_count_by_domain_calls_zcard_on_domain_key(self):
        """count_by_domain calls backend.zcard with domain-specific key."""
        backend = MagicMock()
        backend.zcard.return_value = 15
        repo = _make_repo(backend)

        result = repo.count_by_domain("payment")

        backend.zcard.assert_called_once_with("dlq:by_domain:payment")
        assert result == 15

    def test_count_by_domain_returns_zero_when_empty(self):
        """count_by_domain returns 0 when ZCARD returns 0."""
        backend = MagicMock()
        backend.zcard.return_value = 0
        repo = _make_repo(backend)

        assert repo.count_by_domain("inventory") == 0


# =============================================================================
# B. Behavior Tests — get_oldest_ids key selection and parsing
# =============================================================================


class TestGetOldestIdsBehavior:
    """get_oldest_ids key selection and result parsing behavior."""

    def test_get_oldest_ids_uses_pending_key_without_domain(self):
        """Without domain, uses PENDING_KEY for ZRANGE."""
        backend = MagicMock()
        backend.zrange.return_value = [b"10", b"20", b"30"]
        repo = _make_repo(backend)

        result = repo.get_oldest_ids(3)

        backend.zrange.assert_called_once_with("dlq:pending", 0, 2)
        assert result == ["10", "20", "30"]

    def test_get_oldest_ids_uses_domain_key_with_domain(self):
        """With domain, uses BY_DOMAIN_PREFIX+domain for ZRANGE."""
        backend = MagicMock()
        backend.zrange.return_value = [b"5", b"8"]
        repo = _make_repo(backend)

        result = repo.get_oldest_ids(2, domain="payment")

        backend.zrange.assert_called_once_with("dlq:by_domain:payment", 0, 1)
        assert result == ["5", "8"]

    def test_get_oldest_ids_parses_bytes_to_str(self):
        """ZRANGE byte results are parsed to opaque-string ids (538 D1)."""
        backend = MagicMock()
        backend.zrange.return_value = [b"100", b"200"]
        repo = _make_repo(backend)

        result = repo.get_oldest_ids(2)

        assert all(isinstance(x, str) for x in result)
        assert result == ["100", "200"]

    def test_get_oldest_ids_empty_returns_empty_list(self):
        """Empty ZRANGE result returns empty list."""
        backend = MagicMock()
        backend.zrange.return_value = []
        repo = _make_repo(backend)

        result = repo.get_oldest_ids(5)

        assert result == []

    def test_get_oldest_ids_count_maps_to_zrange_stop(self):
        """count=N maps to ZRANGE(key, 0, N-1)."""
        backend = MagicMock()
        backend.zrange.return_value = []
        repo = _make_repo(backend)

        repo.get_oldest_ids(10)

        backend.zrange.assert_called_once_with("dlq:pending", 0, 9)


# =============================================================================
# C. Behavior Tests — evict_oldest 3-key consistency
# =============================================================================


class TestEvictOldestBehavior:
    """evict_oldest delegates to get_oldest_ids + delete for 3-key consistency."""

    def test_evict_oldest_calls_delete_for_each_id(self):
        """evict_oldest calls delete() for each oldest ID."""
        backend = MagicMock()
        backend.zrange.return_value = [b"1", b"2", b"3"]
        repo = _make_repo(backend)

        # Mock delete to return True
        repo.delete = MagicMock(return_value=True)

        result = repo.evict_oldest(3)

        assert result == 3
        repo.delete.assert_has_calls([call("1"), call("2"), call("3")])

    def test_evict_oldest_counts_only_successful_deletes(self):
        """evict_oldest returns count of successful delete() calls only."""
        backend = MagicMock()
        backend.zrange.return_value = [b"1", b"2", b"3"]
        repo = _make_repo(backend)

        # Second delete fails (entry already gone)
        repo.delete = MagicMock(side_effect=[True, False, True])

        result = repo.evict_oldest(3)

        assert result == 2

    def test_evict_oldest_with_domain_passes_domain_to_get_oldest_ids(self):
        """evict_oldest with domain filters by domain key."""
        backend = MagicMock()
        backend.zrange.return_value = [b"5", b"6"]
        repo = _make_repo(backend)
        repo.delete = MagicMock(return_value=True)

        result = repo.evict_oldest(2, domain="payment")

        backend.zrange.assert_called_once_with("dlq:by_domain:payment", 0, 1)
        assert result == 2

    def test_evict_oldest_empty_queue_returns_zero(self):
        """evict_oldest on empty queue returns 0 without calling delete."""
        backend = MagicMock()
        backend.zrange.return_value = []
        repo = _make_repo(backend)
        repo.delete = MagicMock()

        result = repo.evict_oldest(5)

        assert result == 0
        repo.delete.assert_not_called()


# =============================================================================
# D. Behavior Tests — delete() 3-key cleanup
# =============================================================================


class TestDeleteThreeKeyConsistencyBehavior:
    """delete() collapses blob delete + ZSET cleanups into a single
    ``batch_write_ops`` call (544 D6, 1 RTT). The ops list covers the
    blob delete (delete op), domain ZSET (conditional), per-status ZSET
    (or PENDING_KEY when PENDING), global dlq:all ZSET, and the composite
    (status, domain) ZSET (conditional)."""

    @staticmethod
    def _ops_called_with(backend) -> list[tuple]:
        backend.batch_write_ops.assert_called_once()
        return backend.batch_write_ops.call_args.args[0]

    def test_delete_removes_from_pending_zset(self):
        """delete() includes a ``zrem dlq:pending`` op in the batch."""
        backend = MagicMock()
        repo = _make_repo(backend)
        repo._load_blob = MagicMock(
            return_value=_blob({"domain": "payment", "status": "pending"})
        )

        repo.delete("42")

        ops = self._ops_called_with(backend)
        assert ("zrem", "dlq:pending", ["42"]) in ops

    def test_delete_removes_from_domain_zset(self):
        """delete() includes a ``zrem dlq:by_domain:<domain>`` op in the batch."""
        backend = MagicMock()
        repo = _make_repo(backend)
        repo._load_blob = MagicMock(
            return_value=_blob({"domain": "payment", "status": "pending"})
        )

        repo.delete("42")

        ops = self._ops_called_with(backend)
        assert ("zrem", "dlq:by_domain:payment", ["42"]) in ops

    def test_delete_removes_blob_via_batch(self):
        """delete() includes the blob-delete op as the first batch entry."""
        backend = MagicMock()
        repo = _make_repo(backend)
        repo._load_blob = MagicMock(
            return_value=_blob({"domain": "payment", "status": "pending"})
        )

        repo.delete("42")

        ops = self._ops_called_with(backend)
        # Blob-first ordering: any prefix-application failure leaves the
        # blob deleted with orphan index entries (zrem-recoverable),
        # never the inverse.
        assert ops[0] == ("delete", "dlq:entry:42", None)
        # The per-op backend.delete path is no longer exercised when the
        # entry exists — everything rides the batched pipeline.
        backend.delete.assert_not_called()

    def test_delete_includes_composite_zrem(self):
        """544 D1/D2: delete() includes the composite (status, domain) zrem."""
        backend = MagicMock()
        repo = _make_repo(backend)
        repo._load_blob = MagicMock(
            return_value=_blob({"domain": "payment", "status": "pending"})
        )

        repo.delete("42")

        ops = self._ops_called_with(backend)
        assert ("zrem", "dlq:status_domain:pending:payment", ["42"]) in ops

    def test_delete_skips_domain_cleanup_when_no_domain(self):
        """delete() skips domain + composite cleanup when entry has no domain."""
        backend = MagicMock()
        repo = _make_repo(backend)
        repo._load_blob = MagicMock(
            return_value=_blob({"domain": "", "status": "pending"})
        )

        repo.delete("42")

        ops = self._ops_called_with(backend)
        op_keys = [k for _, k, _ in ops]
        # Pending + global (dlq:all) ZREM fire; domain + composite skipped.
        assert "dlq:pending" in op_keys
        assert "dlq:all" in op_keys
        assert not any(k.startswith("dlq:by_domain:") for k in op_keys)
        assert not any(k.startswith("dlq:status_domain:") for k in op_keys)

    def test_delete_handles_missing_entry_gracefully(self):
        """delete() falls through to ``backend.delete`` for the blob key
        when the entry is already absent — idempotent (no batch issued
        because the body has nothing to delete)."""
        backend = MagicMock()
        backend.delete.return_value = False
        repo = _make_repo(backend)
        repo._load_blob = MagicMock(return_value=None)

        repo.delete("99")

        # Missing entry path: single per-op delete, no batch_write_ops.
        backend.delete.assert_called_once_with("dlq:entry:99")
        backend.batch_write_ops.assert_not_called()
