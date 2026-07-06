"""
Tests for SystemMetricsCache — 220_SYSTEM_METRICS_CACHE_LAYER.

§3.1 services/system_metrics_cache.py의 CachedMetrics, SystemMetricsCache,
모듈 레벨 API(start/stop/get/reset)를 검증한다.

테스트 분류 (UNIT_TEST_GUIDELINES §0):
- Contract: 설계 문서에 명시된 값/구조 검증 (하드코딩)
- Behavior: 함수/메서드 동작 검증 (소스 참조)

참조 소스:
- services/system_metrics_cache.py (SystemMetricsCache, CachedMetrics)
- settings/system_metrics_cache.py (SystemMetricsCacheSettings)
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from baldur.services.system_metrics_cache import (
    CachedMetrics,
    SystemMetricsCache,
    get_cached_cpu_percent,
    get_cached_memory_percent,
    get_system_metrics_cache,
    reset_system_metrics_cache,
    start_system_metrics_cache,
    stop_system_metrics_cache,
)
from baldur.settings.system_metrics_cache import (
    SystemMetricsCacheSettings,
    get_system_metrics_cache_settings,
    reset_system_metrics_cache_settings,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_cache():
    """각 테스트 전후로 글로벌 캐시 인스턴스와 설정 싱글톤을 초기화."""
    reset_system_metrics_cache()
    reset_system_metrics_cache_settings()
    yield
    reset_system_metrics_cache()
    reset_system_metrics_cache_settings()


def _make_virtual_memory(
    percent=55.0, used=1024 * 1024 * 1500, available=1024 * 1024 * 500
):
    """psutil.virtual_memory() 반환 형태의 Mock 생성."""
    mock = MagicMock()
    mock.percent = percent
    mock.used = used
    mock.available = available
    return mock


# =============================================================================
# 1. CachedMetrics — 계약 검증 (Contract)
# =============================================================================


class TestCachedMetricsContract:
    """CachedMetrics dataclass 설계 계약값 검증."""

    def test_default_cpu_percent(self):
        """기본 cpu_percent는 0.0이어야 한다."""
        m = CachedMetrics()
        assert m.cpu_percent == 0.0

    def test_default_memory_percent(self):
        """기본 memory_percent는 0.0이어야 한다."""
        m = CachedMetrics()
        assert m.memory_percent == 0.0

    def test_default_memory_used_mb(self):
        """기본 memory_used_mb는 0.0이어야 한다."""
        m = CachedMetrics()
        assert m.memory_used_mb == 0.0

    def test_default_memory_available_mb(self):
        """기본 memory_available_mb는 0.0이어야 한다."""
        m = CachedMetrics()
        assert m.memory_available_mb == 0.0

    def test_default_measured_at(self):
        """기본 measured_at는 빈 문자열이어야 한다."""
        m = CachedMetrics()
        assert m.measured_at == ""

    def test_default_source(self):
        """기본 source는 'cache'이어야 한다."""
        m = CachedMetrics()
        assert m.source == "cache"

    def test_default_age_seconds(self):
        """기본 age_seconds는 0.0이어야 한다."""
        m = CachedMetrics()
        assert m.age_seconds == 0.0

    def test_frozen_immutable(self):
        """CachedMetrics는 frozen=True이므로 속성 변경 불가."""
        m = CachedMetrics(cpu_percent=50.0)
        with pytest.raises(AttributeError):
            m.cpu_percent = 99.0  # type: ignore[misc]


# =============================================================================
# 2. SystemMetricsCacheSettings — 계약 검증 (Contract)
# =============================================================================


class TestSystemMetricsCacheSettingsContract:
    """SystemMetricsCacheSettings 설계 계약값 검증."""

    def test_enabled_default(self):
        """기본 enabled는 True."""
        settings = SystemMetricsCacheSettings()
        assert settings.enabled is True

    def test_refresh_interval_default(self):
        """기본 refresh_interval은 1.0초."""
        settings = SystemMetricsCacheSettings()
        assert settings.refresh_interval == 1.0

    def test_sample_interval_default(self):
        """기본 sample_interval은 0.1초 (100ms)."""
        settings = SystemMetricsCacheSettings()
        assert settings.sample_interval == 0.1

    def test_max_age_seconds_default(self):
        """기본 max_age_seconds는 5.0초."""
        settings = SystemMetricsCacheSettings()
        assert settings.max_age_seconds == 5.0

    def test_refresh_interval_bounds(self):
        """refresh_interval 범위: ge=0.5, le=10.0."""
        field_info = SystemMetricsCacheSettings.model_fields["refresh_interval"]
        ge_found = any(getattr(m, "ge", None) == 0.5 for m in field_info.metadata)
        le_found = any(getattr(m, "le", None) == 10.0 for m in field_info.metadata)
        assert ge_found, "refresh_interval ge=0.5 계약 누락"
        assert le_found, "refresh_interval le=10.0 계약 누락"

    def test_sample_interval_bounds(self):
        """sample_interval 범위: ge=0.05, le=1.0."""
        field_info = SystemMetricsCacheSettings.model_fields["sample_interval"]
        ge_found = any(getattr(m, "ge", None) == 0.05 for m in field_info.metadata)
        le_found = any(getattr(m, "le", None) == 1.0 for m in field_info.metadata)
        assert ge_found, "sample_interval ge=0.05 계약 누락"
        assert le_found, "sample_interval le=1.0 계약 누락"

    def test_max_age_seconds_bounds(self):
        """max_age_seconds 범위: ge=1.0, le=60.0."""
        field_info = SystemMetricsCacheSettings.model_fields["max_age_seconds"]
        ge_found = any(getattr(m, "ge", None) == 1.0 for m in field_info.metadata)
        le_found = any(getattr(m, "le", None) == 60.0 for m in field_info.metadata)
        assert ge_found, "max_age_seconds ge=1.0 계약 누락"
        assert le_found, "max_age_seconds le=60.0 계약 누락"


# =============================================================================
# 3. SystemMetricsCacheSettings — 동작 검증 (Behavior)
# =============================================================================


class TestSystemMetricsCacheSettingsBehavior:
    """SystemMetricsCacheSettings 동작 검증."""

    def test_env_override_enabled(self, monkeypatch):
        """환경변수로 enabled=false 오버라이드."""
        monkeypatch.setenv("BALDUR_SYSTEM_METRICS_CACHE_ENABLED", "false")
        settings = SystemMetricsCacheSettings()
        assert settings.enabled is False

    def test_env_override_refresh_interval(self, monkeypatch):
        """환경변수로 refresh_interval 오버라이드."""
        monkeypatch.setenv("BALDUR_SYSTEM_METRICS_CACHE_REFRESH_INTERVAL", "2.0")
        settings = SystemMetricsCacheSettings()
        assert settings.refresh_interval == 2.0

    def test_env_override_sample_interval(self, monkeypatch):
        """환경변수로 sample_interval 오버라이드."""
        monkeypatch.setenv("BALDUR_SYSTEM_METRICS_CACHE_SAMPLE_INTERVAL", "0.2")
        monkeypatch.setenv("BALDUR_SYSTEM_METRICS_CACHE_REFRESH_INTERVAL", "1.0")
        settings = SystemMetricsCacheSettings()
        assert settings.sample_interval == 0.2

    def test_env_override_max_age_seconds(self, monkeypatch):
        """환경변수로 max_age_seconds 오버라이드."""
        monkeypatch.setenv("BALDUR_SYSTEM_METRICS_CACHE_MAX_AGE_SECONDS", "10.0")
        settings = SystemMetricsCacheSettings()
        assert settings.max_age_seconds == 10.0

    def test_refresh_must_be_greater_than_sample_validator(self):
        """refresh_interval ≤ sample_interval이면 ValidationError."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="refresh_interval"):
            SystemMetricsCacheSettings(
                refresh_interval=0.1,
                sample_interval=0.1,
            )

    def test_refresh_below_min_raises(self):
        """refresh_interval이 ge=0.5 미만이면 ValidationError."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SystemMetricsCacheSettings(refresh_interval=0.3)

    def test_sample_below_min_raises(self):
        """sample_interval이 ge=0.05 미만이면 ValidationError."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SystemMetricsCacheSettings(sample_interval=0.01)

    def test_singleton_pattern(self):
        """get_system_metrics_cache_settings() 싱글톤 패턴 검증."""
        s1 = get_system_metrics_cache_settings()
        s2 = get_system_metrics_cache_settings()
        assert s1 is s2

    def test_singleton_reset(self):
        """reset_system_metrics_cache_settings() 후 새 인스턴스 반환."""
        s1 = get_system_metrics_cache_settings()
        reset_system_metrics_cache_settings()
        s2 = get_system_metrics_cache_settings()
        assert s1 is not s2


