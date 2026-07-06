"""
IdempotencyService 배치 메서드 테스트.

테스트 범위:
1. batch_check — cache.mget 기반 배치 조회
2. batch_mark_as_processed — cache.mset 기반 배치 마킹
3. NoopCache의 mget/mset 지원
4. 캐시 장애 시 graceful degradation
5. 멱등성 — 동일 호출 N회 동일 결과
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.services.idempotency import (
    IdempotencyKey,
    IdempotencyService,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_cache():
    """Mock cache with mget/mset support."""
    cache = MagicMock()
    cache.get.return_value = None
    cache.mget.return_value = {}
    cache.mset.return_value = False
    return cache


@pytest.fixture
def service_with_mock_cache(mock_cache):
    """IdempotencyService with injected mock cache."""
    service = IdempotencyService()
    service._cache = mock_cache
    return service


@pytest.fixture
def sample_keys():
    """Sample IdempotencyKey list for batch operations."""
    return [
        IdempotencyKey.for_wal_recovery(wal_entry_id=str(i), operation="redis_replay")
        for i in range(1, 6)
    ]


# =============================================================================
# Contract Tests: NoopCache
# =============================================================================


class TestNoopCacheBatchContract:
    """NoopCache의 mget/mset 계약 검증."""

    def test_noop_cache_mget_returns_empty_dict(self):
        """NoopCache.mget()는 빈 딕셔너리 반환."""
        # Given: Django 미사용 환경 — NoopCache 활성화
        with patch.dict("sys.modules", {"django": None, "django.core.cache": None}):
            service = IdempotencyService()
            service._cache = None  # Force lazy re-init
            cache = service._get_cache()

        result = cache.mget(["key1", "key2", "key3"])
        assert result == {}

    def test_noop_cache_mset_does_not_raise(self):
        """NoopCache.mset()는 예외 없이 no-op."""
        with patch.dict("sys.modules", {"django": None, "django.core.cache": None}):
            service = IdempotencyService()
            service._cache = None
            cache = service._get_cache()

        # When/Then: 예외 없이 완료
        from datetime import timedelta

        cache.mset({"key1": True, "key2": True}, ttl=timedelta(seconds=300))


# =============================================================================
# Behavior Tests: batch_check
# =============================================================================


class TestBatchCheckBehavior:
    """batch_check 동작 검증."""

    def test_batch_check_empty_keys_returns_empty_list(self, service_with_mock_cache):
        """빈 키 리스트이면 빈 결과 반환."""
        result = service_with_mock_cache.batch_check([])
        assert result == []

    def test_batch_check_all_new_returns_not_duplicate(
        self, service_with_mock_cache, sample_keys, mock_cache
    ):
        """캐시에 없는 키들은 모두 is_duplicate=False."""
        mock_cache.mget.return_value = {}

        results = service_with_mock_cache.batch_check(sample_keys)

        assert len(results) == 5
        assert all(not r.is_duplicate for r in results)
        assert all(r.message == "Not found" for r in results)

    def test_batch_check_all_cached_returns_duplicate(
        self, service_with_mock_cache, sample_keys, mock_cache
    ):
        """캐시에 모두 존재하면 is_duplicate=True."""
        # Given: 모든 cache_key에 값이 존재
        mock_cache.mget.return_value = {k.cache_key: k.cache_key for k in sample_keys}

        results = service_with_mock_cache.batch_check(sample_keys)

        assert len(results) == 5
        assert all(r.is_duplicate for r in results)
        assert all("batch" in r.message for r in results)

    def test_batch_check_partial_duplicates(
        self, service_with_mock_cache, sample_keys, mock_cache
    ):
        """일부만 캐시에 있으면 해당 키만 duplicate."""
        # Given: 키 1, 3만 캐시에 존재
        mock_cache.mget.return_value = {
            sample_keys[0].cache_key: "val1",
            sample_keys[2].cache_key: "val3",
        }

        results = service_with_mock_cache.batch_check(sample_keys)

        assert results[0].is_duplicate is True
        assert results[1].is_duplicate is False
        assert results[2].is_duplicate is True
        assert results[3].is_duplicate is False
        assert results[4].is_duplicate is False

    def test_batch_check_preserves_existing_record(
        self, service_with_mock_cache, sample_keys, mock_cache
    ):
        """캐시 히트 시 existing_record에 캐시 값 포함."""
        mock_cache.mget.return_value = {
            sample_keys[0].cache_key: 42,
        }

        results = service_with_mock_cache.batch_check(sample_keys)
        assert results[0].existing_record == 42

    def test_batch_check_calls_mget_with_cache_keys(
        self, service_with_mock_cache, sample_keys, mock_cache
    ):
        """mget에 올바른 cache_key 리스트 전달."""
        service_with_mock_cache.batch_check(sample_keys)

        expected_keys = [k.cache_key for k in sample_keys]
        mock_cache.mget.assert_called_once_with(expected_keys)

    def test_batch_check_cache_error_returns_all_not_duplicate(
        self, service_with_mock_cache, sample_keys, mock_cache
    ):
        """캐시 에러 시 모든 결과 is_duplicate=False (graceful degradation)."""
        mock_cache.mget.side_effect = ConnectionError("Redis down")

        results = service_with_mock_cache.batch_check(sample_keys)

        assert len(results) == 5
        assert all(not r.is_duplicate for r in results)

    def test_batch_check_idempotent_same_result_on_repeated_calls(
        self, service_with_mock_cache, sample_keys, mock_cache
    ):
        """동일 입력 반복 호출 시 동일 결과 (멱등성)."""
        mock_cache.mget.return_value = {
            sample_keys[0].cache_key: "v1",
        }

        results1 = service_with_mock_cache.batch_check(sample_keys)
        results2 = service_with_mock_cache.batch_check(sample_keys)

        assert len(results1) == len(results2)
        for r1, r2 in zip(results1, results2, strict=False):
            assert r1.is_duplicate == r2.is_duplicate


# =============================================================================
# Behavior Tests: batch_mark_as_processed
# =============================================================================


class TestBatchMarkAsProcessedBehavior:
    """batch_mark_as_processed 동작 검증."""

    def test_batch_mark_empty_keys_returns_true(self, service_with_mock_cache):
        """빈 키 리스트이면 True 반환."""
        result = service_with_mock_cache.batch_mark_as_processed([])
        assert result is True

    def test_batch_mark_calls_mset(
        self, service_with_mock_cache, sample_keys, mock_cache
    ):
        """mset에 올바른 매핑 전달."""
        service_with_mock_cache.batch_mark_as_processed(sample_keys)

        mock_cache.mset.assert_called_once()
        call_args = mock_cache.mset.call_args
        mapping = call_args[0][0]

        for key in sample_keys:
            assert key.cache_key in mapping
            assert mapping[key.cache_key] is True

    def test_batch_mark_uses_default_ttl(
        self, service_with_mock_cache, sample_keys, mock_cache
    ):
        """TTL 미지정 시 기본 cache_ttl 사용."""
        from datetime import timedelta

        service_with_mock_cache.batch_mark_as_processed(sample_keys)

        call_kwargs = mock_cache.mset.call_args
        ttl = call_kwargs[1].get("ttl")
        assert ttl == timedelta(seconds=service_with_mock_cache.cache_ttl)

    def test_batch_mark_uses_custom_ttl(
        self, service_with_mock_cache, sample_keys, mock_cache
    ):
        """커스텀 TTL 전달."""
        from datetime import timedelta

        service_with_mock_cache.batch_mark_as_processed(sample_keys, ttl=7200)

        call_kwargs = mock_cache.mset.call_args
        ttl = call_kwargs[1].get("ttl")
        assert ttl == timedelta(seconds=7200)

    def test_batch_mark_returns_true_on_success(
        self, service_with_mock_cache, sample_keys
    ):
        """성공 시 True 반환."""
        result = service_with_mock_cache.batch_mark_as_processed(sample_keys)
        assert result is True

    def test_batch_mark_returns_false_on_cache_error(
        self, service_with_mock_cache, sample_keys, mock_cache
    ):
        """캐시 에러 시 False 반환 (graceful degradation)."""
        mock_cache.mset.side_effect = ConnectionError("Redis down")

        result = service_with_mock_cache.batch_mark_as_processed(sample_keys)
        assert result is False
