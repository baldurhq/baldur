"""
Baldur Adapters Module

Concrete implementations of pluggable interfaces.
These adapters bridge the abstract interfaces with specific frameworks,
services, and libraries.

Available Adapters:
    Repository Adapters:
        - RedisCircuitBreakerStateRepository (Production default)
        - RedisDLQRepository (Production default)
        - InMemoryFailedOperationRepository (Testing)
        - InMemoryCircuitBreakerStateRepository (Testing)

    Cache Adapters:
        - RedisCacheAdapter (Redis)
        - InMemoryCacheAdapter (Testing)

    Task Queue Adapters:
        - CeleryTaskAdapter (Celery)
        - SyncTaskAdapter (Testing - synchronous execution)

Usage:
    from baldur.adapters import (
        # Repositories (Production - Redis)
        RedisCircuitBreakerStateRepository,
        RedisDLQRepository,
        # Repositories (Testing - InMemory)
        InMemoryFailedOperationRepository,
        InMemoryCircuitBreakerStateRepository,
        # Cache
        RedisCacheAdapter,
        InMemoryCacheAdapter,
        # Task Queues
        CeleryTaskAdapter,
        SyncTaskAdapter,
    )

NOTE: Django and SQLAlchemy adapters have been removed in v2.0.0.
      Use Redis adapters (with ResilientStorageBackend fallback) instead.

Status: Internal
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

# =============================================================================
# TYPE_CHECKING block for IDE autocomplete and MyPy support
# =============================================================================
if TYPE_CHECKING:
    from baldur.adapters.cache import InMemoryCacheAdapter, RedisCacheAdapter
    from baldur.adapters.health_checker import (
        HealthCheckStrategy,
        LinuxTCPInfoStrategy,
        PortableHealthChecker,
        TTLCacheStrategy,
    )
    from baldur.adapters.memory import (
        InMemoryCircuitBreakerStateRepository,
        InMemoryFailedOperationRepository,
        InMemorySecurityIncidentRepository,
    )
    from baldur.adapters.queues import CeleryTaskAdapter, SyncTaskAdapter
    from baldur.adapters.redis import (
        RedisCircuitBreakerStateRepository,
        RedisDLQRepository,
    )

# =============================================================================
# Lazy Loading via __getattr__
# =============================================================================

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # Repository Adapters - Redis (Production)
    "RedisCircuitBreakerStateRepository": (
        "baldur.adapters.redis",
        "RedisCircuitBreakerStateRepository",
    ),
    "RedisDLQRepository": (
        "baldur.adapters.redis",
        "RedisDLQRepository",
    ),
    # Repository Adapters - InMemory (Testing)
    "InMemoryFailedOperationRepository": (
        "baldur.adapters.memory",
        "InMemoryFailedOperationRepository",
    ),
    "InMemoryCircuitBreakerStateRepository": (
        "baldur.adapters.memory",
        "InMemoryCircuitBreakerStateRepository",
    ),
    "InMemorySecurityIncidentRepository": (
        "baldur.adapters.memory",
        "InMemorySecurityIncidentRepository",
    ),
    # Cache Adapters
    "RedisCacheAdapter": (
        "baldur.adapters.cache",
        "RedisCacheAdapter",
    ),
    "InMemoryCacheAdapter": (
        "baldur.adapters.cache",
        "InMemoryCacheAdapter",
    ),
    # Task Queue Adapters
    "CeleryTaskAdapter": (
        "baldur.adapters.queues",
        "CeleryTaskAdapter",
    ),
    "SyncTaskAdapter": (
        "baldur.adapters.queues",
        "SyncTaskAdapter",
    ),
    # Health Checker Adapters
    "HealthCheckStrategy": (
        "baldur.adapters.health_checker",
        "HealthCheckStrategy",
    ),
    "TTLCacheStrategy": (
        "baldur.adapters.health_checker",
        "TTLCacheStrategy",
    ),
    "LinuxTCPInfoStrategy": (
        "baldur.adapters.health_checker",
        "LinuxTCPInfoStrategy",
    ),
    "PortableHealthChecker": (
        "baldur.adapters.health_checker",
        "PortableHealthChecker",
    ),
}


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_path)
        value = getattr(module, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = list(_LAZY_IMPORTS.keys())
