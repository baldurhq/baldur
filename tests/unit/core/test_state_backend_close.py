"""
Tests for StateBackend close() (386 §4).

Test Categories:
    A. StateBackend ABC close() — default no-op
    B. RedisStateBackend close() — connection close, exception safety, idempotency
"""

from unittest.mock import MagicMock, patch

from baldur.core.state_backend import (
    FileStateBackend,
    MemoryStateBackend,
    RedisStateBackend,
    StateBackend,
)

# =============================================================================
# A. StateBackend ABC close() — default no-op
# =============================================================================


class TestStateBackendCloseContract:
    """StateBackend ABC close() default behavior (386 §4)."""

    def test_close_exists_on_abc(self):
        """StateBackend has close() method."""
        assert hasattr(StateBackend, "close")

    def test_memory_backend_close_is_noop(self):
        """MemoryStateBackend.close() is a no-op (inherited default)."""
        backend = MemoryStateBackend()
        backend.close()
        # Backend still works after close (no-op)
        backend.set("key", {"val": 1})
        assert backend.get("key") == {"val": 1}

    def test_file_backend_close_is_noop(self, tmp_path):
        """FileStateBackend.close() is a no-op (inherited default)."""
        backend = FileStateBackend(directory=tmp_path / "test_state")
        backend.close()
        backend.set("key", {"val": 1})
        assert backend.get("key") == {"val": 1}


# =============================================================================
# B. RedisStateBackend close() — connection close, exception, idempotency
# =============================================================================


class TestRedisStateBackendCloseBehavior:
    """RedisStateBackend.close() behavior (386 §4)."""

    def _make_backend_with_mock_client(self):
        """Create RedisStateBackend with _initialize_client bypassed."""
        mock_client = MagicMock()
        with patch.object(RedisStateBackend, "_initialize_client"):
            backend = RedisStateBackend(redis_url="redis://test:6379/0")
        backend._client = mock_client
        return backend, mock_client

    def test_close_calls_client_close(self):
        """close() calls underlying Redis client.close()."""
        backend, mock_client = self._make_backend_with_mock_client()

        backend.close()

        mock_client.close.assert_called_once()

    def test_close_sets_client_to_none(self):
        """close() sets _client to None after closing."""
        backend, mock_client = self._make_backend_with_mock_client()

        backend.close()

        assert backend._client is None

    def test_close_idempotent_double_call(self):
        """Second close() is safe — client already None, no exception."""
        backend, mock_client = self._make_backend_with_mock_client()

        backend.close()
        backend.close()  # Should not raise

        mock_client.close.assert_called_once()

    def test_close_handles_exception_gracefully(self):
        """Exception from client.close() is caught, _client still set to None."""
        backend, mock_client = self._make_backend_with_mock_client()
        mock_client.close.side_effect = ConnectionError("connection lost")

        backend.close()  # Should not raise

        assert backend._client is None
