"""
Tests for X-Test Session Manager.

XTestSessionManager 클래스 테스트:
- 세션 생성
- 세션 조회
- 만료 세션 감지
- 아티팩트 등록
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest


class TestXTestSessionMetadata:
    """XTestSessionMetadata 데이터클래스 테스트."""

    def test_metadata_creation(self):
        """메타데이터 생성 검증."""
        from baldur.services.xtest_session_manager import XTestSessionMetadata

        now = datetime.now(UTC)
        metadata = XTestSessionMetadata(
            session_id="test-001",
            created_at=now,
            ttl_hours=4,
            user="test_user",
            components=["cb", "dlq"],
            artifacts=["artifact-1", "artifact-2"],
        )

        assert metadata.session_id == "test-001"
        assert metadata.ttl_hours == 4
        assert metadata.user == "test_user"
        assert len(metadata.components) == 2
        assert len(metadata.artifacts) == 2

    def test_metadata_expires_at(self):
        """만료 시간 계산 검증."""
        from baldur.services.xtest_session_manager import XTestSessionMetadata

        now = datetime.now(UTC)
        metadata = XTestSessionMetadata(
            session_id="test-001",
            created_at=now,
            ttl_hours=4,
            user="test_user",
        )

        expected_expires = now + timedelta(hours=4)
        assert abs((metadata.expires_at - expected_expires).total_seconds()) < 1

    def test_metadata_is_expired_false(self):
        """미만료 세션 검증."""
        from baldur.services.xtest_session_manager import XTestSessionMetadata

        now = datetime.now(UTC)
        metadata = XTestSessionMetadata(
            session_id="test-001",
            created_at=now,
            ttl_hours=4,
            user="test_user",
        )

        assert metadata.is_expired is False

    def test_metadata_is_expired_true(self):
        """만료 세션 검증."""
        from baldur.services.xtest_session_manager import XTestSessionMetadata

        # 5시간 전에 생성된 세션 (4시간 TTL)
        past = datetime.now(UTC) - timedelta(hours=5)
        metadata = XTestSessionMetadata(
            session_id="test-001",
            created_at=past,
            ttl_hours=4,
            user="test_user",
        )

        assert metadata.is_expired is True

    def test_metadata_to_dict(self):
        """딕셔너리 변환 검증."""
        from baldur.services.xtest_session_manager import XTestSessionMetadata

        now = datetime.now(UTC)
        metadata = XTestSessionMetadata(
            session_id="test-001",
            created_at=now,
            ttl_hours=4,
            user="test_user",
            components=["cb"],
            artifacts=["a1"],
        )

        result = metadata.to_dict()

        assert result["session_id"] == "test-001"
        assert result["ttl_hours"] == 4
        assert result["user"] == "test_user"
        assert "expires_at" in result
        assert "is_expired" in result

    def test_metadata_from_dict(self):
        """딕셔너리에서 생성 검증."""
        from baldur.services.xtest_session_manager import XTestSessionMetadata

        now = datetime.now(UTC)
        data = {
            "session_id": "test-001",
            "created_at": now.isoformat(),
            "ttl_hours": 4,
            "user": "test_user",
            "components": ["cb", "dlq"],
            "artifacts": ["a1"],
        }

        metadata = XTestSessionMetadata.from_dict(data)

        assert metadata.session_id == "test-001"
        assert metadata.ttl_hours == 4
        assert metadata.user == "test_user"
        assert len(metadata.components) == 2


class TestXTestSessionManagerWithMockRedis:
    """Mock Redis를 사용한 XTestSessionManager 테스트."""

    @pytest.fixture
    def mock_redis(self):
        """Mock Redis 클라이언트."""
        redis_mock = MagicMock()
        redis_mock.hgetall.return_value = {}
        redis_mock.smembers.return_value = set()
        redis_mock.exists.return_value = False
        return redis_mock

    @pytest.fixture
    def session_manager(self, mock_redis):
        """테스트용 세션 매니저."""
        from baldur.services.xtest_session_manager import (
            XTestSessionManager,
            reset_xtest_session_manager,
        )

        reset_xtest_session_manager()
        manager = XTestSessionManager(redis_client=mock_redis)
        return manager

    def test_create_session(self, session_manager, mock_redis):
        """세션 생성 검증."""
        metadata = session_manager.create_session(
            session_id="test-001",
            user="test_user",
            ttl_hours=4,
        )

        assert metadata.session_id == "test-001"
        assert metadata.user == "test_user"
        assert metadata.ttl_hours == 4

        # Redis hset 호출 확인
        mock_redis.hset.assert_called()
        mock_redis.sadd.assert_called()

    def test_create_session_with_default_ttl(self, session_manager, mock_redis):
        """기본 TTL로 세션 생성 검증."""
        metadata = session_manager.create_session(
            session_id="test-002",
            user="test_user",
        )

        # 기본 TTL은 settings에서 가져옴 (4시간)
        assert metadata.ttl_hours == 4

    def test_get_session_not_found(self, session_manager, mock_redis):
        """존재하지 않는 세션 조회."""
        mock_redis.hgetall.return_value = {}

        result = session_manager.get_session("nonexistent")

        assert result is None

    def test_get_session_found(self, session_manager, mock_redis):
        """존재하는 세션 조회."""
        now = datetime.now(UTC)
        mock_redis.hgetall.return_value = {
            b"created_at": now.isoformat().encode(),
            b"ttl_hours": b"4",
            b"user": b"test_user",
            b"components": b'["cb", "dlq"]',
            b"artifacts": b'["a1"]',
        }

        result = session_manager.get_session("test-001")

        assert result is not None
        assert result.session_id == "test-001"
        assert result.user == "test_user"

    def test_register_artifact_creates_session_if_not_exists(
        self, session_manager, mock_redis
    ):
        """세션이 없으면 생성 후 아티팩트 등록."""
        mock_redis.hgetall.return_value = {}
        mock_redis.exists.return_value = False

        session_manager.register_artifact(
            session_id="test-001",
            artifact_id="artifact-1",
            component="dlq",
        )

        # 세션 생성 확인
        assert mock_redis.hset.called

    def test_get_active_sessions(self, session_manager, mock_redis):
        """활성 세션 목록 조회."""
        mock_redis.smembers.return_value = {b"session-1", b"session-2", b"session-3"}

        result = session_manager.get_active_sessions()

        assert len(result) == 3
        assert "session-1" in result
        assert "session-2" in result
        assert "session-3" in result

    def test_get_sessions_count(self, session_manager, mock_redis):
        """활성 세션 수 조회."""
        mock_redis.smembers.return_value = {b"s1", b"s2"}

        count = session_manager.get_sessions_count()

        assert count == 2

    def test_delete_session(self, session_manager, mock_redis):
        """세션 삭제."""
        result = session_manager.delete_session("test-001")

        assert result is True
        mock_redis.delete.assert_called()
        mock_redis.srem.assert_called()


class TestXTestSessionManagerFactory:
    """세션 매니저 팩토리 함수 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.services.xtest_session_manager import (
            reset_xtest_session_manager,
        )

        reset_xtest_session_manager()
        yield
        reset_xtest_session_manager()

    def test_get_xtest_session_manager_singleton(self):
        """싱글톤 동작 검증."""
        from baldur.services.xtest_session_manager import (
            get_xtest_session_manager,
        )

        manager1 = get_xtest_session_manager()
        manager2 = get_xtest_session_manager()

        assert manager1 is manager2

    def test_reset_xtest_session_manager(self):
        """캐시 초기화 검증."""
        from baldur.services.xtest_session_manager import (
            get_xtest_session_manager,
            reset_xtest_session_manager,
        )

        manager1 = get_xtest_session_manager()
        reset_xtest_session_manager()
        manager2 = get_xtest_session_manager()

        assert manager1 is not manager2
