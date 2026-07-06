"""
Protect Facade Settings - Pydantic v2.

Global defaults for ``baldur.protect()`` — the single-call resilience facade
that composes Circuit Breaker + Retry + Fallback + DLQ.

Per-call kwargs override these global defaults. Per-name overrides via
``BALDUR_PROTECT_<NAME>_*`` are a future extension; OSS users pass kwargs.

Environment Variables:
    BALDUR_PROTECT_ENABLED=true
    BALDUR_PROTECT_DEFAULT_CIRCUIT_BREAKER=true
    BALDUR_PROTECT_DEFAULT_RETRY=false
    BALDUR_PROTECT_DEFAULT_DLQ=false
    BALDUR_PROTECT_DEFAULT_TIMEOUT_SECONDS=  # opt-in; set e.g. =30 to restore pre-#482 behavior

Reference:
    429 — Part 1, C1
    482 — D1 (default flip 30.0 → None)
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class ProtectSettings(BaseSettings):
    """Global defaults for ``baldur.protect()`` facade.

    Per-call kwargs always win. These settings control behavior when the
    caller does not specify the corresponding parameter.
    """

    model_config = make_settings_config("BALDUR_PROTECT_")

    enabled: bool = Field(
        default=True,
        description="Master toggle for protect() facade. When False, protect() calls "
        "the function directly with NO policy wrapping — this disables CB, retry, "
        "fallback, dlq, AND timeout uniformly, even when those kwargs are passed "
        "explicitly per call. Use as a kill switch only; per-call timeout= is not "
        "honored when this is False.",
    )
    default_circuit_breaker: bool = Field(
        default=True,
        description="Enable CircuitBreakerPolicy by default when caller does not pass circuit_breaker=.",
    )
    default_retry: bool = Field(
        default=False,
        description="Enable RetryPolicy by default when caller does not pass retry=. "
        "Off by default — protect() is a thin facade; callers opt in.",
    )
    default_dlq: bool = Field(
        default=False,
        description="Route final failures to DLQ by default when caller does not pass dlq=. "
        "Off by default — DLQ requires repository wiring.",
    )
    default_timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        description="Default wall-clock timeout in seconds for protect(). "
        "None (the default) means no Baldur-level timeout is applied — the "
        "I/O-layer timeout in your client (httpx, psycopg statement_timeout, "
        "redis-py socket_timeout, etc.) is the enforced safety net. Per-call "
        "timeout= always overrides. To restore pre-#482 behavior framework-"
        "wide, set BALDUR_PROTECT_DEFAULT_TIMEOUT_SECONDS=30; if a worker "
        "appears to hang, see docs/runbooks/protect-hang-troubleshooting.md.",
    )
    default_timeout_executor_workers: int = Field(
        default=32,
        gt=0,
        description="Maximum worker count for the shared TimeoutPolicy "
        "ThreadPoolExecutor. The executor is process-shared and lazy-spawns "
        "threads on demand — this is the ceiling, not the resident count. "
        "Default 32 covers OSS (50-500 RPS) and most PRO (500-5K RPS) "
        "deployments; PRO operators with high concurrent timeout-bounded "
        "workloads should size up. Read once at first executor lookup; "
        "subsequent changes require reset_protect_caches() to take effect.",
    )


# =============================================================================
# Singleton Pattern (standalone — not part of any settings group)
# =============================================================================


def get_protect_settings() -> ProtectSettings:
    """Return the cached ProtectSettings singleton."""
    from baldur.runtime import get_runtime

    return get_runtime().get_settings(ProtectSettings)


def reset_protect_settings() -> None:
    """Reset the ProtectSettings singleton — for test isolation.

    Also clears process-local protect() caches (per-name ``CircuitBreakerPolicy``
    cache, sticky recorder state) so that settings reset between tests does not
    leak a stale ``CircuitBreakerService`` config snapshot through the cache.
    Lazy import avoids the ``settings/protect.py`` ↔ ``baldur/protect_facade.py``
    cycle.
    """
    from baldur.runtime import get_runtime

    get_runtime().reset_settings(ProtectSettings)

    from baldur.protect_facade import reset_protect_caches

    reset_protect_caches()
