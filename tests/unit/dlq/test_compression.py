"""
DLQ Compression Unit Tests (351_DLQ_COMPRESSION).

Test targets:
    - baldur_pro.services.dlq.compression.compress_entries
    - baldur_pro.services.dlq.compression.CompressResult
    - baldur.adapters.memory.failed_operation (compression methods)
    - baldur_pro.services.dlq.overflow.run_background_eviction (compress strategy)
    - baldur_pro.services.dlq.base.get_dlq_repository
    - baldur_pro.services.audit.dlq_audit.log_dlq_compress_audit
    - baldur.settings.dlq.DLQSettings (compression fields)
    - baldur.celery_tasks.dlq_tasks (distributed lock, cleanup)

Test Categories:
    A. Contract: CompressResult defaults, DLQCompressedEntry fields, Settings defaults,
       module exports, DLQSettings boundary constraints
    B. Behavior — compress_entries: grouping, timestamps, sample selection, empty input
    C. Behavior — InMemory adapter: compress_and_evict, store/get/update/summary
    D. Behavior — overflow: compress strategy calls compress_and_evict_oldest
    E. Behavior — get_dlq_repository: registry/fallback
    F. Behavior — log_dlq_compress_audit: WAL-first, adapter direct, fail-open
    G. Behavior — Celery tasks: distributed lock, cleanup lifecycle
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.memory.failed_operation import (
    InMemoryFailedOperationRepository,
)
from baldur.interfaces.repositories import (
    DLQCompressedEntry,
    FailedOperationData,
)
from baldur.settings.dlq import DLQSettings
from baldur_pro.services.dlq.compression import CompressResult, compress_entries

# =============================================================================
# Helpers
# =============================================================================


def _make_entry(
    *,
    id: int = 1,
    domain: str = "payment",
    failure_type: str = "timeout",
    error_code: str = "E_TIMEOUT",
    error_message: str = "Connection timed out",
    created_at: datetime | None = None,
    entity_type: str | None = "order",
    entity_id: str | None = "123",
    metadata: dict | None = None,
) -> FailedOperationData:
    """Create a FailedOperationData entry for testing."""
    return FailedOperationData(
        id=id,
        domain=domain,
        failure_type=failure_type,
        error_code=error_code,
        error_message=error_message,
        status="pending",
        created_at=created_at or datetime.now(UTC),
        entity_type=entity_type,
        entity_id=entity_id,
        metadata=metadata or {},
    )


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestCompressResultContract:
    """CompressResult dataclass contract values."""

    def test_default_compressed_count_is_zero(self):
        """Default compressed_count is 0."""
        result = CompressResult()
        assert result.compressed_count == 0

    def test_default_summary_count_is_zero(self):
        """Default summary_count is 0."""
        result = CompressResult()
        assert result.summary_count == 0

    def test_default_entries_is_empty_list(self):
        """Default entries is empty list."""
        result = CompressResult()
        assert result.entries == []


class TestDLQCompressedEntryContract:
    """DLQCompressedEntry dataclass contract fields."""

    def test_default_status_is_active(self):
        """Default status is 'active'."""
        entry = DLQCompressedEntry(
            id="test:1",
            domain="payment",
            failure_type="timeout",
            error_code="E_TIMEOUT",
            count=10,
            first_seen=datetime.now(UTC),
            last_seen=datetime.now(UTC),
            sample_error_message="test",
        )
        assert entry.status == "active"

    def test_default_stale_at_is_none(self):
        """Default stale_at is None."""
        entry = DLQCompressedEntry(
            id="test:1",
            domain="payment",
            failure_type="timeout",
            error_code="E_TIMEOUT",
            count=10,
            first_seen=datetime.now(UTC),
            last_seen=datetime.now(UTC),
            sample_error_message="test",
        )
        assert entry.stale_at is None

    def test_default_archived_at_is_none(self):
        """Default archived_at is None."""
        entry = DLQCompressedEntry(
            id="test:1",
            domain="payment",
            failure_type="timeout",
            error_code="E_TIMEOUT",
            count=10,
            first_seen=datetime.now(UTC),
            last_seen=datetime.now(UTC),
            sample_error_message="test",
        )
        assert entry.archived_at is None

    def test_compressed_at_defaults_to_utc_now(self):
        """compressed_at defaults to a UTC datetime."""
        before = datetime.now(UTC)
        entry = DLQCompressedEntry(
            id="test:1",
            domain="payment",
            failure_type="timeout",
            error_code="E_TIMEOUT",
            count=10,
            first_seen=datetime.now(UTC),
            last_seen=datetime.now(UTC),
            sample_error_message="test",
        )
        after = datetime.now(UTC)
        assert before <= entry.compressed_at <= after


class TestCompressionSettingsContract:
    """DLQ compression settings field contracts."""

    def test_compress_stale_after_days_default(self):
        """Default compress_stale_after_days is 30."""
        assert DLQSettings().compress_stale_after_days == 30

    def test_compress_archive_after_days_default(self):
        """Default compress_archive_after_days is 90."""
        assert DLQSettings().compress_archive_after_days == 90

    def test_compress_stale_after_days_lower_bound(self):
        """compress_stale_after_days rejects below 7."""
        with pytest.raises(Exception):
            DLQSettings(compress_stale_after_days=6)

    def test_compress_stale_after_days_upper_bound(self):
        """compress_stale_after_days rejects above 365."""
        with pytest.raises(Exception):
            DLQSettings(compress_stale_after_days=366)

    def test_compress_archive_after_days_lower_bound(self):
        """compress_archive_after_days rejects below 30."""
        with pytest.raises(Exception):
            DLQSettings(compress_archive_after_days=29)

    def test_compress_archive_after_days_upper_bound(self):
        """compress_archive_after_days rejects above 730."""
        with pytest.raises(Exception):
            DLQSettings(compress_archive_after_days=731)


class TestCompressionModuleExportsContract:
    """Module __all__ exports contract."""

    def test_compression_module_exports(self):
        """compression.py exports compress_entries and CompressResult."""
        from baldur_pro.services.dlq import compression

        assert "compress_entries" in compression.__all__
        assert "CompressResult" in compression.__all__


# =============================================================================
# B. Behavior Tests — compress_entries()
# =============================================================================


class TestCompressEntriesBehavior:
    """compress_entries() grouping and summary behavior."""

    def test_empty_entries_returns_empty_result(self):
        """Empty list returns CompressResult with all zeros."""
        result = compress_entries([])
        assert result.compressed_count == 0
        assert result.summary_count == 0
        assert result.entries == []

    def test_single_group_creates_one_summary(self):
        """Entries with same (domain, failure_type, error_code) produce one summary."""
        entries = [
            _make_entry(
                id=1, domain="payment", failure_type="timeout", error_code="E_TIMEOUT"
            ),
            _make_entry(
                id=2, domain="payment", failure_type="timeout", error_code="E_TIMEOUT"
            ),
            _make_entry(
                id=3, domain="payment", failure_type="timeout", error_code="E_TIMEOUT"
            ),
        ]
        result = compress_entries(entries)
        assert result.summary_count == 1
        assert result.entries[0].count == 3

    def test_multiple_groups_creates_multiple_summaries(self):
        """Different grouping keys produce separate summaries."""
        entries = [
            _make_entry(
                id=1, domain="payment", failure_type="timeout", error_code="E_TIMEOUT"
            ),
            _make_entry(
                id=2,
                domain="auth",
                failure_type="connection_refused",
                error_code="E_CONN",
            ),
        ]
        result = compress_entries(entries)
        assert result.summary_count == 2
        assert result.compressed_count == 2

    def test_compressed_count_equals_input_count(self):
        """compressed_count matches the number of input entries."""
        entries = [_make_entry(id=i) for i in range(5)]
        result = compress_entries(entries)
        assert result.compressed_count == 5

    def test_first_seen_is_earliest_timestamp(self):
        """first_seen is the earliest created_at in the group."""
        now = datetime.now(UTC)
        entries = [
            _make_entry(id=1, created_at=now - timedelta(hours=3)),
            _make_entry(id=2, created_at=now - timedelta(hours=1)),
            _make_entry(id=3, created_at=now),
        ]
        result = compress_entries(entries)
        assert result.entries[0].first_seen == now - timedelta(hours=3)

    def test_last_seen_is_latest_timestamp(self):
        """last_seen is the latest created_at in the group."""
        now = datetime.now(UTC)
        entries = [
            _make_entry(id=1, created_at=now - timedelta(hours=3)),
            _make_entry(id=2, created_at=now - timedelta(hours=1)),
            _make_entry(id=3, created_at=now),
        ]
        result = compress_entries(entries)
        assert result.entries[0].last_seen == now

    def test_sample_uses_most_recent_entry(self):
        """sample_error_message comes from the entry with the latest created_at."""
        now = datetime.now(UTC)
        entries = [
            _make_entry(id=1, created_at=now - timedelta(hours=2), error_message="old"),
            _make_entry(id=2, created_at=now, error_message="newest"),
        ]
        result = compress_entries(entries)
        assert result.entries[0].sample_error_message == "newest"

    def test_sample_context_contains_entity_info(self):
        """sample_context includes entity_type, entity_id, metadata from most recent."""
        now = datetime.now(UTC)
        entries = [
            _make_entry(
                id=1,
                created_at=now,
                entity_type="order",
                entity_id="456",
                metadata={"region": "us-east"},
            ),
        ]
        result = compress_entries(entries)
        ctx = result.entries[0].sample_context
        assert ctx["entity_type"] == "order"
        assert ctx["entity_id"] == "456"
        assert ctx["metadata"] == {"region": "us-east"}

    def test_entry_id_format_contains_grouping_key(self):
        """Summary entry ID contains domain, failure_type, error_code."""
        entries = [
            _make_entry(
                id=1, domain="payment", failure_type="timeout", error_code="E_TIMEOUT"
            ),
        ]
        result = compress_entries(entries)
        entry_id = result.entries[0].id
        assert entry_id.startswith("compressed:payment:timeout:E_TIMEOUT:")

    def test_entries_with_none_created_at_use_now_as_fallback(self):
        """Entries with None created_at fall back to current time."""
        entries = [_make_entry(id=1, created_at=None)]
        result = compress_entries(entries)
        # Should not raise; first_seen/last_seen should be set
        assert result.entries[0].first_seen is not None
        assert result.entries[0].last_seen is not None


# =============================================================================
# C. Behavior Tests — InMemory Adapter Compression
# =============================================================================


class TestInMemoryCompressAndEvictBehavior:
    """InMemory adapter compress_and_evict_oldest behavior."""

    def setup_method(self):
        """Set up fresh repository with entries."""
        self.repo = InMemoryFailedOperationRepository()

    def _populate_entries(
        self, domain="payment", failure_type="timeout", error_code="E_TIMEOUT", count=5
    ):
        """Create entries in the repository."""
        for _ in range(count):
            self.repo.create(
                domain=domain,
                failure_type=failure_type,
                error_code=error_code,
                error_message=f"{failure_type} error",
            )

    def test_compress_stores_in_memory_dict(self):
        """Compressed entries stored in _compressed_storage dict."""
        self._populate_entries(count=3)
        self.repo.compress_and_evict_oldest(3)
        assert len(self.repo._compressed_storage) == 1

    def test_compress_evicts_originals(self):
        """Original entries are deleted after compression."""
        self._populate_entries(count=5)
        evicted = self.repo.compress_and_evict_oldest(5)
        assert evicted == 5
        assert self.repo.count_all() == 0

    def test_compress_returns_evicted_count(self):
        """Return value matches number of deleted originals."""
        self._populate_entries(count=3)
        evicted = self.repo.compress_and_evict_oldest(3)
        assert evicted == 3

    def test_compress_with_no_entries_returns_zero(self):
        """Empty repo returns 0."""
        evicted = self.repo.compress_and_evict_oldest(10)
        assert evicted == 0

    def test_compress_creates_correct_summary_count(self):
        """Compression of entries with same key creates one summary."""
        self._populate_entries(domain="payment", count=3)
        self._populate_entries(
            domain="auth",
            failure_type="connection_refused",
            error_code="E_CONN",
            count=2,
        )
        self.repo.compress_and_evict_oldest(5)
        compressed = self.repo.get_compressed_entries()
        assert len(compressed) == 2

    def test_get_compressed_entries_filters_by_domain(self):
        """domain filter returns only matching entries."""
        self._populate_entries(domain="payment", count=3)
        self._populate_entries(
            domain="auth", failure_type="conn", error_code="E_CONN", count=2
        )
        self.repo.compress_and_evict_oldest(5)

        payment_entries = self.repo.get_compressed_entries(domain="payment")
        assert len(payment_entries) == 1
        assert payment_entries[0].domain == "payment"

    def test_get_compressed_entries_filters_by_status(self):
        """status filter returns only matching entries."""
        self._populate_entries(count=3)
        self.repo.compress_and_evict_oldest(3)

        active = self.repo.get_compressed_entries(status="active")
        assert len(active) == 1
        stale = self.repo.get_compressed_entries(status="stale")
        assert len(stale) == 0

    def test_get_compressed_entries_respects_limit(self):
        """limit parameter caps the returned entries."""
        # Create entries with different domains to get multiple summaries
        for i in range(5):
            self.repo.create(
                domain=f"domain_{i}",
                failure_type="timeout",
                error_code="E_TIMEOUT",
                error_message="err",
            )
        self.repo.compress_and_evict_oldest(5)
        entries = self.repo.get_compressed_entries(limit=2)
        assert len(entries) == 2

    def test_get_compressed_summary_aggregates_counts(self):
        """Summary correctly aggregates item counts and status."""
        self._populate_entries(domain="payment", count=7)
        self._populate_entries(
            domain="auth", failure_type="conn", error_code="E", count=3
        )
        self.repo.compress_and_evict_oldest(10)

        summary = self.repo.get_compressed_summary()
        assert summary["total_summaries"] == 2
        assert summary["total_compressed_items"] == 10
        assert summary["by_status"]["active"] == 2

    def test_update_compressed_status_sets_stale_timestamp(self):
        """Transitioning to stale sets stale_at."""
        self._populate_entries(count=3)
        self.repo.compress_and_evict_oldest(3)
        entry = self.repo.get_compressed_entries()[0]

        result = self.repo.update_compressed_status(entry.id, "stale")
        assert result is True

        updated = self.repo.get_compressed_entries(status="stale")
        assert len(updated) == 1
        assert updated[0].stale_at is not None

    def test_update_compressed_status_sets_archived_timestamp(self):
        """Transitioning to archived sets archived_at."""
        self._populate_entries(count=3)
        self.repo.compress_and_evict_oldest(3)
        entry = self.repo.get_compressed_entries()[0]

        self.repo.update_compressed_status(entry.id, "archived")
        updated = self.repo.get_compressed_entries(status="archived")
        assert len(updated) == 1
        assert updated[0].archived_at is not None

    def test_update_compressed_status_nonexistent_returns_false(self):
        """Updating nonexistent entry returns False."""
        result = self.repo.update_compressed_status("nonexistent", "stale")
        assert result is False

    def test_store_compressed_entry_returns_true(self):
        """store_compressed_entry returns True on success."""
        entry = DLQCompressedEntry(
            id="test:1",
            domain="payment",
            failure_type="timeout",
            error_code="E_TIMEOUT",
            count=5,
            first_seen=datetime.now(UTC),
            last_seen=datetime.now(UTC),
            sample_error_message="test",
        )
        assert self.repo.store_compressed_entry(entry) is True


# =============================================================================
# D. Behavior Tests — Overflow compress strategy
# =============================================================================


class TestOverflowCompressStrategyBehavior:
    """run_background_eviction with compress_oldest strategy."""

    @patch("baldur.settings.dlq.get_dlq_settings")
    @patch("baldur_pro.services.dlq.overflow._get_repository")
    def test_compress_strategy_calls_compress_and_evict(
        self, mock_get_repo, mock_get_settings
    ):
        """compress_oldest strategy calls repository.compress_and_evict_oldest()."""
        from baldur_pro.services.dlq.overflow import run_background_eviction

        mock_repo = MagicMock()
        mock_repo.count_all.return_value = 1_000
        mock_repo.compress_and_evict_oldest.return_value = 300
        mock_get_repo.return_value = mock_repo

        mock_settings = MagicMock()
        mock_settings.max_size = 1_000
        mock_settings.overflow_strategy = "compress_oldest"
        mock_settings.emergency_purge_threshold = 0.8
        mock_settings.overflow_evict_batch_size = 1_000
        mock_get_settings.return_value = mock_settings

        run_background_eviction()

        mock_repo.compress_and_evict_oldest.assert_called()

    @patch("baldur_pro.services.dlq.overflow._evict_overflow_domains", return_value=0)
    @patch("baldur.settings.dlq.get_dlq_settings")
    @patch("baldur_pro.services.dlq.overflow._get_repository")
    def test_drop_strategy_does_not_call_compress(
        self, mock_get_repo, mock_get_settings, _mock_domain_evict
    ):
        """drop_oldest strategy does NOT call compress_and_evict_oldest."""
        from baldur_pro.services.dlq.overflow import run_background_eviction

        mock_repo = MagicMock()
        mock_repo.count_all.return_value = 1_000
        mock_repo.evict_oldest.return_value = 300
        mock_get_repo.return_value = mock_repo

        mock_settings = MagicMock()
        mock_settings.max_size = 1_000
        mock_settings.overflow_strategy = "drop_oldest"
        mock_settings.emergency_purge_threshold = 0.8
        mock_settings.overflow_evict_batch_size = 1_000
        mock_get_settings.return_value = mock_settings

        run_background_eviction()

        mock_repo.compress_and_evict_oldest.assert_not_called()

    @patch("baldur.settings.dlq.get_dlq_settings")
    @patch("baldur_pro.services.dlq.overflow._get_repository")
    def test_compress_strategy_no_longer_warns_not_implemented(
        self, mock_get_repo, mock_get_settings
    ):
        """compress_oldest no longer logs dlq.compress_oldest_not_implemented."""
        from baldur_pro.services.dlq.overflow import run_background_eviction

        mock_repo = MagicMock()
        mock_repo.count_all.return_value = 800
        mock_repo.compress_and_evict_oldest.return_value = 100
        mock_get_repo.return_value = mock_repo

        mock_settings = MagicMock()
        mock_settings.max_size = 1_000
        mock_settings.overflow_strategy = "compress_oldest"
        mock_settings.emergency_purge_threshold = 0.8
        mock_settings.overflow_evict_batch_size = 1_000
        mock_get_settings.return_value = mock_settings

        with patch("baldur_pro.services.dlq.overflow.logger") as mock_logger:
            run_background_eviction()
            # Verify no warning about "not_implemented"
            for call in mock_logger.warning.call_args_list:
                assert "compress_oldest_not_implemented" not in str(call)


# =============================================================================
# E. Behavior Tests — get_dlq_repository()
# =============================================================================


class TestGetDlqRepositoryBehavior:
    """get_dlq_repository() public function behavior."""

    @patch("baldur.core.di_fallback.resolve_with_fallback")
    def test_calls_resolve_with_fallback(self, mock_resolve):
        """Uses resolve_with_fallback with correct service_name."""
        from baldur_pro.services.dlq.base import get_dlq_repository

        mock_resolve.return_value = MagicMock()
        get_dlq_repository()
        mock_resolve.assert_called_once()
        _, kwargs = mock_resolve.call_args
        assert kwargs["service_name"] == "DLQRepository"

    @patch("baldur.core.di_fallback.resolve_with_fallback")
    def test_fallback_class_is_inmemory(self, mock_resolve):
        """Fallback class is InMemoryFailedOperationRepository."""
        from baldur.adapters.memory import InMemoryFailedOperationRepository
        from baldur_pro.services.dlq.base import get_dlq_repository

        mock_resolve.return_value = MagicMock()
        get_dlq_repository()
        _, kwargs = mock_resolve.call_args
        assert kwargs["fallback_class"] is InMemoryFailedOperationRepository


# =============================================================================
# F. Behavior Tests — log_dlq_compress_audit()
# =============================================================================


class TestLogDlqCompressAuditBehavior:
    """log_dlq_compress_audit() WAL hybrid pattern behavior."""

    @patch("baldur_pro.services.audit.dlq_audit._get_audit_adapter")
    @patch("baldur_pro.services.audit.dlq_audit._write_to_wal")
    def test_writes_to_wal_first(self, mock_wal, mock_adapter):
        """WAL write is called with DLQ_COMPRESS event type."""
        from baldur_pro.services.audit.dlq_audit import log_dlq_compress_audit

        mock_wal.return_value = 42
        mock_adapter.return_value = None

        result = log_dlq_compress_audit(
            source_count=10, summary_count=2, details={"test": True}
        )

        mock_wal.assert_called_once()
        call_kwargs = mock_wal.call_args[1]
        assert call_kwargs["event_type"] == "DLQ_COMPRESS"
        assert call_kwargs["source"] == "DLQCompression"
        assert result == 42

    @patch("baldur_pro.services.audit.dlq_audit._get_audit_adapter")
    @patch("baldur_pro.services.audit.dlq_audit._write_to_wal")
    def test_writes_to_adapter_directly(self, mock_wal, mock_adapter):
        """Direct adapter write uses the canonical AuditEntry + log() (D3)."""
        from baldur.interfaces.audit_adapter import AuditEntry, AuditLogAdapter
        from baldur_pro.services.audit.dlq_audit import log_dlq_compress_audit

        mock_wal.return_value = 1
        # spec'd adapter: a reintroduced phantom log_event would raise.
        mock_a = MagicMock(spec=AuditLogAdapter)
        mock_adapter.return_value = mock_a

        log_dlq_compress_audit(source_count=10, summary_count=2, details={"key": "val"})

        mock_a.log.assert_called_once()
        entry = mock_a.log.call_args.args[0]
        assert isinstance(entry, AuditEntry)
        assert entry.action == "dlq_compress"
        assert entry.target_type == "dlq_compress"
        assert entry.details["key"] == "val"
        assert entry.details["source"] == "DLQCompression"

    @patch("baldur_pro.services.audit.dlq_audit._get_audit_adapter")
    @patch("baldur_pro.services.audit.dlq_audit._write_to_wal")
    def test_fail_open_on_adapter_error(self, mock_wal, mock_adapter):
        """Adapter exception does not propagate — fail-open."""
        from baldur.interfaces.audit_adapter import AuditLogAdapter
        from baldur_pro.services.audit.dlq_audit import log_dlq_compress_audit

        mock_wal.return_value = 1
        mock_a = MagicMock(spec=AuditLogAdapter)
        mock_a.log.side_effect = RuntimeError("adapter down")
        mock_adapter.return_value = mock_a

        # Should not raise
        result = log_dlq_compress_audit(source_count=10, summary_count=2, details={})
        assert result == 1  # WAL result still returned

    @patch("baldur_pro.services.audit.dlq_audit._get_audit_adapter")
    @patch("baldur_pro.services.audit.dlq_audit._write_to_wal")
    def test_no_adapter_skips_direct_write(self, mock_wal, mock_adapter):
        """When adapter is None, direct write is skipped gracefully."""
        from baldur_pro.services.audit.dlq_audit import log_dlq_compress_audit

        mock_wal.return_value = 1
        mock_adapter.return_value = None

        result = log_dlq_compress_audit(source_count=5, summary_count=1, details={})
        assert result == 1


# =============================================================================
# G. Behavior Tests — Celery Tasks
# =============================================================================


class TestEvictOverflowDistributedLockBehavior:
    """evict_overflow_dlq_entries distributed lock behavior."""

    @patch("baldur_pro.services.dlq.overflow.run_background_eviction")
    def test_lock_import_error_proceeds_without_lock(self, mock_eviction):
        """DistributedRecoveryLock import failure → fail-open, proceeds."""
        from baldur.celery_tasks.dlq_tasks import evict_overflow_dlq_entries

        mock_eviction.return_value = {"evicted": 5}

        # Simulate import failure for distributed lock module
        with patch.dict(
            "sys.modules",
            {"baldur_pro.services.coordination.distributed_recovery_lock": None},
        ):
            result = evict_overflow_dlq_entries.apply()

        assert result.result == {"evicted": 5}
        mock_eviction.assert_called_once()

    @patch("baldur_pro.services.dlq.overflow.run_background_eviction")
    def test_lock_acquired_runs_eviction(self, mock_eviction):
        """Lock acquired → run_background_eviction() executes."""
        from baldur.celery_tasks.dlq_tasks import evict_overflow_dlq_entries

        mock_eviction.return_value = {"evicted": 10}

        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True

        with patch(
            "baldur_pro.services.coordination.distributed_recovery_lock.DistributedRecoveryLock",
            return_value=mock_lock,
        ):
            result = evict_overflow_dlq_entries.apply()

        assert result.result == {"evicted": 10}

    @patch("baldur_pro.services.dlq.overflow.run_background_eviction")
    def test_lock_not_acquired_skips_eviction(self, mock_eviction):
        """Lock not acquired → skip, eviction not called."""
        from baldur.celery_tasks.dlq_tasks import evict_overflow_dlq_entries

        mock_lock = MagicMock()
        mock_lock.acquire.return_value = False

        with patch(
            "baldur_pro.services.coordination.distributed_recovery_lock.DistributedRecoveryLock",
            return_value=mock_lock,
        ):
            result = evict_overflow_dlq_entries.apply()

        assert result.result == {"status": "skipped", "reason": "lock_not_acquired"}
        mock_eviction.assert_not_called()

    @patch("baldur_pro.services.dlq.overflow.run_background_eviction")
    def test_lock_released_after_eviction(self, mock_eviction):
        """Lock is released in finally block after eviction."""
        from baldur.celery_tasks.dlq_tasks import evict_overflow_dlq_entries

        mock_eviction.return_value = {"evicted": 5}

        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True

        with patch(
            "baldur_pro.services.coordination.distributed_recovery_lock.DistributedRecoveryLock",
            return_value=mock_lock,
        ):
            evict_overflow_dlq_entries.apply()

        mock_lock.release.assert_called_once()

    @patch("baldur_pro.services.dlq.overflow.run_background_eviction")
    def test_lock_released_on_eviction_error(self, mock_eviction):
        """Lock is released even when eviction raises an exception."""
        from baldur.celery_tasks.dlq_tasks import evict_overflow_dlq_entries

        mock_eviction.side_effect = RuntimeError("eviction failed")

        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True

        with patch(
            "baldur_pro.services.coordination.distributed_recovery_lock.DistributedRecoveryLock",
            return_value=mock_lock,
        ):
            # apply() wraps raised exceptions; the task catches and returns error dict
            # but the logger.exception call with keyword arg may re-raise in test env
            evict_overflow_dlq_entries.apply()

        # Regardless of exception behavior, lock must be released (finally block)
        mock_lock.release.assert_called_once()


class TestCleanupCompressedEntriesBehavior:
    """cleanup_compressed_dlq_entries task behavior."""

    @patch("baldur.settings.dlq.get_dlq_settings")
    @patch("baldur_pro.services.dlq.base.get_dlq_repository")
    def test_transitions_active_to_stale(self, mock_get_repo, mock_get_settings):
        """ACTIVE entries older than stale cutoff are transitioned to STALE."""
        from baldur.celery_tasks.dlq_tasks import cleanup_compressed_dlq_entries

        now = datetime.now(UTC)

        old_entry = MagicMock()
        old_entry.compressed_at = now - timedelta(days=31)
        old_entry.id = "compressed:test:1"

        mock_repo = MagicMock()
        mock_repo.get_compressed_entries.side_effect = lambda status=None, **kw: (
            [old_entry] if status == "active" else []
        )
        mock_get_repo.return_value = mock_repo

        mock_settings = MagicMock()
        mock_settings.compress_stale_after_days = 30
        mock_settings.compress_archive_after_days = 90
        mock_get_settings.return_value = mock_settings

        result = cleanup_compressed_dlq_entries.apply()

        assert result.result["success"] is True
        assert result.result["stale_count"] == 1
        mock_repo.update_compressed_status.assert_called_with(
            "compressed:test:1", "stale"
        )

    @patch("baldur.settings.dlq.get_dlq_settings")
    @patch("baldur_pro.services.dlq.base.get_dlq_repository")
    def test_transitions_stale_to_archived(self, mock_get_repo, mock_get_settings):
        """STALE entries older than archive cutoff are transitioned to ARCHIVED."""
        from baldur.celery_tasks.dlq_tasks import cleanup_compressed_dlq_entries

        now = datetime.now(UTC)

        stale_entry = MagicMock()
        stale_entry.stale_at = now - timedelta(days=91)
        stale_entry.id = "compressed:test:2"

        mock_repo = MagicMock()
        mock_repo.get_compressed_entries.side_effect = lambda status=None, **kw: (
            [stale_entry] if status == "stale" else []
        )
        mock_get_repo.return_value = mock_repo

        mock_settings = MagicMock()
        mock_settings.compress_stale_after_days = 30
        mock_settings.compress_archive_after_days = 90
        mock_get_settings.return_value = mock_settings

        result = cleanup_compressed_dlq_entries.apply()

        assert result.result["success"] is True
        assert result.result["archived_count"] == 1
        mock_repo.update_compressed_status.assert_called_with(
            "compressed:test:2", "archived"
        )

    @patch("baldur.settings.dlq.get_dlq_settings")
    @patch("baldur_pro.services.dlq.base.get_dlq_repository")
    def test_recent_entries_not_transitioned(self, mock_get_repo, mock_get_settings):
        """Recent entries within cutoff are NOT transitioned."""
        from baldur.celery_tasks.dlq_tasks import cleanup_compressed_dlq_entries

        now = datetime.now(UTC)

        recent_entry = MagicMock()
        recent_entry.compressed_at = now - timedelta(days=5)
        recent_entry.id = "compressed:recent:1"

        mock_repo = MagicMock()
        mock_repo.get_compressed_entries.side_effect = lambda status=None, **kw: (
            [recent_entry] if status == "active" else []
        )
        mock_get_repo.return_value = mock_repo

        mock_settings = MagicMock()
        mock_settings.compress_stale_after_days = 30
        mock_settings.compress_archive_after_days = 90
        mock_get_settings.return_value = mock_settings

        result = cleanup_compressed_dlq_entries.apply()

        assert result.result["stale_count"] == 0
        assert result.result["archived_count"] == 0
        mock_repo.update_compressed_status.assert_not_called()
