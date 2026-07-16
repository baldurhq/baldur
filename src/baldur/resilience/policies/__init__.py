"""
Resilience Policies — unified Policy package.

Provides the pure Policy implementation of each resilience pattern plus the
PolicyComposer composition engine.

Example::

    from baldur.resilience.policies import compose, FallbackPolicy
    result = compose(
        FallbackPolicy(default_value={"degraded": True}),
    ).execute(lambda: fetch_a())

Note:
    HedgingPolicy, AsyncHedgingPolicy, HedgingConfigUpdateHook, and
    ThrottlePolicy are resolvable from this module via ``__getattr__`` but are
    deliberately not advertised in ``__all__``: their engines require
    ``baldur_pro`` at runtime (the Hedging names additionally use lazy import
    due to a circular reference with core.hedging — prefer
    ``from baldur.resilience.policies.hedging import HedgingPolicy`` directly).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Core interfaces (re-export from interfaces)
from baldur.interfaces.resilience_policy import (
    AsyncResiliencePolicy,
    PolicyContext,
    PolicyOutcome,
    PolicyRejectedException,
    PolicyResult,
    ResiliencePolicy,
)
from baldur.resilience.policies.async_retry import (
    AsyncRetryPolicy,
    async_retry_policy,
    retry,
)

# Composer
from baldur.resilience.policies.composer import (
    AsyncPolicyComposer,
    PolicyComposer,
    compose,
    compose_async,
)

# Policies — Fallback (no circular reference)
from baldur.resilience.policies.fallback import (
    AsyncFallbackPolicy,
    FallbackPolicy,
    partition_aware_chain,
)

# Guards
from baldur.resilience.policies.guards import (
    BackpressureGuard,
    ErrorBudgetGuard,
    FullStopGuard,
    KillSwitchGuard,
    LoadSheddingGuard,
    ThrottleGovernanceGuard,
    create_default_full_stop_guard,
)

# Hooks
from baldur.resilience.policies.hooks import (
    AuditHook,
    EventBusHook,
    MetricsHook,
)

# Presets
from baldur.resilience.policies.presets import ha_pipeline, standard_pipeline

# Sinks
from baldur.resilience.policies.sinks import DLQSink

# Policies — Timeout
from baldur.resilience.policies.timeout import AsyncTimeoutPolicy, TimeoutPolicy

# Policies — Bulkhead (core-tier)
from baldur.services.bulkhead.policy import BulkheadPolicy
from baldur.services.circuit_breaker.policy import (
    AsyncCircuitBreakerPolicy,
    CircuitBreakerPolicy,
)
from baldur.services.retry_handler.policy import RetryPolicy

if TYPE_CHECKING:
    from baldur.resilience.policies.hedging import (
        AsyncHedgingPolicy,
        HedgingConfigUpdateHook,
        HedgingPolicy,
    )

# ThrottlePolicy is PRO-tier and the Hedging engine requires baldur_pro at
# runtime. These names resolve via ``__getattr__`` below — kept resolvable so
# existing import statements keep working — but are deliberately absent from
# ``__all__`` (honest advertisement surface). IDE/mypy treat ThrottlePolicy as
# ``Any`` at this re-export site; consumers needing precise typing can import
# the concrete class from its PRO submodule directly.


def __getattr__(name: str):
    """Lazy import for the PRO-tier ThrottlePolicy and the in-tree Hedging
    policies (which use deferred imports to break the module-load cycle with
    core.hedging — see ``hedging.py`` for the rationale). These names are
    resolvable but not in ``__all__``; their engines require baldur_pro."""
    _hedging_names = {"AsyncHedgingPolicy", "HedgingConfigUpdateHook", "HedgingPolicy"}
    if name in _hedging_names:
        try:
            from baldur.resilience.policies import hedging as _hedging_mod

            return getattr(_hedging_mod, name)
        except ImportError as e:
            raise AttributeError(
                f"Cannot import {name} from hedging module: {e}"
            ) from e
    if name == "ThrottlePolicy":
        try:
            from baldur_pro.services.throttle.policy import ThrottlePolicy

            return ThrottlePolicy
        except ImportError as e:
            raise AttributeError(f"Cannot import ThrottlePolicy (PRO tier): {e}") from e
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Core interfaces
    "AsyncResiliencePolicy",
    "PolicyContext",
    "PolicyOutcome",
    "PolicyRejectedException",
    "PolicyResult",
    "ResiliencePolicy",
    # Composer
    "AsyncPolicyComposer",
    "PolicyComposer",
    "compose",
    "compose_async",
    # Policies
    "AsyncCircuitBreakerPolicy",
    "AsyncFallbackPolicy",
    "AsyncRetryPolicy",
    "AsyncTimeoutPolicy",
    "BulkheadPolicy",
    "CircuitBreakerPolicy",
    "FallbackPolicy",
    "RetryPolicy",
    "TimeoutPolicy",
    "async_retry_policy",
    "partition_aware_chain",
    "retry",
    # Guards
    "BackpressureGuard",
    "ErrorBudgetGuard",
    "FullStopGuard",
    "KillSwitchGuard",
    "LoadSheddingGuard",
    "ThrottleGovernanceGuard",
    "create_default_full_stop_guard",
    # Hooks
    "AuditHook",
    "EventBusHook",
    "MetricsHook",
    # Sinks
    "DLQSink",
    # Presets
    "ha_pipeline",
    "standard_pipeline",
]
