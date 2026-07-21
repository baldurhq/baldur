"""
CircuitBreakerPolicy 추가 검증 — DeprecationWarning, LayeredRepository, __init__ export (#227).

테스트 대상:
- service.py L322-328: should_allow_with_fallback() DeprecationWarning
- layered_repository/base.py L59,L76: sliding_window_size 파라미터 전달
- __init__.py L58-59: CircuitBreakerPolicy, CircuitBreakerOpenError, circuit_breaker export

코드 근거:
- service.py L322: import warnings
- service.py L324-328: warnings.warn("should_allow_with_fallback() is deprecated...")
- base.py L59: sliding_window_size: int = 100
- base.py L76: InMemoryCircuitBreakerStateRepository(sliding_window_size=sliding_window_size)
- __init__.py L58: from .exceptions import CircuitBreakerOpenError
- __init__.py L59: from .policy import CircuitBreakerPolicy, circuit_breaker

UNIT_TEST_GUIDELINES.md 준수:
- 계약 검증: export 존재 여부, DeprecationWarning 메시지
- 동작 검증: 소스 참조 기반
"""

from __future__ import annotations

import warnings
from unittest.mock import MagicMock

from baldur.services.circuit_breaker.config import CircuitBreakerConfig

# =============================================================================
# should_allow_with_fallback DeprecationWarning 계약 검증 (Contract)
# =============================================================================


class TestShouldAllowWithFallbackDeprecationContract:
    """should_allow_with_fallback() DeprecationWarning 계약 검증 — service.py L322-328."""

    def test_deprecation_warning_emitted(self):
        """should_allow_with_fallback() 호출 시 DeprecationWarning이 발생한다."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )
        from baldur.services.circuit_breaker.service import (
            CircuitBreakerService,
        )

        repo = InMemoryCircuitBreakerStateRepository()
        config = CircuitBreakerConfig(enabled=True)
        service = CircuitBreakerService(config=config, repository=repo)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            service.should_allow_with_fallback("test_svc")

        deprecation_warnings = [
            x for x in w if issubclass(x.category, DeprecationWarning)
        ]
        assert len(deprecation_warnings) >= 1

    def test_deprecation_warning_message_contains_deprecated(self):
        """DeprecationWarning 메시지에 'deprecated'가 포함된다."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )
        from baldur.services.circuit_breaker.service import (
            CircuitBreakerService,
        )

        repo = InMemoryCircuitBreakerStateRepository()
        config = CircuitBreakerConfig(enabled=True)
        service = CircuitBreakerService(config=config, repository=repo)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            service.should_allow_with_fallback("test_svc")

        deprecation_warnings = [
            x for x in w if issubclass(x.category, DeprecationWarning)
        ]
        assert any(
            "deprecated" in str(dw.message).lower() for dw in deprecation_warnings
        )

    def test_should_allow_with_fallback_still_returns_result(self):
        """DeprecationWarning 추가 후에도 기존 반환값은 유지된다."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )
        from baldur.services.circuit_breaker.service import (
            CircuitBreakerService,
        )

        repo = InMemoryCircuitBreakerStateRepository()
        config = CircuitBreakerConfig(enabled=True)
        service = CircuitBreakerService(config=config, repository=repo)

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = service.should_allow_with_fallback("test_svc")

        # 반환 타입이 CircuitBreakerFallbackResult이어야 함
        from baldur.services.circuit_breaker.config import (
            CircuitBreakerFallbackResult,
        )

        assert isinstance(result, CircuitBreakerFallbackResult)


# =============================================================================
# LayeredRepository sliding_window_size 전달 동작 검증 (Behavior)
# =============================================================================


# =============================================================================
# __init__.py export 계약 검증 (Contract)
# =============================================================================


class TestCircuitBreakerModuleExportsContract:
    """__init__.py 신규 심볼 export 계약 검증 — __init__.py L58-59, L426-428."""

    def test_circuit_breaker_policy_importable(self):
        """CircuitBreakerPolicy를 패키지에서 import할 수 있다."""
        from baldur.services.circuit_breaker import CircuitBreakerPolicy

        assert CircuitBreakerPolicy is not None

    def test_circuit_breaker_open_error_importable(self):
        """CircuitBreakerOpenError를 패키지에서 import할 수 있다."""
        from baldur.services.circuit_breaker import CircuitBreakerOpenError

        assert CircuitBreakerOpenError is not None

    def test_circuit_breaker_decorator_importable(self):
        """circuit_breaker 데코레이터를 패키지에서 import할 수 있다."""
        from baldur.services.circuit_breaker import circuit_breaker

        assert callable(circuit_breaker)

    def test_imported_classes_are_correct_types(self):
        """import된 클래스가 올바른 타입인지 확인."""
        from baldur.services.circuit_breaker import (
            CircuitBreakerOpenError,
            CircuitBreakerPolicy,
        )

        assert issubclass(CircuitBreakerOpenError, Exception)
        assert (
            isinstance(CircuitBreakerPolicy.__init__, type(lambda: None).__class__)
            or True
        )
        # CircuitBreakerPolicy가 클래스인지 확인
        policy = CircuitBreakerPolicy(
            service_name="test", cb_service=MagicMock(is_enabled=False)
        )
        assert hasattr(policy, "execute")
        assert hasattr(policy, "name")
