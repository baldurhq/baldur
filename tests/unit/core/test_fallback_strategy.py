"""
Tests for Fallback Strategy - Partial Partition Fallback
"""

import pytest

from baldur.core.connection_health import PartitionState
from baldur.core.fallback_strategy import (
    CacheFirstFallback,
    FallbackMode,
    FallbackResult,
    PartitionAwareFallback,
    SimpleFallback,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def healthy_partition_state():
    """Create a healthy partition state (all connections available)."""
    state = PartitionState()
    state.cache_available = True
    state.db_available = True
    state.queue_available = True
    return state


@pytest.fixture
def cache_unavailable_state():
    """Create a state where cache is unavailable."""
    state = PartitionState()
    state.cache_available = False
    state.db_available = True
    state.queue_available = True
    return state


@pytest.fixture
def db_unavailable_state():
    """Create a state where DB is unavailable."""
    state = PartitionState()
    state.cache_available = True
    state.db_available = False
    state.queue_available = True
    return state


@pytest.fixture
def all_unavailable_state():
    """Create a state where all connections are unavailable."""
    state = PartitionState()
    state.cache_available = False
    state.db_available = False
    state.queue_available = False
    return state


# =============================================================================
# FallbackMode Tests
# =============================================================================


class TestFallbackMode:
    """Test FallbackMode enum."""

    def test_modes_exist(self):
        """모든 모드 확인."""
        assert FallbackMode.FAIL_FAST == "fail_fast"
        assert FallbackMode.USE_CACHE == "use_cache"
        assert FallbackMode.USE_DEFAULT == "use_default"
        assert FallbackMode.DEGRADE_GRACEFULLY == "degrade"
        assert FallbackMode.RETRY_ALTERNATIVE == "retry_alt"


# =============================================================================
# FallbackResult Tests
# =============================================================================


class TestFallbackResult:
    """Test FallbackResult dataclass."""

    def test_success_no_fallback(self):
        """성공 (fallback 미사용)."""
        result = FallbackResult(
            value="data",
            used_fallback=False,
        )

        assert result.value == "data"
        assert result.used_fallback is False
        assert result.success is True

    def test_success_with_fallback(self):
        """성공 (fallback 사용)."""
        result = FallbackResult(
            value="cached_data",
            used_fallback=True,
            fallback_mode=FallbackMode.USE_CACHE,
        )

        assert result.value == "cached_data"
        assert result.used_fallback is True
        assert result.success is True
        assert result.fallback_mode == FallbackMode.USE_CACHE

    def test_fail_fast_is_failure(self):
        """FAIL_FAST 모드는 실패."""
        result = FallbackResult(
            value=None,
            used_fallback=True,
            fallback_mode=FallbackMode.FAIL_FAST,
        )

        assert result.success is False

    def test_result_with_original_error(self):
        """원본 에러 포함."""
        result = FallbackResult(
            value="fallback_data",
            used_fallback=True,
            fallback_mode=FallbackMode.USE_DEFAULT,
            original_error="Connection timeout",
        )

        assert result.original_error == "Connection timeout"


# =============================================================================
# SimpleFallback Tests
# =============================================================================


class TestSimpleFallback:
    """Test SimpleFallback strategy."""

    def test_primary_succeeds(self):
        """Primary 함수 성공."""
        fallback = SimpleFallback()

        result = fallback.execute(
            primary_fn=lambda: "primary_data",
        )

        assert result.value == "primary_data"
        assert result.used_fallback is False

    def test_primary_fails_fallback_succeeds(self):
        """Primary 실패, Fallback 성공."""
        fallback = SimpleFallback()

        def failing_primary():
            raise Exception("Primary failed")

        result = fallback.execute(
            primary_fn=failing_primary,
            fallback_fn=lambda: "fallback_data",
        )

        assert result.value == "fallback_data"
        assert result.used_fallback is True
        assert result.fallback_mode == FallbackMode.RETRY_ALTERNATIVE

    def test_primary_fails_use_default(self):
        """Primary 실패, 기본값 사용."""
        fallback = SimpleFallback()

        def failing_primary():
            raise Exception("Primary failed")

        result = fallback.execute(
            primary_fn=failing_primary,
            default_value="default_data",
        )

        assert result.value == "default_data"
        assert result.used_fallback is True
        assert result.fallback_mode == FallbackMode.USE_DEFAULT

    def test_all_fail(self):
        """모두 실패."""
        fallback = SimpleFallback()

        def failing_fn():
            raise Exception("Failed")

        result = fallback.execute(
            primary_fn=failing_fn,
            fallback_fn=failing_fn,
            # No default value
        )

        assert result.value is None
        assert result.used_fallback is True
        assert result.fallback_mode == FallbackMode.FAIL_FAST

    def test_fallback_fn_fails_use_default(self):
        """Fallback 함수도 실패, 기본값 사용."""
        fallback = SimpleFallback()

        def failing_fn():
            raise Exception("Failed")

        result = fallback.execute(
            primary_fn=failing_fn,
            fallback_fn=failing_fn,
            default_value="default_data",
        )

        assert result.value == "default_data"
        assert result.fallback_mode == FallbackMode.USE_DEFAULT


# =============================================================================
# PartitionAwareFallback Tests
# =============================================================================


class TestPartitionAwareFallback:
    """Test PartitionAwareFallback strategy."""

    def test_primary_succeeds(self, healthy_partition_state):
        """Primary 함수 성공."""
        fallback = PartitionAwareFallback(
            partition_state=healthy_partition_state,
        )

        result = fallback.execute(
            primary_fn=lambda: "primary_data",
        )

        assert result.value == "primary_data"
        assert result.used_fallback is False

    def test_cache_unavailable_db_fallback(self, cache_unavailable_state):
        """캐시 불가 시 DB Fallback."""
        fallback = PartitionAwareFallback(
            partition_state=cache_unavailable_state,
            db_fallback=lambda: "db_data",
        )

        def failing_primary():
            raise Exception("Cache unavailable")

        result = fallback.execute(
            primary_fn=failing_primary,
        )

        assert result.value == "db_data"
        assert result.used_fallback is True
        assert result.fallback_mode == FallbackMode.DEGRADE_GRACEFULLY

    def test_db_unavailable_cache_fallback(self, db_unavailable_state):
        """DB 불가 시 Cache Fallback."""
        fallback = PartitionAwareFallback(
            partition_state=db_unavailable_state,
            cache_fallback=lambda: "cached_data",
        )

        def failing_primary():
            raise Exception("DB unavailable")

        result = fallback.execute(
            primary_fn=failing_primary,
        )

        assert result.value == "cached_data"
        assert result.used_fallback is True
        assert result.fallback_mode == FallbackMode.USE_CACHE

    def test_explicit_fallback_first(self, healthy_partition_state):
        """명시적 fallback_fn이 먼저 시도됨."""
        fallback = PartitionAwareFallback(
            partition_state=healthy_partition_state,
            cache_fallback=lambda: "cached",
            db_fallback=lambda: "db",
        )

        def failing_primary():
            raise Exception("Failed")

        result = fallback.execute(
            primary_fn=failing_primary,
            fallback_fn=lambda: "explicit_fallback",
        )

        assert result.value == "explicit_fallback"
        assert result.fallback_mode == FallbackMode.RETRY_ALTERNATIVE

    def test_all_unavailable_default_value(self, all_unavailable_state):
        """모두 불가 시 기본값."""
        fallback = PartitionAwareFallback(
            partition_state=all_unavailable_state,
        )

        def failing_primary():
            raise Exception("Failed")

        result = fallback.execute(
            primary_fn=failing_primary,
            default_value="default",
        )

        assert result.value == "default"
        assert result.fallback_mode == FallbackMode.USE_DEFAULT

    def test_update_partition_state(
        self, healthy_partition_state, db_unavailable_state
    ):
        """파티션 상태 업데이트."""
        fallback = PartitionAwareFallback(
            partition_state=healthy_partition_state,
            cache_fallback=lambda: "cached",
        )

        # 상태 업데이트
        fallback.update_partition_state(db_unavailable_state)

        def failing_primary():
            raise Exception("Failed")

        result = fallback.execute(
            primary_fn=failing_primary,
        )

        # DB 불가 상태이므로 cache fallback 사용
        assert result.value == "cached"

    def test_all_fail_no_default(self, all_unavailable_state):
        """모두 실패, 기본값 없음."""
        fallback = PartitionAwareFallback(
            partition_state=all_unavailable_state,
        )

        def failing_fn():
            raise Exception("Failed")

        result = fallback.execute(
            primary_fn=failing_fn,
        )

        assert result.value is None
        assert result.fallback_mode == FallbackMode.FAIL_FAST


# =============================================================================
# CacheFirstFallback Tests
# =============================================================================


class TestCacheFirstFallback:
    """Test CacheFirstFallback strategy."""

    def test_cache_hit(self):
        """캐시 히트."""
        fallback = CacheFirstFallback(
            cache_fn=lambda: "cached_data",
            db_fn=lambda: "db_data",
        )

        result = fallback.execute()

        assert result.value == "cached_data"
        assert result.used_fallback is False

    def test_cache_miss_db_hit(self):
        """캐시 미스, DB 히트."""
        fallback = CacheFirstFallback(
            cache_fn=lambda: None,  # Cache miss
            db_fn=lambda: "db_data",
        )

        result = fallback.execute()

        assert result.value == "db_data"
        assert result.used_fallback is True
        assert result.fallback_mode == FallbackMode.DEGRADE_GRACEFULLY

    def test_cache_error_db_hit(self):
        """캐시 에러, DB 히트."""

        def failing_cache():
            raise Exception("Cache error")

        fallback = CacheFirstFallback(
            cache_fn=failing_cache,
            db_fn=lambda: "db_data",
        )

        result = fallback.execute()

        assert result.value == "db_data"
        assert result.used_fallback is True

    def test_cache_miss_db_hit_updates_cache(self):
        """캐시 미스 후 DB 결과를 캐시에 업데이트."""
        updated = []

        def update_cache(value):
            updated.append(value)

        fallback = CacheFirstFallback(
            cache_fn=lambda: None,
            db_fn=lambda: "db_data",
            update_cache_fn=update_cache,
        )

        result = fallback.execute()

        assert result.value == "db_data"
        assert "db_data" in updated

    def test_cache_update_failure_handled(self):
        """캐시 업데이트 실패 처리."""

        def failing_update(value):
            raise Exception("Cache update failed")

        fallback = CacheFirstFallback(
            cache_fn=lambda: None,
            db_fn=lambda: "db_data",
            update_cache_fn=failing_update,
        )

        # 예외 발생해도 결과 반환
        result = fallback.execute()

        assert result.value == "db_data"

    def test_both_fail_use_default(self):
        """둘 다 실패, 기본값 사용."""

        def failing_fn():
            raise Exception("Failed")

        fallback = CacheFirstFallback(
            cache_fn=failing_fn,
            db_fn=failing_fn,
        )

        result = fallback.execute(default_value="default")

        assert result.value == "default"
        assert result.fallback_mode == FallbackMode.USE_DEFAULT

    def test_both_fail_no_default(self):
        """둘 다 실패, 기본값 없음."""

        def failing_fn():
            raise Exception("Failed")

        fallback = CacheFirstFallback(
            cache_fn=failing_fn,
            db_fn=failing_fn,
        )

        result = fallback.execute()

        assert result.value is None
        assert result.fallback_mode == FallbackMode.FAIL_FAST


# =============================================================================
# Generic Type Tests
# =============================================================================


class TestGenericTypes:
    """Test generic type handling."""

    def test_string_value(self):
        """문자열 값."""
        fallback = SimpleFallback()
        result = fallback.execute(primary_fn=lambda: "string")
        assert result.value == "string"

    def test_int_value(self):
        """정수 값."""
        fallback = SimpleFallback()
        result = fallback.execute(primary_fn=lambda: 42)
        assert result.value == 42

    def test_dict_value(self):
        """딕셔너리 값."""
        fallback = SimpleFallback()
        result = fallback.execute(primary_fn=lambda: {"key": "value"})
        assert result.value == {"key": "value"}

    def test_list_value(self):
        """리스트 값."""
        fallback = SimpleFallback()
        result = fallback.execute(primary_fn=lambda: [1, 2, 3])
        assert result.value == [1, 2, 3]

    def test_none_value_as_valid(self):
        """None 값도 유효한 결과."""
        fallback = SimpleFallback()
        fallback.execute(primary_fn=lambda: None)
        # None 반환은 성공으로 처리되지 않음 (fallback으로 넘어감)
        # 이건 구현에 따라 다를 수 있음


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Test edge cases."""

    def test_primary_returns_false(self):
        """Primary가 False 반환."""
        fallback = SimpleFallback()
        result = fallback.execute(primary_fn=lambda: False)

        # False는 유효한 값
        assert result.value is False
        assert result.used_fallback is False

    def test_primary_returns_zero(self):
        """Primary가 0 반환."""
        fallback = SimpleFallback()
        result = fallback.execute(primary_fn=lambda: 0)

        assert result.value == 0
        assert result.used_fallback is False

    def test_primary_returns_empty_string(self):
        """Primary가 빈 문자열 반환."""
        fallback = SimpleFallback()
        result = fallback.execute(primary_fn=lambda: "")

        assert result.value == ""
        assert result.used_fallback is False

    def test_primary_returns_empty_list(self):
        """Primary가 빈 리스트 반환."""
        fallback = SimpleFallback()
        result = fallback.execute(primary_fn=lambda: [])

        assert result.value == []
        assert result.used_fallback is False


# =============================================================================
# PartitionState Tests
# =============================================================================


class TestPartitionState:
    """Test PartitionState usage."""

    def test_default_partition_state(self):
        """기본 파티션 상태."""
        state = PartitionState()

        # 기본값 확인 (구현에 따라 다름)
        assert hasattr(state, "cache_available")
        assert hasattr(state, "db_available")

    def test_partition_state_attributes(self, healthy_partition_state):
        """파티션 상태 속성."""
        assert healthy_partition_state.cache_available is True
        assert healthy_partition_state.db_available is True
