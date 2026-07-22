"""
Redis DLQ Sub-Module Unit Tests (354 God Object Refactoring).

Test targets:
    - baldur.adapters.redis.dlq_query.RedisDLQQuery
    - baldur.adapters.redis.dlq_lifecycle.RedisDLQLifecycle
    - baldur.adapters.redis.dlq_maintenance.RedisDLQMaintenance
    - baldur.adapters.redis.dlq_compression.RedisDLQCompression
    - baldur.adapters.redis.dlq_compression._deserialize_compressed_entry

Test Categories:
    A. Behavior: RedisDLQQuery — query, filter, statistics
    B. Behavior: RedisDLQLifecycle — state transitions, replay
    C. Behavior: RedisDLQMaintenance — archive, purge, eviction
    D. Behavior: RedisDLQCompression — compressed entry CRUD
    E. Behavior: _deserialize_compressed_entry — deserialization
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, call, patch

import pytest
from structlog.testing import capture_logs


def _blob(data: dict) -> bytes:
    """Encode dict as the orjson-style JSON bytes a real entry blob would hold."""
    return json.dumps(data).encode("utf-8")


from baldur.adapters.redis.dlq_compression import (
    _COMPRESSED_INDEX_KEY,
    _SUMMARY_MGET_CHUNK,
    RedisDLQCompression,
    _compressed_score,
    _deserialize_compressed_entry,
    _is_reserved_compressed_id,
    _summary_scan_cap,
)
from baldur.adapters.redis.dlq_lifecycle import RedisDLQLifecycle
from baldur.adapters.redis.dlq_maintenance import RedisDLQMaintenance
from baldur.adapters.redis.dlq_query import RedisDLQQuery
from baldur.interfaces.repositories import (
    DLQCompressedEntry,
    DLQCompressedStatus,
    FailedOperationData,
    FailedOperationStatus,
)

# =============================================================================
# Helpers
# =============================================================================


def _make_failed_op_data(
    *,
    id: int = 1,
    domain: str = "payment",
    failure_type: str = "timeout",
    status: str = FailedOperationStatus.PENDING.value,
    retry_count: int = 0,
    max_retries: int = 3,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    resolved_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> FailedOperationData:
    """Create a FailedOperationData with test defaults."""
    return FailedOperationData(
        id=id,
        domain=domain,
        failure_type=failure_type,
        status=status,
        retry_count=retry_count,
        max_retries=max_retries,
        created_at=created_at,
        updated_at=updated_at,
        resolved_at=resolved_at,
        expires_at=expires_at,
    )


def _make_compressed_entry(
    *,
    id: str = "compressed:payment:timeout:E001:1700000000",
    domain: str = "payment",
    failure_type: str = "timeout",
    error_code: str = "E001",
    count: int = 10,
    status: str = "active",
    compressed_at: datetime | None = None,
    first_seen: datetime | None = None,
    last_seen: datetime | None = None,
) -> DLQCompressedEntry:
    """Create a DLQCompressedEntry with test defaults."""
    now = datetime.now(UTC)
    return DLQCompressedEntry(
        id=id,
        domain=domain,
        failure_type=failure_type,
        error_code=error_code,
        count=count,
        first_seen=first_seen or now - timedelta(days=7),
        last_seen=last_seen or now,
        sample_error_message="Connection timeout",
        sample_context={"endpoint": "/api/pay"},
        status=status,
        compressed_at=compressed_at or now,
    )


def _make_repo(mock_backend=None):
    """Create RedisDLQRepository with mock backend, matching overflow test pattern."""
    from baldur.adapters.redis.dlq import RedisDLQRepository

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
# A. RedisDLQQuery Behavior Tests
# =============================================================================


class TestRedisDLQQueryBehavior:
    """RedisDLQQuery query and filter operations behavior."""

    def test_get_pending_by_domain_filters_by_pending_status(self):
        """get_pending_by_domain returns only entries with PENDING status."""
        backend = MagicMock()
        backend.zrange.return_value = [b"1", b"2", b"3"]
        repo = _make_repo(backend)
        repo._load_blob = MagicMock(
            side_effect=[
                _blob(
                    {
                        "id": "1",
                        "domain": "payment",
                        "status": FailedOperationStatus.PENDING.value,
                    }
                ),
                _blob(
                    {
                        "id": "2",
                        "domain": "payment",
                        "status": FailedOperationStatus.RESOLVED.value,
                    }
                ),
                _blob(
                    {
                        "id": "3",
                        "domain": "payment",
                        "status": FailedOperationStatus.PENDING.value,
                    }
                ),
            ]
        )
        repo._make_key = MagicMock(side_effect=lambda eid: f"dlq:{eid}")
        repo._to_data = MagicMock(
            side_effect=lambda data: _make_failed_op_data(
                id=int(data["id"]),
                status=data["status"],
            )
        )

        results = repo.query.get_pending_by_domain("payment", limit=100)

        assert len(results) == 2
        assert all(r.status == FailedOperationStatus.PENDING.value for r in results)

    def test_get_pending_by_domain_uses_domain_key(self):
        """get_pending_by_domain queries the correct domain sorted set key."""
        backend = MagicMock()
        backend.zrange.return_value = []
        repo = _make_repo(backend)

        repo.query.get_pending_by_domain("inventory", limit=50)

        backend.zrange.assert_called_once_with("dlq:by_domain:inventory", 0, 49)

    def test_get_pending_by_domain_respects_limit(self):
        """get_pending_by_domain passes limit-1 as ZRANGE stop argument."""
        backend = MagicMock()
        backend.zrange.return_value = []
        repo = _make_repo(backend)

        repo.query.get_pending_by_domain("payment", limit=25)

        backend.zrange.assert_called_once_with("dlq:by_domain:payment", 0, 24)

    def test_get_pending_count_by_domain_delegates_to_get_pending_by_domain(self):
        """get_pending_count_by_domain returns length from get_pending_by_domain."""
        backend = MagicMock()
        backend.zrange.return_value = [b"1", b"2"]
        repo = _make_repo(backend)
        repo._load_blob = MagicMock(
            side_effect=[
                _blob(
                    {
                        "id": "1",
                        "domain": "payment",
                        "status": FailedOperationStatus.PENDING.value,
                    }
                ),
                _blob(
                    {
                        "id": "2",
                        "domain": "payment",
                        "status": FailedOperationStatus.PENDING.value,
                    }
                ),
            ]
        )
        repo._make_key = MagicMock(side_effect=lambda eid: f"dlq:{eid}")
        repo._to_data = MagicMock(
            side_effect=lambda data: _make_failed_op_data(
                id=int(data["id"]),
            )
        )

        count = repo.query.get_pending_count_by_domain("payment")

        assert count == 2

    def test_by_status_pending_delegates_to_get_pending(self):
        """by_status with PENDING status delegates to repo.get_pending."""
        repo = _make_repo()
        expected = [_make_failed_op_data(id=1)]
        repo.get_pending = MagicMock(return_value=expected)

        results = repo.query.by_status(FailedOperationStatus.PENDING.value, limit=50)

        repo.get_pending.assert_called_once_with(50)
        assert results == expected

    def test_by_status_non_pending_degraded_mode_scans_memory(self):
        """by_status for non-PENDING in degraded mode scans in-memory storage."""
        backend = MagicMock()
        backend.is_degraded = True
        backend._memory = {
            "dlq:1": _blob({"id": "1", "status": FailedOperationStatus.RESOLVED.value}),
            "dlq:2": _blob({"id": "2", "status": FailedOperationStatus.REJECTED.value}),
            "dlq:pending": "special_key",
        }
        repo = _make_repo(backend)
        repo._is_valid_entry_key = MagicMock(
            side_effect=lambda k: k.startswith("dlq:") and k not in ["dlq:pending"]
        )
        repo._to_data = MagicMock(
            side_effect=lambda data: _make_failed_op_data(
                id=int(data["id"]),
                status=data["status"],
            )
        )

        results = repo.query.by_status(FailedOperationStatus.RESOLVED.value, limit=100)

        assert len(results) == 1
        assert results[0].status == FailedOperationStatus.RESOLVED.value

    def test_count_by_status_pending_delegates_to_zcard(self):
        """count_by_status for PENDING delegates to backend.zcard on PENDING_KEY."""
        backend = MagicMock()
        backend.zcard.return_value = 42
        repo = _make_repo(backend)

        result = repo.query.count_by_status(FailedOperationStatus.PENDING.value)

        backend.zcard.assert_called_once_with("dlq:pending")
        assert result == 42

    def test_count_by_status_non_pending_uses_zcard_on_status_index(self):
        """count_by_status for indexed statuses uses ZCARD on status sorted set."""
        backend = MagicMock()
        backend.zcard.return_value = 42
        repo = _make_repo(backend)

        result = repo.query.count_by_status(FailedOperationStatus.RESOLVED.value)

        backend.zcard.assert_called_once_with("dlq:status:resolved")
        assert result == 42

    def test_find_by_status_applies_domain_filter(self):
        """find_by_status filters results by domain when provided."""
        repo = _make_repo()
        repo.query.by_status = MagicMock(
            return_value=[
                _make_failed_op_data(id=1, domain="payment"),
                _make_failed_op_data(id=2, domain="inventory"),
                _make_failed_op_data(id=3, domain="payment"),
            ]
        )

        results = repo.query.find_by_status(
            FailedOperationStatus.PENDING.value,
            domain="payment",
        )

        assert len(results) == 2
        assert all(r.domain == "payment" for r in results)

    def test_find_by_status_applies_failure_type_filter(self):
        """find_by_status filters results by failure_type when provided."""
        repo = _make_repo()
        repo.query.by_status = MagicMock(
            return_value=[
                _make_failed_op_data(id=1, failure_type="timeout"),
                _make_failed_op_data(id=2, failure_type="connection_error"),
                _make_failed_op_data(id=3, failure_type="timeout"),
            ]
        )

        results = repo.query.find_by_status(
            FailedOperationStatus.PENDING.value,
            failure_type="timeout",
        )

        assert len(results) == 2
        assert all(r.failure_type == "timeout" for r in results)

    def test_find_by_status_applies_both_domain_and_failure_type_filters(self):
        """find_by_status applies both domain and failure_type filters."""
        repo = _make_repo()
        repo.query.by_status = MagicMock(
            return_value=[
                _make_failed_op_data(id=1, domain="payment", failure_type="timeout"),
                _make_failed_op_data(
                    id=2, domain="payment", failure_type="connection_error"
                ),
                _make_failed_op_data(id=3, domain="inventory", failure_type="timeout"),
            ]
        )

        results = repo.query.find_by_status(
            FailedOperationStatus.PENDING.value,
            domain="payment",
            failure_type="timeout",
        )

        assert len(results) == 1
        assert results[0].id == 1

    def test_find_by_status_respects_limit(self):
        """find_by_status returns at most `limit` entries."""
        repo = _make_repo()
        repo.query.by_status = MagicMock(
            return_value=[
                _make_failed_op_data(id=i, domain="payment") for i in range(10)
            ]
        )

        results = repo.query.find_by_status(
            FailedOperationStatus.PENDING.value,
            domain="payment",
            limit=3,
        )

        assert len(results) == 3

    def test_find_replayable_filters_by_retry_count_below_max_retries(self):
        """find_replayable returns PENDING entries with retry_count < max_retries."""
        repo = _make_repo()
        repo.query.find_by_status = MagicMock(
            return_value=[
                _make_failed_op_data(id=1, retry_count=0, max_retries=3),
                _make_failed_op_data(id=2, retry_count=3, max_retries=3),
                _make_failed_op_data(id=3, retry_count=1, max_retries=3),
            ]
        )

        results = repo.query.find_replayable(max_retries=3)

        assert len(results) == 2
        assert results[0].id == 1
        assert results[1].id == 3

    def test_find_replayable_passes_filters_to_find_by_status(self):
        """find_replayable passes domain and failure_type to find_by_status."""
        repo = _make_repo()
        repo.query.find_by_status = MagicMock(return_value=[])

        repo.query.find_replayable(
            max_retries=3,
            domain="payment",
            failure_type="timeout",
            limit=50,
        )

        repo.query.find_by_status.assert_called_once_with(
            status=FailedOperationStatus.PENDING.value,
            domain="payment",
            failure_type="timeout",
            limit=100,  # limit * 2
        )

    def test_find_sla_breached_returns_entries_past_sla_deadline(self):
        """find_sla_breached returns entries created before SLA deadline."""
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        repo = _make_repo()
        pending_entries = [
            _make_failed_op_data(
                id=1,
                domain="payment",
                created_at=now - timedelta(hours=25),
            ),
            _make_failed_op_data(
                id=2,
                domain="payment",
                created_at=now - timedelta(hours=10),
            ),
            _make_failed_op_data(
                id=3,
                domain="inventory",
                created_at=now - timedelta(hours=5),
            ),
        ]
        repo.get_pending = MagicMock(return_value=pending_entries)

        sla_thresholds = {
            "default": timedelta(hours=24),
            "inventory": timedelta(hours=4),
        }

        results = repo.query.find_sla_breached(now, sla_thresholds)

        assert len(results) == 2
        result_ids = {r.id for r in results}
        assert result_ids == {1, 3}

    def test_find_sla_breached_uses_default_threshold_for_unknown_domain(self):
        """find_sla_breached uses 'default' threshold when domain not in thresholds."""
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        repo = _make_repo()
        pending_entries = [
            _make_failed_op_data(
                id=1,
                domain="unknown_domain",
                created_at=now - timedelta(hours=25),
            ),
        ]
        repo.get_pending = MagicMock(return_value=pending_entries)

        sla_thresholds = {"default": timedelta(hours=24)}

        results = repo.query.find_sla_breached(now, sla_thresholds)

        assert len(results) == 1

    def test_find_sla_breached_skips_entries_without_created_at(self):
        """find_sla_breached skips entries that have no created_at."""
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        repo = _make_repo()
        pending_entries = [
            _make_failed_op_data(id=1, domain="payment", created_at=None),
        ]
        repo.get_pending = MagicMock(return_value=pending_entries)

        results = repo.query.find_sla_breached(
            now,
            {"default": timedelta(hours=24)},
        )

        assert len(results) == 0

    def test_find_expired_delegates_to_get_expired_operations(self):
        """find_expired calls get_expired_operations with current_time."""
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        repo = _make_repo()
        expected = [_make_failed_op_data(id=1)]
        repo.query.get_expired_operations = MagicMock(return_value=expected)

        results = repo.query.find_expired(now)

        repo.query.get_expired_operations.assert_called_once_with(now)
        assert results == expected

    def test_get_expired_operations_returns_entries_past_expiry(self):
        """get_expired_operations returns entries with expires_at < before_date."""
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        repo = _make_repo()
        pending_entries = [
            _make_failed_op_data(id=1, expires_at=now - timedelta(hours=1)),
            _make_failed_op_data(id=2, expires_at=now + timedelta(hours=1)),
            _make_failed_op_data(id=3, expires_at=None),
        ]
        repo.get_pending = MagicMock(return_value=pending_entries)

        results = repo.query.get_expired_operations(now)

        assert len(results) == 1
        assert results[0].id == 1

    def test_get_statistics_returns_complete_status_breakdown(self):
        """get_statistics uses ZCARD for all indexed statuses."""
        backend = MagicMock()
        repo = _make_repo(backend)
        repo.count_pending = MagicMock(return_value=10)

        zcard_values = {
            "dlq:pending": 10,
            "dlq:status:resolved": 5,
            "dlq:status:requires_review": 3,
            "dlq:status:rejected": 2,
            "dlq:status:archived": 1,
            # 541 D6: total is the global-index ZCARD, independent of the
            # 5-status partial sum (here 25 > 21 — it includes escalated/
            # terminal statuses like permanently_failed the sum omitted).
            "dlq:all": 25,
        }
        backend.zcard.side_effect = lambda key: zcard_values.get(key, 0)

        stats = repo.query.get_statistics()

        assert stats["pending"] == 10
        assert stats["pending_count"] == 10
        assert stats["resolved"] == 5
        assert stats["resolved_count"] == 5
        assert stats["requires_review"] == 3
        assert stats["reviewing_count"] == 3
        assert stats["rejected"] == 2
        assert stats["rejected_count"] == 2
        assert stats["archived"] == 1
        assert stats["archived_count"] == 1
        assert stats["total"] == 25


# =============================================================================
# B. RedisDLQLifecycle Behavior Tests
# =============================================================================


class TestRedisDLQLifecycleBehavior:
    """RedisDLQLifecycle state transition and replay behavior."""

    def test_mark_as_resolved_calls_update_with_resolved_status(self):
        """mark_as_resolved delegates to _update with RESOLVED status."""
        repo = _make_repo()
        repo._update = MagicMock(return_value=True)

        result = repo.lifecycle.mark_as_resolved(
            id=42,
            resolution_type="manual",
            resolution_note="Fixed",
            resolved_by_id=7,
        )

        assert result is True
        repo._update.assert_called_once()
        call_kwargs = repo._update.call_args[1]
        assert call_kwargs["entry_id"] == 42
        assert call_kwargs["status"] == FailedOperationStatus.RESOLVED.value
        assert call_kwargs["resolution_type"] == "manual"
        assert call_kwargs["resolution_note"] == "Fixed"
        assert call_kwargs["resolved_by_id"] == 7
        assert "resolved_at" in call_kwargs

    def test_mark_rejected_calls_update_with_rejected_status(self):
        """mark_rejected delegates to _update with REJECTED status."""
        repo = _make_repo()
        repo._update = MagicMock(return_value=True)

        result = repo.lifecycle.mark_rejected(
            id=42,
            reason="Invalid entry",
            rejected_by_id=5,
        )

        assert result is True
        call_kwargs = repo._update.call_args[1]
        assert call_kwargs["entry_id"] == 42
        assert call_kwargs["status"] == FailedOperationStatus.REJECTED.value
        assert call_kwargs["resolution_type"] == "rejected"
        assert call_kwargs["resolution_note"] == "Invalid entry"
        assert call_kwargs["resolved_by_id"] == 5

    def test_mark_rejected_default_reason_is_empty(self):
        """mark_rejected uses empty string as default reason."""
        repo = _make_repo()
        repo._update = MagicMock(return_value=True)

        repo.lifecycle.mark_rejected(id=42)

        call_kwargs = repo._update.call_args[1]
        assert call_kwargs["resolution_note"] == ""
        assert call_kwargs["resolved_by_id"] is None

    def test_increment_retry_count_increments_and_updates_last_retry_at(self):
        """increment_retry_count increments count and sets last_retry_at."""
        repo = _make_repo()
        repo._load_blob = MagicMock(
            return_value=b'{"retry_count": 1, "max_retries": 5}'
        )
        repo._update = MagicMock(return_value=True)

        result = repo.lifecycle.increment_retry_count(42)

        assert result is True
        first_call_kwargs = repo._update.call_args_list[0][1]
        assert first_call_kwargs["entry_id"] == 42
        assert first_call_kwargs["retry_count"] == 2
        assert "last_retry_at" in first_call_kwargs

    def test_increment_retry_count_changes_status_to_requires_review_when_max_reached(
        self,
    ):
        """increment_retry_count sets REQUIRES_REVIEW when new count >= max_retries."""
        repo = _make_repo()
        repo._load_blob = MagicMock(
            return_value=b'{"retry_count": 2, "max_retries": 3}'
        )
        repo._update = MagicMock(return_value=True)

        repo.lifecycle.increment_retry_count(42)

        # Single _update call includes both retry_count and status
        assert repo._update.call_count == 1
        call_kwargs = repo._update.call_args[1]
        assert call_kwargs["retry_count"] == 3
        assert call_kwargs["status"] == FailedOperationStatus.REQUIRES_REVIEW.value

    def test_increment_retry_count_does_not_change_status_when_below_max(self):
        """increment_retry_count does not change status when below max_retries."""
        repo = _make_repo()
        repo._load_blob = MagicMock(
            return_value=b'{"retry_count": 0, "max_retries": 3}'
        )
        repo._update = MagicMock(return_value=True)

        repo.lifecycle.increment_retry_count(42)

        # Only one _update call for retry_count
        assert repo._update.call_count == 1

    def test_increment_retry_count_returns_false_when_entry_not_found(self):
        """increment_retry_count returns False when blob is missing."""
        repo = _make_repo()
        repo._load_blob = MagicMock(return_value=None)

        result = repo.lifecycle.increment_retry_count(99)

        assert result is False

    def test_increment_retry_count_returns_false_when_update_write_fails(self):
        """increment_retry_count propagates _update's False — no false success.

        Scenario plan §328 row 1.9: when the entry exists but the Redis
        write phase of ``_update`` fails, callers must learn about the
        failure. The previous implementation discarded the ``_update``
        return value and reported True unconditionally.
        """
        repo = _make_repo()
        repo._load_blob = MagicMock(
            return_value=b'{"retry_count": 1, "max_retries": 5}'
        )
        repo._update = MagicMock(return_value=False)

        result = repo.lifecycle.increment_retry_count(42)

        assert result is False

    def test_increment_retry_count_returns_update_result_on_successful_write(self):
        """increment_retry_count returns True only when _update returns True."""
        repo = _make_repo()
        repo._load_blob = MagicMock(
            return_value=b'{"retry_count": 0, "max_retries": 5}'
        )
        repo._update = MagicMock(return_value=True)

        result = repo.lifecycle.increment_retry_count(42)

        assert result is True

    def test_try_acquire_for_replay_returns_none_for_non_pending_entry(self):
        """try_acquire_for_replay returns None when entry is not PENDING."""
        repo = _make_repo()
        repo.get_by_id = MagicMock(
            return_value=_make_failed_op_data(
                id=42,
                status=FailedOperationStatus.RESOLVED.value,
            )
        )

        result = repo.lifecycle.try_acquire_for_replay(42, max_retries=3)

        assert result is None

    def test_try_acquire_for_replay_returns_none_when_max_retries_reached(self):
        """try_acquire_for_replay returns None when retry_count >= max_retries."""
        repo = _make_repo()
        repo.get_by_id = MagicMock(
            return_value=_make_failed_op_data(
                id=42,
                retry_count=3,
                max_retries=3,
            )
        )

        result = repo.lifecycle.try_acquire_for_replay(42, max_retries=3)

        assert result is None

    def test_try_acquire_for_replay_returns_none_when_entry_not_found(self):
        """try_acquire_for_replay returns None when entry does not exist."""
        repo = _make_repo()
        repo.get_by_id = MagicMock(return_value=None)

        result = repo.lifecycle.try_acquire_for_replay(42, max_retries=3)

        assert result is None

    def test_try_acquire_for_replay_sets_replaying_status_and_increments_retry(self):
        """try_acquire_for_replay updates status to 'replaying' and increments retry_count."""
        repo = _make_repo()
        repo._ensure_redis_available = MagicMock(return_value=False)
        entry = _make_failed_op_data(id=42, retry_count=1, max_retries=3)
        updated_entry = _make_failed_op_data(
            id=42,
            retry_count=2,
            max_retries=3,
            status="replaying",
        )
        repo.get_by_id = MagicMock(side_effect=[entry, updated_entry])
        repo._update = MagicMock(return_value=True)

        result = repo.lifecycle.try_acquire_for_replay(42, max_retries=3)

        assert result is not None
        assert result.status == "replaying"
        call_kwargs = repo._update.call_args[1]
        assert call_kwargs["status"] == "replaying"
        assert call_kwargs["retry_count"] == 2

    def test_try_acquire_for_replay_returns_none_when_update_fails(self):
        """try_acquire_for_replay returns None when _update returns False."""
        repo = _make_repo()
        repo._ensure_redis_available = MagicMock(return_value=False)
        repo.get_by_id = MagicMock(
            return_value=_make_failed_op_data(
                id=42,
                retry_count=0,
                max_retries=3,
            )
        )
        repo._update = MagicMock(return_value=False)

        result = repo.lifecycle.try_acquire_for_replay(42, max_retries=3)

        assert result is None

    def test_complete_replay_success_delegates_to_mark_as_resolved(self):
        """complete_replay with success=True calls mark_as_resolved."""
        repo = _make_repo()
        repo._update = MagicMock(return_value=True)

        result = repo.lifecycle.complete_replay(
            id=42,
            success=True,
            resolution_type="auto_replay",
            note="Replay succeeded",
        )

        assert result is True
        call_kwargs = repo._update.call_args[1]
        assert call_kwargs["status"] == FailedOperationStatus.RESOLVED.value

    def test_complete_replay_success_does_not_fabricate_auto_replay(self):
        """complete_replay(success=True) with an empty resolution_type no longer
        stamps the blanket 'auto_replay' — a fresh entry has no prior value, so
        the empty stamp is preserved rather than fabricated (honest provenance).
        """
        repo = _make_repo()
        repo.get_by_id = MagicMock(return_value=_make_failed_op_data(id=42))
        repo._update = MagicMock(return_value=True)

        repo.lifecycle.complete_replay(id=42, success=True)

        call_kwargs = repo._update.call_args[1]
        assert call_kwargs["resolution_type"] != "auto_replay"
        assert call_kwargs["resolution_type"] == ""

    def test_complete_replay_success_preserves_prior_resolution_type_when_empty(self):
        """An empty resolution_type falls back to the entry's existing value
        (memory-adapter parity), never a fabricated blanket stamp.
        """
        repo = _make_repo()
        prior = _make_failed_op_data(id=42)
        prior.resolution_type = "manual_replay"
        repo.get_by_id = MagicMock(return_value=prior)
        repo._update = MagicMock(return_value=True)

        repo.lifecycle.complete_replay(id=42, success=True)

        assert repo._update.call_args[1]["resolution_type"] == "manual_replay"

    def test_complete_replay_failure_returns_to_pending_if_retries_remain(self):
        """complete_replay with failure returns to PENDING when retries remain."""
        repo = _make_repo()
        repo.get_by_id = MagicMock(
            return_value=_make_failed_op_data(
                id=42,
                retry_count=1,
                max_retries=3,
            )
        )
        repo._update = MagicMock(return_value=True)

        result = repo.lifecycle.complete_replay(
            id=42,
            success=False,
            note="Failed again",
            error_details={"reason": "timeout"},
        )

        assert result is True
        call_kwargs = repo._update.call_args[1]
        assert call_kwargs["status"] == FailedOperationStatus.PENDING.value
        assert call_kwargs["metadata"] == {"reason": "timeout"}

    def test_complete_replay_failure_sets_requires_review_when_retries_exhausted(self):
        """complete_replay with failure sets REQUIRES_REVIEW when max_retries reached."""
        repo = _make_repo()
        repo.get_by_id = MagicMock(
            return_value=_make_failed_op_data(
                id=42,
                retry_count=3,
                max_retries=3,
            )
        )
        repo._update = MagicMock(return_value=True)

        repo.lifecycle.complete_replay(id=42, success=False)

        call_kwargs = repo._update.call_args[1]
        assert call_kwargs["status"] == FailedOperationStatus.REQUIRES_REVIEW.value

    def test_release_stale_replaying_releases_old_entries(self):
        """release_stale_replaying resets stale replaying entries to PENDING."""
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        repo = _make_repo()
        stale_entry = _make_failed_op_data(
            id=1,
            status="replaying",
            updated_at=now - timedelta(minutes=60),
        )
        fresh_entry = _make_failed_op_data(
            id=2,
            status="replaying",
            updated_at=now - timedelta(minutes=10),
        )
        repo.query.by_status = MagicMock(return_value=[stale_entry, fresh_entry])
        repo._update = MagicMock(return_value=True)

        with patch(
            "baldur.adapters.redis.dlq_lifecycle.utc_now",
            return_value=now,
        ):
            released = repo.lifecycle.release_stale_replaying(older_than_minutes=30)

        assert released == 1
        call_kwargs = repo._update.call_args[1]
        assert call_kwargs["entry_id"] == 1
        assert call_kwargs["status"] == FailedOperationStatus.PENDING.value

    def test_release_stale_replaying_skips_entries_without_updated_at(self):
        """release_stale_replaying skips entries with no updated_at."""
        repo = _make_repo()
        entry_no_update = _make_failed_op_data(
            id=1,
            status="replaying",
            updated_at=None,
        )
        repo.query.by_status = MagicMock(return_value=[entry_no_update])
        repo._update = MagicMock()

        released = repo.lifecycle.release_stale_replaying(older_than_minutes=30)

        assert released == 0
        repo._update.assert_not_called()

    def test_release_stale_replaying_counts_only_successful_writes(self):
        """release_stale_replaying counts only entries _update accepted.

                Scenario plan §328 row 1.9: when several stale entries exist but the
                Redis write fails for some, the returned count must reflect actual
                state changes — not attempts. The previous implementation
                incremented ``released`` unconditionally, so a partial Redis outage
                could report the released count as larger than what truly entered
        PENDING. Mirrors ``bulk_update_status``'s accounting pattern.
        """
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        repo = _make_repo()
        stale_entries = [
            _make_failed_op_data(
                id=i,
                status="replaying",
                updated_at=now - timedelta(minutes=60),
            )
            for i in (1, 2, 3)
        ]
        repo.query.by_status = MagicMock(return_value=stale_entries)
        # First and third writes succeed; second one fails (e.g. Redis stutter
        # mid-loop). Expected released count = 2, not 3.
        repo._update = MagicMock(side_effect=[True, False, True])

        with patch(
            "baldur.adapters.redis.dlq_lifecycle.utc_now",
            return_value=now,
        ):
            released = repo.lifecycle.release_stale_replaying(older_than_minutes=30)

        assert released == 2
        # All three writes were attempted (the loop visits every stale entry)
        # — only the successful ones increment the counter.
        assert repo._update.call_count == 3

    def test_bulk_update_status_counts_successful_updates(self):
        """bulk_update_status returns count of successful update_status calls."""
        repo = _make_repo()
        repo.update_status = MagicMock(side_effect=[True, False, True])

        result = repo.lifecycle.bulk_update_status(
            [1, 2, 3],
            FailedOperationStatus.RESOLVED.value,
        )

        assert result == 2
        assert repo.update_status.call_count == 3

    def test_bulk_update_status_passes_status_to_each_call(self):
        """bulk_update_status calls update_status with correct id and status."""
        repo = _make_repo()
        repo.update_status = MagicMock(return_value=True)

        repo.lifecycle.bulk_update_status(
            [10, 20],
            FailedOperationStatus.ARCHIVED.value,
        )

        repo.update_status.assert_has_calls(
            [
                call(10, FailedOperationStatus.ARCHIVED.value),
                call(20, FailedOperationStatus.ARCHIVED.value),
            ]
        )

    def test_bulk_update_status_empty_ids_returns_zero(self):
        """bulk_update_status with empty ids list returns 0."""
        repo = _make_repo()
        repo.update_status = MagicMock()

        result = repo.lifecycle.bulk_update_status(
            [],
            FailedOperationStatus.RESOLVED.value,
        )

        assert result == 0
        repo.update_status.assert_not_called()


# =============================================================================
# C. RedisDLQMaintenance Behavior Tests
# =============================================================================


class TestRedisDLQMaintenanceBehavior:
    """RedisDLQMaintenance archiving, purging, and cleanup behavior."""

    def test_archive_old_resolved_archives_entries_older_than_cutoff(self):
        """archive_old_resolved sets ARCHIVED status for old resolved entries."""
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        repo = _make_repo()
        old_entry = _make_failed_op_data(
            id=1,
            status=FailedOperationStatus.RESOLVED.value,
            resolved_at=now - timedelta(days=45),
        )
        recent_entry = _make_failed_op_data(
            id=2,
            status=FailedOperationStatus.RESOLVED.value,
            resolved_at=now - timedelta(days=10),
        )
        repo.query.by_status = MagicMock(return_value=[old_entry, recent_entry])
        repo._update = MagicMock(return_value=True)

        with patch(
            "baldur.adapters.redis.dlq_maintenance.utc_now",
            return_value=now,
        ):
            archived = repo.maintenance.archive_old_resolved(older_than_days=30)

        assert archived == 1
        call_kwargs = repo._update.call_args[1]
        assert call_kwargs["entry_id"] == 1
        assert call_kwargs["status"] == FailedOperationStatus.ARCHIVED.value

    def test_archive_old_resolved_skips_entries_without_resolved_at(self):
        """archive_old_resolved skips entries with no resolved_at timestamp."""
        repo = _make_repo()
        entry_no_resolved = _make_failed_op_data(
            id=1,
            status=FailedOperationStatus.RESOLVED.value,
            resolved_at=None,
        )
        repo.query.by_status = MagicMock(return_value=[entry_no_resolved])
        repo._update = MagicMock()

        archived = repo.maintenance.archive_old_resolved(older_than_days=30)

        assert archived == 0
        repo._update.assert_not_called()

    def test_purge_archived_raises_value_error_when_both_ids_and_older_than_days(self):
        """purge_archived raises ValueError when both ids and older_than_days given."""
        repo = _make_repo()

        with pytest.raises(ValueError, match="Specify either ids or older_than_days"):
            repo.maintenance.purge_archived(ids=[1, 2], older_than_days=30)

    def test_purge_archived_deletes_only_archived_entries_by_ids(self):
        """purge_archived with ids deletes only entries with ARCHIVED status."""
        repo = _make_repo()
        archived_entry = _make_failed_op_data(
            id=1,
            status=FailedOperationStatus.ARCHIVED.value,
        )
        pending_entry = _make_failed_op_data(
            id=2,
            status=FailedOperationStatus.PENDING.value,
        )
        repo.get_by_id = MagicMock(side_effect=[archived_entry, pending_entry])
        repo.delete = MagicMock(return_value=True)

        purged = repo.maintenance.purge_archived(ids=[1, 2])

        assert purged == 1
        repo.delete.assert_called_once_with(1)

    def test_purge_archived_by_older_than_days_deletes_old_archived(self):
        """purge_archived with older_than_days deletes old ARCHIVED entries."""
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        repo = _make_repo()
        old_archived = _make_failed_op_data(
            id=1,
            status=FailedOperationStatus.ARCHIVED.value,
            resolved_at=now - timedelta(days=100),
        )
        recent_archived = _make_failed_op_data(
            id=2,
            status=FailedOperationStatus.ARCHIVED.value,
            resolved_at=now - timedelta(days=10),
        )
        repo.query.by_status = MagicMock(return_value=[old_archived, recent_archived])
        repo.delete = MagicMock(return_value=True)

        with patch(
            "baldur.adapters.redis.dlq_maintenance.utc_now",
            return_value=now,
        ):
            purged = repo.maintenance.purge_archived(older_than_days=30)

        assert purged == 1
        repo.delete.assert_called_once_with(1)

    def test_purge_archived_returns_zero_when_no_args(self):
        """purge_archived returns 0 when neither ids nor older_than_days given."""
        repo = _make_repo()

        purged = repo.maintenance.purge_archived()

        assert purged == 0

    def test_purge_archived_older_than_days_zero_purges_all_archived(self):
        """older_than_days=0 is a real filter ("older than 0 days" = all archived
        resolved before now), not a falsy no-op. Contract parity with memory/SQL."""
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        repo = _make_repo()
        a = _make_failed_op_data(
            id=1,
            status=FailedOperationStatus.ARCHIVED.value,
            resolved_at=now - timedelta(days=1),
        )
        b = _make_failed_op_data(
            id=2,
            status=FailedOperationStatus.ARCHIVED.value,
            resolved_at=now - timedelta(seconds=5),
        )
        repo.query.by_status = MagicMock(return_value=[a, b])
        repo.delete = MagicMock(return_value=True)

        with patch(
            "baldur.adapters.redis.dlq_maintenance.utc_now",
            return_value=now,
        ):
            purged = repo.maintenance.purge_archived(older_than_days=0)

        assert purged == 2
        assert repo.delete.call_count == 2

    def test_count_all_delegates_to_zcard_on_pending_key(self):
        """count_all delegates to backend.zcard on PENDING_KEY."""
        backend = MagicMock()
        backend.zcard.return_value = 100
        repo = _make_repo(backend)

        result = repo.maintenance.count_all()

        backend.zcard.assert_called_once_with("dlq:pending")
        assert result == 100

    def test_count_by_domain_delegates_to_zcard_on_domain_key(self):
        """count_by_domain delegates to backend.zcard on domain key."""
        backend = MagicMock()
        backend.zcard.return_value = 25
        repo = _make_repo(backend)

        result = repo.maintenance.count_by_domain("payment")

        backend.zcard.assert_called_once_with("dlq:by_domain:payment")
        assert result == 25

    def test_get_oldest_ids_uses_pending_key_without_domain(self):
        """get_oldest_ids uses PENDING_KEY when no domain is provided."""
        backend = MagicMock()
        backend.zrange.return_value = [b"1", b"2"]
        repo = _make_repo(backend)

        result = repo.maintenance.get_oldest_ids(2)

        backend.zrange.assert_called_once_with("dlq:pending", 0, 1)
        assert result == ["1", "2"]

    def test_get_oldest_ids_uses_domain_key_with_domain(self):
        """get_oldest_ids uses domain key when domain is provided."""
        backend = MagicMock()
        backend.zrange.return_value = [b"5"]
        repo = _make_repo(backend)

        result = repo.maintenance.get_oldest_ids(1, domain="payment")

        backend.zrange.assert_called_once_with("dlq:by_domain:payment", 0, 0)
        assert result == ["5"]

    def test_evict_oldest_deletes_oldest_entries(self):
        """evict_oldest calls delete for each oldest entry."""
        backend = MagicMock()
        backend.zrange.return_value = [b"1", b"2", b"3"]
        repo = _make_repo(backend)
        repo.delete = MagicMock(return_value=True)

        result = repo.maintenance.evict_oldest(3)

        assert result == 3
        repo.delete.assert_has_calls([call("1"), call("2"), call("3")])

    def test_evict_oldest_counts_only_successful_deletes(self):
        """evict_oldest returns count of successful deletes only."""
        backend = MagicMock()
        backend.zrange.return_value = [b"1", b"2", b"3"]
        repo = _make_repo(backend)
        repo.delete = MagicMock(side_effect=[True, False, True])

        result = repo.maintenance.evict_oldest(3)

        assert result == 2

    def test_get_cleanup_stats_includes_status_breakdown_and_age_counts(self):
        """get_cleanup_stats returns status breakdown and age-based counts."""
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        repo = _make_repo()

        repo.query.get_statistics = MagicMock(
            return_value={
                "total": 20,
                "pending": 10,
                "resolved": 5,
                "requires_review": 3,
                "rejected": 1,
                "archived": 1,
            }
        )

        # 542 D5: by_status is now built from count_by_status (one ZCARD per
        # indexed status) rather than the prior hardcoded 5-key dict pulled
        # from get_statistics. Mock the per-status count source accordingly.
        status_counts = {
            FailedOperationStatus.PENDING.value: 10,
            FailedOperationStatus.RESOLVED.value: 5,
            FailedOperationStatus.REQUIRES_REVIEW.value: 3,
            FailedOperationStatus.REJECTED.value: 1,
            FailedOperationStatus.ARCHIVED.value: 1,
        }
        repo.query.count_by_status = MagicMock(
            side_effect=lambda s: status_counts.get(s, 0)
        )

        resolved_entries = [
            _make_failed_op_data(
                id=1,
                status=FailedOperationStatus.RESOLVED.value,
                resolved_at=now - timedelta(days=45),
            ),
            _make_failed_op_data(
                id=2,
                status=FailedOperationStatus.RESOLVED.value,
                resolved_at=now - timedelta(days=10),
            ),
        ]
        archived_entries = [
            _make_failed_op_data(
                id=3,
                status=FailedOperationStatus.ARCHIVED.value,
                resolved_at=now - timedelta(days=100),
            ),
        ]

        def by_status_side_effect(status, limit=10000):
            if status == FailedOperationStatus.RESOLVED.value:
                return resolved_entries
            if status == FailedOperationStatus.ARCHIVED.value:
                return archived_entries
            return []

        repo.query.by_status = MagicMock(side_effect=by_status_side_effect)

        with patch(
            "baldur.adapters.redis.dlq_maintenance.utc_now",
            return_value=now,
        ):
            stats = repo.maintenance.get_cleanup_stats()

        assert stats["total"] == 20
        assert stats["by_status"]["pending"] == 10
        assert stats["by_status"]["resolved"] == 5
        assert stats["resolved_older_than_30_days"] == 1
        assert stats["archived_older_than_90_days"] == 1


# =============================================================================
# C-2. RedisDLQMaintenance counter integrity (a write must land to be counted)
# =============================================================================


class TestDLQMaintenanceCounterBehavior:
    """Archive/purge counters reflect writes that landed, not writes attempted.

    Both underlying primitives report a no-op rather than raising: ``_update``
    returns False when the blob is already gone (a concurrent purge, an
    eviction, or TTL expiry between the snapshot read and the write), and
    ``delete`` is idempotent and returns False for an already-absent entry.
    Counting those inflates the archived/purged statistics that reach the
    audit trail and the daily digest — a lane that moved nothing still
    reports work.

    The sibling ``evict_oldest`` and the lifecycle helpers already count this
    way; these two sites were the outliers.
    """

    @staticmethod
    def _resolved_entry(now, id=1):
        return _make_failed_op_data(
            id=id,
            status=FailedOperationStatus.RESOLVED.value,
            resolved_at=now - timedelta(days=45),
        )

    @staticmethod
    def _archived_entry(now, id=1):
        return _make_failed_op_data(
            id=id,
            status=FailedOperationStatus.ARCHIVED.value,
            resolved_at=now - timedelta(days=100),
        )

    @pytest.mark.parametrize(
        ("write_landed", "expected"),
        [
            pytest.param(True, 1, id="write_landed_counts"),
            pytest.param(False, 0, id="no_op_write_not_counted"),
        ],
    )
    def test_archive_counts_only_updates_that_landed(self, write_landed, expected):
        """An `_update` reporting a no-op yields no archived count."""
        # Given: one eligible entry whose write may or may not land
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        repo = _make_repo()
        repo.query.by_status = MagicMock(
            spec=repo.query.by_status, return_value=[self._resolved_entry(now)]
        )
        repo._update = MagicMock(spec=repo._update, return_value=write_landed)

        # When
        with patch(
            "baldur.adapters.redis.dlq_maintenance.utc_now",
            return_value=now,
        ):
            archived = repo.maintenance.archive_old_resolved(older_than_days=30)

        # Then
        assert archived == expected
        repo._update.assert_called_once()

    @pytest.mark.parametrize(
        ("write_landed", "expected"),
        [
            pytest.param(True, 1, id="delete_landed_counts"),
            pytest.param(False, 0, id="no_op_delete_not_counted"),
        ],
    )
    def test_purge_by_ids_counts_only_deletes_that_landed(self, write_landed, expected):
        """A `delete` reporting an already-absent entry yields no purged count."""
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        repo = _make_repo()
        repo.get_by_id = MagicMock(
            spec=repo.get_by_id, return_value=self._archived_entry(now)
        )
        repo.delete = MagicMock(spec=repo.delete, return_value=write_landed)

        purged = repo.maintenance.purge_archived(ids=[1])

        assert purged == expected
        repo.delete.assert_called_once_with(1)

    @pytest.mark.parametrize(
        ("write_landed", "expected"),
        [
            pytest.param(True, 1, id="delete_landed_counts"),
            pytest.param(False, 0, id="no_op_delete_not_counted"),
        ],
    )
    def test_purge_by_age_counts_only_deletes_that_landed(self, write_landed, expected):
        """The age-filtered purge lane gates on the same return."""
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        repo = _make_repo()
        repo.query.by_status = MagicMock(
            spec=repo.query.by_status, return_value=[self._archived_entry(now)]
        )
        repo.delete = MagicMock(spec=repo.delete, return_value=write_landed)

        with patch(
            "baldur.adapters.redis.dlq_maintenance.utc_now",
            return_value=now,
        ):
            purged = repo.maintenance.purge_archived(older_than_days=30)

        assert purged == expected
        repo.delete.assert_called_once_with(1)

    def test_archive_counts_the_landed_subset_of_a_mixed_batch(self):
        """A partially-landing batch counts exactly the entries that moved.

        Pins the count instead of a "greater than zero" check: a gate applied
        to the loop rather than to the counter would still return a non-zero
        number here while over-reporting.
        """
        # Given: three eligible entries, the middle write losing its blob
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        repo = _make_repo()
        entries = [self._resolved_entry(now, id=i) for i in (1, 2, 3)]
        repo.query.by_status = MagicMock(
            spec=repo.query.by_status, return_value=entries
        )
        repo._update = MagicMock(spec=repo._update, side_effect=[True, False, True])

        # When
        with patch(
            "baldur.adapters.redis.dlq_maintenance.utc_now",
            return_value=now,
        ):
            archived = repo.maintenance.archive_old_resolved(older_than_days=30)

        # Then
        assert archived == 2
        assert repo._update.call_count == 3

    def test_purge_by_ids_skips_non_archived_without_attempting_a_delete(self):
        """The status filter still runs ahead of the return gate.

        Keeps the two skip reasons distinguishable: a non-archived entry is
        never deleted at all, whereas an archived one is attempted and only
        then gated on the result.
        """
        repo = _make_repo()
        repo.get_by_id = MagicMock(
            spec=repo.get_by_id,
            return_value=_make_failed_op_data(
                id=1, status=FailedOperationStatus.PENDING.value
            ),
        )
        repo.delete = MagicMock(spec=repo.delete, return_value=True)

        purged = repo.maintenance.purge_archived(ids=[1])

        assert purged == 0
        repo.delete.assert_not_called()


# =============================================================================
# D. RedisDLQCompression Behavior Tests
# =============================================================================


class TestRedisDLQCompressionBehavior:
    """RedisDLQCompression compressed entry CRUD behavior."""

    def test_store_compressed_entry_writes_blob_and_sorted_sets(self):
        """store_compressed_entry writes the entry blob + the four index ZSETs
        (index, by_domain, per-status, composite) as one batch_write_ops call."""
        backend = MagicMock()
        repo = _make_repo(backend)

        entry = _make_compressed_entry(
            id="compressed:payment:timeout:E001:1700000000",
            domain="payment",
        )

        result = repo.compression.store_compressed_entry(entry)

        assert result is True
        backend.batch_write_ops.assert_called_once()
        ops = backend.batch_write_ops.call_args[0][0]
        assert len(ops) == 5

        # The entry blob is a single STRING/blob (bytes) under the compressed key.
        op_name, key, blob = ops[0]
        assert op_name == "set_blob"
        assert key == "dlq:compressed:compressed:payment:timeout:E001:1700000000"
        assert isinstance(blob, bytes)
        decoded = json.loads(blob)
        assert decoded["domain"] == "payment"
        assert decoded["failure_type"] == "timeout"
        assert decoded["error_code"] == "E001"
        assert decoded["status"] == "active"

        # index, by_domain, per-status and composite writes ride the same batch.
        assert ops[1][0] == "zadd"
        assert ops[2][0] == "zadd"
        assert ops[3][0] == "zadd"
        assert ops[3][1] == "dlq:compressed:status:active"
        assert entry.id in ops[3][2]
        assert ops[4][0] == "zadd"
        assert ops[4][1] == "dlq:compressed:status_domain:active:payment"
        assert entry.id in ops[4][2]

    def test_store_compressed_entry_adds_to_index_sorted_set(self):
        """store_compressed_entry adds entry to global compressed index."""
        backend = MagicMock()
        repo = _make_repo(backend)

        entry = _make_compressed_entry(id="c1")

        repo.compression.store_compressed_entry(entry)

        ops = backend.batch_write_ops.call_args[0][0]
        index_op = ops[1]
        assert index_op[0] == "zadd"
        assert index_op[1] == "dlq:compressed:index"
        assert "c1" in index_op[2]

    def test_store_compressed_entry_adds_to_domain_sorted_set(self):
        """store_compressed_entry adds entry to domain-specific sorted set."""
        backend = MagicMock()
        repo = _make_repo(backend)

        entry = _make_compressed_entry(id="c1", domain="payment")

        repo.compression.store_compressed_entry(entry)

        ops = backend.batch_write_ops.call_args[0][0]
        domain_op = ops[2]
        assert domain_op[0] == "zadd"
        assert domain_op[1] == "dlq:compressed:by_domain:payment"
        assert "c1" in domain_op[2]

    def test_get_compressed_entries_queries_global_index_without_domain(self):
        """get_compressed_entries uses global index key when no domain."""
        backend = MagicMock()
        backend.zrevrange.return_value = []
        repo = _make_repo(backend)

        repo.compression.get_compressed_entries()

        backend.zrevrange.assert_called_once_with(
            "dlq:compressed:index",
            0,
            99,
        )

    def test_get_compressed_entries_queries_domain_key_with_domain(self):
        """get_compressed_entries uses domain key when domain is provided."""
        backend = MagicMock()
        backend.zrevrange.return_value = []
        repo = _make_repo(backend)

        repo.compression.get_compressed_entries(domain="payment")

        backend.zrevrange.assert_called_once_with(
            "dlq:compressed:by_domain:payment",
            0,
            99,
        )

    def test_get_compressed_entries_filters_by_status(self):
        """get_compressed_entries filters results by status when provided."""
        backend = MagicMock()
        backend.zrevrange.return_value = ["c1", "c2"]
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        backend.get_blob.side_effect = [
            _blob(
                {
                    "id": "c1",
                    "domain": "payment",
                    "failure_type": "timeout",
                    "error_code": "E001",
                    "count": "5",
                    "first_seen": now.isoformat(),
                    "last_seen": now.isoformat(),
                    "sample_error_message": "err",
                    "sample_context": "{}",
                    "status": "active",
                    "compressed_at": now.isoformat(),
                }
            ),
            _blob(
                {
                    "id": "c2",
                    "domain": "payment",
                    "failure_type": "timeout",
                    "error_code": "E002",
                    "count": "3",
                    "first_seen": now.isoformat(),
                    "last_seen": now.isoformat(),
                    "sample_error_message": "err",
                    "sample_context": "{}",
                    "status": "stale",
                    "compressed_at": now.isoformat(),
                }
            ),
        ]
        repo = _make_repo(backend)

        results = repo.compression.get_compressed_entries(status="active")

        assert len(results) == 1
        assert results[0].id == "c1"

    def test_get_compressed_entries_skips_missing_blob_data(self):
        """get_compressed_entries skips members whose blob is absent (None)."""
        backend = MagicMock()
        backend.zrevrange.return_value = ["c1", "c2"]
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        backend.get_blob.side_effect = [
            None,  # Missing blob
            _blob(
                {
                    "id": "c2",
                    "domain": "payment",
                    "failure_type": "timeout",
                    "error_code": "E002",
                    "count": "3",
                    "first_seen": now.isoformat(),
                    "last_seen": now.isoformat(),
                    "sample_error_message": "err",
                    "sample_context": "{}",
                    "status": "active",
                    "compressed_at": now.isoformat(),
                }
            ),
        ]
        repo = _make_repo(backend)

        results = repo.compression.get_compressed_entries()

        assert len(results) == 1
        assert results[0].id == "c2"

    def test_get_compressed_summary_aggregates_statistics(self):
        """get_compressed_summary returns total, item counts, and status breakdown."""
        backend = MagicMock()
        backend.zcard.return_value = 3
        # Below the cap (default 5000): the whole index is walked newest-first
        # and the blobs are fetched via one chunked get_blobs call.
        backend.zrevrange.return_value = ["c1", "c2", "c3"]
        backend.get_blobs.return_value = [
            _blob({"status": "active", "count": "10"}),
            _blob({"status": "active", "count": "5"}),
            _blob({"status": "stale", "count": "3"}),
        ]
        repo = _make_repo(backend)

        summary = repo.compression.get_compressed_summary()

        assert summary["total_summaries"] == 3
        assert summary["total_compressed_items"] == 18
        assert summary["by_status"]["active"] == 2
        assert summary["by_status"]["stale"] == 1
        assert summary["by_status"]["archived"] == 0
        assert "summary_truncated" not in summary

    def test_get_compressed_summary_empty_index(self):
        """get_compressed_summary returns zeros when no compressed entries exist."""
        backend = MagicMock()
        backend.zcard.return_value = 0
        backend.zrevrange.return_value = []
        repo = _make_repo(backend)

        summary = repo.compression.get_compressed_summary()

        assert summary["total_summaries"] == 0
        assert summary["total_compressed_items"] == 0

    def test_update_compressed_status_sets_stale_at_for_stale_status(self):
        """update_compressed_status sets stale_at when transitioning to stale.

        A real status change (active->stale) relocates the per-status index
        membership, so the blob rewrite rides a single batch_write_ops call
        (blob-first) rather than a standalone set_blob.
        """
        backend = MagicMock()
        backend.get_blob.return_value = _blob(
            {"id": "c1", "status": "active", "count": "5"}
        )
        repo = _make_repo(backend)

        result = repo.compression.update_compressed_status("c1", "stale")

        assert result is True
        backend.batch_write_ops.assert_called_once()
        ops = backend.batch_write_ops.call_args[0][0]
        assert ops[0][0] == "set_blob"
        written = json.loads(ops[0][2])
        assert written["status"] == "stale"
        assert "stale_at" in written

    def test_update_compressed_status_sets_archived_at_for_archived_status(self):
        """update_compressed_status sets archived_at when transitioning to archived."""
        backend = MagicMock()
        backend.get_blob.return_value = _blob(
            {"id": "c1", "status": "active", "count": "5"}
        )
        repo = _make_repo(backend)

        result = repo.compression.update_compressed_status("c1", "archived")

        assert result is True
        ops = backend.batch_write_ops.call_args[0][0]
        assert ops[0][0] == "set_blob"
        written = json.loads(ops[0][2])
        assert written["status"] == "archived"
        assert "archived_at" in written

    def test_update_compressed_status_active_does_not_set_timestamp_fields(self):
        """update_compressed_status for 'active' does not set stale_at or archived_at."""
        backend = MagicMock()
        backend.get_blob.return_value = _blob(
            {"id": "c1", "status": "active", "count": "5"}
        )
        repo = _make_repo(backend)

        repo.compression.update_compressed_status("c1", "active")

        written = json.loads(backend.set_blob.call_args[0][1])
        assert written["status"] == "active"
        assert "stale_at" not in written
        assert "archived_at" not in written

    def test_update_compressed_status_returns_false_for_nonexistent_entry(self):
        """update_compressed_status returns False when entry blob is absent."""
        backend = MagicMock()
        backend.get_blob.return_value = None
        repo = _make_repo(backend)

        result = repo.compression.update_compressed_status("nonexistent", "stale")

        assert result is False
        backend.set_blob.assert_not_called()

    def test_update_compressed_status_checks_correct_key(self):
        """update_compressed_status reads the correct compressed blob key."""
        backend = MagicMock()
        backend.get_blob.return_value = _blob(
            {"id": "c1", "status": "active", "count": "5"}
        )
        repo = _make_repo(backend)

        repo.compression.update_compressed_status("c1", "stale")

        backend.get_blob.assert_called_once_with("dlq:compressed:c1")


# =============================================================================
# E. _deserialize_compressed_entry Behavior Tests
# =============================================================================


class TestDeserializeCompressedEntryBehavior:
    """_deserialize_compressed_entry deserialization from a decoded blob dict."""

    def test_deserialize_creates_entry_from_complete_data(self):
        """_deserialize_compressed_entry creates DLQCompressedEntry from dict."""
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        stale_at = now - timedelta(hours=1)
        archived_at = now
        data = {
            "id": "c1",
            "domain": "payment",
            "failure_type": "timeout",
            "error_code": "E001",
            "count": "10",
            "first_seen": (now - timedelta(days=7)).isoformat(),
            "last_seen": now.isoformat(),
            "sample_error_message": "Connection timeout",
            "sample_context": '{"endpoint": "/api/pay"}',
            "status": "archived",
            "compressed_at": now.isoformat(),
            "stale_at": stale_at.isoformat(),
            "archived_at": archived_at.isoformat(),
        }

        entry = _deserialize_compressed_entry(data)

        assert isinstance(entry, DLQCompressedEntry)
        assert entry.id == "c1"
        assert entry.domain == "payment"
        assert entry.failure_type == "timeout"
        assert entry.error_code == "E001"
        assert entry.count == 10
        assert entry.sample_error_message == "Connection timeout"
        assert entry.sample_context == {"endpoint": "/api/pay"}
        assert entry.status == "archived"
        assert entry.stale_at == stale_at
        assert entry.archived_at == archived_at

    def test_deserialize_handles_missing_stale_at(self):
        """_deserialize_compressed_entry sets stale_at=None when field missing."""
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        data = {
            "id": "c1",
            "domain": "payment",
            "failure_type": "timeout",
            "error_code": "E001",
            "count": "5",
            "first_seen": now.isoformat(),
            "last_seen": now.isoformat(),
            "sample_error_message": "err",
            "sample_context": "{}",
            "status": "active",
            "compressed_at": now.isoformat(),
        }

        entry = _deserialize_compressed_entry(data)

        assert entry.stale_at is None
        assert entry.archived_at is None

    def test_deserialize_handles_missing_archived_at(self):
        """_deserialize_compressed_entry sets archived_at=None when field missing."""
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        stale_at = now - timedelta(hours=1)
        data = {
            "id": "c1",
            "domain": "payment",
            "failure_type": "timeout",
            "error_code": "E001",
            "count": "5",
            "first_seen": now.isoformat(),
            "last_seen": now.isoformat(),
            "sample_error_message": "err",
            "sample_context": "{}",
            "status": "stale",
            "compressed_at": now.isoformat(),
            "stale_at": stale_at.isoformat(),
        }

        entry = _deserialize_compressed_entry(data)

        assert entry.stale_at == stale_at
        assert entry.archived_at is None

    def test_deserialize_defaults_status_to_active(self):
        """_deserialize_compressed_entry defaults status to 'active' when missing."""
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        data = {
            "id": "c1",
            "domain": "payment",
            "failure_type": "timeout",
            "error_code": "E001",
            "count": "5",
            "first_seen": now.isoformat(),
            "last_seen": now.isoformat(),
            "compressed_at": now.isoformat(),
        }

        entry = _deserialize_compressed_entry(data)

        assert entry.status == "active"

    def test_deserialize_defaults_sample_error_message_to_empty(self):
        """_deserialize_compressed_entry defaults sample_error_message to ''."""
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        data = {
            "id": "c1",
            "domain": "payment",
            "failure_type": "timeout",
            "error_code": "E001",
            "count": "5",
            "first_seen": now.isoformat(),
            "last_seen": now.isoformat(),
            "compressed_at": now.isoformat(),
        }

        entry = _deserialize_compressed_entry(data)

        assert entry.sample_error_message == ""

    def test_deserialize_defaults_sample_context_to_empty_dict(self):
        """_deserialize_compressed_entry defaults sample_context to {} when missing."""
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        data = {
            "id": "c1",
            "domain": "payment",
            "failure_type": "timeout",
            "error_code": "E001",
            "count": "5",
            "first_seen": now.isoformat(),
            "last_seen": now.isoformat(),
            "compressed_at": now.isoformat(),
        }

        entry = _deserialize_compressed_entry(data)

        assert entry.sample_context == {}

    def test_deserialize_parses_count_as_integer(self):
        """_deserialize_compressed_entry parses string count to int."""
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        data = {
            "id": "c1",
            "domain": "payment",
            "failure_type": "timeout",
            "error_code": "E001",
            "count": "42",
            "first_seen": now.isoformat(),
            "last_seen": now.isoformat(),
            "compressed_at": now.isoformat(),
        }

        entry = _deserialize_compressed_entry(data)

        assert entry.count == 42
        assert isinstance(entry.count, int)

    def test_deserialize_parses_datetime_fields(self):
        """_deserialize_compressed_entry parses ISO datetime strings."""
        first = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        last = datetime(2026, 3, 15, 0, 0, 0, tzinfo=UTC)
        compressed = datetime(2026, 3, 16, 0, 0, 0, tzinfo=UTC)
        data = {
            "id": "c1",
            "domain": "payment",
            "failure_type": "timeout",
            "error_code": "E001",
            "count": "5",
            "first_seen": first.isoformat(),
            "last_seen": last.isoformat(),
            "compressed_at": compressed.isoformat(),
        }

        entry = _deserialize_compressed_entry(data)

        assert entry.first_seen == first
        assert entry.last_seen == last
        assert entry.compressed_at == compressed


# =============================================================================
# F. get_compressed_entry — by-id read + reserved-name guard (721 D2)
# =============================================================================


def _entry_blob(entry_id="c1", *, domain="payment", status="active", count=5) -> bytes:
    """A complete compressed-entry blob the by-id read can deserialize."""
    now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
    return _blob(
        {
            "id": entry_id,
            "domain": domain,
            "failure_type": "timeout",
            "error_code": "E001",
            "count": str(count),
            "first_seen": now.isoformat(),
            "last_seen": now.isoformat(),
            "sample_error_message": "err",
            "sample_context": "{}",
            "status": status,
            "compressed_at": now.isoformat(),
        }
    )


class TestRedisCompressedByIdBehavior:
    """get_compressed_entry: index-free by-id read, guarded against reserved keys."""

    def test_get_compressed_entry_present_returns_deserialized_entry(self):
        """A present id reads its direct blob key and deserializes it."""
        backend = MagicMock()
        backend.get_blob.return_value = _entry_blob("c1", status="stale")
        repo = _make_repo(backend)

        entry = repo.compression.get_compressed_entry("c1")

        assert entry is not None
        assert entry.id == "c1"
        assert entry.status == "stale"
        backend.get_blob.assert_called_once_with("dlq:compressed:c1")

    def test_get_compressed_entry_absent_returns_none(self):
        """A missing blob maps to None (sibling get_by_id precedent)."""
        backend = MagicMock()
        backend.get_blob.return_value = None
        repo = _make_repo(backend)

        assert repo.compression.get_compressed_entry("c1") is None

    def test_get_compressed_entry_is_read_only_and_repeatable(self):
        """The by-id read has no write side effects and repeats identically."""
        backend = MagicMock()
        backend.get_blob.return_value = _entry_blob("c1")
        repo = _make_repo(backend)

        first = repo.compression.get_compressed_entry("c1")
        second = repo.compression.get_compressed_entry("c1")

        assert first.id == second.id == "c1"
        backend.set_blob.assert_not_called()
        backend.batch_write_ops.assert_not_called()

    @pytest.mark.parametrize(
        "reserved_id",
        [
            "index",
            "by_domain:payment",
            "status:active",
            "status_domain:active:payment",
        ],
        ids=["index", "by_domain", "status", "status_domain"],
    )
    def test_reserved_key_name_returns_none_without_reading_backend(self, reserved_id):
        """A reserved structural key name short-circuits to None before get_blob.

        Reading it would hit a sorted set and raise WRONGTYPE, degrading the
        backend; the guard prevents the read entirely (§8.13 proximate cause).
        """
        backend = MagicMock()
        repo = _make_repo(backend)

        assert repo.compression.get_compressed_entry(reserved_id) is None
        backend.get_blob.assert_not_called()


# =============================================================================
# G. update_compressed_status — index relocation + same-status short-circuit
# =============================================================================


class TestRedisCompressedTransition:
    """update_compressed_status relocates per-status/composite index membership."""

    @pytest.mark.parametrize(
        ("old_status", "new_status"),
        [("active", "stale"), ("stale", "archived")],
        ids=["active_to_stale", "stale_to_archived"],
    )
    def test_real_transition_adds_new_and_removes_old_membership(
        self, old_status, new_status
    ):
        """A status change is one blob-first batch: zadd new keys, zrem old keys."""
        backend = MagicMock()
        backend.get_blob.return_value = _blob(
            {
                "id": "c1",
                "status": old_status,
                "domain": "payment",
                "count": "5",
                "compressed_at": datetime(2026, 3, 16, tzinfo=UTC).isoformat(),
            }
        )
        repo = _make_repo(backend)

        assert repo.compression.update_compressed_status("c1", new_status) is True

        ops = backend.batch_write_ops.call_args[0][0]
        # Blob-first (723 D9), then add-before-remove. The score value itself
        # is covered by the _compressed_score helper tests below.
        assert ops[0][0] == "set_blob"
        assert ops[1][0] == "zadd"
        assert ops[1][1] == f"dlq:compressed:status:{new_status}"
        assert "c1" in ops[1][2]
        assert ops[2][0] == "zadd"
        assert ops[2][1] == f"dlq:compressed:status_domain:{new_status}:payment"
        assert ops[3] == ("zrem", f"dlq:compressed:status:{old_status}", ["c1"])
        assert ops[4] == (
            "zrem",
            f"dlq:compressed:status_domain:{old_status}:payment",
            ["c1"],
        )

    def test_transition_never_adds_and_removes_the_same_key(self):
        """No emitted key is both zadd'd and zrem'd (the annihilation guard)."""
        backend = MagicMock()
        backend.get_blob.return_value = _blob(
            {
                "id": "c1",
                "status": "active",
                "domain": "payment",
                "count": "5",
                "compressed_at": datetime(2026, 3, 16, tzinfo=UTC).isoformat(),
            }
        )
        repo = _make_repo(backend)

        repo.compression.update_compressed_status("c1", "stale")

        ops = backend.batch_write_ops.call_args[0][0]
        zadd_keys = {op[1] for op in ops if op[0] == "zadd"}
        zrem_keys = {op[1] for op in ops if op[0] == "zrem"}
        assert zadd_keys.isdisjoint(zrem_keys)

    def test_same_status_transition_short_circuits_to_a_blob_only_write(self):
        """A same-status transition writes only the blob and emits no index ops.

        An add-then-remove on one per-status key/member in a single pipeline
        would annihilate the membership, so the short-circuit is correctness,
        not an optimization.
        """
        backend = MagicMock()
        backend.get_blob.return_value = _blob(
            {"id": "c1", "status": "active", "domain": "payment", "count": "5"}
        )
        repo = _make_repo(backend)

        assert repo.compression.update_compressed_status("c1", "active") is True

        backend.set_blob.assert_called_once()
        backend.batch_write_ops.assert_not_called()

    def test_empty_domain_transition_skips_the_composite_ops(self):
        """With no domain the composite (status, domain) ops are skipped."""
        backend = MagicMock()
        backend.get_blob.return_value = _blob(
            {
                "id": "c1",
                "status": "active",
                "domain": "",
                "count": "5",
                "compressed_at": datetime(2026, 3, 16, tzinfo=UTC).isoformat(),
            }
        )
        repo = _make_repo(backend)

        repo.compression.update_compressed_status("c1", "stale")

        ops = backend.batch_write_ops.call_args[0][0]
        op_shapes = [(op[0], op[1]) for op in ops]
        assert op_shapes == [
            ("set_blob", "dlq:compressed:c1"),
            ("zadd", "dlq:compressed:status:stale"),
            ("zrem", "dlq:compressed:status:active"),
        ]


# =============================================================================
# H. get_compressed_summary — chunked walk + cap rail (721 D4/D7)
# =============================================================================


class TestRedisCompressedSummaryBound:
    """The summary walk collapses round trips via chunked MGET and caps at the rail."""

    def test_summary_fetches_blobs_in_ceil_n_over_chunk_batches(self):
        """The walk issues ceil(n / _SUMMARY_MGET_CHUNK) get_blobs calls, not n."""
        n = _SUMMARY_MGET_CHUNK * 2 + 1
        backend = MagicMock()
        backend.zcard.return_value = n
        backend.zrevrange.return_value = [f"c{i}" for i in range(n)]
        backend.get_blobs.side_effect = lambda keys: [
            _blob({"status": "active", "count": "1"}) for _ in keys
        ]
        repo = _make_repo(backend)

        with patch(
            "baldur.adapters.redis.dlq_compression._summary_scan_cap",
            return_value=100_000,
        ):
            summary = repo.compression.get_compressed_summary()

        assert backend.get_blobs.call_count == math.ceil(n / _SUMMARY_MGET_CHUNK)
        chunk_sizes = [len(c.args[0]) for c in backend.get_blobs.call_args_list]
        assert chunk_sizes == [_SUMMARY_MGET_CHUNK, _SUMMARY_MGET_CHUNK, 1]
        # Exactness is preserved across the chunked walk.
        assert summary["total_summaries"] == n
        assert summary["total_compressed_items"] == n
        assert summary["by_status"]["active"] == n
        assert "summary_truncated" not in summary

    def test_summary_below_cap_is_exact_and_logs_no_warning(self):
        """Below the cap the summary is exact and emits no truncation WARNING."""
        backend = MagicMock()
        backend.zcard.return_value = 2
        backend.zrevrange.return_value = ["c1", "c2"]
        backend.get_blobs.return_value = [
            _blob({"status": "active", "count": "4"}),
            _blob({"status": "stale", "count": "6"}),
        ]
        repo = _make_repo(backend)

        with capture_logs() as logs:
            with patch(
                "baldur.adapters.redis.dlq_compression._summary_scan_cap",
                return_value=10,
            ):
                summary = repo.compression.get_compressed_summary()

        assert "summary_truncated" not in summary
        assert summary["total_compressed_items"] == 10
        assert not [e for e in logs if e["event"] == "dlq.compressed_summary_truncated"]

    def test_summary_at_cap_is_not_truncated(self):
        """total == cap is NOT truncated: the walk still covers the whole index."""
        backend = MagicMock()
        backend.zcard.return_value = 3
        backend.zrevrange.return_value = ["c1", "c2", "c3"]
        backend.get_blobs.side_effect = lambda keys: [
            _blob({"status": "active", "count": "1"}) for _ in keys
        ]
        repo = _make_repo(backend)

        with patch(
            "baldur.adapters.redis.dlq_compression._summary_scan_cap",
            return_value=3,
        ):
            summary = repo.compression.get_compressed_summary()

        assert "summary_truncated" not in summary
        # Not truncated -> the whole index is walked (stop == -1).
        backend.zrevrange.assert_called_once_with(_COMPRESSED_INDEX_KEY, 0, -1)

    def test_summary_above_cap_windows_to_newest_cap_and_warns_once(self):
        """Above the cap the walk windows to the newest ``cap`` and warns once."""
        backend = MagicMock()
        backend.zcard.return_value = 4
        backend.zrevrange.return_value = ["c1", "c2", "c3"]  # newest cap=3
        backend.get_blobs.side_effect = lambda keys: [
            _blob({"status": "active", "count": "1"}) for _ in keys
        ]
        repo = _make_repo(backend)

        with capture_logs() as logs:
            with patch(
                "baldur.adapters.redis.dlq_compression._summary_scan_cap",
                return_value=3,
            ):
                summary = repo.compression.get_compressed_summary()

        assert summary["summary_truncated"] is True
        assert summary["total_summaries"] == 4  # zcard, not the windowed count
        # Windowed newest-first to cap-1 == 2.
        backend.zrevrange.assert_called_once_with(_COMPRESSED_INDEX_KEY, 0, 2)
        warnings = [e for e in logs if e["event"] == "dlq.compressed_summary_truncated"]
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"
        assert warnings[0]["total"] == 4
        assert warnings[0]["cap"] == 3


# =============================================================================
# I. Module helpers — reserved-name guard, cap resolution, score
# =============================================================================


class TestCompressedModuleHelpersBehavior:
    """_is_reserved_compressed_id / _summary_scan_cap / _compressed_score."""

    @pytest.mark.parametrize(
        ("entry_id", "expected"),
        [
            ("index", True),
            ("by_domain:payment", True),
            ("status:active", True),
            ("status_domain:active:payment", True),
            ("compressed:payment:timeout:E001:1700000000", False),
            ("some-opaque-id", False),
        ],
        ids=[
            "index",
            "by_domain",
            "status",
            "status_domain",
            "real_compressed_id",
            "opaque",
        ],
    )
    def test_is_reserved_compressed_id_classifies_structural_names(
        self, entry_id, expected
    ):
        """Structural key names are reserved; real entry ids are not."""
        assert _is_reserved_compressed_id(entry_id) is expected

    def test_summary_scan_cap_reads_the_settings_field(self):
        """_summary_scan_cap resolves the DLQSettings cap field."""
        with patch("baldur.settings.dlq.get_dlq_settings") as mock_get:
            mock_get.return_value.compress_summary_scan_cap = 1234
            assert _summary_scan_cap() == 1234

    def test_summary_scan_cap_falls_back_to_5000_when_settings_unreachable(self):
        """When settings raise, the cap falls back to the field default 5000."""
        with patch(
            "baldur.settings.dlq.get_dlq_settings", side_effect=RuntimeError("boom")
        ):
            assert _summary_scan_cap() == 5000

    def test_compressed_score_uses_the_compressed_at_timestamp(self):
        """The per-status score is the entry's compressed_at epoch."""
        now = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        assert _compressed_score({"compressed_at": now.isoformat()}) == now.timestamp()

    @pytest.mark.parametrize(
        "data",
        [{}, {"compressed_at": "not-a-date"}, {"compressed_at": None}],
        ids=["absent", "unparseable", "none"],
    )
    def test_compressed_score_falls_back_to_a_positive_epoch(self, data):
        """A missing or unparseable timestamp falls back to a positive epoch."""
        score = _compressed_score(data)
        assert isinstance(score, float)
        assert score > 0


class TestDLQCompressedStatusEnumContract:
    """The status set the summary seeds its by_status breakdown from."""

    def test_summary_status_buckets_cover_all_statuses(self):
        """get_compressed_summary seeds a bucket for every DLQCompressedStatus."""
        backend = MagicMock()
        backend.zcard.return_value = 0
        backend.zrevrange.return_value = []
        repo = _make_repo(backend)

        summary = repo.compression.get_compressed_summary()

        for status in DLQCompressedStatus:
            assert status.value in summary["by_status"]
