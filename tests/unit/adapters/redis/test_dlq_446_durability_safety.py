"""
DLQ Durability, Observability & Safety Hardening Tests (446).

Test targets:
    - baldur.adapters.redis.dlq_lifecycle (acquire path, timing)
    - baldur.adapters.redis.dlq (Redis access delegation)

Test Categories:
    Behavior: Python acquire path, timing
    Behavior: Redis access delegation

Note (#502 D5): the Lua-script-based acquire path was replaced with a
Python WATCH/MULTI/EXEC pipeline. Lua-specific tests previously hosted
here are gone with `dlq_lua.py`; the WATCH/MULTI/EXEC behavior is covered
by `test_dlq_sub_modules.py` (round-trip + CAS scenarios per #502).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# =============================================================================
# Fixtures
# =============================================================================


def _make_failed_op_data(**overrides):
    """Create a FailedOperationData with defaults."""
    from baldur.interfaces.repositories import (
        FailedOperationData,
        FailedOperationStatus,
    )

    defaults = {
        "id": 1,
        "domain": "payment",
        "failure_type": "PG_TIMEOUT",
        "status": FailedOperationStatus.PENDING.value,
        "error_message": "timeout",
        "retry_count": 0,
        "max_retries": 3,
    }
    defaults.update(overrides)
    return FailedOperationData(**defaults)


def _make_lifecycle():
    """Create RedisDLQLifecycle with mock repo, Redis access blocked."""
    from baldur.adapters.redis.dlq_lifecycle import RedisDLQLifecycle

    repo = MagicMock()
    repo._key_prefix = "dlq:"
    repo._pending_key = "dlq:pending"
    repo.PENDING_KEY = "dlq:pending"
    repo._make_key = MagicMock(side_effect=lambda id: f"dlq:{id}")
    repo._backend._get_full_key = MagicMock(side_effect=lambda k: f"baldur:{k}")
    repo._ensure_redis_available = MagicMock(return_value=False)
    repo._raw_redis_client = None

    lifecycle = RedisDLQLifecycle(repo)
    return lifecycle, repo


# =============================================================================
# Behavior Tests — try_acquire Python Fallback (degraded mode primary path)
# =============================================================================


class TestTryAcquirePythonFallbackBehavior:
    """try_acquire_for_replay Python fallback path (degraded mode primary)."""

    def test_python_fallback_returns_entry_on_success(self):
        """Python fallback acquires entry and returns updated data."""
        lifecycle, repo = _make_lifecycle()
        entry = _make_failed_op_data(id=10, retry_count=0, max_retries=3)
        updated = _make_failed_op_data(
            id=10, retry_count=1, max_retries=3, status="replaying"
        )
        repo.get_by_id = MagicMock(side_effect=[entry, updated])
        repo._update = MagicMock(return_value=True)

        result = lifecycle.try_acquire_for_replay(10, max_retries=3)

        assert result is not None
        assert result.status == "replaying"

    def test_python_fallback_returns_none_for_nonexistent_entry(self):
        """Returns None when entry doesn't exist."""
        lifecycle, repo = _make_lifecycle()
        repo.get_by_id = MagicMock(return_value=None)

        result = lifecycle.try_acquire_for_replay(99, max_retries=3)

        assert result is None

    def test_python_fallback_returns_none_when_not_pending(self):
        """Returns None when entry status is not 'pending'."""
        lifecycle, repo = _make_lifecycle()
        entry = _make_failed_op_data(id=10, status="replaying")
        repo.get_by_id = MagicMock(return_value=entry)

        result = lifecycle.try_acquire_for_replay(10, max_retries=3)

        assert result is None

    def test_python_fallback_returns_none_when_max_retries_exceeded(self):
        """Returns None when retry_count >= max_retries."""
        lifecycle, repo = _make_lifecycle()
        entry = _make_failed_op_data(id=10, retry_count=3, max_retries=3)
        repo.get_by_id = MagicMock(return_value=entry)

        result = lifecycle.try_acquire_for_replay(10, max_retries=3)

        assert result is None

    def test_python_fallback_returns_none_when_update_fails(self):
        """Returns None when _update returns False."""
        lifecycle, repo = _make_lifecycle()
        entry = _make_failed_op_data(id=10, retry_count=0, max_retries=3)
        repo.get_by_id = MagicMock(return_value=entry)
        repo._update = MagicMock(return_value=False)

        result = lifecycle.try_acquire_for_replay(10, max_retries=3)

        assert result is None


