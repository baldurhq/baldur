"""
Backoff Calculator Package — Exponential Backoff with Throttle Awareness.

Provides configurable exponential backoff with jitter for retry logic.

Features:
- Exponential backoff: base^attempt (4, 16, 64, ...)
- Maximum delay cap to prevent excessive wait times
- Jitter (±25%) to prevent thundering herd problem
- Per-domain configuration support
- Throttle-aware backoff with dynamic multipliers
- EventBus push-based state caching
- Global (Redis) throttle state sharing

Modules:
    - models: ThrottleState, PushBasedThrottleStateCache, GlobalThrottleState, BackoffConfig
    - budget: AdaptiveRetryBudget
    - global_state: GlobalThrottleStateManager
    - calculator: ThrottleAwareBackoffCalculator
    - strategy_adapter: ThrottleAwareBackoffStrategy (BackoffStrategy interface)

Usage:
    from baldur.services.backoff_calculator import (
        ThrottleAwareBackoffCalculator,
        BackoffConfig,
    )

    # For BackoffStrategy interface:
    from baldur.services.backoff_calculator.strategy_adapter import (
        ThrottleAwareBackoffStrategy,
    )

.. versionadded:: 2.2.0
    ``backoff_calculator.py`` flat file to ``backoff_calculator/`` package.

.. versionchanged:: 2.3.0
    BackoffCalculator removed. Use core.backoff.ExponentialBackoff or
    ThrottleAwareBackoffCalculator directly.
"""

import sys as _sys

from baldur.services.backoff_calculator import budget as _budget_module
from baldur.services.backoff_calculator import calculator as _calculator_module
from baldur.services.backoff_calculator import global_state as _global_state_module
from baldur.services.backoff_calculator import models as _models_module
from baldur.services.backoff_calculator.budget import (
    AdaptiveRetryBudget,
)
from baldur.services.backoff_calculator.calculator import (
    ThrottleAwareBackoffCalculator,
    get_backoff_calculator,
    reset_backoff_calculator,
)
from baldur.services.backoff_calculator.global_state import (
    GlobalThrottleStateManager,
)
from baldur.services.backoff_calculator.models import (
    SYSTEM_TIMEOUT_SECONDS,
    BackoffConfig,
    GlobalThrottleState,
    PushBasedThrottleStateCache,
    ThrottleState,
)

_pkg = _sys.modules[__name__]
for _mod in (_models_module, _budget_module, _global_state_module, _calculator_module):
    for _name in dir(_mod):
        if not _name.startswith("__") and not hasattr(_pkg, _name):
            setattr(_pkg, _name, getattr(_mod, _name))
del _name, _mod, _pkg

__all__ = [
    # Constants
    "SYSTEM_TIMEOUT_SECONDS",
    # Models
    "ThrottleState",
    "PushBasedThrottleStateCache",
    "GlobalThrottleState",
    "BackoffConfig",
    # Budget
    "AdaptiveRetryBudget",
    # Global State
    "GlobalThrottleStateManager",
    # Calculator
    "ThrottleAwareBackoffCalculator",
    "get_backoff_calculator",
    "reset_backoff_calculator",
]