# =============================================================================
# 4. SystemMetricsCache — 동작 검증 (Behavior)
# =============================================================================


class TestSystemMetricsCacheBehavior:
    """SystemMetricsCache 핵심 동작 검증."""

    # -----------------------------------------------------------------
    # 4.1 __init__ 기본값: 소스 참조 (동작 검증)
    # -----------------------------------------------------------------

    def test_init_defaults_from_settings(self):
        """생성 시 기본 파라미터가 소스 코드 기본값과 일치."""
        cache = SystemMetricsCache()
        assert cache._refresh_interval == 1.0
        assert cache._sample_interval == 0.1
        assert cache._max_age_seconds == 5.0
        assert cache._running is False
        assert cache._last_refresh == 0.0

    def test_init_custom_params(self):
        """커스텀 파라미터로 생성."""
        cache = SystemMetricsCache(
            refresh_interval=2.0,
            sample_interval=0.2,
            max_age_seconds=10.0,
        )
        assert cache._refresh_interval == 2.0
        assert cache._sample_interval == 0.2
        assert cache._max_age_seconds == 10.0

    def test_initial_cached_is_default_metrics(self):
        """시작 전 _cached는 기본 CachedMetrics 인스턴스."""
        cache = SystemMetricsCache()
        m = cache._cached
        default = CachedMetrics()
        assert m.cpu_percent == default.cpu_percent
        assert m.memory_percent == default.memory_percent
        assert m.measured_at == default.measured_at

    # -----------------------------------------------------------------
    # 4.2 start() / stop() — Cold Start 동기 초기화
    # -----------------------------------------------------------------

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_start_performs_sync_refresh(self, mock_cpu, mock_vm):
        """start()는 즉시 동기 _do_refresh()를 호출하여 Cold Start를 방지한다."""
        mock_cpu.return_value = 42.3
        mock_vm.return_value = _make_virtual_memory(percent=65.7)

        cache = SystemMetricsCache(refresh_interval=5.0, sample_interval=0.01)
        cache.start()
        try:
            assert cache._cached.cpu_percent == round(42.3, 1)
            assert cache._cached.memory_percent == round(65.7, 1)
            assert cache._cached.measured_at != ""
            assert cache._cached.source == "cache"
            assert cache.is_running() is True
        finally:
            cache.stop()

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_start_idempotent(self, mock_cpu, mock_vm):
        """이미 running이면 start() 재호출 시 아무 동작하지 않는다."""
        mock_cpu.return_value = 10.0
        mock_vm.return_value = _make_virtual_memory()

        cache = SystemMetricsCache(refresh_interval=5.0, sample_interval=0.01)
        cache.start()
        try:
            call_count_before = mock_cpu.call_count
            cache.start()  # 중복 호출
            call_count_after = mock_cpu.call_count
            assert call_count_after == call_count_before
        finally:
            cache.stop()

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_stop_sets_running_false(self, mock_cpu, mock_vm):
        """stop() 후 is_running()은 False."""
        mock_cpu.return_value = 10.0
        mock_vm.return_value = _make_virtual_memory()

        cache = SystemMetricsCache(refresh_interval=5.0, sample_interval=0.01)
        cache.start()
        assert cache.is_running() is True
        cache.stop()
        assert cache.is_running() is False

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_stop_cancels_timer(self, mock_cpu, mock_vm):
        """stop() 후 _timer가 None이 되어야 한다."""
        mock_cpu.return_value = 10.0
        mock_vm.return_value = _make_virtual_memory()

        cache = SystemMetricsCache(refresh_interval=5.0, sample_interval=0.01)
        cache.start()
        assert cache._timer is not None
        cache.stop()
        assert cache._timer is None

    # -----------------------------------------------------------------
    # 4.3 _do_refresh() — 메트릭 갱신 및 round(val, 1) 정밀도
    # -----------------------------------------------------------------

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_do_refresh_round_precision(self, mock_cpu, mock_vm):
        """_do_refresh()는 round(val, 1) 정밀도를 적용한다 (리뷰 14.2)."""
        mock_cpu.return_value = 45.23456789
        mock_vm.return_value = _make_virtual_memory(
            percent=72.87654321,
            used=int(1482.567890625 * 1024 * 1024),
            available=int(565.912345678 * 1024 * 1024),
        )

        cache = SystemMetricsCache(refresh_interval=5.0, sample_interval=0.01)
        cache._running = True
        cache._do_refresh()

        m = cache._cached
        assert m.cpu_percent == round(45.23456789, 1)
        assert m.memory_percent == round(72.87654321, 1)
        assert m.memory_used_mb == round(1482.567890625, 1)
        assert m.memory_available_mb == round(565.912345678, 1)

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_do_refresh_sets_timestamp(self, mock_cpu, mock_vm):
        """_do_refresh() 후 measured_at에 ISO 타임스탬프가 설정된다."""
        mock_cpu.return_value = 10.0
        mock_vm.return_value = _make_virtual_memory()

        cache = SystemMetricsCache(refresh_interval=5.0, sample_interval=0.01)
        cache._running = True
        cache._do_refresh()

        m = cache._cached
        assert m.measured_at != ""
        parsed = datetime.fromisoformat(m.measured_at)
        assert parsed.tzinfo is not None

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_do_refresh_updates_last_refresh(self, mock_cpu, mock_vm):
        """_do_refresh() 후 _last_refresh가 갱신된다."""
        mock_cpu.return_value = 10.0
        mock_vm.return_value = _make_virtual_memory()

        cache = SystemMetricsCache(refresh_interval=5.0, sample_interval=0.01)
        assert cache._last_refresh == 0.0
        cache._running = True
        cache._do_refresh()
        assert cache._last_refresh > 0.0

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_do_refresh_exception_keeps_old_cache(self, mock_cpu, mock_vm):
        """_do_refresh() 예외 시 기존 캐시 값이 유지된다."""
        mock_cpu.return_value = 30.0
        mock_vm.return_value = _make_virtual_memory(percent=50.0)

        cache = SystemMetricsCache(refresh_interval=5.0, sample_interval=0.01)
        cache._running = True
        cache._do_refresh()
        old_cached = cache._cached

        # 두 번째 갱신에서 예외 발생
        mock_cpu.side_effect = RuntimeError("psutil failed")
        cache._do_refresh()

        assert cache._cached.cpu_percent == old_cached.cpu_percent
        assert cache._cached.memory_percent == old_cached.memory_percent

    # -----------------------------------------------------------------
    # 4.4 get_metrics() — max_age_seconds 초과 시 source="stale"
    # -----------------------------------------------------------------

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_get_metrics_returns_cache_source(self, mock_cpu, mock_vm):
        """정상 캐시: source='cache'."""
        mock_cpu.return_value = 25.0
        mock_vm.return_value = _make_virtual_memory()

        cache = SystemMetricsCache(
            refresh_interval=5.0, sample_interval=0.01, max_age_seconds=5.0
        )
        cache.start()
        try:
            m = cache.get_metrics()
            assert m.source == "cache"
        finally:
            cache.stop()

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_get_metrics_stale_when_max_age_exceeded(self, mock_cpu, mock_vm):
        """캐시 age가 max_age_seconds를 초과하면 source='stale'."""
        mock_cpu.return_value = 25.0
        mock_vm.return_value = _make_virtual_memory()

        cache = SystemMetricsCache(
            refresh_interval=5.0, sample_interval=0.01, max_age_seconds=0.01
        )
        cache._running = True
        cache._do_refresh()

        # max_age_seconds(0.01초) 확실히 초과 대기
        time.sleep(0.15)

        m = cache.get_metrics()
        assert m.source == "stale"
        # age_seconds는 round(age, 1) — 0.15초 sleep이므로 0.1 이상
        assert m.age_seconds >= 0.1

    def test_get_metrics_never_refreshed(self):
        """한 번도 갱신되지 않은 캐시: source='stale' (age=inf)."""
        cache = SystemMetricsCache(max_age_seconds=5.0)
        m = cache.get_metrics()
        assert m.source == "stale"

    # -----------------------------------------------------------------
    # 4.5 get_cpu_percent / get_memory_percent — 읽기 전용
    # -----------------------------------------------------------------

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_get_cpu_percent_returns_cached(self, mock_cpu, mock_vm):
        """get_cpu_percent()는 캐시 값 반환."""
        mock_cpu.return_value = 78.5
        mock_vm.return_value = _make_virtual_memory()

        cache = SystemMetricsCache(refresh_interval=5.0, sample_interval=0.01)
        cache._running = True
        cache._do_refresh()

        assert cache.get_cpu_percent() == round(78.5, 1)

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_get_memory_percent_returns_cached(self, mock_cpu, mock_vm):
        """get_memory_percent()는 캐시 값 반환."""
        mock_cpu.return_value = 10.0
        mock_vm.return_value = _make_virtual_memory(percent=82.3)

        cache = SystemMetricsCache(refresh_interval=5.0, sample_interval=0.01)
        cache._running = True
        cache._do_refresh()

        assert cache.get_memory_percent() == round(82.3, 1)

    # -----------------------------------------------------------------
    # 4.6 get_snapshot_dict() — collect_system_snapshot() 호환
    # -----------------------------------------------------------------

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_get_snapshot_dict_structure(self, mock_cpu, mock_vm):
        """get_snapshot_dict()는 6개 필드의 딕셔너리를 반환한다."""
        mock_cpu.return_value = 40.0
        mock_vm.return_value = _make_virtual_memory(
            percent=60.0, used=1500 * 1024 * 1024, available=500 * 1024 * 1024
        )

        cache = SystemMetricsCache(refresh_interval=5.0, sample_interval=0.01)
        cache._running = True
        cache._do_refresh()

        d = cache.get_snapshot_dict()
        expected_keys = {
            "cpu_percent",
            "memory_percent",
            "memory_used_mb",
            "memory_available_mb",
            "metrics_source",
            "metrics_measured_at",
        }
        assert set(d.keys()) == expected_keys

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_get_snapshot_dict_values_match_cached(self, mock_cpu, mock_vm):
        """get_snapshot_dict() 값이 _cached와 일치."""
        mock_cpu.return_value = 55.5
        mock_vm.return_value = _make_virtual_memory(percent=70.1)

        cache = SystemMetricsCache(refresh_interval=5.0, sample_interval=0.01)
        cache._running = True
        cache._do_refresh()

        d = cache.get_snapshot_dict()
        m = cache._cached
        assert d["cpu_percent"] == m.cpu_percent
        assert d["memory_percent"] == m.memory_percent
        assert d["memory_used_mb"] == m.memory_used_mb
        assert d["memory_available_mb"] == m.memory_available_mb
        assert d["metrics_source"] == m.source
        assert d["metrics_measured_at"] == m.measured_at

    # -----------------------------------------------------------------
    # 4.7 get_stats() — 디버깅 정보
    # -----------------------------------------------------------------

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_get_stats_running(self, mock_cpu, mock_vm):
        """가동 중 get_stats() 반환값 구조 검증."""
        mock_cpu.return_value = 20.0
        mock_vm.return_value = _make_virtual_memory()

        cache = SystemMetricsCache(refresh_interval=1.0, sample_interval=0.01)
        cache.start()
        try:
            stats = cache.get_stats()
            assert stats["running"] is True
            assert stats["refresh_interval"] == cache._refresh_interval
            assert stats["sample_interval"] == cache._sample_interval
            assert stats["max_age_seconds"] == cache._max_age_seconds
            assert stats["cache_age_seconds"] is not None
            assert stats["source"] == "cache"
        finally:
            cache.stop()

    def test_get_stats_not_started(self):
        """미가동: cache_age_seconds가 None이어야 한다."""
        cache = SystemMetricsCache()
        stats = cache.get_stats()
        assert stats["running"] is False
        assert stats["cache_age_seconds"] is None

    # -----------------------------------------------------------------
    # 4.8 _schedule_refresh — 데몬 스레드
    # -----------------------------------------------------------------

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_timer_is_daemon(self, mock_cpu, mock_vm):
        """Timer 스레드는 daemon=True이어야 한다."""
        mock_cpu.return_value = 10.0
        mock_vm.return_value = _make_virtual_memory()

        cache = SystemMetricsCache(refresh_interval=5.0, sample_interval=0.01)
        cache.start()
        try:
            assert cache._timer is not None
            assert cache._timer.daemon is True
        finally:
            cache.stop()

    def test_schedule_refresh_not_running(self):
        """_running=False면 _schedule_refresh()는 Timer를 생성하지 않는다."""
        cache = SystemMetricsCache()
        cache._running = False
        cache._schedule_refresh()
        assert cache._timer is None


