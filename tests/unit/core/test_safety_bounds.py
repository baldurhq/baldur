"""
Tests for Safety Bounds - 자율 조정 안전 한계
"""

import threading

import pytest

from baldur.core.safety_bounds import (
    ParameterBound,
    SafetyBounds,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def safety_bounds():
    """Create a SafetyBounds instance with defaults."""
    return SafetyBounds()


@pytest.fixture
def strict_safety_bounds():
    """Create a strict SafetyBounds instance."""
    return SafetyBounds(strict_mode=True)


@pytest.fixture
def lenient_safety_bounds():
    """Create a lenient SafetyBounds instance."""
    return SafetyBounds(strict_mode=False)


@pytest.fixture
def custom_safety_bounds():
    """Create SafetyBounds with custom bounds."""
    return SafetyBounds(
        custom_bounds={
            "custom_param": {
                "min_value": 10,
                "max_value": 100,
                "max_change_per_cycle": 0.25,
            }
        }
    )


# =============================================================================
# ParameterBound Tests
# =============================================================================


class TestParameterBound:
    """Test ParameterBound dataclass."""

    def test_bound_creation(self):
        """ParameterBound 생성."""
        bound = ParameterBound(
            min_value=100,
            max_value=10000,
            max_change_per_cycle=0.3,
        )

        assert bound.min_value == 100
        assert bound.max_value == 10000
        assert bound.max_change_per_cycle == 0.3

    @pytest.mark.parametrize(
        ("min_val", "max_val", "change_rate", "expected"),
        [
            (100, 10000, 0.3, True),  # 유효한 설정
            (0, 100, 1.0, True),  # 변경률 정확히 1.0 유효
            (10000, 100, 0.3, False),  # min > max
            (100, 10000, 0, False),  # 변경률 0
            (100, 10000, -0.1, False),  # 변경률 음수
            (100, 10000, 1.5, False),  # 변경률 1 초과
        ],
        ids=[
            "valid",
            "exact_one",
            "min_gt_max",
            "zero_rate",
            "negative_rate",
            "over_one_rate",
        ],
    )
    def test_bound_validate(self, min_val, max_val, change_rate, expected):
        """ParameterBound.validate() 각 조건별 검증."""
        bound = ParameterBound(
            min_value=min_val,
            max_value=max_val,
            max_change_per_cycle=change_rate,
        )
        assert bound.validate() is expected


# =============================================================================
# SafetyBounds Initialization Tests
# =============================================================================


class TestSafetyBoundsInit:
    """Test SafetyBounds initialization."""

    def test_init_with_defaults(self, safety_bounds):
        """기본 한계가 로드되어야 함."""
        assert len(safety_bounds.bounds) > 0
        assert "timeout_ms" in safety_bounds.bounds
        assert "retry_count" in safety_bounds.bounds
        assert "circuit_breaker_threshold" in safety_bounds.bounds

    def test_init_with_custom_bounds(self, custom_safety_bounds):
        """커스텀 한계 적용."""
        assert "custom_param" in custom_safety_bounds.bounds

        bound = custom_safety_bounds.bounds["custom_param"]
        assert bound.min_value == 10
        assert bound.max_value == 100

    def test_init_strict_mode(self, strict_safety_bounds):
        """strict_mode 설정."""
        assert strict_safety_bounds.strict_mode is True

    def test_init_lenient_mode(self, lenient_safety_bounds):
        """lenient_mode 설정."""
        assert lenient_safety_bounds.strict_mode is False


# =============================================================================
# is_within_bounds Tests
# =============================================================================


class TestIsWithinBounds:
    """Test is_within_bounds method."""

    def test_within_bounds(self, safety_bounds):
        """값이 범위 내인 경우 True."""
        # timeout_ms: 100 ~ 30000
        assert safety_bounds.is_within_bounds("timeout_ms", 5000) is True
        assert safety_bounds.is_within_bounds("timeout_ms", 100) is True
        assert safety_bounds.is_within_bounds("timeout_ms", 30000) is True

    def test_below_minimum(self, safety_bounds):
        """값이 최소값 미만인 경우 False."""
        # timeout_ms min: 100
        assert safety_bounds.is_within_bounds("timeout_ms", 50) is False

    def test_above_maximum(self, safety_bounds):
        """값이 최대값 초과인 경우 False."""
        # timeout_ms max: 30000
        assert safety_bounds.is_within_bounds("timeout_ms", 50000) is False

    def test_change_ratio_within_limit(self, safety_bounds):
        """변경폭이 제한 내인 경우 True."""
        # timeout_ms max_change: 0.3 (30%)
        # current: 1000, new: 1200 (20% 증가) -> OK
        assert (
            safety_bounds.is_within_bounds("timeout_ms", 1200, current_value=1000)
            is True
        )

    def test_change_ratio_exceeds_limit(self, safety_bounds):
        """변경폭이 제한 초과인 경우 False."""
        # timeout_ms max_change: 0.3 (30%)
        # current: 1000, new: 1500 (50% 증가) -> NOT OK
        assert (
            safety_bounds.is_within_bounds("timeout_ms", 1500, current_value=1000)
            is False
        )

    def test_unknown_parameter_strict_mode(self, strict_safety_bounds):
        """strict mode에서 알 수 없는 파라미터 거부."""
        assert strict_safety_bounds.is_within_bounds("unknown_param", 100) is False

    def test_unknown_parameter_lenient_mode(self, lenient_safety_bounds):
        """lenient mode에서 알 수 없는 파라미터 허용."""
        assert lenient_safety_bounds.is_within_bounds("unknown_param", 100) is True

    def test_change_ratio_with_zero_current(self, safety_bounds):
        """현재 값이 0인 경우 변경폭 검증 스킵."""
        # 절대 범위만 검증
        assert (
            safety_bounds.is_within_bounds("timeout_ms", 1000, current_value=0) is True
        )


# =============================================================================
# clamp_to_bounds Tests
# =============================================================================


class TestClampToBounds:
    """Test clamp_to_bounds method."""

    def test_clamp_within_bounds(self, safety_bounds):
        """범위 내 값은 변경 없음."""
        result = safety_bounds.clamp_to_bounds("timeout_ms", 5000)
        assert result == 5000

    def test_clamp_below_minimum(self, safety_bounds):
        """최소값 미만이면 최소값으로."""
        # timeout_ms min: 100
        result = safety_bounds.clamp_to_bounds("timeout_ms", 50)
        assert result == 100

    def test_clamp_above_maximum(self, safety_bounds):
        """최대값 초과면 최대값으로."""
        # timeout_ms max: 30000
        result = safety_bounds.clamp_to_bounds("timeout_ms", 50000)
        assert result == 30000

    def test_clamp_change_ratio(self, safety_bounds):
        """변경폭 제한 적용."""
        # timeout_ms max_change: 0.3 (30%)
        # current: 1000, new: 2000 (100% 증가) -> 1300으로 제한
        result = safety_bounds.clamp_to_bounds("timeout_ms", 2000, current_value=1000)
        assert result == 1300  # 1000 * 1.3

    def test_clamp_change_ratio_decrease(self, safety_bounds):
        """감소 시에도 변경폭 제한 적용."""
        # current: 1000, new: 500 (50% 감소) -> 700으로 제한
        result = safety_bounds.clamp_to_bounds("timeout_ms", 500, current_value=1000)
        assert result == 700  # 1000 - (1000 * 0.3)

    def test_clamp_unknown_parameter(self, safety_bounds):
        """알 수 없는 파라미터는 변경 없음."""
        result = safety_bounds.clamp_to_bounds("unknown_param", 999)
        assert result == 999


# =============================================================================
# update_bounds Tests
# =============================================================================


class TestUpdateBounds:
    """Test update_bounds method."""

    def test_update_existing_bounds(self, safety_bounds):
        """기존 한계 업데이트."""
        success = safety_bounds.update_bounds(
            "timeout_ms",
            {"min_value": 200, "max_value": 20000, "max_change_per_cycle": 0.2},
        )

        assert success is True

        bound = safety_bounds.bounds["timeout_ms"]
        assert bound.min_value == 200
        assert bound.max_value == 20000
        assert bound.max_change_per_cycle == 0.2

    def test_add_new_bounds(self, safety_bounds):
        """새로운 한계 추가."""
        success = safety_bounds.update_bounds(
            "new_param", {"min_value": 1, "max_value": 100, "max_change_per_cycle": 0.5}
        )

        assert success is True
        assert "new_param" in safety_bounds.bounds

    def test_update_invalid_bounds(self, safety_bounds):
        """유효하지 않은 한계는 거부."""
        # min > max
        success = safety_bounds.update_bounds(
            "timeout_ms",
            {"min_value": 10000, "max_value": 100, "max_change_per_cycle": 0.3},
        )

        assert success is False

    def test_update_with_defaults(self, safety_bounds):
        """부분 설정 시 기본값 사용."""
        success = safety_bounds.update_bounds(
            "partial_param", {"min_value": 10}
        )  # max_value와 max_change_per_cycle 누락

        assert success is True


# =============================================================================
# remove_bounds Tests
# =============================================================================


class TestRemoveBounds:
    """Test remove_bounds method."""

    def test_remove_existing_bounds(self, safety_bounds):
        """기존 한계 제거."""
        assert "timeout_ms" in safety_bounds.bounds

        success = safety_bounds.remove_bounds("timeout_ms")

        assert success is True
        assert "timeout_ms" not in safety_bounds.bounds

    def test_remove_nonexistent_bounds(self, safety_bounds):
        """존재하지 않는 한계 제거 시 False."""
        success = safety_bounds.remove_bounds("nonexistent_param")

        assert success is False


# =============================================================================
# get_bounds Tests
# =============================================================================


class TestGetBounds:
    """Test get_bounds method."""

    def test_get_existing_bounds(self, safety_bounds):
        """기존 한계 조회."""
        bounds = safety_bounds.get_bounds("timeout_ms")

        assert bounds is not None
        assert "min_value" in bounds
        assert "max_value" in bounds
        assert "max_change_per_cycle" in bounds

    def test_get_nonexistent_bounds(self, safety_bounds):
        """존재하지 않는 한계 조회 시 None."""
        bounds = safety_bounds.get_bounds("nonexistent_param")

        assert bounds is None

    def test_get_all_bounds(self, safety_bounds):
        """모든 한계 조회."""
        all_bounds = safety_bounds.get_all_bounds()

        assert isinstance(all_bounds, dict)
        assert len(all_bounds) > 0
        assert "timeout_ms" in all_bounds
        assert "retry_count" in all_bounds


# =============================================================================
# check_all Tests
# =============================================================================


class TestCheckAll:
    """Test check_all method."""

    def test_check_all_within_bounds(self, safety_bounds):
        """모든 값이 범위 내."""
        values = {
            "timeout_ms": 5000,
            "retry_count": 3,
        }

        results = safety_bounds.check_all(values)

        assert results["timeout_ms"] is True
        assert results["retry_count"] is True

    def test_check_all_some_invalid(self, safety_bounds):
        """일부 값이 범위 밖."""
        values = {
            "timeout_ms": 5000,  # Valid
            "retry_count": 100,  # Invalid (max: 10)
        }

        results = safety_bounds.check_all(values)

        assert results["timeout_ms"] is True
        assert results["retry_count"] is False

    def test_check_all_with_current_values(self, safety_bounds):
        """현재 값과 함께 검증."""
        values = {
            "timeout_ms": 1500,  # 50% increase
        }
        current_values = {
            "timeout_ms": 1000,
        }

        results = safety_bounds.check_all(values, current_values)

        # 30% 제한 초과
        assert results["timeout_ms"] is False


# =============================================================================
# reset_to_defaults Tests
# =============================================================================


class TestResetToDefaults:
    """Test reset_to_defaults method."""

    def test_reset_to_defaults(self, safety_bounds):
        """기본값으로 리셋."""
        # 먼저 변경
        safety_bounds.update_bounds(
            "timeout_ms",
            {"min_value": 999, "max_value": 999, "max_change_per_cycle": 0.99},
        )
        safety_bounds.update_bounds(
            "custom_param",
            {"min_value": 1, "max_value": 100, "max_change_per_cycle": 0.5},
        )

        # 리셋
        safety_bounds.reset_to_defaults()

        # 기본값 확인 (_get_default_bounds() 메서드 사용)
        default_bounds = SafetyBounds._get_default_bounds()
        timeout_bound = safety_bounds.bounds["timeout_ms"]
        assert timeout_bound.min_value == default_bounds["timeout_ms"].min_value
        assert timeout_bound.max_value == default_bounds["timeout_ms"].max_value

        # 커스텀 파라미터는 제거됨
        assert "custom_param" not in safety_bounds.bounds


# =============================================================================
# Thread Safety Tests
# =============================================================================


class TestThreadSafety:
    """Test thread safety of SafetyBounds."""

    def test_concurrent_reads(self, safety_bounds):
        """동시 읽기 테스트."""
        results = []

        def read_bounds():
            for _ in range(100):
                safety_bounds.is_within_bounds("timeout_ms", 5000)
                safety_bounds.get_bounds("timeout_ms")
            results.append(True)

        threads = [threading.Thread(target=read_bounds) for _ in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 5

    def test_concurrent_writes(self, safety_bounds):
        """동시 쓰기 테스트."""
        errors = []

        def update_bounds(param_id):
            try:
                for i in range(50):
                    safety_bounds.update_bounds(
                        f"param_{param_id}_{i}",
                        {"min_value": 1, "max_value": 100, "max_change_per_cycle": 0.3},
                    )
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=update_bounds, args=(i,)) for i in range(3)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


# =============================================================================
# Default Bounds Values Tests
# =============================================================================


class TestDefaultBoundsValues:
    """Test default bounds values."""

    def test_timeout_ms_defaults(self, safety_bounds):
        """timeout_ms 기본값."""
        bounds = safety_bounds.get_bounds("timeout_ms")

        assert bounds["min_value"] == 100
        assert bounds["max_value"] == 30000
        assert bounds["max_change_per_cycle"] == 0.3

    def test_retry_count_defaults(self, safety_bounds):
        """retry_count 기본값."""
        bounds = safety_bounds.get_bounds("retry_count")

        assert bounds["min_value"] == 0
        assert bounds["max_value"] == 10
        assert bounds["max_change_per_cycle"] == 0.5

    def test_circuit_breaker_threshold_defaults(self, safety_bounds):
        """circuit_breaker_threshold 기본값."""
        bounds = safety_bounds.get_bounds("circuit_breaker_threshold")

        assert bounds["min_value"] == 0.1
        assert bounds["max_value"] == 0.9
        assert bounds["max_change_per_cycle"] == 0.2

    def test_rate_limit_rps_defaults(self, safety_bounds):
        """rate_limit_rps 기본값."""
        bounds = safety_bounds.get_bounds("rate_limit_rps")

        assert bounds["min_value"] == 10
        assert bounds["max_value"] == 10000
        assert bounds["max_change_per_cycle"] == 0.2


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Test edge cases."""

    def test_zero_value_within_bounds(self, safety_bounds):
        """0 값이 범위 내인 경우."""
        # retry_count min: 0
        assert safety_bounds.is_within_bounds("retry_count", 0) is True

    def test_float_values(self, safety_bounds):
        """실수 값 처리."""
        assert safety_bounds.is_within_bounds("circuit_breaker_threshold", 0.55) is True

        clamped = safety_bounds.clamp_to_bounds("circuit_breaker_threshold", 0.55)
        assert clamped == 0.55

    def test_boundary_values(self, safety_bounds):
        """경계값 처리."""
        # 정확히 min
        assert safety_bounds.is_within_bounds("timeout_ms", 100) is True
        # 정확히 max
        assert safety_bounds.is_within_bounds("timeout_ms", 30000) is True

    def test_very_small_change(self, safety_bounds):
        """매우 작은 변경."""
        # 0.1% 변경 - OK
        assert (
            safety_bounds.is_within_bounds("timeout_ms", 1001, current_value=1000)
            is True
        )

    def test_exactly_max_change(self, safety_bounds):
        """정확히 최대 변경폭."""
        # 정확히 30% 변경
        assert (
            safety_bounds.is_within_bounds("timeout_ms", 1300, current_value=1000)
            is True
        )

    def test_slightly_over_max_change(self, safety_bounds):
        """최대 변경폭 약간 초과."""
        # 30.1% 변경
        assert (
            safety_bounds.is_within_bounds("timeout_ms", 1301, current_value=1000)
            is False
        )
