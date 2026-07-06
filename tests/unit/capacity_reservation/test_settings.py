"""
Capacity Reservation Settings Unit Tests.

Test Categories:
    A. Contract: 설계 문서 계약값 검증 (하드코딩)
    B. Boundary: Pydantic Field 제약 경계값 분석
    C. Behavior: 싱글톤/캐시 동작 검증
"""

import pytest
from pydantic import ValidationError

from baldur.settings.capacity_reservation import (
    CapacityReservationSettings,
    get_capacity_reservation_settings,
    reset_capacity_reservation_settings,
)

# =============================================================================
# A. Contract Tests — 설계 문서 304 계약값 검증
# =============================================================================


class TestCapacityReservationSettingsContract:
    """설계 문서 304에 명시된 기본값 및 제약 조건 계약 검증."""

    def test_enabled_default_is_false(self):
        """enabled 기본값: False."""
        settings = CapacityReservationSettings()
        assert settings.enabled is False

    def test_default_warmup_minutes_contract(self):
        """default_warmup_minutes 기본값: 5."""
        settings = CapacityReservationSettings()
        assert settings.default_warmup_minutes == 5

    def test_scheduler_interval_seconds_contract(self):
        """scheduler_interval_seconds 기본값: 30."""
        settings = CapacityReservationSettings()
        assert settings.scheduler_interval_seconds == 30

    def test_max_rate_multiplier_contract(self):
        """max_rate_multiplier 기본값: 5.0."""
        settings = CapacityReservationSettings()
        assert settings.max_rate_multiplier == 5.0

    def test_max_pool_multiplier_contract(self):
        """max_pool_multiplier 기본값: 3.0."""
        settings = CapacityReservationSettings()
        assert settings.max_pool_multiplier == 3.0

    def test_max_bulkhead_extra_permits_contract(self):
        """max_bulkhead_extra_permits 기본값: 100."""
        settings = CapacityReservationSettings()
        assert settings.max_bulkhead_extra_permits == 100

    def test_max_concurrent_events_contract(self):
        """max_concurrent_events 기본값: 3."""
        settings = CapacityReservationSettings()
        assert settings.max_concurrent_events == 3

    def test_dry_run_default_is_true(self):
        """dry_run 기본값: True."""
        settings = CapacityReservationSettings()
        assert settings.dry_run is True

    def test_safety_valve_cpu_threshold_contract(self):
        """safety_valve_cpu_threshold 기본값: 0.95."""
        settings = CapacityReservationSettings()
        assert settings.safety_valve_cpu_threshold == 0.95

    def test_safety_valve_error_rate_threshold_contract(self):
        """safety_valve_error_rate_threshold 기본값: 0.10."""
        settings = CapacityReservationSettings()
        assert settings.safety_valve_error_rate_threshold == 0.10

    def test_safety_valve_min_hold_seconds_contract(self):
        """safety_valve_min_hold_seconds 기본값: 120."""
        settings = CapacityReservationSettings()
        assert settings.safety_valve_min_hold_seconds == 120

    def test_env_prefix_contract(self):
        """환경변수 접두사: BALDUR_CAPACITY_RESERVATION_."""
        assert (
            CapacityReservationSettings.model_config["env_prefix"]
            == "BALDUR_CAPACITY_RESERVATION_"
        )

    def test_cooldown_grace_period_seconds_contract(self):
        """cooldown_grace_period_seconds 기본값: 300."""
        settings = CapacityReservationSettings()
        assert settings.cooldown_grace_period_seconds == 300

    def test_field_count_contract(self):
        """설정 필드 수: 12개."""
        assert len(CapacityReservationSettings.model_fields) == 12


# =============================================================================
# B. Boundary Tests — Pydantic Field 제약 경계값 분석
# =============================================================================


