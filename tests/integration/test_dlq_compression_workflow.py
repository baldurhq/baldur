"""
DLQ Compression Workflow Integration Tests (351_DLQ_COMPRESSION).

Tests multi-component composition of DLQ compression:
    - overflow + compress_oldest → InMemoryRepo → summary creation
    - compress → query via get_compressed_entries
    - lifecycle cleanup: ACTIVE → STALE → ARCHIVED
    - audit trail records source IDs
    - get_dlq_repository used consistently across components

Test Categories:
    A. Overflow with compress: overflow triggers compression, summaries created
    B. Compressed entry query: domain/status filtering after compression
    C. Lifecycle transitions: cleanup task transitions ACTIVE → STALE → ARCHIVED
    D. Audit trail: compression audit includes source IDs
    E. Full workflow: store → overflow → compress → query → lifecycle

Note: All tests use InMemoryFailedOperationRepository - no DB/Redis dependency.
      This enables parallel test execution with pytest-xdist.
"""

from __future__ import annotations

from baldur.adapters.memory.failed_operation import (
    InMemoryFailedOperationRepository,
)
from baldur.settings.dlq import DLQSettings


def _make_settings(**overrides):
    """Create DLQSettings with overrides for testing."""
    defaults = {
        "max_size": 1_000,
        "max_size_per_domain": 500,
        "overflow_strategy": "compress_oldest",
        "emergency_purge_threshold": 0.8,
        "overflow_evict_batch_size": 100,
        "compress_stale_after_days": 30,
        "compress_archive_after_days": 90,
    }
    defaults.update(overrides)
    return DLQSettings(**defaults)


# =============================================================================
# A. Overflow with compress strategy
# =============================================================================


# =============================================================================
# B. Compressed entry query
# =============================================================================


class TestCompressedEntryQuery:
    """Query compressed entries after compression."""

    def setup_method(self):
        """Set up repository with diverse entries and compress."""
        self.repo = InMemoryFailedOperationRepository()
        # Create entries across 3 domains
        for domain, ft, ec, count in [
            ("payment", "timeout", "E_TIMEOUT", 50),
            ("auth", "conn_refused", "E_CONN", 30),
            ("data_sync", "schema_error", "E_001", 20),
        ]:
            for _ in range(count):
                self.repo.create(
                    domain=domain, failure_type=ft, error_code=ec, error_message="err"
                )
        self.repo.compress_and_evict_oldest(100)

    def test_all_entries_returned_without_filter(self):
        """No filter returns all 3 summaries."""
        entries = self.repo.get_compressed_entries()
        assert len(entries) == 3

    def test_domain_filter_returns_only_matching(self):
        """Domain filter returns only that domain's summary."""
        entries = self.repo.get_compressed_entries(domain="auth")
        assert len(entries) == 1
        assert entries[0].domain == "auth"
        assert entries[0].count == 30

    def test_status_filter_after_transition(self):
        """After transition to stale, status filter correctly separates entries."""
        # Transition one to stale
        all_entries = self.repo.get_compressed_entries()
        self.repo.update_compressed_status(all_entries[0].id, "stale")

        active = self.repo.get_compressed_entries(status="active")
        stale = self.repo.get_compressed_entries(status="stale")
        assert len(active) == 2
        assert len(stale) == 1

    def test_summary_aggregates_all_entries(self):
        """get_compressed_summary returns correct totals."""
        summary = self.repo.get_compressed_summary()
        assert summary["total_summaries"] == 3
        assert summary["total_compressed_items"] == 100
        assert summary["by_status"]["active"] == 3


# =============================================================================
# C. Lifecycle transitions
# =============================================================================


class TestCompressedEntryLifecycle:
    """Compressed entry lifecycle: ACTIVE → STALE → ARCHIVED."""

    def setup_method(self):
        """Set up repository with compressed entries."""
        self.repo = InMemoryFailedOperationRepository()
        for _ in range(10):
            self.repo.create(
                domain="payment",
                failure_type="timeout",
                error_code="E_TIMEOUT",
                error_message="err",
            )
        self.repo.compress_and_evict_oldest(10)

    def test_active_transitions_to_stale(self):
        """ACTIVE → STALE sets stale_at timestamp."""
        entry = self.repo.get_compressed_entries()[0]
        assert entry.status == "active"

        self.repo.update_compressed_status(entry.id, "stale")
        updated = self.repo.get_compressed_entries(status="stale")[0]
        assert updated.status == "stale"
        assert updated.stale_at is not None

    def test_stale_transitions_to_archived(self):
        """STALE → ARCHIVED sets archived_at timestamp."""
        entry = self.repo.get_compressed_entries()[0]
        self.repo.update_compressed_status(entry.id, "stale")
        self.repo.update_compressed_status(entry.id, "archived")

        updated = self.repo.get_compressed_entries(status="archived")[0]
        assert updated.status == "archived"
        assert updated.archived_at is not None

    def test_archived_never_deleted(self):
        """ARCHIVED entries remain in storage (never hard deleted)."""
        entry = self.repo.get_compressed_entries()[0]
        self.repo.update_compressed_status(entry.id, "stale")
        self.repo.update_compressed_status(entry.id, "archived")

        # No hard-delete method exists; verify entry persists
        all_entries = self.repo.get_compressed_entries(status="archived")
        assert len(all_entries) == 1

    def test_full_lifecycle_summary_reflects_transitions(self):
        """Summary by_status reflects lifecycle transitions."""
        entry = self.repo.get_compressed_entries()[0]

        # Initially active
        summary = self.repo.get_compressed_summary()
        assert summary["by_status"]["active"] == 1

        # After stale
        self.repo.update_compressed_status(entry.id, "stale")
        summary = self.repo.get_compressed_summary()
        assert summary["by_status"]["active"] == 0
        assert summary["by_status"]["stale"] == 1

        # After archived
        self.repo.update_compressed_status(entry.id, "archived")
        summary = self.repo.get_compressed_summary()
        assert summary["by_status"]["stale"] == 0
        assert summary["by_status"]["archived"] == 1


# =============================================================================
# D. Audit trail
# =============================================================================


# =============================================================================
# E. Full workflow
# =============================================================================
