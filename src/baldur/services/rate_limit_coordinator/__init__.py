"""
Rate Limit Coordinator

Central coordinator for distributed rate limit management.
Prevents Self-DDoS by coordinating retry behavior across all workers.

Key Features:
    - Global cooldown on 429 responses
    - Exponential backoff with jitter
    - Distributed state via pluggable storage
    - 100% coverage with database fallback

Design Philosophy:
    "어떤 고객 환경이든 100% Self-DDoS 차단"
    - Redis 있으면 사용 (최고 성능)
    - 없으면 Database 사용 (100% 호환)
    - DB도 없으면 InMemory (단일 프로세스)
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

# Re-export all public symbols from sub-modules
from .coordinator import (
    RateLimitCoordinator,
    T,
    get_rate_limit_coordinator,
    logger,
)
from .helpers import (
    _default_get_retry_after,
    _default_is_429,
    _emit_rate_limit_event,
    _record_rate_limit_metrics,
)
from .models import (
    RateLimitCoordinatorConfig,
    RateLimitResult,
)

__all__ = [
    # Models
    "RateLimitCoordinatorConfig",
    "RateLimitResult",
    # Helpers
    "_emit_rate_limit_event",
    "_record_rate_limit_metrics",
    "_default_is_429",
    "_default_get_retry_after",
    # Coordinator
    "RateLimitCoordinator",
    "get_rate_limit_coordinator",
    "T",
    "logger",
]

# =============================================================================
# Dynamic forwarding (event_bus pattern)
# =============================================================================
# Sub-module list for dynamic attribute forwarding
_SUBMODULES = {
    "models": "baldur.services.rate_limit_coordinator.models",
    "helpers": "baldur.services.rate_limit_coordinator.helpers",
    "coordinator": "baldur.services.rate_limit_coordinator.coordinator",
}


def __getattr__(name: str) -> Any:
    """Dynamic attribute forwarding for sub-modules and their contents."""
    # Direct sub-module access
    if name in _SUBMODULES:
        module = importlib.import_module(_SUBMODULES[name])
        setattr(sys.modules[__name__], name, module)
        return module

    # Search through sub-modules for the attribute
    for submod_path in _SUBMODULES.values():
        try:
            module = importlib.import_module(submod_path)
            if hasattr(module, name):
                attr = getattr(module, name)
                setattr(sys.modules[__name__], name, attr)
                return attr
        except ImportError:
            continue

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