# =============================================================================
# Behavior Tests — Acquire Timing
# =============================================================================


class TestAcquireTimingBehavior:
    """try_acquire_for_replay timing measurement."""

    @patch("baldur.metrics.prometheus.get_metrics")
    def test_timing_observed_on_success(self, mock_get_metrics):
        """Histogram observe is called on successful acquire."""
        lifecycle, repo = _make_lifecycle()
        entry = _make_failed_op_data(id=10, retry_count=0, max_retries=3)
        updated = _make_failed_op_data(id=10, retry_count=1, status="replaying")
        repo.get_by_id = MagicMock(side_effect=[entry, updated])
        repo._update = MagicMock(return_value=True)

        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        lifecycle.try_acquire_for_replay(10, max_retries=3)

        mock_metrics.dlq.record_acquire_duration.assert_called_once()
        args = mock_metrics.dlq.record_acquire_duration.call_args[0]
        assert args[0] == "payment"
        assert isinstance(args[1], float)
        assert args[1] >= 0

    @patch("baldur.metrics.prometheus.get_metrics")
    def test_timing_observed_on_failure(self, mock_get_metrics):
        """Histogram observe is called even when acquire fails."""
        lifecycle, repo = _make_lifecycle()
        repo.get_by_id = MagicMock(return_value=None)

        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        lifecycle.try_acquire_for_replay(99, max_retries=3)

        mock_metrics.dlq.record_acquire_duration.assert_called_once()


# =============================================================================
# Behavior Tests — Redis Access Delegation
# =============================================================================


class TestRedisDLQAccessDelegationBehavior:
    """RedisDLQRepository._ensure_redis_available and _raw_redis_client."""

    def test_ensure_redis_available_delegates_to_backend(self):
        """_ensure_redis_available delegates to backend.ensure_redis()."""
        from baldur.adapters.redis.dlq import RedisDLQRepository

        with patch.object(RedisDLQRepository, "__init__", lambda self, **kw: None):
            repo = RedisDLQRepository.__new__(RedisDLQRepository)

        mock_backend = MagicMock()
        mock_backend.ensure_redis.return_value = True
        repo._backend = mock_backend

        assert repo._ensure_redis_available() is True
        mock_backend.ensure_redis.assert_called_once()

    def test_raw_redis_client_returns_none_when_no_redis(self):
        """_raw_redis_client returns None when backend exposes no raw client."""
        from baldur.adapters.redis.dlq import RedisDLQRepository

        with patch.object(RedisDLQRepository, "__init__", lambda self, **kw: None):
            repo = RedisDLQRepository.__new__(RedisDLQRepository)

        mock_backend = MagicMock()
        mock_backend.raw_redis_client = None
        repo._backend = mock_backend

        assert repo._raw_redis_client is None

    def test_raw_redis_client_returns_inner_client(self):
        """_raw_redis_client returns backend.raw_redis_client."""
        from baldur.adapters.redis.dlq import RedisDLQRepository

        with patch.object(RedisDLQRepository, "__init__", lambda self, **kw: None):
            repo = RedisDLQRepository.__new__(RedisDLQRepository)

        mock_inner = MagicMock()
        mock_backend = MagicMock()
        mock_backend.raw_redis_client = mock_inner
        repo._backend = mock_backend

        assert repo._raw_redis_client is mock_inner
