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


from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

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
    """cleanup_compressed_dlq_entries task behavior.

    Driven against a real in-memory repository rather than a mocked one. The
    defect this lane carried was in adapter query ordering, and a mocked
    repository returns whatever the test hands it — it would pass against
    either ordering and so prove nothing.
    """

    @staticmethod
    def _entry(entry_id, *, compressed_days_ago, status="active", stale_days_ago=None):
        now = datetime.now(UTC)
        return DLQCompressedEntry(
            id=entry_id,
            domain="payment",
            failure_type="timeout",
            error_code="ETIMEDOUT",
            count=1,
            first_seen=now,
            last_seen=now,
            sample_error_message="boom",
            status=status,
            compressed_at=now - timedelta(days=compressed_days_ago),
            stale_at=(
                now - timedelta(days=stale_days_ago)
                if stale_days_ago is not None
                else None
            ),
        )

    def _run(self, repo, mock_get_settings, *, lock_acquired=True):
        from baldur.celery_tasks.dlq_tasks import cleanup_compressed_dlq_entries

        settings = MagicMock()
        settings.compress_stale_after_days = 30
        settings.compress_archive_after_days = 90
        mock_get_settings.return_value = settings

        # The sweep holds a distributed lock. No lock backend is configured in
        # a unit environment, so a real acquisition reports "someone else is
        # sweeping" and the drains under test would never run.
        @contextmanager
        def _lock(session_id):
            yield lock_acquired

        with (
            patch("baldur.factory.registry.ProviderRegistry") as registry,
            patch("baldur.dlq.helpers.compressed_lifecycle_lock", _lock),
        ):
            registry.dlq_repository.safe_get.return_value = repo
            return cleanup_compressed_dlq_entries.apply()

    @patch("baldur.settings.dlq.get_dlq_settings")
    def test_transitions_active_to_stale(self, mock_get_settings):
        """ACTIVE entries older than the stale cutoff are transitioned to STALE."""
        repo = InMemoryFailedOperationRepository()
        repo.store_compressed_entry(self._entry("c:1", compressed_days_ago=31))

        result = self._run(repo, mock_get_settings)

        assert result.result["success"] is True
        assert result.result["stale_count"] == 1
        assert repo._compressed_storage["c:1"].status == "stale"
        assert repo._compressed_storage["c:1"].stale_at is not None

    @patch("baldur.settings.dlq.get_dlq_settings")
    def test_transitions_stale_to_archived(self, mock_get_settings):
        """STALE entries whose stale_at predates the archive cutoff are ARCHIVED."""
        repo = InMemoryFailedOperationRepository()
        repo.store_compressed_entry(
            self._entry(
                "c:2", compressed_days_ago=200, status="stale", stale_days_ago=91
            )
        )

        result = self._run(repo, mock_get_settings)

        assert result.result["success"] is True
        assert result.result["archived_count"] == 1
        assert repo._compressed_storage["c:2"].status == "archived"

    @patch("baldur.settings.dlq.get_dlq_settings")
    def test_recent_entries_not_transitioned(self, mock_get_settings):
        """Entries inside the cutoff are left alone."""
        repo = InMemoryFailedOperationRepository()
        repo.store_compressed_entry(self._entry("c:3", compressed_days_ago=5))

        result = self._run(repo, mock_get_settings)

        assert result.result["stale_count"] == 0
        assert result.result["archived_count"] == 0
        assert repo._compressed_storage["c:3"].status == "active"

    @patch("baldur.settings.dlq.get_dlq_settings")
    def test_transitions_entries_beyond_a_single_page(self, mock_get_settings):
        """The drain crosses page boundaries instead of stopping at the first.

        Pins the regression that made this lane inert: reading a single page of
        newest-first entries transitioned nothing once volume outgrew the page.
        """
        from baldur.celery_tasks.dlq_tasks import _COMPRESSED_DRAIN_PAGE_SIZE

        repo = InMemoryFailedOperationRepository()
        total = _COMPRESSED_DRAIN_PAGE_SIZE + 250
        for i in range(total):
            repo.store_compressed_entry(
                self._entry(f"c:{i:05d}", compressed_days_ago=31 + i % 7)
            )

        result = self._run(repo, mock_get_settings)

        assert result.result["stale_count"] == total
        assert all(e.status == "stale" for e in repo._compressed_storage.values())

    @patch("baldur.settings.dlq.get_dlq_settings")
    def test_ineligible_stale_entries_do_not_stall_the_drain(self, mock_get_settings):
        """STALE entries not yet archivable are stepped over, not re-read forever.

        A skipped entry keeps its status, so it stays in the query's result set;
        the drain has to advance past it or it would re-read the same page until
        the iteration bound runs out. The outcome is right either way, so this
        asserts the query count — the only place the difference shows.
        """
        repo = InMemoryFailedOperationRepository()
        for i in range(60):
            repo.store_compressed_entry(
                self._entry(
                    f"old:{i:03d}",
                    compressed_days_ago=200,
                    status="stale",
                    stale_days_ago=91,
                )
            )
        for i in range(40):
            repo.store_compressed_entry(
                self._entry(
                    f"new:{i:03d}",
                    compressed_days_ago=200,
                    status="stale",
                    stale_days_ago=1,
                )
            )

        calls = {"n": 0}
        original = repo.get_compressed_entries_before

        def counting(*args, **kwargs):
            calls["n"] += 1
            return original(*args, **kwargs)

        repo.get_compressed_entries_before = counting

        result = self._run(repo, mock_get_settings)

        assert result.result["archived_count"] == 60
        # Two queries per transition lane (one page + one empty page) plus the
        # ACTIVE lane's single empty page. A drain that never advanced its
        # cursor would spin to _COMPRESSED_DRAIN_MAX_ITERATIONS instead.
        assert calls["n"] <= 6, f"drain re-read its own page ({calls['n']} queries)"
        archived = [
            e for e in repo._compressed_storage.values() if e.status == "archived"
        ]
        still_stale = [
            e for e in repo._compressed_storage.values() if e.status == "stale"
        ]
        assert len(archived) == 60
        assert len(still_stale) == 40

    @patch("baldur.settings.dlq.get_dlq_settings", autospec=True)
    def test_drain_advances_the_score_cursor_across_pages(self, mock_get_settings):
        """Each page is fetched with a lower bound taken from the last one.

        Without it the query restarts at the oldest entry every iteration.
        The memory adapter does not care, but Redis keeps status in the entry
        blob rather than in the index, so a head-anchored scan re-reads the
        whole transitioned prefix once per page.
        """
        from baldur.celery_tasks.dlq_tasks import _COMPRESSED_DRAIN_PAGE_SIZE

        repo = InMemoryFailedOperationRepository()
        for i in range(_COMPRESSED_DRAIN_PAGE_SIZE + 250):
            repo.store_compressed_entry(
                self._entry(f"c:{i:05d}", compressed_days_ago=31 + i % 7)
            )

        seen_after = []
        original = repo.get_compressed_entries_before

        def spy(*args, **kwargs):
            seen_after.append(kwargs.get("after"))
            return original(*args, **kwargs)

        repo.get_compressed_entries_before = spy

        result = self._run(repo, mock_get_settings)

        assert result.result["stale_count"] == _COMPRESSED_DRAIN_PAGE_SIZE + 250
        # The first call of a lane opens the window; later calls carry a
        # cursor forward instead of restarting at the oldest entry.
        assert seen_after[0] is None
        assert any(a is not None for a in seen_after[1:]), (
            f"drain never advanced its cursor: {seen_after}"
        )

    @patch("baldur.settings.dlq.get_dlq_settings", autospec=True)
    def test_a_run_that_loses_the_lock_transitions_nothing(self, mock_get_settings):
        """Overlapping runs page through a shrinking key and step over entries.

        Harmless while the walked index only ever grew; once a transition
        removes its entry from the key being walked, the other run's positional
        offset skips whatever moved.
        """
        repo = InMemoryFailedOperationRepository()
        repo.store_compressed_entry(self._entry("c:1", compressed_days_ago=31))

        result = self._run(repo, mock_get_settings, lock_acquired=False)

        assert result.result == {"status": "skipped", "reason": "lock_not_acquired"}
        assert repo._compressed_storage["c:1"].status == "active"

    @patch("baldur.settings.dlq.get_dlq_settings", autospec=True)
    def test_the_index_is_reconciled_before_the_drains_read_it(self, mock_get_settings):
        """Step 0 is what makes the drains' index trustworthy, so it runs first.

        Reconciling after the drains would leave every run reading an index
        the previous run reconciled — one run behind, forever.
        """
        repo = InMemoryFailedOperationRepository()
        repo.store_compressed_entry(self._entry("c:1", compressed_days_ago=31))
        order = []
        original_backfill = repo.backfill_compressed_status_index
        original_query = repo.get_compressed_entries_before

        def backfill(*args, **kwargs):
            order.append("backfill")
            return original_backfill(*args, **kwargs)

        def query(*args, **kwargs):
            order.append("query")
            return original_query(*args, **kwargs)

        repo.backfill_compressed_status_index = backfill
        repo.get_compressed_entries_before = query

        result = self._run(repo, mock_get_settings)

        assert order[0] == "backfill"
        assert order.count("backfill") == 1
        assert result.result["stale_count"] == 1

    @patch("baldur.settings.dlq.get_dlq_settings", autospec=True)
    def test_a_failed_reconciliation_still_lets_the_drains_run(self, mock_get_settings):
        """Fail-open: the drains fall back to whichever index the repository
        considers trustworthy, which is exactly today's behaviour."""
        repo = InMemoryFailedOperationRepository()
        repo.store_compressed_entry(self._entry("c:1", compressed_days_ago=31))

        def exploding_backfill(**kwargs):
            raise RuntimeError("redis down")

        repo.backfill_compressed_status_index = exploding_backfill

        with capture_logs() as logs:
            result = self._run(repo, mock_get_settings)

        assert result.result["stale_count"] == 1
        assert repo._compressed_storage["c:1"].status == "stale"
        failures = [
            e for e in logs if e["event"] == "dlq.compressed_backfill_step_failed"
        ]
        assert len(failures) == 1
        assert failures[0]["log_level"] == "warning"


class TestInMemoryCompressedByIdBehavior:
    """InMemory adapter get_compressed_entry point read (721 D2)."""

    def _compressed_entry(self, entry_id: str, *, count: int = 7) -> DLQCompressedEntry:
        now = datetime.now(UTC)
        return DLQCompressedEntry(
            id=entry_id,
            domain="payment",
            failure_type="timeout",
            error_code="E_X",
            count=count,
            first_seen=now - timedelta(days=7),
            last_seen=now,
            sample_error_message="x",
        )

    def test_get_compressed_entry_returns_stored_entry_by_id(self):
        """A stored compressed entry is returned by its id."""
        repo = InMemoryFailedOperationRepository()
        entry = self._compressed_entry("compressed:payment:timeout:E_X:1", count=7)
        repo.store_compressed_entry(entry)

        fetched = repo.get_compressed_entry(entry.id)

        assert fetched is not None
        assert fetched.id == entry.id
        assert fetched.count == 7

    def test_get_compressed_entry_returns_none_for_absent_id(self):
        """An id with no stored entry returns None."""
        repo = InMemoryFailedOperationRepository()
        assert repo.get_compressed_entry("compressed:absent:1") is None