# =============================================================================
# 5. Module-level API — 동작 검증 (Behavior)
# =============================================================================


class TestModuleLevelApiBehavior:
    """모듈 레벨 편의 함수 동작 검증."""

    def test_get_system_metrics_cache_returns_instance(self):
        """get_system_metrics_cache()는 SystemMetricsCache 인스턴스를 반환."""
        cache = get_system_metrics_cache()
        assert isinstance(cache, SystemMetricsCache)

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_start_and_stop_system_metrics_cache(self, mock_cpu, mock_vm):
        """start_system_metrics_cache() / stop_system_metrics_cache() 동작."""
        mock_cpu.return_value = 10.0
        mock_vm.return_value = _make_virtual_memory()

        start_system_metrics_cache()
        cache = get_system_metrics_cache()
        try:
            assert cache.is_running() is True
        finally:
            stop_system_metrics_cache()
        assert cache.is_running() is False

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_get_cached_cpu_percent(self, mock_cpu, mock_vm):
        """get_cached_cpu_percent()는 글로벌 캐시의 CPU 값 반환."""
        mock_cpu.return_value = 33.3
        mock_vm.return_value = _make_virtual_memory()

        cache = get_system_metrics_cache()
        cache._refresh_interval = 5.0
        cache._sample_interval = 0.01
        cache.start()
        try:
            result = get_cached_cpu_percent()
            assert result == round(33.3, 1)
        finally:
            cache.stop()

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_get_cached_memory_percent(self, mock_cpu, mock_vm):
        """get_cached_memory_percent()는 글로벌 캐시의 Memory 값 반환."""
        mock_cpu.return_value = 10.0
        mock_vm.return_value = _make_virtual_memory(percent=77.7)

        cache = get_system_metrics_cache()
        cache._refresh_interval = 5.0
        cache._sample_interval = 0.01
        cache.start()
        try:
            result = get_cached_memory_percent()
            assert result == round(77.7, 1)
        finally:
            cache.stop()

    def test_reset_creates_new_instance(self):
        """reset_system_metrics_cache()는 새 글로벌 인스턴스를 생성."""
        cache1 = get_system_metrics_cache()
        reset_system_metrics_cache()
        cache2 = get_system_metrics_cache()
        assert cache1 is not cache2


