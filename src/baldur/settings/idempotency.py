"""
Idempotency Settings - Pydantic v2.

Single Source of Truth for idempotency service configuration.

Replaces:
- core/config.py:IdempotencyConfig (lines 166-171)
- core/safe_defaults.py:SAFE_DEFAULTS["idempotency"]
- core/safe_defaults.py:VALIDATION_RULES["idempotency"]

Environment Variables:
    BALDUR_IDEMPOTENCY_DEFAULT_CACHE_TTL=60
    BALDUR_IDEMPOTENCY_EXTENDED_CACHE_TTL=300

Reference:
- docs/baldur/middleware_system/40_PYDANTIC_CONFIG_MIGRATION.md
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.core.idempotency_gate import IDEMPOTENCY_DEFAULT_TTL_SECONDS
from baldur.settings.base import make_settings_config
from baldur.settings.field_types import IntervalDuration


class IdempotencySettings(BaseSettings):
    """
    Idempotency service configuration with validation.

    All defaults match core/config.py:IdempotencyConfig
    All validation rules match core/safe_defaults.py:VALIDATION_RULES["idempotency"]
    """

    model_config = make_settings_config("BALDUR_IDEMPOTENCY_")

    # ==========================================================================
    # Master Toggle
    # ==========================================================================
    enabled: bool = Field(
        default=True,
        description="Enable/disable idempotency checks globally. When False, "
        "IdempotencyGuard allows all requests through without checking.",
    )

    fail_open_on_cache_error: bool = Field(
        default=False,
        description="Fail direction when a cache I/O error occurs during an "
        "idempotency check (e.g. Redis unreachable) on an enabled, explicitly-"
        "requested gate (@idempotent / protect(idempotency_key=)). Default "
        "False fails CLOSED — the check raises IdempotencyUnavailableError (or, "
        "on the protect facade, rejects) so a transient blip cannot let a "
        "duplicate side effect through. Set True to fail OPEN (treat the "
        "unverifiable check as CONTINUE) when availability outweighs the "
        "duplicate-prevention guarantee. Honored uniformly by both surfaces.",
    )

    allow_inmemory_fallback: bool = Field(
        default=False,
        description="Permit the @idempotent decorator AND "
        "IdempotencyService._get_cache() to fall back to a module-level "
        "in-process cache when no cache adapter is registered via "
        "ProviderRegistry. Default False enforces fail-closed semantics in "
        "production: multi-worker deployments cannot silently degrade to "
        "per-worker dedup. The decorator raises ConfigurationError on "
        "prod+no-toggle; the service emits a loud WARN + Prometheus counter "
        "and returns the in-process fallback so audit/recovery callers "
        "(which are fail-open by design) keep running. Set True only for "
        "single-worker OSS installs that knowingly accept in-process-only "
        "semantics.",
    )

    # ==========================================================================
    # Cache TTL Settings (from core/config.py lines 168-171)
    # Validation rules from core/safe_defaults.py lines 296-300
    # ==========================================================================
    default_cache_ttl: IntervalDuration = Field(
        default=60,
        description="Default cache TTL in seconds",
    )
    extended_cache_ttl: int = Field(
        default=300,
        ge=1,
        le=86400,
        description="Extended cache TTL for operations requiring longer TTL",
    )
    clock_skew_tolerance_seconds: float = Field(
        default=5.0,
        ge=0.0,
        le=60.0,
        description="Clock skew tolerance for idempotency checks",
    )

    # ==========================================================================
    # Gate dedup memory window
    # ==========================================================================
    # Reference: 595 D5. Lower bound 60:
    # a global memory default below one minute is dedup in name only (per-call
    # ttl= remains available for shorter windows); upper bound matches the
    # extended_cache_ttl ceiling above.
    gate_memory_ttl_seconds: int = Field(
        default=IDEMPOTENCY_DEFAULT_TTL_SECONDS,
        ge=60,
        le=86400,
        description="Default dedup memory window in seconds for IdempotencyGate: "
        "how long a completed/failed record is remembered after mark_completed/"
        "mark_failed when the caller supplies no per-call ttl. Governs the dedup "
        "memory window only — NOT the in-flight execution (EXECUTING-claim) "
        "window, and unrelated to the in-process fallback cache toggled by "
        "allow_inmemory_fallback. Honored by every gate construction site via "
        "the gate-internal default; re-read per mark, so an env change plus "
        "reset_idempotency_settings() retunes the window at runtime.",
    )


# =============================================================================
# Singleton Pattern (cached settings)
# =============================================================================


def get_idempotency_settings() -> "IdempotencySettings":
    from baldur.settings.root import get_config

    return get_config().services_group.idempotency


def reset_idempotency_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["idempotency"]
    except KeyError:
        pass
