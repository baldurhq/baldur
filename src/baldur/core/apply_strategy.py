"""
Configuration Apply Strategy.

Defines how configuration changes are applied to the running system.

Strategies:
- IMMEDIATE: Apply changes right away
- DELAYED: Apply changes after N seconds (cancellable)
- GRACEFUL: Wait for in-progress operations to complete, then apply

Settings are overridable via ApplyStrategySettings environment variables:
- BALDUR_APPLY_STRATEGY_CIRCUIT_BREAKER_DELAY
- BALDUR_APPLY_STRATEGY_SECURITY_DELAY
- Other per-config-type delays...
"""

from dataclasses import dataclass, field
from enum import Enum

from baldur.settings.apply_strategy import get_apply_strategy_settings


class ApplyStrategy(str, Enum):
    """Configuration apply strategy."""

    IMMEDIATE = "immediate"  # Apply right now
    DELAYED = "delayed"  # Apply after N seconds (cancellable)
    GRACEFUL = "graceful"  # Wait for in-progress ops, then apply


def _get_default_grace_timeout() -> int:
    """Load the default grace timeout from ApplyStrategySettings."""
    try:
        return get_apply_strategy_settings().default_grace_timeout
    except Exception:
        return 60


@dataclass
class ApplyOptions:
    """Options for applying configuration changes."""

    strategy: ApplyStrategy = ApplyStrategy.IMMEDIATE
    delay_seconds: int = 0  # For DELAYED strategy
    grace_timeout_seconds: int = field(default_factory=_get_default_grace_timeout)

    def __post_init__(self):
        """Validate options."""
        if self.strategy == ApplyStrategy.DELAYED and self.delay_seconds <= 0:
            raise ValueError("delay_seconds must be > 0 for DELAYED strategy")
        if self.grace_timeout_seconds <= 0:
            raise ValueError("grace_timeout_seconds must be > 0")


# =============================================================================
# Default strategies per config type
# =============================================================================

# Config types where changes have no operational impact
SAFE_IMMEDIATE_CONFIGS = frozenset(
    [
        "sla",
        "metrics",
        "notification",
        "forensic",
    ]
)

# Config types that control traffic/protection - need care
CRITICAL_CONFIGS = frozenset(
    [
        "circuit_breaker",
        "rate_limit",
        "security",
        "idempotency",
    ]
)

# Config types that affect processing
PROCESSING_CONFIGS = frozenset(
    [
        "retry",
        "dlq",
    ]
)


@dataclass
class DefaultApplyConfig:
    """Default apply configuration for a config type."""

    strategy: ApplyStrategy
    delay_seconds: int = 0
    grace_timeout_seconds: int = 60
    warning_message: str | None = None


def _get_default_apply_strategies() -> dict[str, DefaultApplyConfig]:
    """
    ApplyStrategySettings에서 delay 값을 로드하여 기본 전략 딕셔너리 생성.

    환경변수로 config 타입별 delay_seconds 오버라이드 가능.
    """
    settings = get_apply_strategy_settings()
    return {
        # Safe - immediate by default
        "sla": DefaultApplyConfig(
            strategy=ApplyStrategy.IMMEDIATE,
            delay_seconds=settings.sla_delay,
        ),
        "metrics": DefaultApplyConfig(
            strategy=ApplyStrategy.IMMEDIATE,
            delay_seconds=settings.metrics_delay,
        ),
        "notification": DefaultApplyConfig(
            strategy=ApplyStrategy.IMMEDIATE,
            delay_seconds=settings.notification_delay,
        ),
        "forensic": DefaultApplyConfig(
            strategy=ApplyStrategy.IMMEDIATE,
            delay_seconds=settings.forensic_delay,
        ),
        # Traffic control - immediate but with warning
        "rate_limit": DefaultApplyConfig(
            strategy=ApplyStrategy.IMMEDIATE,
            delay_seconds=settings.rate_limit_delay,
            warning_message="Rate limit changes take effect immediately for new requests",
        ),
        # Processing - delayed to protect in-flight operations
        "retry": DefaultApplyConfig(
            strategy=ApplyStrategy.DELAYED,
            delay_seconds=settings.retry_delay,
        ),
        "dlq": DefaultApplyConfig(
            strategy=ApplyStrategy.DELAYED,
            delay_seconds=settings.dlq_delay,
        ),
        # Critical - delayed with longer window for cancellation
        "circuit_breaker": DefaultApplyConfig(
            strategy=ApplyStrategy.DELAYED,
            delay_seconds=settings.circuit_breaker_delay,
            warning_message="Circuit breaker protects system stability. Change with caution.",
        ),
        "idempotency": DefaultApplyConfig(
            strategy=ApplyStrategy.DELAYED,
            delay_seconds=settings.idempotency_delay,
            warning_message="Idempotency prevents duplicate transactions. Change with caution.",
        ),
        "security": DefaultApplyConfig(
            strategy=ApplyStrategy.DELAYED,
            delay_seconds=settings.security_delay,
            warning_message="Security settings are highly sensitive. Ensure you have reviewed the changes.",
        ),
        # Error Budget - delayed to prevent alert storm from threshold changes
        "error_budget": DefaultApplyConfig(
            strategy=ApplyStrategy.DELAYED,
            delay_seconds=settings.error_budget_delay,
            warning_message="Error budget threshold changes can trigger immediate state transitions and alerts.",
        ),
    }


def get_default_apply_config(config_type: str) -> DefaultApplyConfig:
    """Get default apply configuration for a config type."""
    strategies = _get_default_apply_strategies()
    return strategies.get(
        config_type,
        DefaultApplyConfig(strategy=ApplyStrategy.IMMEDIATE),
    )


def get_effective_apply_options(
    config_type: str,
    strategy: str | None = None,
    delay_seconds: int | None = None,
    grace_timeout_seconds: int | None = None,
) -> ApplyOptions:
    """
    Get effective apply options, merging user overrides with defaults.

    Args:
        config_type: The configuration type (e.g., "circuit_breaker")
        strategy: User-specified strategy (overrides default)
        delay_seconds: User-specified delay (overrides default)
        grace_timeout_seconds: User-specified grace timeout

    Returns:
        ApplyOptions with effective values
    """
    default = get_default_apply_config(config_type)

    # Determine effective strategy
    effective_strategy = ApplyStrategy(strategy) if strategy else default.strategy

    # Determine effective delay
    if effective_strategy == ApplyStrategy.DELAYED:
        effective_delay = (
            delay_seconds if delay_seconds is not None else default.delay_seconds
        )
        # Ensure at least 1 second for delayed
        if effective_delay <= 0:
            effective_delay = default.delay_seconds or 10
    else:
        effective_delay = 0

    # Determine effective grace timeout
    effective_grace = (
        grace_timeout_seconds
        if grace_timeout_seconds is not None
        else default.grace_timeout_seconds
    )

    return ApplyOptions(
        strategy=effective_strategy,
        delay_seconds=effective_delay,
        grace_timeout_seconds=effective_grace,
    )