# =============================================================================
# 6. 소비자 연동 — collect_system_snapshot() 캐시 경로 (Behavior)
# =============================================================================


class TestCollectSystemSnapshotCacheIntegrationBehavior:
    """collect_system_snapshot()이 캐시를 우선 조회하는 동작 검증.

    220 §4.1: cache.is_running() → 캐시 사용, 아니면 psutil 직접 호출.
    """

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_snapshot_uses_cache_when_running(self, mock_cpu, mock_vm):
        """캐시 가동 중 → get_snapshot_dict()으로 캐시 메트릭 조회."""
        mock_cpu.return_value = 60.0
        mock_vm.return_value = _make_virtual_memory(percent=70.0)

        cache = get_system_metrics_cache()
        cache._refresh_interval = 5.0
        cache._sample_interval = 0.01
        cache.start()
        try:
            snapshot = cache.get_snapshot_dict()
            assert snapshot["metrics_source"] == "cache"
            assert snapshot["cpu_percent"] == round(60.0, 1)
            assert snapshot["memory_percent"] == round(70.0, 1)
        finally:
            cache.stop()


# =============================================================================
# 7. 소비자 연동 — web_server_metrics 병합 (Behavior)
# =============================================================================


class TestWebServerMetricsMergeBehavior:
    """web_server_metrics 병합 동작 검증 (220 §4.4.2~4.4.3).

    Celery Task에서 web_server_metrics가 전달되면:
    1. Worker 원본 값을 worker_* 접두사 필드에 보존
    2. 주 필드를 Web Server 값으로 교체
    3. snapshot_source = "web_server_cache+worker"
    """

    @staticmethod
    def _apply_web_metrics_merge(snapshot: dict, web_metrics: dict | None) -> dict:
        """Reproduce the same merge logic as circuit_breaker.py / postmortem.py.

        Code basis:
        - adapters/celery/tasks/circuit_breaker.py L495-L510
        - adapters/celery/tasks/postmortem.py L660-L675
        """
        if web_metrics:
            snapshot["worker_cpu_percent"] = snapshot.get("cpu_percent")
            snapshot["worker_memory_percent"] = snapshot.get("memory_percent")
            snapshot["worker_memory_used_mb"] = snapshot.get("memory_used_mb")
            snapshot["worker_memory_available_mb"] = snapshot.get("memory_available_mb")
            snapshot["cpu_percent"] = web_metrics.get(
                "cpu_percent", snapshot["cpu_percent"]
            )
            snapshot["memory_percent"] = web_metrics.get(
                "memory_percent", snapshot["memory_percent"]
            )
            snapshot["memory_used_mb"] = web_metrics.get(
                "memory_used_mb", snapshot.get("memory_used_mb", 0)
            )
            snapshot["memory_available_mb"] = web_metrics.get(
                "memory_available_mb", snapshot.get("memory_available_mb", 0)
            )
            snapshot["snapshot_source"] = "web_server_cache+worker"
            snapshot["snapshot_note"] = (
                "Main CPU/Memory=Web Server cache, worker_*=Celery Worker measurements."
            )
        else:
            snapshot["snapshot_source"] = "celery_worker"
            snapshot["snapshot_note"] = (
                "CPU/Memory of the Worker node. May differ from the Web Server."
            )
        return snapshot

    def test_merge_web_server_metrics_replaces_main_fields(self):
        """web_server_metrics 전달 시 주 필드가 Web Server 값으로 교체된다."""
        snapshot = {
            "cpu_percent": 45.2,
            "memory_percent": 58.7,
            "memory_used_mb": 1203.4,
            "memory_available_mb": 844.6,
            "timestamp": "2026-02-12T10:00:00+00:00",
        }
        web_metrics = {
            "cpu_percent": 78.5,
            "memory_percent": 72.3,
            "memory_used_mb": 1482.1,
            "memory_available_mb": 565.9,
        }

        result = self._apply_web_metrics_merge(snapshot, web_metrics)

        # 주 필드 = Web Server 값
        assert result["cpu_percent"] == 78.5
        assert result["memory_percent"] == 72.3
        assert result["memory_used_mb"] == 1482.1
        assert result["memory_available_mb"] == 565.9

        # Worker 원본 보존
        assert result["worker_cpu_percent"] == 45.2
        assert result["worker_memory_percent"] == 58.7
        assert result["worker_memory_used_mb"] == 1203.4
        assert result["worker_memory_available_mb"] == 844.6

        # 출처 메타데이터
        assert result["snapshot_source"] == "web_server_cache+worker"

    def test_no_web_server_metrics_uses_celery_worker_source(self):
        """web_server_metrics 미전달 시 snapshot_source='celery_worker'."""
        snapshot = {
            "cpu_percent": 45.2,
            "memory_percent": 58.7,
        }

        result = self._apply_web_metrics_merge(snapshot, None)
        assert result["snapshot_source"] == "celery_worker"

    def test_merge_preserves_existing_fields(self):
        """병합 시 기존 스냅샷 필드(timestamp 등)가 보존된다."""
        snapshot = {
            "cpu_percent": 45.0,
            "memory_percent": 58.0,
            "memory_used_mb": 1200.0,
            "memory_available_mb": 800.0,
            "timestamp": "2026-02-12T10:00:00+00:00",
            "captured_at": "open",
            "service": "payment",
        }
        web_metrics = {"cpu_percent": 80.0, "memory_percent": 75.0}

        result = self._apply_web_metrics_merge(snapshot, web_metrics)

        assert result["timestamp"] == "2026-02-12T10:00:00+00:00"
        assert result["captured_at"] == "open"
        assert result["service"] == "payment"

    def test_partial_web_server_metrics_fallback(self):
        """web_server_metrics에 일부 필드만 있으면 나머지는 Worker 값 유지."""
        snapshot = {
            "cpu_percent": 45.0,
            "memory_percent": 58.0,
            "memory_used_mb": 1200.0,
            "memory_available_mb": 800.0,
        }
        web_metrics = {
            "cpu_percent": 80.0,
            # memory_percent 누락
        }

        result = self._apply_web_metrics_merge(snapshot, web_metrics)

        # cpu_percent는 Web Server 값
        assert result["cpu_percent"] == 80.0
        # memory_percent는 Worker 값 유지 (web_metrics에 없으므로)
        assert result["memory_percent"] == 58.0


