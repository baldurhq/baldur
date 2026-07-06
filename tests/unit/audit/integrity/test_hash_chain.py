"""
Hash Chain 및 Checkpoint 단위 테스트 (Phase 3).

Tests:
- 체크포인트 생성 및 조회
- 체크포인트 기반 무결성 검증
- 특정 시각 이후 이벤트 조회
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from baldur.audit.cascade_auditor import (
    CascadeEventAuditor,
    reset_cascade_auditor,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def memory_backend():
    """메모리 백엔드 fixture."""
    from baldur.core.state_backend import MemoryStateBackend

    return MemoryStateBackend()


@pytest.fixture
def cascade_auditor(memory_backend):
    """CascadeEventAuditor fixture with memory backend."""
    reset_cascade_auditor()

    auditor = CascadeEventAuditor()
    auditor._get_backend = MagicMock(return_value=memory_backend)

    return auditor


@pytest.fixture
def populated_auditor(cascade_auditor):
    """
    여러 Cascade Event가 등록된 auditor fixture.

    5개의 이벤트를 생성하여 체인을 구성합니다.
    """
    for i in range(5):
        cascade_auditor.record(
            trigger_type=f"TEST_TRIGGER_{i}",
            trigger_details={"index": i},
            effects=[
                {"action_type": f"ACTION_{i}", "success": True},
            ],
            namespace="test",
            triggered_by="test",
        )

    return cascade_auditor


# =============================================================================
# Checkpoint Creation Tests
# =============================================================================


class TestCreateCheckpoint:
    """체크포인트 생성 테스트."""

    def test_create_checkpoint_empty_namespace(self, cascade_auditor):
        """빈 네임스페이스에서 체크포인트 생성."""
        checkpoint = cascade_auditor.create_checkpoint("empty")

        assert checkpoint is not None
        assert checkpoint["namespace"] == "empty"
        assert checkpoint["event_count"] == 0
        assert checkpoint["last_hash"] is None
        assert "verified_at" in checkpoint

    def test_create_checkpoint_with_events(self, populated_auditor):
        """이벤트가 있는 네임스페이스에서 체크포인트 생성."""
        checkpoint = populated_auditor.create_checkpoint("test")

        assert checkpoint is not None
        assert checkpoint["namespace"] == "test"
        assert checkpoint["event_count"] == 5
        assert checkpoint["last_hash"] is not None
        assert len(checkpoint["last_hash"]) == 64  # SHA-256 hex
        assert "verified_at" in checkpoint
        assert checkpoint["version"] == "1.0"

    def test_create_checkpoint_updates_existing(self, populated_auditor):
        """체크포인트가 업데이트되는지 확인."""
        # 첫 번째 체크포인트
        checkpoint1 = populated_auditor.create_checkpoint("test")

        # 새 이벤트 추가
        populated_auditor.record(
            trigger_type="NEW_EVENT",
            trigger_details={},
            effects=[],
            namespace="test",
        )

        # 두 번째 체크포인트
        checkpoint2 = populated_auditor.create_checkpoint("test")

        assert checkpoint2["event_count"] == 6
        assert checkpoint2["last_hash"] != checkpoint1["last_hash"]
        assert checkpoint2["verified_at"] >= checkpoint1["verified_at"]


# =============================================================================
# Get Checkpoint Tests
# =============================================================================


class TestGetCheckpoint:
    """체크포인트 조회 테스트."""

    def test_get_checkpoint_not_exists(self, cascade_auditor):
        """존재하지 않는 체크포인트 조회."""
        result = cascade_auditor.get_checkpoint("nonexistent")
        assert result is None

    def test_get_checkpoint_exists(self, populated_auditor):
        """존재하는 체크포인트 조회."""
        # 체크포인트 생성
        created = populated_auditor.create_checkpoint("test")

        # 조회
        retrieved = populated_auditor.get_checkpoint("test")

        assert retrieved is not None
        assert retrieved["last_hash"] == created["last_hash"]
        assert retrieved["event_count"] == created["event_count"]
        assert retrieved["verified_at"] == created["verified_at"]


# =============================================================================
# Verify Chain Integrity From Checkpoint Tests
# =============================================================================


class TestVerifyChainIntegrityFromCheckpoint:
    """체크포인트 기반 무결성 검증 테스트."""

    def test_verify_no_checkpoint_falls_back(self, populated_auditor):
        """체크포인트 없으면 전체 검증으로 폴백."""
        result = populated_auditor.verify_chain_integrity_from_checkpoint("test")

        assert result["valid"] is True
        assert result["checked"] == 5
        # from_checkpoint 없거나 None

    def test_verify_with_checkpoint_no_new_events(self, populated_auditor):
        """체크포인트 이후 새 이벤트 없음."""
        # 체크포인트 생성
        populated_auditor.create_checkpoint("test")

        # 검증 (새 이벤트 없음)
        result = populated_auditor.verify_chain_integrity_from_checkpoint("test")

        assert result["valid"] is True
        assert result["checked"] == 0
        assert "from_checkpoint" in result

    def test_verify_with_checkpoint_new_events(self, populated_auditor):
        """체크포인트 이후 새 이벤트 검증."""
        # 체크포인트 생성
        populated_auditor.create_checkpoint("test")

        # 새 이벤트 추가
        populated_auditor.record(
            trigger_type="NEW_EVENT_1",
            trigger_details={},
            effects=[{"action_type": "ACTION", "success": True}],
            namespace="test",
        )
        populated_auditor.record(
            trigger_type="NEW_EVENT_2",
            trigger_details={},
            effects=[],
            namespace="test",
        )

        # 검증 (2개만 검증)
        result = populated_auditor.verify_chain_integrity_from_checkpoint("test")

        assert result["valid"] is True
        assert result["checked"] == 2
        assert "from_checkpoint" in result

    def test_verify_empty_namespace(self, cascade_auditor):
        """빈 네임스페이스 검증."""
        result = cascade_auditor.verify_chain_integrity_from_checkpoint("empty")

        assert result["valid"] is True
        assert result["checked"] == 0


# =============================================================================
# Hash Chain Integrity Tests
# =============================================================================


class TestHashChainIntegrity:
    """해시 체인 무결성 테스트."""

    def test_chain_integrity_valid(self, populated_auditor):
        """유효한 체인 검증."""
        result = populated_auditor.verify_chain_integrity("test")

        assert result["valid"] is True
        assert result["checked"] == 5
        assert result["errors"] == []

    def test_hash_values_unique(self, populated_auditor):
        """각 이벤트의 해시가 고유한지 확인."""
        events = populated_auditor.get_recent_events("test")
        hashes = [e.current_hash for e in events]

        assert len(hashes) == len(set(hashes))  # 모두 고유

    def test_previous_hash_chain(self, populated_auditor):
        """이전 해시가 체인으로 연결되는지 확인."""
        events = populated_auditor.get_recent_events("test")

        # 최신순 정렬이므로 events[0]이 최신
        for i in range(len(events) - 1):
            newer = events[i]
            older = events[i + 1]

            assert newer.previous_hash == older.current_hash


# =============================================================================
# Get Events After Timestamp Tests
# =============================================================================


class TestGetEventsAfterTimestamp:
    """특정 시각 이후 이벤트 조회 테스트."""

    def test_get_events_after_past_timestamp(self, populated_auditor):
        """과거 시각 이후 모든 이벤트 조회."""
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()

        events = populated_auditor.get_events_after_timestamp("test", past)

        assert len(events) == 5

    def test_get_events_after_future_timestamp(self, populated_auditor):
        """미래 시각 이후 이벤트 없음."""
        future = (datetime.now(UTC) + timedelta(days=1)).isoformat()

        events = populated_auditor.get_events_after_timestamp("test", future)

        assert len(events) == 0

    def test_get_events_invalid_timestamp(self, populated_auditor):
        """잘못된 시각 형식은 전체 반환."""
        events = populated_auditor.get_events_after_timestamp("test", "invalid")

        assert len(events) == 5


# =============================================================================
# CascadeRetentionConfig Tests
# =============================================================================


class TestCascadeRetentionConfig:
    """Cascade 보관 정책 설정 테스트."""

    def test_default_config(self):
        """기본 설정값 확인."""
        from baldur.audit.cascade_config import (
            DEFAULT_CASCADE_RETENTION_CONFIG,
        )

        config = DEFAULT_CASCADE_RETENTION_CONFIG

        assert config.hot_retention_days == 7
        assert config.hot_max_count == 10000
        assert config.warm_retention_days == 90
        assert config.cold_retention_days == 365
        assert config.index_retention_days == 30
        assert config.anchor_retention_days == 90

    def test_custom_config(self):
        """커스텀 설정값."""
        from baldur.audit.cascade_config import CascadeRetentionConfig

        config = CascadeRetentionConfig(
            hot_retention_days=3,
            hot_max_count=5000,
            warm_retention_days=60,
            cold_retention_days=180,
        )

        assert config.hot_retention_days == 3
        assert config.hot_max_count == 5000
        assert config.warm_retention_days == 60
        assert config.cold_retention_days == 180

    def test_get_retention_config(self):
        """설정 로더 테스트."""
        from baldur.audit.cascade_config import get_cascade_retention_config

        config = get_cascade_retention_config()

        assert config is not None
        assert config.hot_retention_days > 0
        assert config.warm_retention_days > config.hot_retention_days
        assert config.cold_retention_days > config.warm_retention_days


# =============================================================================
# Checkpoint Key Pattern Tests
# =============================================================================


class TestCheckpointKeyPattern:
    """체크포인트 키 패턴 테스트."""

    def test_checkpoint_key_format(self):
        """체크포인트 키 형식 확인."""
        auditor = CascadeEventAuditor()

        key = auditor.CHECKPOINT_KEY.format(namespace="test")

        assert key == "baldur:test:audit:cascade_checkpoint"

    def test_checkpoint_key_with_namespace(self):
        """다양한 네임스페이스 키 형식."""
        auditor = CascadeEventAuditor()

        assert (
            auditor.CHECKPOINT_KEY.format(namespace="seoul")
            == "baldur:seoul:audit:cascade_checkpoint"
        )
        assert (
            auditor.CHECKPOINT_KEY.format(namespace="global")
            == "baldur:global:audit:cascade_checkpoint"
        )
