"""
CompositeConfigApplier 단위 테스트.

여러 ConfigApplier를 조합하여 올바른 라우팅이 수행되는지 검증한다.

- 소스 상수 참조: ThrottleConfigApplier.PARAM_TO_CONFIG에서 SLA 파라미터 세트 파생
- 테스트 입력값: 라우팅 동작 검증용 (guideline 2.1 허용)
"""

from __future__ import annotations

import pytest

from baldur.adapters.config_applier.composite import CompositeConfigApplier
from baldur.adapters.config_applier.throttle import ThrottleConfigApplier

# 소스에서 SLA 파라미터 세트 참조 (하드코딩 방지)
_SLA_PARAMS: set[str] = set(ThrottleConfigApplier.PARAM_TO_CONFIG.keys())


class StubConfigApplier:
    """테스트용 ConfigApplier 스텁."""

    def __init__(self, handled_params: set[str] | None = None):
        self._handled_params = handled_params or set()
        self._values: dict[str, float] = {}

    def get_current(self, parameter: str) -> float:
        if parameter not in self._handled_params:
            raise ValueError(f"Not handled: {parameter}")
        return self._values.get(parameter, 0.0)

    def apply(self, parameter: str, value: float) -> bool:
        if parameter not in self._handled_params:
            return False
        self._values[parameter] = value
        return True

    def rollback(self, parameter: str, value: float) -> bool:
        if parameter not in self._handled_params:
            return False
        self._values[parameter] = value
        return True


class FallbackConfigApplier:
    """모든 파라미터를 수용하는 Fallback 스텁."""

    def __init__(self):
        self._values: dict[str, float] = {}

    def get_current(self, parameter: str) -> float:
        return self._values.get(parameter, 0.0)

    def apply(self, parameter: str, value: float) -> bool:
        self._values[parameter] = value
        return True

    def rollback(self, parameter: str, value: float) -> bool:
        self._values[parameter] = value
        return True


# SLA 파라미터 중 하나를 대표로 선택 (소스 상수에서 파생)
_SLA_WARNING_PARAM = "throttle_sla_warning_ms"
_SLA_CRITICAL_PARAM = "throttle_sla_critical_ms"


@pytest.fixture
def throttle_applier():
    """소스의 SLA 파라미터만 처리하는 스텁."""
    return StubConfigApplier(_SLA_PARAMS)


@pytest.fixture
def fallback_applier():
    """모든 파라미터를 처리하는 Fallback."""
    return FallbackConfigApplier()


@pytest.fixture
def composite(throttle_applier, fallback_applier):
    """Composite = ThrottleStub + Fallback."""
    return CompositeConfigApplier([throttle_applier, fallback_applier])


class TestCompositeConfigApplierInit:
    """생성자 테스트."""

    def test_empty_appliers_raises(self):
        """빈 appliers 리스트는 ValueError를 발생시켜야 한다."""
        with pytest.raises(ValueError, match="requires at least one applier"):
            CompositeConfigApplier([])


class TestCompositeConfigApplierApply:
    """apply() 라우팅 테스트."""

    def test_apply_routes_to_first_handler(self, composite, throttle_applier):
        """SLA 파라미터는 첫 번째 applier(throttle)가 처리해야 한다."""
        result = composite.apply(_SLA_WARNING_PARAM, 250.0)

        assert result is True
        assert throttle_applier._values[_SLA_WARNING_PARAM] == 250.0

    def test_apply_falls_through_to_fallback(
        self, composite, throttle_applier, fallback_applier
    ):
        """비-SLA 파라미터는 첫 번째 applier를 건너뛰고 fallback이 처리해야 한다."""
        non_sla_param = "circuit_breaker_threshold"
        result = composite.apply(non_sla_param, 5.0)

        assert result is True
        assert non_sla_param not in throttle_applier._values
        assert fallback_applier._values[non_sla_param] == 5.0

    def test_apply_returns_false_when_no_handler(self):
        """모든 applier가 거부하면 False를 반환해야 한다."""
        strict_applier = StubConfigApplier({"only_this"})
        composite = CompositeConfigApplier([strict_applier])

        result = composite.apply("unknown_param", 100.0)
        assert result is False


class TestCompositeConfigApplierGetCurrent:
    """get_current() 라우팅 테스트."""

    def test_get_current_from_first_handler(self, composite, throttle_applier):
        """SLA 파라미터는 첫 번째 applier에서 값을 조회해야 한다."""
        test_value = 200.0
        throttle_applier._values[_SLA_WARNING_PARAM] = test_value
        assert composite.get_current(_SLA_WARNING_PARAM) == test_value

    def test_get_current_falls_through_to_fallback(self, composite, fallback_applier):
        """비-SLA 파라미터는 fallback에서 조회해야 한다."""
        non_sla_param = "timeout_ms"
        test_value = 3000.0
        fallback_applier._values[non_sla_param] = test_value
        assert composite.get_current(non_sla_param) == test_value

    def test_get_current_raises_when_no_handler(self):
        """모든 applier가 실패하면 ValueError를 발생시켜야 한다."""
        strict_applier = StubConfigApplier({"only_this"})
        another = StubConfigApplier({"only_that"})
        composite = CompositeConfigApplier([strict_applier, another])

        with pytest.raises(ValueError, match="No applier can handle"):
            composite.get_current("unknown_param")


class TestCompositeConfigApplierRollback:
    """rollback() 라우팅 테스트."""

    def test_rollback_routes_to_first_handler(self, composite, throttle_applier):
        """SLA 파라미터 롤백은 첫 번째 applier가 처리해야 한다."""
        test_value = 500.0
        result = composite.rollback(_SLA_CRITICAL_PARAM, test_value)

        assert result is True
        assert throttle_applier._values[_SLA_CRITICAL_PARAM] == test_value

    def test_rollback_falls_through_to_fallback(self, composite, fallback_applier):
        """비-SLA 파라미터 롤백은 fallback이 처리해야 한다."""
        non_sla_param = "retry_count"
        test_value = 3.0
        result = composite.rollback(non_sla_param, test_value)

        assert result is True
        assert fallback_applier._values[non_sla_param] == test_value

    def test_rollback_returns_false_when_no_handler(self):
        """모든 applier가 거부하면 False를 반환해야 한다."""
        strict_applier = StubConfigApplier({"only_this"})
        composite = CompositeConfigApplier([strict_applier])

        result = composite.rollback("unknown_param", 0.0)
        assert result is False


class TestCompositeConfigApplierRoutingFlow:
    """문서의 라우팅 흐름 시나리오 검증."""

    def test_throttle_sla_routed_to_throttle_applier(
        self, composite, throttle_applier, fallback_applier
    ):
        """SLA 파라미터 → ThrottleApplier 처리, Fallback 도달 안 함."""
        composite.apply(_SLA_WARNING_PARAM, 250.0)

        assert _SLA_WARNING_PARAM in throttle_applier._values
        assert _SLA_WARNING_PARAM not in fallback_applier._values

    def test_circuit_breaker_routed_to_fallback(
        self, composite, throttle_applier, fallback_applier
    ):
        """비-SLA 파라미터 → ThrottleApplier skip → Fallback 처리."""
        non_sla_param = "circuit_breaker_threshold"
        composite.apply(non_sla_param, 5.0)

        assert non_sla_param not in throttle_applier._values
        assert non_sla_param in fallback_applier._values