# =============================================================================
# 8. _collect_web_server_metrics() — 동작 검증 (Behavior)
# =============================================================================


class TestCollectWebServerMetricsBehavior:
    """bus.py _collect_web_server_metrics() 패턴 동작 검증.

    220 §4.4.1: 캐시 가동 → get_snapshot_dict(), 미가동 → None.
    """

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_returns_dict_when_cache_running(self, mock_cpu, mock_vm):
        """캐시 가동 시 get_snapshot_dict() 반환."""
        mock_cpu.return_value = 50.0
        mock_vm.return_value = _make_virtual_memory()

        cache = get_system_metrics_cache()
        cache._refresh_interval = 5.0
        cache._sample_interval = 0.01
        cache.start()
        try:
            assert cache.is_running() is True
            result = cache.get_snapshot_dict()
            assert isinstance(result, dict)
            assert "cpu_percent" in result
            assert "memory_percent" in result
        finally:
            cache.stop()

    def test_returns_none_equivalent_when_not_running(self):
        """캐시 미가동 시 is_running()이 False → 호출 측에서 None 반환 로직."""
        cache = get_system_metrics_cache()
        assert cache.is_running() is False
        # bus.py의 _collect_web_server_metrics() 패턴 재현
        result = None
        if cache.is_running():
            result = cache.get_snapshot_dict()
        assert result is None


