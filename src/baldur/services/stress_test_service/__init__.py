"""
Stress Test Service Package.

Business logic for DB connection pool stress testing.

This module is test-only — never use it in production!
Keeping the business logic out of the view layer preserves a clean architecture.

Modules:
    - models: stress test result dataclasses
    - service: the StressTestService class and its singleton

Usage:
    from baldur.services.stress_test_service import (
        get_stress_test_service,
        StressTestResult,
    )

.. versionadded:: 2.2.0
    Converted from the flat ``stress_test_service.py`` file to the
    ``stress_test_service/`` package.
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
