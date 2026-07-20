"""
ApplyStrategy Settings - Pydantic v2.

Per-strategy delay and grace_timeout settings for config application.
Default delay per config type is configurable via environment variables.

Environment Variables:
    # Delay per config type
    BALDUR_APPLY_STRATEGY_SLA_DELAY=0
    BALDUR_APPLY_STRATEGY_METRICS_DELAY=0
    BALDUR_APPLY_STRATEGY_NOTIFICATION_DELAY=0
    BALDUR_APPLY_STRATEGY_FORENSIC_DELAY=0
    BALDUR_APPLY_STRATEGY_RATE_LIMIT_DELAY=0
    BALDUR_APPLY_STRATEGY_RETRY_DELAY=10
    BALDUR_APPLY_STRATEGY_DLQ_DELAY=10
    BALDUR_APPLY_STRATEGY_CIRCUIT_BREAKER_DELAY=30
    BALDUR_APPLY_STRATEGY_IDEMPOTENCY_DELAY=30
    BALDUR_APPLY_STRATEGY_SECURITY_DELAY=60
    BALDUR_APPLY_STRATEGY_ERROR_BUDGET_DELAY=30
    BALDUR_APPLY_STRATEGY_DEFAULT_GRACE_TIMEOUT=60

    # Celery task retry settings
    BALDUR_APPLY_STRATEGY_PENDING_MAX_RETRIES=3
    BALDUR_APPLY_STRATEGY_PENDING_RETRY_DELAY=10
    BALDUR_APPLY_STRATEGY_GRACEFUL_MAX_RETRIES=10
    BALDUR_APPLY_STRATEGY_GRACEFUL_RETRY_DELAY=5
    BALDUR_APPLY_STRATEGY_CLEANUP_MAX_AGE_HOURS=24
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class ApplyStrategySettings(BaseSettings):
    """
    ApplyStrategy settings.

    Per-type delay and grace timeout applied when a config change is rolled out.
    """

    model_config = make_settings_config("BALDUR_APPLY_STRATEGY_")

    # ==========================================================================
    # Immediate apply (Safe Immediate) - delay_seconds
    # ==========================================================================
    sla_delay: int = Field(
        default=0,
        ge=0,
        le=300,
        description="SLA config apply delay (seconds)",
    )
    metrics_delay: int = Field(
        default=0,
        ge=0,
        le=300,
        description="Metrics config apply delay (seconds)",
    )
    notification_delay: int = Field(
        default=0,
        ge=0,
        le=300,
        description="Notification config apply delay (seconds)",
    )
    forensic_delay: int = Field(
        default=0,
        ge=0,
        le=300,
        description="Forensic config apply delay (seconds)",
    )

    # ==========================================================================
    # Traffic control - immediate, but requires care
    # ==========================================================================
    rate_limit_delay: int = Field(
        default=0,
        ge=0,
        le=300,
        description="Rate limit config apply delay (seconds)",
    )

    # ==========================================================================
    # Processing related - delayed apply
    # ==========================================================================
    retry_delay: int = Field(
        default=10,
        ge=0,
        le=300,
        description="Retry config apply delay (seconds)",
    )
    dlq_delay: int = Field(
        default=10,
        ge=0,
        le=300,
        description="DLQ config apply delay (seconds)",
    )

    # ==========================================================================
    # Core protection - long delay
    # ==========================================================================
    circuit_breaker_delay: int = Field(
        default=30,
        ge=0,
        le=600,
        description="Circuit breaker config apply delay (seconds)",
    )
    idempotency_delay: int = Field(
        default=30,
        ge=0,
        le=600,
        description="Idempotency config apply delay (seconds)",
    )
    security_delay: int = Field(
        default=60,
        ge=0,
        le=600,
        description="Security config apply delay (seconds)",
    )
    error_budget_delay: int = Field(
        default=30,
        ge=0,
        le=600,
        description="Error budget config apply delay (seconds)",
    )

    # ==========================================================================
    # Common settings
    # ==========================================================================
    default_grace_timeout: int = Field(
        default=60,
        ge=10,
        le=600,
        description="Default maximum wait time for GRACEFUL strategy (seconds)",
    )

    # ==========================================================================
    # Celery task retry settings (apply_pending_config_changes)
    # ==========================================================================
    pending_max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Maximum retries for pending config apply task",
    )
    pending_retry_delay: int = Field(
        default=10,
        ge=1,
        le=300,
        description="Retry delay for pending config apply task (seconds)",
    )

    # ==========================================================================
    # Celery task retry settings (apply_graceful_config_change)
    # Retry count is high because in-flight work must be allowed to finish
    # ==========================================================================
    graceful_max_retries: int = Field(
        default=10,
        ge=0,
        le=20,
        description="Maximum retries for graceful config apply task",
    )
    graceful_retry_delay: int = Field(
        default=5,
        ge=1,
        le=60,
        description="Retry delay for graceful config apply task (seconds)",
    )

    # ==========================================================================
    # Expired config cleanup (cleanup_expired_config_changes)
    # ==========================================================================
    cleanup_max_age_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="Maximum age for expired config change cleanup (hours)",
    )


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_apply_strategy_settings() -> "ApplyStrategySettings":
    """
    Return the cached ApplyStrategySettings instance.

    Returns:
        ApplyStrategySettings: singleton instance
    """
    from baldur.settings.root import get_config

    return get_config().services_group.apply_strategy


def reset_apply_strategy_settings() -> None:
    """
    Reset cached settings (for testing).
    """
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["apply_strategy"]
    except KeyError:
        pass