# =============================================================================
# 9. 스레드 안전성 — 동작 검증 (Behavior)
# =============================================================================


class TestThreadSafetyBehavior:
    """동시성 안전 동작 검증."""

    @patch("psutil.virtual_memory")
    @patch("psutil.cpu_percent")
    def test_concurrent_reads_during_refresh(self, mock_cpu, mock_vm):
        """백그라운드 갱신 중에도 읽기 호출이 예외 없이 성공한다."""
        call_count = 0

        def slow_cpu_percent(interval=None):
            nonlocal call_count
            call_count += 1
            time.sleep(0.01)
            return 50.0 + call_count

        mock_cpu.side_effect = slow_cpu_percent
        mock_vm.return_value = _make_virtual_memory()

        cache = SystemMetricsCache(refresh_interval=5.0, sample_interval=0.01)
        cache._running = True
        cache._do_refresh()  # 초기값 설정

        results = []
        errors = []

        def reader():
            try:
                for _ in range(100):
                    _ = cache.get_cpu_percent()
                    _ = cache.get_memory_percent()
                    _ = cache.get_metrics()
                results.append(True)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=reader) for _ in range(5)]
        for t in threads:
            t.start()

        # 읽기 중 갱신 수행
        cache._do_refresh()

        for t in threads:
            t.join(timeout=5.0)

        assert len(errors) == 0, f"동시 읽기 중 에러 발생: {errors}"
        assert len(results) == 5
