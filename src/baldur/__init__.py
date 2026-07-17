"""
Baldur Reliability Layer for Python Applications.

Public API:
    from baldur import ProviderRegistry
    from baldur import get_circuit_breaker_service
    from baldur import CircuitState
    from baldur import FailedOperationData
    from baldur import ReplayService
    from baldur import BaldurError, AdapterNotFoundError

Top-level surface is the marquee public API per impl doc 508 D13. Advanced /
specialised surfaces stay at their nested paths:

    from baldur.protect_facade import protect_with_meta, aprotect_with_meta, ProtectResult
    from baldur.core.exceptions import RunbookError, StoreError, ...
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

__version__ = "1.2.0"
__author__ = "Baldur Contributors"

# === Core Types (Eager — lightweight, no heavy deps) ===
from baldur.interfaces.repositories import (
    CircuitBreakerStateEnum as CircuitState,
)
from baldur.interfaces.repositories import (
    FailedOperationData as FailedOperationData,
)

# =========================================================================
# Lazy Import (PEP 562) — Heavy objects are loaded on first access.
#
# Applies the same verified pattern from adapters/__init__.py.
# Prevents chain-loading of ProviderRegistry → structlog → settings
# when Consumer only needs CircuitState Enum.
# =========================================================================
if TYPE_CHECKING:
    from baldur.adapters.fastapi import (
        fastapi_lifespan as fastapi_lifespan,
    )
    from baldur.adapters.flask import init_flask as init_flask
    from baldur.adapters.sql import sql_transaction as sql_transaction
    from baldur.api.admin import start_admin_server as start_admin_server
    from baldur.api.admin import stop_admin_server as stop_admin_server
    from baldur.bootstrap import init as init
    from baldur.coordination.scheduler import (
        get_leader_scheduler as get_leader_scheduler,
    )
    from baldur.core.exceptions import (
        AdapterError as AdapterError,
    )
    from baldur.core.exceptions import (
        AdapterNotFoundError as AdapterNotFoundError,
    )
    from baldur.core.exceptions import (
        BaldurError as BaldurError,
    )
    from baldur.core.exceptions import (
        CircuitBreakerError as CircuitBreakerError,
    )
    from baldur.core.exceptions import (
        ConfigurationError as ConfigurationError,
    )
    from baldur.core.exceptions import (
        DLQError as DLQError,
    )
    from baldur.core.exceptions import (
        DLQReplayError as DLQReplayError,
    )
    from baldur.core.exceptions import (
        DomainValidationError as DomainValidationError,
    )
    from baldur.core.exceptions import (
        IdempotencyDuplicateError as IdempotencyDuplicateError,
    )
    from baldur.core.exceptions import (
        IdempotencyUnavailableError as IdempotencyUnavailableError,
    )
    from baldur.core.exceptions import (
        RateLimitExceeded as RateLimitExceeded,
    )
    from baldur.core.exceptions import (
        ResilienceError as ResilienceError,
    )
    from baldur.core.exceptions import (
        RetryExhaustedError as RetryExhaustedError,
    )
    from baldur.core.exceptions import (
        TimeoutPolicyError as TimeoutPolicyError,
    )
    from baldur.factory import ProviderRegistry as ProviderRegistry
    from baldur.protect_facade import aprotect as aprotect
    from baldur.protect_facade import aprotected as aprotected
    from baldur.protect_facade import protect as protect
    from baldur.protect_facade import protected as protected
    from baldur.services import (
        get_circuit_breaker_service as get_circuit_breaker_service,
    )
    from baldur.services.replay_service import (
        ReplayService as ReplayService,
    )

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # Bootstrap
    "init": ("baldur.bootstrap", "init"),
    # Resilience facade — marquee primary API (508 D3).
    # The _with_meta variants live at baldur.protect_facade (advanced surface).
    "protect": ("baldur.protect_facade", "protect"),
    "aprotect": ("baldur.protect_facade", "aprotect"),
    "protected": ("baldur.protect_facade", "protected"),
    "aprotected": ("baldur.protect_facade", "aprotected"),
    # Scheduler (508 D4: canonical name only — get_scheduler alias dropped)
    "get_leader_scheduler": (
        "baldur.coordination.scheduler",
        "get_leader_scheduler",
    ),
    # SQL storage — cross-repo transaction scope
    "sql_transaction": ("baldur.adapters.sql", "sql_transaction"),
    # Admin server
    "start_admin_server": ("baldur.api.admin", "start_admin_server"),
    "stop_admin_server": ("baldur.api.admin", "stop_admin_server"),
    # Framework extras (508 D5: framework-idiomatic naming preserved)
    "fastapi_lifespan": ("baldur.adapters.fastapi", "fastapi_lifespan"),
    "init_flask": ("baldur.adapters.flask", "init_flask"),
    # Service Access
    "ProviderRegistry": ("baldur.factory", "ProviderRegistry"),
    "get_circuit_breaker_service": (
        "baldur.services",
        "get_circuit_breaker_service",
    ),
    # Replay
    "ReplayService": ("baldur.services.replay_service", "ReplayService"),
    # Exceptions (508 D6: base classes + leaves raised by top-level public surface)
    "BaldurError": ("baldur.core.exceptions", "BaldurError"),
    "AdapterError": ("baldur.core.exceptions", "AdapterError"),
    "AdapterNotFoundError": ("baldur.core.exceptions", "AdapterNotFoundError"),
    "CircuitBreakerError": ("baldur.core.exceptions", "CircuitBreakerError"),
    "DLQError": ("baldur.core.exceptions", "DLQError"),
    "DLQReplayError": ("baldur.core.exceptions", "DLQReplayError"),
    "ResilienceError": ("baldur.core.exceptions", "ResilienceError"),
    "RetryExhaustedError": ("baldur.core.exceptions", "RetryExhaustedError"),
    "TimeoutPolicyError": ("baldur.core.exceptions", "TimeoutPolicyError"),
    "RateLimitExceeded": ("baldur.core.exceptions", "RateLimitExceeded"),
    "IdempotencyDuplicateError": (
        "baldur.core.exceptions",
        "IdempotencyDuplicateError",
    ),
    "IdempotencyUnavailableError": (
        "baldur.core.exceptions",
        "IdempotencyUnavailableError",
    ),
    "DomainValidationError": (
        "baldur.core.exceptions",
        "DomainValidationError",
    ),
    "ConfigurationError": ("baldur.core.exceptions", "ConfigurationError"),
}


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_path)
        value = getattr(module, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "__version__",
    # Bootstrap
    "init",
    # Resilience facade — marquee API (508 D3)
    "protect",
    "aprotect",
    "protected",
    "aprotected",
    # Scheduler (508 D4)
    "get_leader_scheduler",
    # SQL storage
    "sql_transaction",
    # Admin server
    "start_admin_server",
    "stop_admin_server",
    # Framework extras (508 D5)
    "fastapi_lifespan",
    "init_flask",
    # Core Types
    "CircuitState",
    "FailedOperationData",
    # Service Access
    "ProviderRegistry",
    "get_circuit_breaker_service",
    # Replay
    "ReplayService",
    # Exceptions (508 D6: base classes + top-level-API leaves)
    "BaldurError",
    "AdapterError",
    "AdapterNotFoundError",
    "CircuitBreakerError",
    "DLQError",
    "DLQReplayError",
    "ResilienceError",
    "RetryExhaustedError",
    "TimeoutPolicyError",
    "RateLimitExceeded",
    "IdempotencyDuplicateError",
    "IdempotencyUnavailableError",
    "DomainValidationError",
    "ConfigurationError",
]
