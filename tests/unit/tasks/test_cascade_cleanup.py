"""
Cascade Cleanup Tasks 단위 테스트 (Phase 4).

Tests:
- archive_cascade_events: Redis → PostgreSQL 이관
- purge_old_cascade_events: 오래된 이벤트 영구 삭제
- create_cascade_daily_checkpoint: 일일 체크포인트 생성
- verify_cascade_chain_integrity: 체인 무결성 검증
- recover_cascade_from_fallback: 로컬 폴백 복구
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from baldur.tasks.cascade_cleanup_tasks import (
    CASCADE_CLEANUP_SCHEDULE,
    archive_cascade_events,
    create_cascade_daily_checkpoint,
    purge_old_cascade_events,
    recover_cascade_from_fallback,
    verify_cascade_chain_integrity,
)

# Patch 경로 상수 (함수 내부에서 import하므로 원본 모듈 경로 사용)
PATCH_GET_AUDITOR = "baldur.audit.cascade_auditor.get_cascade_event_auditor"


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def memory_backend():
    """메모리 백엔드 fixture."""
    from baldur.core.state_backend import MemoryStateBackend

    return MemoryStateBackend()


@pytest.fixture
def mock_auditor(memory_backend):
    """Mock CascadeEventAuditor fixture."""
    from baldur.audit.cascade_auditor import (
        CascadeEventAuditor,
        reset_cascade_auditor,
    )

    reset_cascade_auditor()

    auditor = CascadeEventAuditor()
    auditor._get_backend = MagicMock(return_value=memory_backend)

    # 테스트용 이벤트 생성
    for i in range(5):
        auditor.record(
            trigger_type=f"TEST_TRIGGER_{i}",
            trigger_details={"index": i},
            effects=[{"action_type": f"ACTION_{i}", "success": True}],
            namespace="test",
            triggered_by="test",
        )

    return auditor


@pytest.fixture
def old_events_auditor(memory_backend):
    """오래된 이벤트가 있는 auditor fixture."""
    from baldur.audit.cascade_auditor import (
        CascadeEventAuditor,
        reset_cascade_auditor,
    )
    from baldur.audit.cascade_event import (
        CascadeEvent,
        CascadeTrigger,
    )

    reset_cascade_auditor()

    auditor = CascadeEventAuditor()
    auditor._get_backend = MagicMock(return_value=memory_backend)

    # 오래된 이벤트 생성 (30일 전)
    old_timestamp = (datetime.now(UTC) - timedelta(days=30)).isoformat()

    old_trigger = CascadeTrigger(
        trigger_type="OLD_EVENT",
        event_id="evt-old",
        details={},
    )

    old_event = CascadeEvent(
        id="cascade-old-001",
        trigger=old_trigger,
        effects=[],
        namespace="test",
        timestamp=old_timestamp,
    )
    old_event.current_hash = old_event.calculate_hash()

    auditor._save_cascade_event(old_event)
    auditor._add_to_index("test", old_event.id)

    # 최근 이벤트도 생성
    auditor.record(
        trigger_type="RECENT_EVENT",
        trigger_details={},
        effects=[],
        namespace="test",
    )

    return auditor


# =============================================================================
# Archive Task Tests
# =============================================================================


class TestArchiveCascadeEvents:
    """archive_cascade_events 테스트."""

    def test_archive_dry_run(self, mock_auditor):
        """Dry run 모드 테스트."""
        with patch(
            PATCH_GET_AUDITOR,
            return_value=mock_auditor,
        ):
            result = archive_cascade_events(
                namespace="test",
                older_than_days=7,
                dry_run=True,
            )

        assert result["status"] == "dry_run"
        assert result["namespace"] == "test"
        assert result["archived"] == 0
        assert "cutoff_time" in result

    def test_archive_no_old_events(self, mock_auditor):
        """오래된 이벤트 없는 경우."""
        with patch(
            PATCH_GET_AUDITOR,
            return_value=mock_auditor,
        ):
            result = archive_cascade_events(
                namespace="test",
                older_than_days=7,
                dry_run=True,
            )

        assert result["events_to_archive"] == 0

    def test_archive_with_old_events(self, old_events_auditor):
        """오래된 이벤트 있는 경우."""
        with patch(
            PATCH_GET_AUDITOR,
            return_value=old_events_auditor,
        ):
            result = archive_cascade_events(
                namespace="test",
                older_than_days=7,
                dry_run=True,
            )

        # 30일 전 이벤트 1개가 7일 기준 초과
        assert result["events_to_archive"] == 1


# =============================================================================
# Purge Task Tests
# =============================================================================


class TestPurgeOldCascadeEvents:
    """purge_old_cascade_events 테스트."""

    def test_purge_default_dry_run(self, mock_auditor):
        """기본값 dry_run=True 확인."""
        with patch(
            PATCH_GET_AUDITOR,
            return_value=mock_auditor,
        ):
            result = purge_old_cascade_events(
                namespace="test",
                older_than_days=365,
            )

        assert result["status"] == "dry_run"
        assert result["purged"] == 0

    def test_purge_no_old_events(self, mock_auditor):
        """오래된 이벤트 없는 경우."""
        with patch(
            PATCH_GET_AUDITOR,
            return_value=mock_auditor,
        ):
            result = purge_old_cascade_events(
                namespace="test",
                older_than_days=1,
                dry_run=True,
            )

        # 모든 이벤트가 1일 미만
        assert result["events_to_purge"] == 0

    def test_purge_with_old_events_dry_run(self, old_events_auditor):
        """오래된 이벤트 삭제 대상 확인 (dry run)."""
        with patch(
            PATCH_GET_AUDITOR,
            return_value=old_events_auditor,
        ):
            result = purge_old_cascade_events(
                namespace="test",
                older_than_days=7,
                dry_run=True,
            )

        # 30일 전 이벤트 1개
        assert result["events_to_purge"] == 1
        assert result["status"] == "dry_run"


# =============================================================================
# Checkpoint Task Tests
# =============================================================================


class TestCreateCascadeDailyCheckpoint:
    """create_cascade_daily_checkpoint 테스트."""

    def test_create_checkpoint(self, mock_auditor):
        """체크포인트 생성 테스트."""
        with (
            patch(
                PATCH_GET_AUDITOR,
                return_value=mock_auditor,
            ),
            patch(
                "baldur.adapters.redis.get_redis_client",
                side_effect=Exception("redis not available"),
            ),
        ):
            result = create_cascade_daily_checkpoint(namespace="test")

        assert result is not None
        assert result["namespace"] == "test"
        assert result["event_count"] == 5
        assert result["last_hash"] is not None
        assert "verified_at" in result

    def test_create_checkpoint_empty_namespace(self, mock_auditor):
        """빈 네임스페이스 체크포인트 생성."""
        with (
            patch(
                PATCH_GET_AUDITOR,
                return_value=mock_auditor,
            ),
            patch(
                "baldur.adapters.redis.get_redis_client",
                side_effect=Exception("redis not available"),
            ),
        ):
            result = create_cascade_daily_checkpoint(namespace="empty")

        assert result["event_count"] == 0
        assert result["last_hash"] is None


# =============================================================================
# Integrity Verification Task Tests
# =============================================================================


class TestVerifyCascadeChainIntegrity:
    """verify_cascade_chain_integrity 테스트."""

    def test_verify_with_checkpoint(self, mock_auditor):
        """체크포인트 사용 검증."""
        with patch(
            PATCH_GET_AUDITOR,
            return_value=mock_auditor,
        ):
            result = verify_cascade_chain_integrity(
                namespace="test",
                use_checkpoint=True,
            )

        assert result["valid"] is True
        assert "checked" in result

    def test_verify_without_checkpoint(self, mock_auditor):
        """체크포인트 미사용 전체 검증."""
        with patch(
            PATCH_GET_AUDITOR,
            return_value=mock_auditor,
        ):
            result = verify_cascade_chain_integrity(
                namespace="test",
                use_checkpoint=False,
            )

        assert result["valid"] is True
        assert result["checked"] == 5


# =============================================================================
# Fallback Recovery Task Tests
# =============================================================================


class TestRecoverCascadeFromFallback:
    """recover_cascade_from_fallback 테스트."""

    def test_recover_no_fallback_file(self, mock_auditor):
        """폴백 파일 없는 경우."""
        with patch.object(Path, "exists", return_value=False):
            with patch(
                PATCH_GET_AUDITOR,
                return_value=mock_auditor,
            ):
                result = recover_cascade_from_fallback(namespace="test")

        assert result["status"] == "no_wal_data"
        assert result["recovered"] == 0

    def test_recover_dry_run(self, mock_auditor):
        """Dry run 모드 테스트."""
        fallback_data = [
            {
                "id": "cascade-001",
                "namespace": "test",
                "trigger": {"trigger_type": "TEST", "event_id": "evt-1", "details": {}},
                "effects": [],
                "timestamp": "2026-01-20T00:00:00Z",
            },
        ]
        fallback_content = "\n".join(json.dumps(d) for d in fallback_data)

        with patch.object(Path, "exists", return_value=True):
            with patch("builtins.open", mock_open(read_data=fallback_content)):
                with patch(
                    PATCH_GET_AUDITOR,
                    return_value=mock_auditor,
                ):
                    result = recover_cascade_from_fallback(
                        namespace="test",
                        dry_run=True,
                    )

        assert result["status"] == "dry_run"
        assert result["entries_to_recover"] == 1


# =============================================================================
# Celery Beat Schedule Tests
# =============================================================================


class TestCeleryBeatSchedule:
    """Celery Beat 스케줄 정의 테스트."""

    def test_schedule_defined(self):
        """스케줄이 정의되어 있는지 확인."""
        assert "archive-cascade-to-postgres" in CASCADE_CLEANUP_SCHEDULE
        assert "create-cascade-daily-checkpoint" in CASCADE_CLEANUP_SCHEDULE
        assert "verify-cascade-chain-integrity" in CASCADE_CLEANUP_SCHEDULE

    def test_schedule_has_task(self):
        """각 스케줄에 task가 정의되어 있는지 확인."""
        for name, config in CASCADE_CLEANUP_SCHEDULE.items():
            assert "task" in config, f"{name} missing 'task'"

    def test_archive_schedule_kwargs(self):
        """archive 스케줄 kwargs 확인."""
        config = CASCADE_CLEANUP_SCHEDULE["archive-cascade-to-postgres"]

        assert "kwargs" in config
        assert config["kwargs"]["older_than_days"] == 7


# =============================================================================
# Integration Tests (Local)
# =============================================================================


class TestCleanupTasksIntegration:
    """정리 태스크 통합 테스트."""

    def test_full_workflow(self, mock_auditor):
        """전체 워크플로우 테스트."""
        with (
            patch(
                PATCH_GET_AUDITOR,
                return_value=mock_auditor,
            ),
            patch(
                "baldur.adapters.redis.get_redis_client",
                side_effect=Exception("redis not available"),
            ),
        ):
            # 1. 체크포인트 생성
            checkpoint = create_cascade_daily_checkpoint(namespace="test")
            assert checkpoint["event_count"] == 5

            # 2. 무결성 검증
            integrity = verify_cascade_chain_integrity(namespace="test")
            assert integrity["valid"] is True

            # 3. 아카이브 (dry run)
            archive = archive_cascade_events(
                namespace="test",
                older_than_days=7,
                dry_run=True,
            )
            assert archive["status"] == "dry_run"

            # 4. 삭제 (dry run)
            purge = purge_old_cascade_events(
                namespace="test",
                older_than_days=365,
                dry_run=True,
            )
            assert purge["status"] == "dry_run"
