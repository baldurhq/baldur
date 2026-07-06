"""
Meta-Watchdog Settings - Pydantic v2.

Settings management for monitoring the Baldur system itself.
Configurable via the BALDUR_META_WATCHDOG_* environment variables.

Moved from: meta/config.py (location unification)
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class MetaWatchdogSettings(BaseSettings):
    """
    Meta-Watchdog settings.

    Settings for monitoring the health of the Baldur system itself.

    Environment variable examples:
        BALDUR_META_WATCHDOG_ENABLED=true
        BALDUR_META_WATCHDOG_PROBE_INTERVAL_SECONDS=30
        BALDUR_META_WATCHDOG_PAGERDUTY_ROUTING_KEY=xxx
    """

    model_config = make_settings_config("BALDUR_META_WATCHDOG_")

    # Activation
    enabled: bool = Field(
        default=True,
        description="Enable Meta-Watchdog",
    )

    # Health Probe
    probe_interval_seconds: float = Field(
        default=30.0,
        description="Health probe execution interval (seconds)",
        ge=5.0,
    )
    probe_timeout_seconds: float = Field(
        default=10.0,
        description="Health probe timeout (seconds)",
        ge=1.0,
    )

    # Stuck Detection
    stuck_threshold_seconds: float = Field(
        default=300.0,
        description="Stuck detection threshold (seconds, default 5 minutes)",
        ge=60.0,
    )
    dlq_stuck_threshold_entries: int = Field(
        default=1000,
        description="DLQ stuck detection threshold (pending entry count)",
        ge=100,
    )
    emergency_stuck_threshold_seconds: float = Field(
        default=1800.0,
        description=(
            "Emergency recovery/hold stuck detection threshold (seconds, "
            "default 30 minutes). Emergency recovery/hold is a tens-of-minutes "
            "phenomenon, distinct from the generic stuck_threshold_seconds."
        ),
        ge=60.0,
    )

    # Baldur Circuit Breaker (self-protection)
    self_cb_enabled: bool = Field(
        default=False,
        description="Enable Circuit Breaker for Baldur",
    )
    self_cb_failure_threshold: int = Field(
        default=5,
        description="Consecutive failure count to transition CB to Open",
        ge=1,
    )
    self_cb_recovery_timeout_seconds: float = Field(
        default=60.0,
        description="CB Half-Open transition wait time (seconds)",
        ge=10.0,
    )

    # Escalation
    escalation_enabled: bool = Field(
        default=True,
        description="Enable escalation",
    )
    escalation_delay_seconds: float = Field(
        default=180.0,
        description="Escalation delay (wait for auto-recovery, seconds)",
        ge=0.0,
    )
    escalation_cooldown_seconds: float = Field(
        default=3600.0,
        description="Escalation cooldown per component (seconds, default 1 hour)",
        ge=60.0,
    )

    # Recovery toggle (558 D1) — independent of escalation.
    # False (default, slice A): detect + escalate on detection, take NO recovery
    # action. True (FULL mode): governance → cooldown → recovery → delayed
    # escalation. Distinct from dry_run_mode, which suppresses BOTH recovery and
    # escalation (observe-only).
    recovery_enabled: bool = Field(
        default=False,
        description="Enable automatic recovery actions (False: detect+escalate only)",
    )

    # Recovery cooldown
    recovery_cooldown_seconds: float = Field(
        default=300.0,
        description="Recovery attempt cooldown per component (seconds, default 5 minutes)",
        ge=30.0,
    )

    # Workload names (K8s Deployment/StatefulSet resource names)
    redis_workload_name: str = Field(
        default="redis",
        description="Redis Deployment/StatefulSet name (K8s resource name)",
    )
    dlq_worker_workload_name: str = Field(
        default="celery-dlq-worker",
        description="DLQ Worker Deployment name (K8s resource name)",
    )

    # PagerDuty
    pagerduty_routing_key: str | None = Field(
        default=None,
        description="PagerDuty Events API v2 Routing Key",
    )
    pagerduty_severity: Literal["critical", "error", "warning", "info"] = Field(
        default="critical",
        description="PagerDuty alert severity",
    )

    # Slack
    slack_webhook_url: str | None = Field(
        default=None,
        description=(
            "Slack Incoming Webhook URL. WARNING: when set, the OSS "
            "circuit-breaker open/close push POSTs to it for real — including "
            "on a core-only install (no celery extra) and in local "
            "development. Leave it unset locally to avoid sending live "
            "messages to shared channels."
        ),
    )

    # Dry-run mode (observe only; no recovery/escalation)
    dry_run_mode: bool = Field(
        default=False,
        description="Run probes only, skip recovery/escalation (observation mode)",
    )

    # Maintenance components (alerts suppressed for these)
    maintenance_components: list[str] = Field(
        default_factory=list,
        description="List of components under maintenance (alerts suppressed for these)",
    )

    # Escalation API timeout
    escalation_api_timeout_seconds: float = Field(
        default=10.0,
        description="PagerDuty/Slack API call timeout (seconds)",
        ge=1.0,
        le=60.0,
    )

    # Recovery timeout settings (391)
    recovery_total_timeout_seconds: float = Field(
        default=60.0,
        description="Total timeout for all recovery attempts in one check_health() cycle",
        ge=10.0,
    )
    max_items_per_recovery: int = Field(
        default=5,
        description="Max items each recovery method processes per cycle (CB states, zombies)",
        ge=1,
        le=50,
    )
    k8s_api_timeout_seconds: float = Field(
        default=30.0,
        description="Timeout for K8s API calls in RecoveryAdapter",
        ge=5.0,
    )
    docker_restart_timeout_seconds: float = Field(
        default=60.0,
        ge=5.0,
        le=600.0,
        description="Timeout for the docker-compose restart subprocess in RecoveryAdapter",
    )
    docker_scale_timeout_seconds: float = Field(
        default=120.0,
        ge=5.0,
        le=600.0,
        description="Timeout for the docker-compose scale subprocess in RecoveryAdapter",
    )

    # Probe staleness settings (411)
    probe_cache_staleness_multiplier: float = Field(
        default=2.0,
        description="Cache staleness threshold as multiple of refresh interval",
        ge=1.1,
        le=10.0,
    )

    @model_validator(mode="after")
    def _warn_timeout_vs_probe(self) -> Self:
        if self.recovery_total_timeout_seconds > 2 * self.probe_interval_seconds:
            import warnings

            warnings.warn(
                f"recovery_total_timeout_seconds ({self.recovery_total_timeout_seconds}) "
                f"> 2× probe_interval_seconds ({self.probe_interval_seconds}). "
                f"Watchdog loop may fall behind.",
                UserWarning,
                stacklevel=2,
            )
        return self


def get_meta_watchdog_settings() -> MetaWatchdogSettings:
    from baldur.settings.root import get_config

    return get_config().meta.meta_watchdog


def reset_meta_watchdog_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().meta.__dict__["meta_watchdog"]
    except KeyError:
        pass
