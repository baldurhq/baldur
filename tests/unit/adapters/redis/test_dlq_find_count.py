"""
Unit tests for the Redis DLQ paginated find() / count() primitive (541 D6).

Targets:
  - RedisDLQQuery._driving_index_key / _residual_filters — index dispatch.
  - RedisDLQQuery.find / count — windowed zrevrange (no residual) vs
    full-driving-index scan + Python residual filter (status+domain combo or
    failure_type), created_at DESC ordering, sparse-intersection pagination.
  - RedisDLQRepository._update — per-status index re-scored by the created_at
    epoch (NOT the transition time); the global dlq:all index is untouched by
    _update.

The decode chain (_load_blob → _decode_entry → _to_data) is stubbed with
passthroughs so a FailedOperationData stored under an id is returned verbatim;
the MagicMock backend simulates ZSET zrevrange/zcard over DESC-ordered id lists.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.interfaces.repositories import (
    FailedOperationData,
    FailedOperationStatus,
)

PENDING = FailedOperationStatus.PENDING.value
RESOLVED = FailedOperationStatus.RESOLVED.value


def _op(
    *,
    id: str,
    domain: str = "payment",
    failure_type: str = "timeout",
    status: str = PENDING,
) -> FailedOperationData:
    return FailedOperationData(
        id=id,
        domain=domain,
        failure_type=failure_type,
        status=status,
    )


def _wire(index_ids: dict[str, list[str]], store: dict[str, FailedOperationData]):
    """Wire a real repo over a MagicMock backend simulating ZSET indexes.

    ``index_ids`` maps a relative index key to a DESC-ordered id list (what a
    real zrevrange would yield); ``store`` resolves an id to its entry.
    """
    backend = MagicMock()

    def _zrevrange(key, start, end):
        ids = index_ids.get(key, [])
        end_idx = end + 1 if end >= 0 else None
        return ids[start:end_idx]

    backend.zrevrange.side_effect = _zrevrange
    backend.zcard.side_effect = lambda key: len(index_ids.get(key, []))

    repo = RedisDLQRepository(backend)
    repo._load_blob = MagicMock(side_effect=lambda eid: store.get(eid))
    repo._decode_entry = MagicMock(side_effect=lambda blob: blob)
    repo._to_data = MagicMock(side_effect=lambda data: data)
    return repo, backend


class TestRedisDrivingIndexDispatchBehavior:
    """_driving_index_key precedence + _residual_filters composition."""

    @pytest.fixture
    def repo(self):
        return RedisDLQRepository(MagicMock())

    def test_no_filter_drives_global_index(self, repo):
        assert repo.query._driving_index_key(None, None) == repo.ALL_KEY

    def test_pending_status_drives_pending_key(self, repo):
        assert repo.query._driving_index_key(PENDING, None) == repo.PENDING_KEY

    def test_non_pending_status_drives_status_index(self, repo):
        assert repo.query._driving_index_key(RESOLVED, None) == "dlq:status:resolved"

    def test_domain_only_drives_by_domain_index(self, repo):
        assert repo.query._driving_index_key(None, "payment") == "dlq:by_domain:payment"

    def test_status_wins_over_domain_as_driving_index(self, repo):
        """status takes precedence over domain as the driving index (fixed
        precedence, not a per-call cardinality comparison); domain becomes
        residual."""
        assert (
            repo.query._driving_index_key(RESOLVED, "payment") == "dlq:status:resolved"
        )

    def test_single_dimension_filters_have_no_residual(self, repo):
        assert repo.query._residual_filters(None, None, None) == []
        assert repo.query._residual_filters(RESOLVED, None, None) == []
        assert repo.query._residual_filters(None, "payment", None) == []

    def test_status_and_domain_combo_yields_one_residual(self, repo):
        residual = repo.query._residual_filters(RESOLVED, "payment", None)
        assert len(residual) == 1
        # The residual predicate filters on the (non-driving) domain dimension.
        assert residual[0](_op(id="1", domain="payment")) is True
        assert residual[0](_op(id="2", domain="auth")) is False

    def test_failure_type_always_residual(self, repo):
        residual = repo.query._residual_filters(None, None, "timeout")
        assert len(residual) == 1
        assert residual[0](_op(id="1", failure_type="timeout")) is True
        assert residual[0](_op(id="2", failure_type="http_5xx")) is False

    def test_all_three_dimensions_yield_two_residuals(self, repo):
        residual = repo.query._residual_filters(RESOLVED, "payment", "timeout")
        assert len(residual) == 2


class TestRedisDLQFindBehavior:
    """find() windowed vs full-scan dispatch + ordering + pagination."""

    def test_no_residual_uses_windowed_zrevrange(self):
        """No residual filter → O(limit) windowed zrevrange [offset, offset+limit-1]."""
        store = {i: _op(id=i) for i in ("3", "2", "1")}
        repo, backend = _wire({"dlq:all": ["3", "2", "1"]}, store)

        results = repo.find(offset=0, limit=2)

        assert [e.id for e in results] == ["3", "2"]
        backend.zrevrange.assert_called_once_with("dlq:all", 0, 1)

    def test_no_residual_offset_shifts_window(self):
        store = {i: _op(id=i) for i in ("3", "2", "1")}
        repo, backend = _wire({"dlq:all": ["3", "2", "1"]}, store)

        results = repo.find(offset=1, limit=2)

        assert [e.id for e in results] == ["2", "1"]
        backend.zrevrange.assert_called_once_with("dlq:all", 1, 2)

    def test_status_filter_windows_the_status_index(self):
        store = {i: _op(id=i, status=RESOLVED) for i in ("b", "a")}
        repo, backend = _wire({"dlq:status:resolved": ["b", "a"]}, store)

        results = repo.find(status=RESOLVED, limit=10)

        assert [e.id for e in results] == ["b", "a"]
        backend.zrevrange.assert_called_once_with("dlq:status:resolved", 0, 9)

    def test_domain_filter_windows_the_by_domain_index(self):
        store = {"x": _op(id="x", domain="payment")}
        repo, backend = _wire({"dlq:by_domain:payment": ["x"]}, store)

        results = repo.find(domain="payment", limit=5)

        assert [e.id for e in results] == ["x"]
        backend.zrevrange.assert_called_once_with("dlq:by_domain:payment", 0, 4)

    def test_combo_status_domain_full_scans_then_filters_and_slices(self):
        """status+domain → full driving-index scan, residual domain filter, slice."""
        # Driving index = status:resolved; only some are domain=payment.
        ids = ["5", "4", "3", "2", "1"]
        store = {
            "5": _op(id="5", status=RESOLVED, domain="payment"),
            "4": _op(id="4", status=RESOLVED, domain="auth"),
            "3": _op(id="3", status=RESOLVED, domain="payment"),
            "2": _op(id="2", status=RESOLVED, domain="auth"),
            "1": _op(id="1", status=RESOLVED, domain="payment"),
        }
        repo, backend = _wire({"dlq:status:resolved": ids}, store)

        results = repo.find(status=RESOLVED, domain="payment", offset=0, limit=10)

        # Full driving-index scan (0, -1) — page computed over the filtered set.
        assert [e.id for e in results] == ["5", "3", "1"]
        backend.zrevrange.assert_called_once_with("dlq:status:resolved", 0, -1)

    def test_sparse_intersection_pagination_over_filtered_set(self):
        """A rare domain scattered through a large status index pages correctly
        over the *filtered* intersection (a windowed fetch would under-return).
        """
        # 10-entry status index; only ids 9,6,3,0 are domain=rare (DESC order).
        ids = [str(i) for i in range(9, -1, -1)]  # ["9","8",...,"0"]
        store = {}
        for i in ids:
            dom = "rare" if int(i) % 3 == 0 else "common"
            store[i] = _op(id=i, status=RESOLVED, domain=dom)
        repo, _ = _wire({"dlq:status:resolved": ids}, store)

        # Filtered (DESC) intersection is ["9","6","3","0"]; page offset=1,limit=2.
        results = repo.find(status=RESOLVED, domain="rare", offset=1, limit=2)

        assert [e.id for e in results] == ["6", "3"]

    def test_failure_type_residual_filters_full_scan(self):
        ids = ["3", "2", "1"]
        store = {
            "3": _op(id="3", failure_type="timeout"),
            "2": _op(id="2", failure_type="http_5xx"),
            "1": _op(id="1", failure_type="timeout"),
        }
        repo, backend = _wire({"dlq:all": ids}, store)

        results = repo.find(failure_type="timeout", limit=10)

        assert [e.id for e in results] == ["3", "1"]
        backend.zrevrange.assert_called_once_with("dlq:all", 0, -1)

    def test_find_preserves_descending_order_from_zrevrange(self):
        # zrevrange yields newest-first; find must not reorder.
        ids = ["c", "b", "a"]
        store = {i: _op(id=i) for i in ids}
        repo, _ = _wire({"dlq:all": ids}, store)

        assert [e.id for e in repo.find(limit=10)] == ["c", "b", "a"]

    def test_find_empty_index_returns_empty(self):
        repo, _ = _wire({}, {})
        assert repo.find() == []


class TestRedisDLQCountBehavior:
    """count() O(1) ZCARD (no residual) vs full-scan count (with residual)."""

    def test_no_residual_count_uses_zcard(self):
        repo, backend = _wire({"dlq:all": ["3", "2", "1"]}, {})

        result = repo.count()

        assert result == 3
        backend.zcard.assert_called_once_with("dlq:all")

    def test_status_count_uses_zcard_on_status_index(self):
        repo, backend = _wire({"dlq:status:resolved": ["b", "a"]}, {})

        assert repo.count(status=RESOLVED) == 2
        backend.zcard.assert_called_once_with("dlq:status:resolved")

    def test_residual_count_full_scans_without_early_stop(self):
        """count must scan the entire driving index (no find-style early stop)."""
        ids = ["5", "4", "3", "2", "1"]
        store = {
            "5": _op(id="5", status=RESOLVED, domain="payment"),
            "4": _op(id="4", status=RESOLVED, domain="auth"),
            "3": _op(id="3", status=RESOLVED, domain="payment"),
            "2": _op(id="2", status=RESOLVED, domain="auth"),
            "1": _op(id="1", status=RESOLVED, domain="payment"),
        }
        repo, backend = _wire({"dlq:status:resolved": ids}, store)

        result = repo.count(status=RESOLVED, domain="payment")

        assert result == 3  # all payment matches, not just a page
        backend.zrevrange.assert_called_once_with("dlq:status:resolved", 0, -1)
        backend.zcard.assert_not_called()

    def test_count_agrees_with_find_cardinality(self):
        ids = ["3", "2", "1"]
        store = {
            "3": _op(id="3", failure_type="timeout"),
            "2": _op(id="2", failure_type="http_5xx"),
            "1": _op(id="1", failure_type="timeout"),
        }
        repo, _ = _wire({"dlq:all": ids}, store)

        assert repo.count(failure_type="timeout") == 2
        assert len(repo.find(failure_type="timeout", limit=100)) == 2


class TestRedisIndexScoreBehavior:
    """_update re-scores per-status indexes by created_at epoch (541 D6)."""

    def _repo_with_entry(self, *, created_at_iso: str, status: str = PENDING):
        """Real repo whose _load_blob yields a single entry with a fixed
        created_at; the global/per-status writes hit the MagicMock backend."""
        backend = MagicMock()
        repo = RedisDLQRepository(backend)
        data = {
            "id": "e1",
            "domain": "payment",
            "failure_type": "timeout",
            "status": status,
            "created_at": created_at_iso,
            "retry_count": 0,
            "max_retries": 2,
        }
        repo._load_blob = MagicMock(return_value=repo._encode_entry(data))
        return repo, backend

    @staticmethod
    def _ops_from_batch(backend) -> list[tuple]:
        """544 D6: _update status transitions ride a single batch_write_ops
        call covering [zrem old per-status, zadd new per-status, zrem old
        composite, zadd new composite, set_blob]."""
        backend.batch_write_ops.assert_called_once()
        return backend.batch_write_ops.call_args.args[0]

    def test_status_index_scored_by_created_at_not_transition_time(self):
        """Per-status zadd score == created_at epoch, not utc_now()."""
        created_iso = "2026-01-01T10:00:00+00:00"
        expected_score = datetime.fromisoformat(created_iso).timestamp()
        repo, backend = self._repo_with_entry(created_at_iso=created_iso)

        assert repo._update(entry_id="e1", status=RESOLVED) is True

        ops = self._ops_from_batch(backend)
        # Old PENDING index removal + new RESOLVED index add at created_at score.
        assert ("zrem", "dlq:pending", ["e1"]) in ops
        resolved_zadds = [
            op for op in ops if op[0] == "zadd" and op[1] == "dlq:status:resolved"
        ]
        assert len(resolved_zadds) == 1
        assert resolved_zadds[0][2] == {"e1": expected_score}

    def test_global_index_untouched_by_update(self):
        """_update never zadds/zrems dlq:all — the entry stays in it across
        every transition (541 D6)."""
        repo, backend = self._repo_with_entry(
            created_at_iso="2026-01-01T10:00:00+00:00"
        )

        repo._update(entry_id="e1", status=RESOLVED)

        ops = self._ops_from_batch(backend)
        touched_keys = [op[1] for op in ops]
        assert "dlq:all" not in touched_keys

    def test_unparseable_created_at_falls_back_without_crashing(self):
        """A malformed created_at falls back to time.time() (no exception)."""
        repo, backend = self._repo_with_entry(created_at_iso="not-a-date")

        assert repo._update(entry_id="e1", status=RESOLVED) is True
        ops = self._ops_from_batch(backend)
        resolved_zadds = [
            op for op in ops if op[0] == "zadd" and op[1] == "dlq:status:resolved"
        ]
        # A float score is still written (fallback path), entry not lost.
        assert isinstance(resolved_zadds[0][2]["e1"], float)


# =============================================================================
# 544 D8: composite-driven find/count + get_pending_by_domain routing
# =============================================================================


@pytest.fixture(autouse=True)
def _restore_raw_redis_client_property():
    """Snapshot the ``_raw_redis_client`` property before each test and
    restore it after, so ``_wire_composite``'s class-level patch does not
    bleed across tests in this module."""
    from baldur.adapters.redis.dlq import RedisDLQRepository as _R

    original = _R._raw_redis_client
    try:
        yield
    finally:
        _R._raw_redis_client = original


def _wire_composite(
    index_ids: dict[str, list[str]],
    store: dict[str, FailedOperationData],
    *,
    is_degraded: bool = False,
    composite_warm: bool = True,
    raw_client_exists: int = 1,
):
    """Wire a real repo + RedisDLQQuery with a MagicMock backend and a
    raw-client EXISTS/ZINTERSTORE shim so the composite warmup path can
    be exercised without a live Redis.

    ``composite_warm=True`` pre-seeds ``_composite_warmed`` so the fast
    path is taken without any EXISTS call; ``composite_warm=False``
    lets the test exercise the lazy-warm code path with the raw_client_exists
    knob simulating EXISTS=0 or 1.
    """
    backend = MagicMock()
    backend.is_degraded = is_degraded

    def _zrevrange(key, start, end):
        ids = index_ids.get(key, [])
        end_idx = end + 1 if end >= 0 else None
        return ids[start:end_idx]

    def _zrange(key, start, end):
        ids = index_ids.get(key, [])
        end_idx = end + 1 if end >= 0 else None
        return ids[start:end_idx]

    backend.zrevrange.side_effect = _zrevrange
    backend.zrange.side_effect = _zrange
    backend.zcard.side_effect = lambda key: len(index_ids.get(key, []))
    backend._get_full_key.side_effect = lambda key: key

    repo = RedisDLQRepository(backend)
    repo._load_blob = MagicMock(side_effect=lambda eid: store.get(eid))
    repo._decode_entry = MagicMock(side_effect=lambda blob: blob)
    repo._to_data = MagicMock(side_effect=lambda data: data)

    raw_client = MagicMock()
    raw_client.execute_command.return_value = raw_client_exists

    # Patch _raw_redis_client property on the class so the query sees the
    # injected raw client. The autouse fixture _restore_raw_redis_client_property
    # snapshots and restores the original property around every test.
    type(repo)._raw_redis_client = property(lambda self: raw_client)

    if composite_warm:
        # Pre-seed the cache for every (status, domain) pair the test
        # wires through index_ids -- the production path would lazily
        # warm these via EXISTS+ZINTERSTORE on first call.
        for key in index_ids:
            if key.startswith("dlq:status_domain:"):
                rest = key[len("dlq:status_domain:") :]
                status, _, domain = rest.partition(":")
                repo.query._composite_warmed.add((status, domain))

    return repo, backend, raw_client


class TestRedisFindCountCompositeBehavior:
    """544 D8: find/count(status, domain) routes through composite ZSET
    when the (s,d) pair is warm -- windowed ZREVRANGE / O(1) ZCARD with
    NO Python domain residual filter."""

    def test_find_uses_windowed_zrevrange_on_composite_when_warm(self):
        """When the composite is warm, find(status=RESOLVED, domain=payment)
        issues a single ZREVRANGE [0, limit-1] on dlq:status_domain:resolved:
        payment -- no full driving-index scan, no Python residual filter."""
        ids = ["3", "2", "1"]
        store = {i: _op(id=i, status=RESOLVED, domain="payment") for i in ids}
        repo, backend, _ = _wire_composite(
            {"dlq:status_domain:resolved:payment": ids}, store
        )

        results = repo.find(status=RESOLVED, domain="payment", limit=2)

        assert [e.id for e in results] == ["3", "2"]
        backend.zrevrange.assert_called_once_with(
            "dlq:status_domain:resolved:payment", 0, 1
        )

    def test_count_uses_o1_zcard_on_composite_when_warm(self):
        ids = ["3", "2", "1"]
        store = {i: _op(id=i, status=RESOLVED, domain="payment") for i in ids}
        repo, backend, _ = _wire_composite(
            {"dlq:status_domain:resolved:payment": ids}, store
        )

        assert repo.count(status=RESOLVED, domain="payment") == 3
        backend.zcard.assert_called_once_with("dlq:status_domain:resolved:payment")
        backend.zrevrange.assert_not_called()

    def test_residual_filters_returns_empty_when_composite_warm(self):
        """When the composite is warm, both status AND domain are resolved
        by the index -- the domain predicate drops from _residual_filters."""
        repo, _, _ = _wire_composite({}, {})
        repo.query._composite_warmed.add((RESOLVED, "payment"))

        # Pass composite_warm=True so the helper drops the domain residual.
        residual = repo.query._residual_filters(
            RESOLVED, "payment", None, composite_warm=True
        )
        assert residual == []

    def test_degraded_mode_keeps_legacy_per_status_driving_index(self):
        """In degraded mode, the composite is unavailable (raw client not
        used) -- find falls back to the legacy per-status driving index
        + Python domain residual."""
        ids = ["3", "2"]
        store = {i: _op(id=i, status=RESOLVED, domain="payment") for i in ids}
        repo, backend, _ = _wire_composite(
            {"dlq:status:resolved": ids}, store, is_degraded=True
        )

        results = repo.find(status=RESOLVED, domain="payment", limit=10)

        assert [e.id for e in results] == ids
        # Driving index was the per-status ZSET, scanned in full (no
        # composite call).
        backend.zrevrange.assert_called_once_with("dlq:status:resolved", 0, -1)


class TestGetPendingByDomainCompositeBehavior:
    """544 D8: get_pending_by_domain / get_pending_count_by_domain route
    through composite (PENDING, domain) ZSET in normal mode; degraded mode
    keeps the legacy by_domain + Python status filter."""

    def test_get_pending_by_domain_normal_warm_uses_composite_zrange(self):
        ids = ["3", "2", "1"]
        store = {i: _op(id=i, status=PENDING, domain="payment") for i in ids}
        repo, backend, _ = _wire_composite(
            {"dlq:status_domain:pending:payment": ids}, store
        )

        results = repo.get_pending_by_domain("payment", limit=2)

        assert [e.id for e in results] == ["3", "2"]
        backend.zrange.assert_called_once_with(
            "dlq:status_domain:pending:payment", 0, 1
        )

    def test_get_pending_count_by_domain_normal_warm_is_o1_zcard(self):
        """No 10K-limit blob load; O(1) ZCARD on the composite key."""
        ids = ["3", "2", "1"]
        store = {i: _op(id=i, status=PENDING, domain="payment") for i in ids}
        repo, backend, _ = _wire_composite(
            {"dlq:status_domain:pending:payment": ids}, store
        )

        assert repo.get_pending_count_by_domain("payment") == 3
        backend.zcard.assert_called_once_with("dlq:status_domain:pending:payment")
        # No blob loads -- the legacy path's len(get_pending_by_domain(limit=10000))
        # would have called _load_blob N times.
        repo._load_blob.assert_not_called()

    def test_get_pending_by_domain_degraded_uses_legacy_by_domain_index(self):
        """Degraded mode keeps the legacy ``dlq:by_domain:{domain}`` ZRANGE
        + Python status==PENDING filter."""
        ids = ["3", "2"]
        # Degraded path calls ``data.get("status")`` before _to_data, so the
        # store must yield dicts; _to_data then maps each dict to a DTO with
        # the same id.
        store = {
            i: {"id": i, "status": PENDING, "domain": "payment", "failure_type": "t"}
            for i in ids
        }
        repo, backend, _ = _wire_composite(
            {"dlq:by_domain:payment": ids}, store, is_degraded=True
        )
        repo._to_data = MagicMock(
            side_effect=lambda d: _op(
                id=d["id"], status=d["status"], domain=d["domain"]
            )
        )

        results = repo.get_pending_by_domain("payment", limit=10)

        assert [e.id for e in results] == ids
        backend.zrange.assert_called_once_with("dlq:by_domain:payment", 0, 9)

    def test_get_pending_by_domain_normal_cold_triggers_warmup_then_zranges(self):
        """A cold (PENDING, domain) pair triggers EXISTS+ZINTERSTORE warmup
        on first call; subsequent ZRANGE hits the composite key."""
        ids = ["3", "2", "1"]
        store = {i: _op(id=i, status=PENDING, domain="payment") for i in ids}
        # Composite ZRANGE returns the ids after warmup, EXISTS=1 means
        # the composite is already populated (any other writer).
        repo, backend, raw_client = _wire_composite(
            {"dlq:status_domain:pending:payment": ids},
            store,
            composite_warm=False,
            raw_client_exists=1,
        )

        results = repo.get_pending_by_domain("payment", limit=2)

        # EXISTS was called once on the composite key.
        assert raw_client.execute_command.call_count >= 1
        assert raw_client.execute_command.call_args_list[0].args[0] == "EXISTS"
        # ZRANGE was issued on the composite key after warmup.
        backend.zrange.assert_called_once_with(
            "dlq:status_domain:pending:payment", 0, 1
        )
        assert [e.id for e in results] == ["3", "2"]
