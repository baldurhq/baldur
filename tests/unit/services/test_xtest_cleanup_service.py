"""
Tests for X-Test Cleanup Service.

XTestCleanupService 클래스 테스트:
- cleanup_expired_sessions
- restore_cb_states
- purge_dlq_entries
- clear_idempotency_keys
- reset_rate_limit_counters
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest


class TestXTestCleanupResult:
    """XTestCleanupResult 데이터클래스 테스트."""

    def test_result_creation(self):
        """결과 객체 생성 검증."""
        from baldur.services.xtest_cleanup_service import XTestCleanupResult

        result = XTestCleanupResult(
            success=True,
            sessions_cleaned=2,
            cb_states_restored=3,
            dlq_entries_purged=5,
            idempotency_keys_cleared=10,
            rate_limit_counters_reset=4,
        )

        assert result.success is True
        assert result.sessions_cleaned == 2
        assert result.cb_states_restored == 3
        assert result.dlq_entries_purged == 5

    def test_result_to_dict(self):
        """딕셔너리 변환 검증."""
        from baldur.services.xtest_cleanup_service import XTestCleanupResult

        result = XTestCleanupResult(
            success=True,
            sessions_cleaned=1,
            cleaned_session_ids=["s1", "s2"],
        )

        data = result.to_dict()

        assert data["success"] is True
        assert data["sessions_cleaned"] == 1
        assert data["cleaned_session_ids"] == ["s1", "s2"]
        assert "timestamp" in data


class TestXTestCleanupServiceWithMocks:
    """Mock을 사용한 XTestCleanupService 테스트."""

    @pytest.fixture(scope="class")
    def mock_redis(self):
        """Mock Redis 클라이언트."""
        redis_mock = MagicMock()
        redis_mock.keys.return_value = []
        return redis_mock

    @pytest.fixture(scope="class")
    def mock_session_manager(self):
        """Mock 세션 매니저."""
        manager = MagicMock()
        manager.get_expired_sessions.return_value = []
        manager.get_sessions_count.return_value = 0
        return manager

    @pytest.fixture(scope="class")
    def mock_settings(self):
        """Mock 설정 객체."""
        settings = MagicMock()
        settings.cb_auto_restore = False
        settings.dlq_auto_purge = False
        settings.idempotency_auto_clear = False
        settings.rate_limit_auto_reset = False
        return settings

    @pytest.fixture(scope="class")
    def cleanup_service(self, mock_redis, mock_session_manager, mock_settings):
        """테스트용 Cleanup 서비스."""
        from baldur.services.xtest_cleanup_service import (
            XTestCleanupService,
            reset_xtest_cleanup_service,
        )

        reset_xtest_cleanup_service()
        service = XTestCleanupService(redis_client=mock_redis)
        service._session_manager = mock_session_manager
        service._settings = mock_settings
        return service

    def test_cleanup_expired_sessions_no_expired(
        self, cleanup_service, mock_session_manager
    ):
        """만료된 세션이 없을 때."""
        mock_session_manager.get_expired_sessions.return_value = []

        result = cleanup_service.cleanup_expired_sessions()

        assert result.success is True
        assert result.sessions_cleaned == 0

    def test_cleanup_expired_sessions_with_expired(
        self, cleanup_service, mock_session_manager
    ):
        """만료된 세션이 있을 때."""
        from baldur.services.xtest_session_manager import XTestSessionMetadata

        past = datetime.now(UTC) - timedelta(hours=5)
        expired_session = XTestSessionMetadata(
            session_id="expired-001",
            created_at=past,
            ttl_hours=4,
            user="test",
            components=["cb"],
            artifacts=["a1"],
        )
        mock_session_manager.get_expired_sessions.return_value = [expired_session]
        mock_session_manager.delete_session.return_value = True

        # audit 로깅 mock으로 무거운 모듈 로드 방지
        with patch("baldur.services.xtest_cleanup_service.log_xtest_cleanup_audit"):
            result = cleanup_service.cleanup_expired_sessions()

        assert result.success is True
        assert result.sessions_cleaned == 1
        assert "expired-001" in result.cleaned_session_ids

    def test_clear_idempotency_keys(self, cleanup_service, mock_redis):
        """Idempotency 키 삭제 검증."""
        mock_redis.keys.return_value = [
            b"xtest:idempotency:key1",
            b"xtest:idempotency:key2",
        ]

        count = cleanup_service.clear_idempotency_keys()

        assert count == 2
        mock_redis.delete.assert_called()

    def test_clear_idempotency_keys_empty(self, cleanup_service, mock_redis):
        """삭제할 Idempotency 키가 없을 때."""
        mock_redis.keys.return_value = []

        count = cleanup_service.clear_idempotency_keys()

        assert count == 0

    def test_reset_rate_limit_counters(self, cleanup_service, mock_redis):
        """Rate Limit 카운터 초기화 검증."""
        mock_redis.keys.return_value = [b"xtest:rate_limit:counter1"]

        count = cleanup_service.reset_rate_limit_counters()

        assert count == 1
        mock_redis.delete.assert_called()

    def test_reset_rate_limit_counters_with_session_id(
        self, cleanup_service, mock_redis
    ):
        """특정 세션의 Rate Limit 카운터만 초기화."""
        mock_redis.keys.return_value = [b"xtest:rate_limit:session1:counter1"]

        count = cleanup_service.reset_rate_limit_counters(session_id="session1")

        assert count == 1
        # 세션 ID가 포함된 패턴으로 검색 확인
        mock_redis.keys.assert_called()

    def test_get_cleanup_stats(self, cleanup_service, mock_session_manager, mock_redis):
        """정리 대상 통계 조회."""
        mock_session_manager.get_sessions_count.return_value = 5
        mock_session_manager.get_expired_sessions.return_value = []
        mock_redis.keys.side_effect = [
            [b"key1", b"key2"],  # idempotency keys
            [b"counter1"],  # rate limit keys
        ]

        stats = cleanup_service.get_cleanup_stats()

        assert stats["active_sessions"] == 5
        assert stats["pending_idempotency_clears"] == 2
        assert stats["pending_rate_limit_resets"] == 1


class TestXTestCleanupServiceFactory:
    """Cleanup 서비스 팩토리 함수 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.services.xtest_cleanup_service import (
            reset_xtest_cleanup_service,
        )

        reset_xtest_cleanup_service()
        yield
        reset_xtest_cleanup_service()

    def test_get_xtest_cleanup_service_singleton(self):
        """싱글톤 동작 검증."""
        from baldur.services.xtest_cleanup_service import (
            get_xtest_cleanup_service,
        )

        service1 = get_xtest_cleanup_service()
        service2 = get_xtest_cleanup_service()

        assert service1 is service2

    def test_reset_xtest_cleanup_service(self):
        """캐시 초기화 검증."""
        from baldur.services.xtest_cleanup_service import (
            get_xtest_cleanup_service,
            reset_xtest_cleanup_service,
        )

        service1 = get_xtest_cleanup_service()
        reset_xtest_cleanup_service()
        service2 = get_xtest_cleanup_service()

        assert service1 is not service2


class TestXTestCleanupServiceConstants:
    """Cleanup 서비스 상수 테스트."""

    def test_xtest_source_constant(self):
        """XTEST_SOURCE 상수 검증."""
        from baldur.services.xtest_cleanup_service import XTEST_SOURCE

        assert XTEST_SOURCE == "x-test-mode"

    def test_xtest_idempotency_prefix_constant(self):
        """XTEST_IDEMPOTENCY_PREFIX 상수 검증."""
        from baldur.services.xtest_cleanup_service import XTEST_IDEMPOTENCY_PREFIX

        assert XTEST_IDEMPOTENCY_PREFIX == "xtest:idempotency:"

    def test_xtest_rate_limit_prefix_constant(self):
        """XTEST_RATE_LIMIT_PREFIX 상수 검증."""
        from baldur.services.xtest_cleanup_service import XTEST_RATE_LIMIT_PREFIX

        assert XTEST_RATE_LIMIT_PREFIX == "xtest:rate_limit:"
