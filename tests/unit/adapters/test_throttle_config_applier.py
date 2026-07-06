"""
ThrottleConfigApplier 단위 테스트.

SLA 파라미터 Atomic Swap, 레거시 No-op, 미지원 파라미터 거부를 검증한다.


- 기본값 참조: ThrottleConfig() 기본값에서 파생
- 상수 import: PARAM_TO_CONFIG, LEGACY_NOOP_PARAMS 소스 참조
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


import pytest

from baldur.adapters.config_applier.throttle import ThrottleConfigApplier
from baldur_pro.services.throttle.adaptive import (
    get_adaptive_throttle,
    reset_adaptive_throttle,
)
from baldur_pro.services.throttle.config import ThrottleConfig

# 소스 상수 참조 (하드코딩 방지)
_PARAM_TO_CONFIG = ThrottleConfigApplier.PARAM_TO_CONFIG
_LEGACY_NOOP_PARAMS = ThrottleConfigApplier.LEGACY_NOOP_PARAMS


@pytest.fixture(autouse=True)
def _reset_throttle():
    """테스트 격리를 위해 글로벌 싱글톤 초기화."""
    reset_adaptive_throttle()
    yield
    reset_adaptive_throttle()


@pytest.fixture
def applier():
    """ThrottleConfigApplier 인스턴스."""
    return ThrottleConfigApplier()


@pytest.fixture
def throttle():
    """테스트용 AdaptiveThrottle 인스턴스 (싱글톤 등록, 기본 config 사용).

    get_adaptive_throttle() lazily creates the global singleton under the
    module lock internally, so no scaffolding lock or _global_adaptive_throttle
    mutation is needed; the autouse _reset_throttle fixture handles teardown.
    """
    return get_adaptive_throttle(ThrottleConfig())  # 기본값 사용 (하드코딩 방지)


class TestThrottleConfigApplierApply:
    """apply() 메서드 테스트."""

    def test_apply_sla_warning_ms(self, applier, throttle):
        """throttle_sla_warning_ms 적용 시 config.sla_warning_ms가 변경되어야 한다."""
        original = throttle.config.sla_warning_ms
        new_value = original + 50
        result = applier.apply("throttle_sla_warning_ms", float(new_value))

        assert result is True
        assert throttle.config.sla_warning_ms == new_value

    def test_apply_sla_critical_ms(self, applier, throttle):
        """throttle_sla_critical_ms 적용 시 config.sla_critical_ms가 변경되어야 한다."""
        original = throttle.config.sla_critical_ms
        new_value = original + 200
        result = applier.apply("throttle_sla_critical_ms", float(new_value))

        assert result is True
        assert throttle.config.sla_critical_ms == new_value

    def test_apply_atomic_swap_preserves_other_fields(self, applier, throttle):
        """SLA 값 변경 시 다른 config 필드는 보존되어야 한다."""
        original_initial_limit = throttle.config.initial_limit
        original_sla_critical = throttle.config.sla_critical_ms

        new_warning = throttle.config.sla_warning_ms + 100
        applier.apply("throttle_sla_warning_ms", float(new_warning))

        assert throttle.config.initial_limit == original_initial_limit
        assert throttle.config.sla_critical_ms == original_sla_critical

    def test_apply_original_config_immutable(self, applier, throttle):
        """apply() 후 이전 config 객체가 변경되지 않아야 한다 (Pydantic model_copy 검증)."""
        old_config = throttle.config
        old_warning = old_config.sla_warning_ms

        applier.apply("throttle_sla_warning_ms", float(old_warning + 799))

        assert old_config.sla_warning_ms == old_warning

    # 525 D4: xdist mock_leak — caplog INFO capture races with sibling
    # tests under -n 6 (project_xdist_isolation pattern).
    @pytest.mark.flaky_quarantine(
        issue="525", first_seen="2026-05-20", category="mock_leak"
    )
    def test_apply_rate_limit_rps_noop(self, applier, throttle, caplog):
        """rate_limit_rps는 No-op 처리되어 True를 반환하고 config 변경 없어야 한다."""
        original_warning = throttle.config.sla_warning_ms
        original_critical = throttle.config.sla_critical_ms

        with caplog.at_level("INFO"):
            result = applier.apply("rate_limit_rps", 1000.0)

        assert result is True
        assert throttle.config.sla_warning_ms == original_warning
        assert throttle.config.sla_critical_ms == original_critical
        assert any("deprecated_no_op" in r.message for r in caplog.records)

    def test_apply_unknown_parameter_returns_false(self, applier, throttle):
        """미지원 파라미터는 False를 반환해야 한다."""
        result = applier.apply("unknown_param", 100.0)
        assert result is False


class TestThrottleConfigApplierGetCurrent:
    """get_current() 메서드 테스트."""

    def test_get_current_sla_warning_ms(self, applier, throttle):
        """throttle_sla_warning_ms의 현재 값을 정확히 반환해야 한다."""
        expected = float(throttle.config.sla_warning_ms)
        assert applier.get_current("throttle_sla_warning_ms") == expected

    def test_get_current_sla_critical_ms(self, applier, throttle):
        """throttle_sla_critical_ms의 현재 값을 정확히 반환해야 한다."""
        expected = float(throttle.config.sla_critical_ms)
        assert applier.get_current("throttle_sla_critical_ms") == expected

    def test_get_current_rate_limit_rps_returns_zero(self, applier, throttle):
        """rate_limit_rps 레거시 파라미터는 0.0을 반환해야 한다."""
        assert applier.get_current("rate_limit_rps") == 0.0

    def test_get_current_unknown_parameter_raises(self, applier, throttle):
        """미지원 파라미터는 ValueError를 발생시켜야 한다."""
        with pytest.raises(ValueError, match="not supported"):
            applier.get_current("unknown_param")


class TestThrottleConfigApplierRollback:
    """rollback() 메서드 테스트."""

    def test_rollback_restores_value(self, applier, throttle):
        """rollback()이 apply()와 동일하게 값을 적용해야 한다."""
        original_value = float(throttle.config.sla_warning_ms)

        # 변경 후 롤백
        applier.apply("throttle_sla_warning_ms", original_value + 799.0)
        result = applier.rollback("throttle_sla_warning_ms", original_value)

        assert result is True
        assert throttle.config.sla_warning_ms == int(original_value)


class TestThrottleConfigApplierConstants:
    """클래스 상수 설계 계약 검증 (하드코딩 허용)."""

    def test_param_to_config_has_sla_parameters(self):
        """PARAM_TO_CONFIG에 SLA 파라미터 2개가 포함되어야 한다."""
        assert "throttle_sla_warning_ms" in _PARAM_TO_CONFIG
        assert "throttle_sla_critical_ms" in _PARAM_TO_CONFIG
        assert len(_PARAM_TO_CONFIG) == 2

    def test_param_to_config_exact_mapping(self):
        """PARAM_TO_CONFIG의 정확한 값 매핑 (설계 계약)."""
        assert _PARAM_TO_CONFIG["throttle_sla_warning_ms"] == "sla_warning_ms"
        assert _PARAM_TO_CONFIG["throttle_sla_critical_ms"] == "sla_critical_ms"

    def test_legacy_noop_params_has_rate_limit_rps(self):
        """LEGACY_NOOP_PARAMS에 rate_limit_rps가 포함되어야 한다."""
        assert "rate_limit_rps" in _LEGACY_NOOP_PARAMS
