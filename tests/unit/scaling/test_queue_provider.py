"""
Unit tests for CachedQueueSizeProvider.

테스트 항목:
- 캐시 TTL 동작
- 실패 시 캐시된 값 유지
- 캐시 무효화
- 캐시 정보 조회
"""

import time
from unittest.mock import Mock

import pytest

from baldur.scaling.config import (
    BackpressureSettings,
    reset_backpressure_settings,
)
from baldur.scaling.queue_provider import CachedQueueSizeProvider


class TestCachedQueueSizeProvider:
    """CachedQueueSizeProvider 테스트."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """각 테스트 전후로 설정 캐시 리셋."""
        reset_backpressure_settings()
        yield
        reset_backpressure_settings()

    def test_initial_call_fetches_from_provider(self):
        """첫 호출 시 provider에서 값을 가져오는지 확인."""
        mock_provider = Mock(return_value=100)
        provider = CachedQueueSizeProvider(
            provider=mock_provider,
            cache_ttl=1.0,
        )

        result = provider()

        assert result == 100
        mock_provider.assert_called_once()

    def test_cached_value_returned_within_ttl(self):
        """TTL 내 재호출 시 캐시된 값 반환."""
        mock_provider = Mock(return_value=100)
        provider = CachedQueueSizeProvider(
            provider=mock_provider,
            cache_ttl=1.0,
        )

        # 첫 호출
        result1 = provider()
        # 두 번째 호출 (TTL 내)
        result2 = provider()
        result3 = provider()

        assert result1 == 100
        assert result2 == 100
        assert result3 == 100
        # provider는 한 번만 호출됨
        mock_provider.assert_called_once()

    def test_provider_called_after_ttl_expires(self):
        """TTL 만료 후 provider 재호출."""
        call_count = [0]

        def mock_provider():
            call_count[0] += 1
            return call_count[0] * 100

        provider = CachedQueueSizeProvider(
            provider=mock_provider,
            cache_ttl=0.05,  # 50ms TTL
        )

        # 첫 호출
        result1 = provider()
        assert result1 == 100

        # TTL 만료 대기
        time.sleep(0.1)

        # 두 번째 호출 (TTL 만료 후)
        result2 = provider()
        assert result2 == 200

        assert call_count[0] == 2

    def test_uses_cached_value_on_provider_failure(self):
        """provider 실패 시 캐시된 값 유지."""
        call_count = [0]

        def failing_provider():
            call_count[0] += 1
            if call_count[0] == 1:
                return 100
            raise Exception("Provider failed")

        provider = CachedQueueSizeProvider(
            provider=failing_provider,
            cache_ttl=0.01,
        )

        # 첫 호출 성공
        result1 = provider()
        assert result1 == 100

        # TTL 만료 대기
        time.sleep(0.02)

        # 두 번째 호출 실패 -> 캐시된 값 반환
        result2 = provider()
        assert result2 == 100

    def test_invalidate_forces_fetch(self):
        """invalidate 후 즉시 provider 호출."""
        call_count = [0]

        def mock_provider():
            call_count[0] += 1
            return call_count[0] * 100

        provider = CachedQueueSizeProvider(
            provider=mock_provider,
            cache_ttl=10.0,  # 긴 TTL
        )

        # 첫 호출
        result1 = provider()
        assert result1 == 100

        # 캐시 무효화
        provider.invalidate()

        # 무효화 후 호출 -> provider 재호출
        result2 = provider()
        assert result2 == 200

        assert call_count[0] == 2

    def test_get_cache_info(self):
        """캐시 정보 조회."""
        mock_provider = Mock(return_value=500)
        provider = CachedQueueSizeProvider(
            provider=mock_provider,
            cache_ttl=5.0,
        )

        # 호출 전 캐시 정보
        info_before = provider.get_cache_info()
        assert info_before["cached_value"] == 0
        assert info_before["cache_ttl"] == 5.0

        # 호출 후 캐시 정보
        provider()
        info_after = provider.get_cache_info()
        assert info_after["cached_value"] == 500
        assert info_after["cache_ttl"] == 5.0
        assert info_after["age_seconds"] >= 0

    def test_uses_settings_cache_ttl_by_default(self):
        """기본값으로 설정의 cache_ttl 사용."""
        settings = BackpressureSettings(queue_size_cache_ttl_seconds=3.0)
        mock_provider = Mock(return_value=100)

        provider = CachedQueueSizeProvider(
            provider=mock_provider,
            settings=settings,
        )

        info = provider.get_cache_info()
        assert info["cache_ttl"] == 3.0

    def test_explicit_cache_ttl_overrides_settings(self):
        """명시적 cache_ttl이 설정을 오버라이드."""
        settings = BackpressureSettings(queue_size_cache_ttl_seconds=3.0)
        mock_provider = Mock(return_value=100)

        provider = CachedQueueSizeProvider(
            provider=mock_provider,
            cache_ttl=7.0,  # 명시적 오버라이드
            settings=settings,
        )

        info = provider.get_cache_info()
        assert info["cache_ttl"] == 7.0
