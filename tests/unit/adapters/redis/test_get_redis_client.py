"""
get_redis_client() TTL-based negative caching 단위 테스트.

테스트 분류 (UNIT_TEST_GUIDELINES §0):
- Contract: negative caching 설계 상수 계약값 (_REDIS_RETRY_INTERVAL)
- Behavior: 캐싱, negative caching, TTL 만료, reset 동작 검증

참조 소스:
- adapters/redis/__init__.py (get_redis_client, reset_redis_client, _try_acquire_redis_client)

검증 기법:
- §8.3 멱등성 — 동일 호출 N회에서 동일 결과 (캐싱)
- §8.4 부수효과 — 모듈 레벨 상태 변경 검증
- §8.5 의존성 상호작용 — _try_acquire_redis_client 호출 횟수/패턴
- §8.10 시간 의존성 — time.monotonic 모킹으로 TTL 경과 시뮬레이션
- §8.9 싱글톤/라이프사이클 — reset_redis_client()로 전체 상태 초기화
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

import baldur.adapters.redis as redis_mod
from baldur.adapters.redis import (
    _REDIS_RETRY_INTERVAL,
    _acquire_from_env,
    _redis_state,  # noqa: F401 — exposed for tests inspecting runtime-scoped state
    get_redis_client,
    reset_redis_client,
)

_FACTORY_PATCH = "baldur.adapters.redis.connection_factory.get_redis_connection_factory"
_URL_RESOLVED_EVENT = "redis.client_url_resolved"


@pytest.fixture(autouse=True)
def _reset_state():
    """Each test starts and ends with clean state."""
    reset_redis_client()
    yield
    reset_redis_client()


# =============================================================================
# 계약 검증 (Contract Tests)
# =============================================================================


class TestNegativeCachingContract:
    """Negative caching 설계 계약값 검증."""

    def test_retry_interval_contract(self):
        """Retry interval 설계 계약값: 30.0초."""
        assert _REDIS_RETRY_INTERVAL == 30.0

    def test_reset_redis_client_in_all(self):
        """reset_redis_client가 __all__에 포함되어야 한다."""
        assert "reset_redis_client" in redis_mod.__all__

    def test_get_redis_client_in_all(self):
        """get_redis_client가 __all__에 포함되어야 한다."""
        assert "get_redis_client" in redis_mod.__all__


# =============================================================================
# 동작 검증 (Behavior Tests)
# =============================================================================


class TestSuccessfulConnectionCachingBehavior:
    """연결 성공 시 캐싱 동작 검증."""

    def test_successful_client_is_cached(self):
        """첫 호출 성공 시 클라이언트가 캐싱된다."""
        mock_client = MagicMock()
        with patch.object(
            redis_mod,
            "_try_acquire_redis_client",
            return_value=mock_client,
        ) as mock_acquire:
            result1 = get_redis_client()
            result2 = get_redis_client()

        assert result1 is mock_client
        assert result2 is mock_client
        mock_acquire.assert_called_once()

    def test_cached_client_returned_without_retry(self):
        """캐싱된 클라이언트는 _try_acquire_redis_client를 재호출하지 않는다."""
        mock_client = MagicMock()
        with patch.object(
            redis_mod,
            "_try_acquire_redis_client",
            return_value=mock_client,
        ) as mock_acquire:
            for _ in range(10):
                get_redis_client()

        mock_acquire.assert_called_once()


class TestNegativeCachingBehavior:
    """연결 실패 시 negative caching 동작 검증."""

    def test_failure_activates_negative_cache(self):
        """모든 전략 실패 시 negative cache가 활성화된다."""
        with patch.object(redis_mod, "_try_acquire_redis_client", return_value=None):
            result = get_redis_client()

        state = _redis_state()
        assert result is None
        assert state.unavailable is True
        assert state.fail_time > 0

    def test_negative_cache_suppresses_retries_within_ttl(self):
        """TTL 내 재호출 시 _try_acquire_redis_client를 호출하지 않는다."""
        with patch.object(
            redis_mod, "_try_acquire_redis_client", return_value=None
        ) as mock_acquire:
            get_redis_client()  # first call — triggers actual attempt
            get_redis_client()  # second call — should be suppressed
            get_redis_client()  # third call — should be suppressed

        mock_acquire.assert_called_once()

    def test_negative_cache_returns_none_within_ttl(self):
        """TTL 내 재호출은 즉시 None을 반환한다."""
        with patch.object(redis_mod, "_try_acquire_redis_client", return_value=None):
            get_redis_client()

        # Subsequent calls: _try_acquire not even called
        result = get_redis_client()
        assert result is None


class TestNegativeCacheTtlExpiryBehavior:
    """TTL 만료 후 재시도 동작 검증."""

    def test_retry_allowed_after_ttl_expiry(self):
        """TTL 만료 후에는 _try_acquire_redis_client가 다시 호출된다."""
        with patch.object(
            redis_mod, "_try_acquire_redis_client", return_value=None
        ) as mock_acquire:
            # First call: activate negative cache
            get_redis_client()
            assert mock_acquire.call_count == 1

            # Simulate TTL expiry
            _redis_state().fail_time -= _REDIS_RETRY_INTERVAL + 1

            # Second call: TTL expired — should retry
            get_redis_client()
            assert mock_acquire.call_count == 2

    def test_recovery_after_ttl_expiry_caches_client(self):
        """TTL 만료 후 재시도가 성공하면 클라이언트가 캐싱된다."""
        mock_client = MagicMock()
        call_count = 0

        def alternating_acquire():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None  # first: fail
            return mock_client  # second: succeed

        with patch.object(
            redis_mod,
            "_try_acquire_redis_client",
            side_effect=alternating_acquire,
        ):
            # First call: fail
            result1 = get_redis_client()
            assert result1 is None

            # Simulate TTL expiry
            _redis_state().fail_time -= _REDIS_RETRY_INTERVAL + 1

            # Second call: succeed
            result2 = get_redis_client()
            assert result2 is mock_client

            # Third call: cached (no more _try_acquire calls)
            result3 = get_redis_client()
            assert result3 is mock_client

        assert call_count == 2

    def test_negative_cache_reactivated_on_retry_failure(self):
        """TTL 만료 후 재시도도 실패하면 negative cache가 다시 활성화된다."""
        with patch.object(
            redis_mod, "_try_acquire_redis_client", return_value=None
        ) as mock_acquire:
            state = _redis_state()
            get_redis_client()
            assert state.unavailable is True

            # Simulate TTL expiry
            state.fail_time -= _REDIS_RETRY_INTERVAL + 1

            get_redis_client()
            assert state.unavailable is True
            assert mock_acquire.call_count == 2


class TestResetRedisClientBehavior:
    """reset_redis_client() 동작 검증."""

    def test_reset_clears_cached_client(self):
        """reset 후 캐싱된 클라이언트가 제거된다."""
        mock_client = MagicMock()
        state = _redis_state()
        with patch.object(
            redis_mod, "_try_acquire_redis_client", return_value=mock_client
        ):
            get_redis_client()

        assert state.client is mock_client

        reset_redis_client()
        assert state.client is None

    def test_reset_clears_negative_cache(self):
        """reset 후 negative cache 상태가 초기화된다."""
        state = _redis_state()
        with patch.object(redis_mod, "_try_acquire_redis_client", return_value=None):
            get_redis_client()

        assert state.unavailable is True

        reset_redis_client()
        assert state.unavailable is False
        assert state.fail_time == 0.0

    def test_reset_allows_fresh_acquisition(self):
        """reset 후 새로운 클라이언트 획득이 시도된다."""
        mock_client = MagicMock()
        with patch.object(redis_mod, "_try_acquire_redis_client", return_value=None):
            get_redis_client()

        reset_redis_client()

        with patch.object(
            redis_mod,
            "_try_acquire_redis_client",
            return_value=mock_client,
        ):
            result = get_redis_client()

        assert result is mock_client


class TestIdempotencyBehavior:
    """멱등성 검증."""

    def test_multiple_failures_produce_same_state(self):
        """실패 후 N회 재호출은 모두 동일 결과(None)."""
        with patch.object(redis_mod, "_try_acquire_redis_client", return_value=None):
            results = [get_redis_client() for _ in range(5)]

        assert all(r is None for r in results)

    def test_multiple_successes_return_same_instance(self):
        """성공 후 N회 재호출은 모두 동일 인스턴스."""
        mock_client = MagicMock()
        with patch.object(
            redis_mod,
            "_try_acquire_redis_client",
            return_value=mock_client,
        ):
            results = [get_redis_client() for _ in range(5)]

        assert all(r is mock_client for r in results)


# =============================================================================
# Strategy 4 — environment-variable fallback priority (Behavior Tests)
# =============================================================================


class TestEnvFallbackPriorityBehavior:
    """``_acquire_from_env()`` prefers ``BALDUR_REDIS_URL`` over bare ``REDIS_URL``."""

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch):
        """Neutralize ambient REDIS_URL / BALDUR_REDIS_URL so they cannot leak in."""
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)

    def test_baldur_redis_url_wins_over_bare_redis_url(self, monkeypatch):
        """When both are set, the documented canonical var routes the client."""
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://baldur-host:6379/0")
        monkeypatch.setenv("REDIS_URL", "redis://stray-host:6379/0")

        mock_factory = MagicMock()
        with patch(_FACTORY_PATCH, return_value=mock_factory):
            result = _acquire_from_env()

        assert result is mock_factory.create.return_value
        mock_factory.create.assert_called_once_with("redis://baldur-host:6379/0")

    def test_bare_redis_url_resolves_when_baldur_unset(self, monkeypatch):
        """Backward-compat: bare REDIS_URL is the fallback when BALDUR is unset."""
        monkeypatch.setenv("REDIS_URL", "redis://legacy-host:6379/0")

        mock_factory = MagicMock()
        with patch(_FACTORY_PATCH, return_value=mock_factory):
            result = _acquire_from_env()

        assert result is mock_factory.create.return_value
        mock_factory.create.assert_called_once_with("redis://legacy-host:6379/0")

    def test_neither_set_returns_none_without_create(self, monkeypatch):
        """No env var → None, and the connection factory is never invoked."""
        mock_factory = MagicMock()
        with patch(_FACTORY_PATCH, return_value=mock_factory):
            result = _acquire_from_env()

        assert result is None
        mock_factory.create.assert_not_called()

    def test_baldur_only_resolves_via_baldur(self, monkeypatch):
        """BALDUR_REDIS_URL alone resolves through the canonical var."""
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://only-baldur:6379/0")

        mock_factory = MagicMock()
        with patch(_FACTORY_PATCH, return_value=mock_factory):
            _acquire_from_env()

        mock_factory.create.assert_called_once_with("redis://only-baldur:6379/0")


class TestEnvFallbackSourceLogBehavior:
    """``_acquire_from_env()`` emits ``redis.client_url_resolved`` with source only."""

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch):
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)

    def _resolved_events(self, captured):
        return [e for e in captured if e.get("event") == _URL_RESOLVED_EVENT]

    def test_source_is_baldur_when_baldur_set(self, monkeypatch):
        """Both set → source names the canonical var that won."""
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://baldur-host:6379/0")
        monkeypatch.setenv("REDIS_URL", "redis://stray-host:6379/0")

        with patch(_FACTORY_PATCH, return_value=MagicMock()):
            with capture_logs() as captured:
                _acquire_from_env()

        events = self._resolved_events(captured)
        assert len(events) == 1
        assert events[0]["source"] == "BALDUR_REDIS_URL"

    def test_source_is_redis_url_when_only_bare_set(self, monkeypatch):
        """Bare-only → source names the fallback var."""
        monkeypatch.setenv("REDIS_URL", "redis://legacy-host:6379/0")

        with patch(_FACTORY_PATCH, return_value=MagicMock()):
            with capture_logs() as captured:
                _acquire_from_env()

        events = self._resolved_events(captured)
        assert len(events) == 1
        assert events[0]["source"] == "REDIS_URL"

    def test_no_event_when_neither_set(self, monkeypatch):
        """No resolution → no log line."""
        with patch(_FACTORY_PATCH, return_value=MagicMock()):
            with capture_logs() as captured:
                _acquire_from_env()

        assert self._resolved_events(captured) == []

    def test_url_value_never_logged(self, monkeypatch):
        """The resolution log carries the source name only — never the URL
        (a Redis URL can embed credentials)."""
        secret_url = "redis://user:pass@baldur-host:6379/0"
        monkeypatch.setenv("BALDUR_REDIS_URL", secret_url)

        with patch(_FACTORY_PATCH, return_value=MagicMock()):
            with capture_logs() as captured:
                _acquire_from_env()

        events = self._resolved_events(captured)
        assert len(events) == 1
        entry = events[0]
        assert secret_url not in entry.values()
        assert all(secret_url not in str(v) for v in entry.values())
