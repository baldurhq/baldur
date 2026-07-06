"""
RedisCacheAdapter.reconnect() 단위 테스트.

커넥션 풀 리셋 및 재연결 동작 검증.
"""

from unittest.mock import MagicMock

import pytest

from baldur.adapters.cache.redis_adapter import RedisCacheAdapter


class TestReconnectBehavior:
    """RedisCacheAdapter.reconnect() 동작 검증."""

    @pytest.fixture
    def mock_redis_client(self):
        """Mock Redis 클라이언트."""
        client = MagicMock()
        client.connection_pool = MagicMock()
        return client

    @pytest.fixture
    def adapter(self, mock_redis_client):
        """테스트 대상 RedisCacheAdapter (mock 클라이언트 주입)."""
        return RedisCacheAdapter(client=mock_redis_client)

    def test_reconnect_disconnects_pool_then_pings(self, adapter, mock_redis_client):
        """reconnect()는 connection_pool.disconnect() 후 ping()을 호출한다."""
        mock_redis_client.ping.return_value = True

        result = adapter.reconnect()

        assert result is True
        mock_redis_client.connection_pool.disconnect.assert_called_once()
        mock_redis_client.ping.assert_called_once()

    def test_reconnect_returns_false_on_ping_failure(self, adapter, mock_redis_client):
        """ping()이 False를 반환하면 reconnect()도 False를 반환한다."""
        mock_redis_client.ping.return_value = False

        result = adapter.reconnect()

        assert result is False
        mock_redis_client.connection_pool.disconnect.assert_called_once()

    def test_reconnect_returns_false_on_disconnect_exception(
        self, adapter, mock_redis_client
    ):
        """disconnect() 중 예외 발생 시 False를 반환한다."""
        mock_redis_client.connection_pool.disconnect.side_effect = Exception(
            "pool error"
        )

        result = adapter.reconnect()

        assert result is False

    def test_reconnect_returns_false_on_ping_exception(
        self, adapter, mock_redis_client
    ):
        """ping() 중 예외 발생 시 False를 반환한다."""
        mock_redis_client.connection_pool.disconnect.return_value = None
        mock_redis_client.ping.side_effect = ConnectionError("refused")

        result = adapter.reconnect()

        assert result is False