class TestCapacityReservationSettingsBoundaryContract:
    """Pydantic Field 제약 경계값 검증."""

    # --- default_warmup_minutes: ge=1, le=60 ---

    def test_warmup_minutes_below_minimum_raises(self):
        """default_warmup_minutes < 1 → ValidationError."""
        with pytest.raises(ValidationError):
            CapacityReservationSettings(default_warmup_minutes=0)

    def test_warmup_minutes_at_minimum_accepted(self):
        """default_warmup_minutes == 1 → 성공."""
        s = CapacityReservationSettings(default_warmup_minutes=1)
        assert s.default_warmup_minutes == 1

    def test_warmup_minutes_at_maximum_accepted(self):
        """default_warmup_minutes == 60 → 성공."""
        s = CapacityReservationSettings(default_warmup_minutes=60)
        assert s.default_warmup_minutes == 60

    def test_warmup_minutes_above_maximum_raises(self):
        """default_warmup_minutes > 60 → ValidationError."""
        with pytest.raises(ValidationError):
            CapacityReservationSettings(default_warmup_minutes=61)

    # --- scheduler_interval_seconds: ge=5, le=300 ---

    def test_scheduler_interval_below_minimum_raises(self):
        """scheduler_interval_seconds < 5 → ValidationError."""
        with pytest.raises(ValidationError):
            CapacityReservationSettings(scheduler_interval_seconds=4)

    def test_scheduler_interval_at_minimum_accepted(self):
        """scheduler_interval_seconds == 5 → 성공."""
        s = CapacityReservationSettings(scheduler_interval_seconds=5)
        assert s.scheduler_interval_seconds == 5

    def test_scheduler_interval_above_maximum_raises(self):
        """scheduler_interval_seconds > 300 → ValidationError."""
        with pytest.raises(ValidationError):
            CapacityReservationSettings(scheduler_interval_seconds=301)

    # --- max_rate_multiplier: ge=1.0, le=20.0 ---

    def test_rate_multiplier_below_minimum_raises(self):
        """max_rate_multiplier < 1.0 → ValidationError."""
        with pytest.raises(ValidationError):
            CapacityReservationSettings(max_rate_multiplier=0.9)

    def test_rate_multiplier_at_minimum_accepted(self):
        """max_rate_multiplier == 1.0 → 성공."""
        s = CapacityReservationSettings(max_rate_multiplier=1.0)
        assert s.max_rate_multiplier == 1.0

    def test_rate_multiplier_above_maximum_raises(self):
        """max_rate_multiplier > 20.0 → ValidationError."""
        with pytest.raises(ValidationError):
            CapacityReservationSettings(max_rate_multiplier=20.1)

    # --- max_pool_multiplier: ge=1.0, le=10.0 ---

    def test_pool_multiplier_below_minimum_raises(self):
        """max_pool_multiplier < 1.0 → ValidationError."""
        with pytest.raises(ValidationError):
            CapacityReservationSettings(max_pool_multiplier=0.9)

    def test_pool_multiplier_above_maximum_raises(self):
        """max_pool_multiplier > 10.0 → ValidationError."""
        with pytest.raises(ValidationError):
            CapacityReservationSettings(max_pool_multiplier=10.1)

    # --- max_bulkhead_extra_permits: ge=0, le=1000 ---

    def test_bulkhead_permits_at_minimum_accepted(self):
        """max_bulkhead_extra_permits == 0 → 성공."""
        s = CapacityReservationSettings(max_bulkhead_extra_permits=0)
        assert s.max_bulkhead_extra_permits == 0

    def test_bulkhead_permits_below_minimum_raises(self):
        """max_bulkhead_extra_permits < 0 → ValidationError."""
        with pytest.raises(ValidationError):
            CapacityReservationSettings(max_bulkhead_extra_permits=-1)

    def test_bulkhead_permits_above_maximum_raises(self):
        """max_bulkhead_extra_permits > 1000 → ValidationError."""
        with pytest.raises(ValidationError):
            CapacityReservationSettings(max_bulkhead_extra_permits=1001)

    # --- safety_valve_cpu_threshold: ge=0.5, le=1.0 ---

    def test_cpu_threshold_below_minimum_raises(self):
        """safety_valve_cpu_threshold < 0.5 → ValidationError."""
        with pytest.raises(ValidationError):
            CapacityReservationSettings(safety_valve_cpu_threshold=0.49)

    def test_cpu_threshold_at_minimum_accepted(self):
        """safety_valve_cpu_threshold == 0.5 → 성공."""
        s = CapacityReservationSettings(safety_valve_cpu_threshold=0.5)
        assert s.safety_valve_cpu_threshold == 0.5

    def test_cpu_threshold_above_maximum_raises(self):
        """safety_valve_cpu_threshold > 1.0 → ValidationError."""
        with pytest.raises(ValidationError):
            CapacityReservationSettings(safety_valve_cpu_threshold=1.01)

    # --- safety_valve_error_rate_threshold: ge=0.01, le=1.0 ---

    def test_error_rate_threshold_below_minimum_raises(self):
        """safety_valve_error_rate_threshold < 0.01 → ValidationError."""
        with pytest.raises(ValidationError):
            CapacityReservationSettings(safety_valve_error_rate_threshold=0.009)

    def test_error_rate_threshold_at_minimum_accepted(self):
        """safety_valve_error_rate_threshold == 0.01 → 성공."""
        s = CapacityReservationSettings(safety_valve_error_rate_threshold=0.01)
        assert s.safety_valve_error_rate_threshold == 0.01

    # --- safety_valve_min_hold_seconds: ge=30, le=600 ---

    def test_min_hold_below_minimum_raises(self):
        """safety_valve_min_hold_seconds < 30 → ValidationError."""
        with pytest.raises(ValidationError):
            CapacityReservationSettings(safety_valve_min_hold_seconds=29)

    def test_min_hold_at_minimum_accepted(self):
        """safety_valve_min_hold_seconds == 30 → 성공."""
        s = CapacityReservationSettings(safety_valve_min_hold_seconds=30)
        assert s.safety_valve_min_hold_seconds == 30

    def test_min_hold_above_maximum_raises(self):
        """safety_valve_min_hold_seconds > 600 → ValidationError."""
        with pytest.raises(ValidationError):
            CapacityReservationSettings(safety_valve_min_hold_seconds=601)

    # --- cooldown_grace_period_seconds: ge=60, le=3600 ---

    def test_cooldown_grace_below_minimum_raises(self):
        """cooldown_grace_period_seconds < 60 → ValidationError."""
        with pytest.raises(ValidationError):
            CapacityReservationSettings(cooldown_grace_period_seconds=59)

    def test_cooldown_grace_at_minimum_accepted(self):
        """cooldown_grace_period_seconds == 60 → 성공."""
        s = CapacityReservationSettings(cooldown_grace_period_seconds=60)
        assert s.cooldown_grace_period_seconds == 60

    def test_cooldown_grace_above_maximum_raises(self):
        """cooldown_grace_period_seconds > 3600 → ValidationError."""
        with pytest.raises(ValidationError):
            CapacityReservationSettings(cooldown_grace_period_seconds=3601)

    # --- max_concurrent_events: ge=1, le=10 ---

    def test_max_concurrent_events_below_minimum_raises(self):
        """max_concurrent_events < 1 → ValidationError."""
        with pytest.raises(ValidationError):
            CapacityReservationSettings(max_concurrent_events=0)

    def test_max_concurrent_events_above_maximum_raises(self):
        """max_concurrent_events > 10 → ValidationError."""
        with pytest.raises(ValidationError):
            CapacityReservationSettings(max_concurrent_events=11)


# =============================================================================
# C. Behavior Tests — 싱글톤/캐시 동작
# =============================================================================


class TestCapacityReservationSettingsSingletonBehavior:
    """설정 싱글톤 캐싱/리셋 동작 검증."""

    def setup_method(self):
        reset_capacity_reservation_settings()

    def teardown_method(self):
        reset_capacity_reservation_settings()

    def test_get_returns_same_instance(self):
        """get_capacity_reservation_settings()는 동일 인스턴스를 반환."""
        first = get_capacity_reservation_settings()
        second = get_capacity_reservation_settings()
        assert first is second

    def test_reset_clears_cached_instance(self):
        """reset 후 새 인스턴스가 생성된다."""
        first = get_capacity_reservation_settings()
        reset_capacity_reservation_settings()
        second = get_capacity_reservation_settings()
        assert first is not second
