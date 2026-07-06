"""
Unit tests for TierRegistry path tier cache (236 작업 2).

테스트 항목:
- 캐시 히트/미스 동작
- _invalidate_path_cache() 호출 시 캐시 초기화
- 캐시 최대 크기 제한
- mutation 메서드 호출 시 캐시 무효화
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")

import django

django.setup()

import pytest

from baldur.scaling.tiering.registry import TierRegistry


class TestTierRegistryCacheBehavior:
    """TierRegistry._path_tier_cache 동작 검증."""

    @pytest.fixture
    def registry(self):
        """격리된 TierRegistry 인스턴스."""
        r = TierRegistry.__new__(TierRegistry)
        r._init()
        return r

    def test_cache_populated_on_first_lookup(self, registry):
        """첫 조회 시 캐시에 결과가 저장된다."""
        assert len(registry._path_tier_cache) == 0

        registry.get_tier_for_path("/api/baldur/control/")

        assert len(registry._path_tier_cache) == 1
        assert ("/api/baldur/control/", None) in registry._path_tier_cache

    def test_cache_hit_returns_same_result(self, registry):
        """캐시 히트 시 동일한 결과를 반환한다."""
        result1 = registry.get_tier_for_path("/api/baldur/control/")
        result2 = registry.get_tier_for_path("/api/baldur/control/")

        assert result1 is result2

    def test_cache_stores_none_for_unmatched_path(self, registry):
        """매칭 실패한 경로도 None으로 캐시된다."""
        result = registry.get_tier_for_path("/unknown/path/")

        assert result is None
        assert ("/unknown/path/", None) in registry._path_tier_cache
        assert registry._path_tier_cache[("/unknown/path/", None)] is None

    def test_invalidate_clears_cache(self, registry):
        """_invalidate_path_cache() 호출 시 캐시가 초기화된다."""
        registry.get_tier_for_path("/api/baldur/control/")
        assert len(registry._path_tier_cache) > 0

        registry._invalidate_path_cache()

        assert len(registry._path_tier_cache) == 0

    def test_cache_max_size_respected(self, registry):
        """캐시 크기가 _PATH_CACHE_MAX_SIZE를 초과하지 않는다."""
        max_size = registry._PATH_CACHE_MAX_SIZE

        # max_size + 100개 경로 조회
        for i in range(max_size + 100):
            registry.get_tier_for_path(f"/test/path/{i}/")

        assert len(registry._path_tier_cache) <= max_size

    def test_set_mappings_invalidates_cache(self, registry):
        """set_mappings() 호출 시 캐시가 무효화된다."""
        registry.get_tier_for_path("/api/baldur/control/")
        assert len(registry._path_tier_cache) > 0

        from baldur.scaling.tiering.defaults import DEFAULT_TIER_MAPPINGS

        registry.set_mappings(list(DEFAULT_TIER_MAPPINGS))

        assert len(registry._path_tier_cache) == 0

    def test_set_tiers_invalidates_cache(self, registry):
        """set_tiers() 호출 시 캐시가 무효화된다."""
        registry.get_tier_for_path("/api/baldur/control/")
        assert len(registry._path_tier_cache) > 0

        from baldur.scaling.tiering.defaults import DEFAULT_TIER_DEFINITIONS

        registry.set_tiers(list(DEFAULT_TIER_DEFINITIONS))

        assert len(registry._path_tier_cache) == 0

    def test_reset_to_defaults_invalidates_cache(self, registry):
        """reset_to_defaults() 호출 시 캐시가 무효화된다."""
        registry.get_tier_for_path("/api/baldur/control/")
        assert len(registry._path_tier_cache) > 0

        registry.reset_to_defaults()

        assert len(registry._path_tier_cache) == 0

    def test_cache_max_size_contract(self, registry):
        """_PATH_CACHE_MAX_SIZE는 1024이다."""
        assert registry._PATH_CACHE_MAX_SIZE == 1024

    def test_different_paths_cached_independently(self, registry):
        """서로 다른 경로는 독립적으로 캐시된다."""
        result_critical = registry.get_tier_for_path("/api/baldur/control/")
        result_dashboard = registry.get_tier_for_path("/api/baldur/dashboard/test")

        assert len(registry._path_tier_cache) == 2

        if result_critical is not None and result_dashboard is not None:
            assert result_critical.id != result_dashboard.id
