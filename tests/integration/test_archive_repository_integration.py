"""Archive Repository Integration Tests (366)

Verifies end-to-end flows between archive services and InMemory repositories
for both CascadeEvent and RecoverySession domains.

Test Categories:
    A. RecoverySessionArchiveService lifecycle:
       archive -> get -> update -> resume -> cleanup
    B. CascadeEvent archive task -> repository flow
    C. ProviderRegistry auto-discover integration

Note: All tests use in-memory mock repositories - no DB dependency.
      This enables parallel test execution with pytest-xdist.
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from datetime import timedelta

from baldur.adapters.memory.cascade_event import (
    InMemoryCascadeEventArchiveRepository,
)
from baldur.adapters.memory.recovery_session import (
    InMemoryRecoverySessionArchiveRepository,
)
from baldur.models.cascade_event import CascadeEventData
from baldur.models.recovery_session import RecoverySessionData
from baldur.utils.time import utc_now
from baldur_pro.services.coordination.enums import RecoveryStatus
from baldur_pro.services.coordination.recovery_session_archive import (
    RecoverySessionArchiveService,
)
from baldur_pro.services.coordination.recovery_state import (
    RecoverySession,
    RecoveryStep,
    RecoveryStepType,
)

# =============================================================================
# A. RecoverySessionArchiveService Lifecycle Integration
# =============================================================================


class TestRecoverySessionArchiveLifecycle:
    """RecoverySessionArchiveService full lifecycle integration.

    Validates:
    - Archive → retrieve → update → resume → cleanup flow
    - Conversion helpers _session_to_data / _data_to_session preserve data
    - Service correctly delegates to repository
    """

    def setup_method(self):
        """Set up fresh service with InMemory repository."""
        self.repo = InMemoryRecoverySessionArchiveRepository()
        self.service = RecoverySessionArchiveService(repo=self.repo)

    def _make_session(
        self,
        session_id: str = "recovery-lifecycle",
        status: RecoveryStatus = RecoveryStatus.COMPLETED,
    ) -> RecoverySession:
        return RecoverySession(
            id=session_id,
            namespace="global",
            trigger_level="LEVEL_3",
            status=status,
            steps=[
                RecoveryStep(
                    step_type=RecoveryStepType.BUDGET_RESET,
                    order=1,
                    status=RecoveryStatus.COMPLETED,
                    started_at="2026-01-23T10:00:00+00:00",
                    completed_at="2026-01-23T10:00:05+00:00",
                ),
            ],
            started_at="2026-01-23T10:00:00+00:00",
            completed_at="2026-01-23T10:05:05+00:00",
            initiated_by="system",
        )

    def test_archive_then_retrieve_preserves_session_data(self):
        """Archive → get_session preserves all session fields.

        Purpose:
            Verify that archiving a RecoverySession and retrieving it via
            the repository produces consistent data.
        Expected:
            - session_id, namespace, trigger_level, status match
            - steps_data is not empty
        """
        session = self._make_session()
        archived = self.service.archive_session(session)

        retrieved = self.service.get_session("recovery-lifecycle")

        assert retrieved is not None
        assert retrieved.session_id == archived.session_id
        assert retrieved.namespace == "global"
        assert retrieved.status == "completed"
        assert len(retrieved.steps_data) == 1

    def test_archive_update_then_retrieve_reflects_changes(self):
        """Archive → mark state change → update → retrieve reflects change.

        Purpose:
            Verify that domain model state mutation + repo.update() persists
            the change through the repository.
        """
        # Given — archive an in-progress session
        session = self._make_session(status=RecoveryStatus.IN_PROGRESS)
        self.service.archive_session(session)

        # When — load, change status, update via repo
        data = self.repo.get_by_session_id("recovery-lifecycle")
        data.status = RecoveryStatus.COMPLETED.value
        self.repo.update(data)

        # Then — retrieve reflects the change
        result = self.service.get_session("recovery-lifecycle")
        assert result.status == "completed"

    def test_archive_and_resume_in_progress_session(self):
        """Archive IN_PROGRESS session → load_for_resume returns RecoverySession.

        Purpose:
            Verify that an in-progress session can be archived and later
            resumed as a full RecoverySession object.
        """
        session = self._make_session(status=RecoveryStatus.IN_PROGRESS)
        session.completed_at = None
        self.service.archive_session(session)

        restored = self.service.load_for_resume("recovery-lifecycle")

        assert restored is not None
        assert restored.status == RecoveryStatus.IN_PROGRESS
        assert len(restored.steps) == 1

    def test_cleanup_removes_old_sessions_preserves_recent(self):
        """cleanup_old_archives removes old sessions, preserves recent ones.

        Purpose:
            Verify that cleanup correctly removes sessions older than
            retention period while preserving recent ones.
        """
        # Given — one old session and one recent session
        old_session = self._make_session("recovery-old")
        self.service.archive_session(old_session)
        # Manually set the old session's started_at to 400 days ago
        old_data = self.repo.get_by_session_id("recovery-old")
        old_data.started_at = utc_now() - timedelta(days=400)
        self.repo.update(old_data)

        recent_session = self._make_session("recovery-recent")
        self.service.archive_session(recent_session)

        # When
        deleted = self.service.cleanup_old_archives(retention_days=365)

        # Then
        assert deleted == 1
        assert self.repo.get_by_session_id("recovery-old") is None
        assert self.repo.get_by_session_id("recovery-recent") is not None

    def test_statistics_aggregation_across_sessions(self):
        """get_statistics aggregates completed/failed/aborted counts.

        Purpose:
            Verify statistics are correctly computed across multiple
            archived sessions with different statuses.
        """
        for i in range(3):
            s = self._make_session(f"s-completed-{i}", RecoveryStatus.COMPLETED)
            self.service.archive_session(s)
        s_failed = self._make_session("s-failed", RecoveryStatus.FAILED)
        self.service.archive_session(s_failed)

        stats = self.service.get_statistics(days=365)

        assert stats["total_sessions"] == 4
        assert stats["completed"] == 3
        assert stats["failed"] == 1
        assert stats["success_rate"] == 75.0


# =============================================================================
# B. CascadeEvent Archive Repository Integration
# =============================================================================


class TestCascadeEventArchiveIntegration:
    """CascadeEvent domain model + InMemory repository integration.

    Validates:
    - CascadeEventData creation → save → find → chain retrieval
    - Hash integrity verification through repository roundtrip
    """

    def setup_method(self):
        """Set up fresh repository."""
        self.repo = InMemoryCascadeEventArchiveRepository()
        self.now = utc_now()

    def test_save_find_chain_end_to_end(self):
        """Save events → find → get_chain produces consistent results.

        Purpose:
            Verify that cascade events saved to repository can be
            queried back through both find() and get_chain().
        """
        # Given — save 3 events across 2 namespaces
        for i, ns in enumerate(["global", "global", "seoul"]):
            self.repo.save(
                CascadeEventData(
                    cascade_id=f"cascade-evt-{i}",
                    namespace=ns,
                    trigger_type="CANARY_ROLLBACK",
                    current_hash=f"hash-{i}",
                    timestamp=self.now - timedelta(hours=i),
                )
            )

        # When — find by namespace
        global_events = self.repo.find(namespace="global")
        chain = self.repo.get_chain("global")

        # Then
        assert len(global_events) == 2
        # find returns DESC
        assert global_events[0].cascade_id == "cascade-evt-0"
        # get_chain returns ASC
        assert chain[0].cascade_id == "cascade-evt-1"
        assert chain[1].cascade_id == "cascade-evt-0"

    def test_hash_integrity_preserved_through_repository(self):
        """Hash integrity is preserved after save → retrieve roundtrip.

        Purpose:
            Verify that verify_hash_integrity() returns True after
            data passes through the repository.
        """
        import hashlib

        from baldur.utils.serialization import fast_canonical_dumps

        content = {
            "id": "cascade-evt-integrity",
            "trigger": {"trigger_type": "DEESCALATION", "details": {}},
            "effects": [],
            "namespace": "global",
            "timestamp": self.now.isoformat(),
            "previous_hash": "",
        }
        valid_hash = hashlib.sha256(fast_canonical_dumps(content)).hexdigest()

        data = CascadeEventData(
            cascade_id="cascade-evt-integrity",
            namespace="global",
            trigger_type="DEESCALATION",
            current_hash=valid_hash,
            timestamp=self.now,
        )
        self.repo.save(data)

        # Retrieve and verify
        retrieved = self.repo.get_by_cascade_id("cascade-evt-integrity")
        assert retrieved.verify_hash_integrity() is True


# =============================================================================
# C. ProviderRegistry Auto-Discover Integration
# =============================================================================


class TestProviderRegistryArchiveRepoIntegration:
    """ProviderRegistry auto-discover + archive repository integration.

    Validates:
    - cascade_event_repo and recovery_session_repo are auto-registered
    - Default (memory) repositories are functional via ProviderRegistry
    """

    def test_cascade_event_repo_auto_discovered(self):
        """ProviderRegistry.get_cascade_event_repo() returns a working instance.

        Purpose:
            Verify auto-discover registers InMemory cascade event repo
            and it can perform basic operations.
        """
        from baldur.factory.registry import ProviderRegistry

        repo = ProviderRegistry.get_cascade_event_repo()
        assert repo is not None

        # Verify basic operation
        data = CascadeEventData(
            cascade_id="registry-test",
            namespace="global",
            trigger_type="CANARY_ROLLBACK",
            current_hash="test",
            timestamp=utc_now(),
        )
        repo.save(data)
        assert repo.get_by_cascade_id("registry-test") is not None

    def test_recovery_session_repo_auto_discovered(self):
        """ProviderRegistry.get_recovery_session_repo() returns a working instance.

        Purpose:
            Verify auto-discover registers InMemory recovery session repo
            and it can perform basic operations.
        """
        from baldur.factory.registry import ProviderRegistry

        repo = ProviderRegistry.get_recovery_session_repo()
        assert repo is not None

        # Verify basic operation
        data = RecoverySessionData(
            session_id="registry-test",
            namespace="global",
            trigger_level="LEVEL_1",
        )
        repo.save(data)
        assert repo.get_by_session_id("registry-test") is not None
