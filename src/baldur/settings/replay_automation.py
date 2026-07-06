"""
Replay Automation Settings - Pydantic v2.

Single Source of Truth for replay automation configuration.
Replaces:
- core/config.py:ReplayAutomationConfig (lines 434-495)
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    LargeCount,
    MediumCount,
    Probability,
)


class ReplayAutomationSettings(BaseSettings):
    """
    DLQ Replay automation settings.

    Replay triggers:
    - On-recovery: event-driven auto-replay on circuit-breaker recovery.
    - Traffic-aware: replay gated on traffic normalization (implemented;
      disabled by default).

    Scheduled batch replay has no automatic scheduler — operator/manual batch
    replay runs via the task-level trigger. Adaptive mode dynamically sizes
    batches, and per-domain differentiated policy is supported.

    Environment variables:
        BALDUR_REPLAY_AUTOMATION_ON_RECOVERY_ENABLED=true
        BALDUR_REPLAY_AUTOMATION_ON_RECOVERY_MAX_ITEMS=100
        ...
    """

    model_config = make_settings_config("BALDUR_REPLAY_AUTOMATION_")

    # =========================================================================
    # On-Recovery Replay (CB CLOSED event-triggered)
    # =========================================================================
    on_recovery_enabled: bool = Field(
        default=True,
        description="Enable event-driven replay on circuit breaker close",
    )
    on_recovery_max_items: LargeCount = Field(
        default=100,
        description="Maximum replay items on CB recovery",
    )

    # =========================================================================
    # Traffic-Aware Replay (implemented; disabled by default)
    # =========================================================================
    traffic_aware_enabled: bool = Field(
        default=False,
        description="Enable traffic-aware replay (default: disabled)",
    )
    traffic_aware_max_items: LargeCount = Field(
        default=30,
        description="Maximum replay items on traffic normalization",
    )

    # =========================================================================
    # Adaptive Mode (dynamic max_items adjustment)
    # =========================================================================
    adaptive_enabled: bool = Field(
        default=False,
        description="Enable adaptive mode",
    )
    adaptive_initial_items: LargeCount = Field(
        default=50,
        description="Initial batch size for adaptive mode",
    )
    adaptive_min_items: MediumCount = Field(
        default=10,
        description="Minimum batch size",
    )
    adaptive_max_items: int = Field(
        default=100,
        ge=10,
        le=1000,
        description="Maximum batch size",
    )
    adaptive_failure_threshold: Probability = Field(
        default=0.2,
        description="Failure rate threshold (0.2 = 20%)",
    )

    # =========================================================================
    # Domain Priority Policy (per-domain differentiated policy)
    # =========================================================================
    priority_enabled: bool = Field(
        default=False,
        description="Enable priority-based batch processing",
    )
    domain_priorities: dict[str, str] = Field(
        default_factory=dict,
        description='Per-domain priority mapping {"payment": "critical", "notification": "low"}',
    )
    domain_max_retries: dict[str, int] = Field(
        default_factory=dict,
        description='Per-domain max_retries override {"payment": 10}',
    )

    # =========================================================================
    # Service → Failure Type Mapping (on-recovery dispatch)
    # =========================================================================
    # ReplayService.replay_on_circuit_close() uses this to translate
    # "service that recovered" into "failure_types whose DLQ entries are
    # now safe to retry". An empty/missing entry for the recovered service
    # is surfaced as a blocked-with-signal outcome (WARNING log +
    # DLQ_REPLAY_BLOCKED event + metric + audit), not a silent no-op —
    # operators MUST configure this for on-recovery replay to drain DLQ.
    service_failure_type_map: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Service→failure_types mapping consulted by "
            "replay_on_circuit_close(). Example: "
            '{"payment_api": ["TIMEOUT", "CONNECTION_ERROR"]}. '
            "Empty default — operator must configure for on-recovery replay to drain."
        ),
    )


# =============================================================================
# Singleton pattern
# =============================================================================


def get_replay_automation_settings() -> "ReplayAutomationSettings":
    """Get cached ReplayAutomationSettings instance."""
    from baldur.settings.root import get_config

    return get_config().services_group.replay_automation


def reset_replay_automation_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["replay_automation"]
    except KeyError:
        pass
