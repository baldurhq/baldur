"""
Stress Test Service Package.

DB Connection Pool 스트레스 테스트를 위한 비즈니스 로직.

이 모듈은 테스트 전용이며, 프로덕션에서는 절대 사용하지 마세요!
비즈니스 로직을 View 레이어에서 분리하여 클린 아키텍처를 유지합니다.

Modules:
    - models: 스트레스 테스트 결과 데이터 클래스
    - service: StressTestService 클래스 및 싱글톤

Usage:
    from baldur.services.stress_test_service import (
        get_stress_test_service,
        StressTestResult,
    )

.. versionadded:: 2.2.0
    ``stress_test_service.py`` 플랫 파일에서 ``stress_test_service/`` 패키지로 전환.
"""

# Dynamic forwarding for patch compatibility
import sys as _sys

from baldur.services.stress_test_service import service as _service_module
from baldur.services.stress_test_service.models import (
    BurstFailureResult,
    LockContentionResult,
    PoolStatusResult,
    StressTestResult,
)
from baldur.services.stress_test_service.service import (
    StressTestService,
    get_stress_test_service,
)

_pkg = _sys.modules[__name__]
for _name in dir(_service_module):
    if not _name.startswith("__") and not hasattr(_pkg, _name):
        setattr(_pkg, _name, getattr(_service_module, _name))
del _name, _pkg

__all__ = [
    # Models
    "StressTestResult",
    "PoolStatusResult",
    "LockContentionResult",
    "BurstFailureResult",
    # Service
    "StressTestService",
    # Singleton
    "get_stress_test_service",
]
