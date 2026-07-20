"""
Post-mortem Settings - Pydantic v2.

Configuration for post-mortem report generation and auto-triggering.

Split out of the X-Test module as standalone settings; also used in production.

Environment Variables:
    BALDUR_POSTMORTEM_HISTORY_LIMIT=100
    BALDUR_POSTMORTEM_AUTO_ENABLED=false
    BALDUR_POSTMORTEM_AUTO_MIN_DURATION=30
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class PostmortemSettings(BaseSettings):
    """
    Post-mortem report generation and auto-trigger settings.

    History lookup:
    - history_limit: Number of events queried when generating a post-mortem (100)

    Auto generation:
    - auto_enabled: Generate a post-mortem automatically on CB CLOSED (False)
    - auto_min_duration: Minimum incident duration for auto generation (30s)

    Notification:
    - notification_enabled: Send a notification on post-mortem generation (False)
    - notification_min_duration: Minimum incident duration for notification (60s)

    Incident list:
    - incidents_default_limit: Default limit for incident list queries (10)
    """

    model_config = make_settings_config("BALDUR_POSTMORTEM_")

    # ==========================================================================
    # History Limit - number of events queried when generating a post-mortem
    # ==========================================================================
    history_limit: int = Field(
        default=100,
        ge=50,
        le=500,
        description="Number of events to query when generating a post-mortem",
    )

    # ==========================================================================
    # Auto Generation - automatic post-mortem generation on CB CLOSED
    # ==========================================================================
    auto_enabled: bool = Field(
        default=False,
        description="Enable auto post-mortem generation on CB CLOSED",
    )

    auto_min_duration: int = Field(
        default=30,
        ge=0,
        le=3600,
        description="Minimum incident duration for auto post-mortem generation (seconds)",
    )

    # ==========================================================================
    # Notification - notification sent on post-mortem generation
    # ==========================================================================
    notification_enabled: bool = Field(
        default=False,
        description="Enable notification on post-mortem generation",
    )

    notification_min_duration: int = Field(
        default=60,
        ge=0,
        le=3600,
        description="Minimum incident duration for post-mortem notification (seconds)",
    )

    # ==========================================================================
    # Incidents List - incident list queries
    # ==========================================================================
    incidents_default_limit: int = Field(
        default=10,
        ge=5,
        le=100,
        description="Default limit for incident list queries",
    )

    # ==========================================================================
    # Incident Group - grouping of cascading CB events
    # ==========================================================================
    incident_group_enabled: bool = Field(
        default=False,
        description="Enable cascading CB event grouping",
    )

    incident_group_window_seconds: int = Field(
        default=600,
        ge=60,
        le=3600,
        description="Grouping window size (seconds, default 10 minutes)",
    )

    incident_group_inactivity_seconds: int = Field(
        default=120,
        ge=30,
        le=600,
        description="Inactivity termination time (seconds, default 2 minutes)",
    )

    incident_group_min_count: int = Field(
        default=2,
        ge=1,
        le=100,
        description="Minimum incident count for grouping",
    )

    # ==========================================================================
    # Notification Aggregation - alert aggregation (prevents alert storms)
    # ==========================================================================
    notification_aggregation_enabled: bool = Field(
        default=False,
        description="Enable notification aggregation",
    )

    notification_aggregation_window_seconds: int = Field(
        default=60,
        ge=10,
        le=300,
        description="Notification aggregation window size (seconds)",
    )

    notification_aggregation_max_wait_seconds: int = Field(
        default=300,
        ge=60,
        le=600,
        description="Maximum notification wait time (seconds)",
    )

    # ==========================================================================
    # Timeline Snapshot - post-mortem timeline snapshot retention
    # ==========================================================================

    # Prometheus query settings
    snapshot_prometheus_enabled: bool = Field(
        default=False,
        description="Enable Prometheus peak metrics query",
    )

    snapshot_prometheus_url: str = Field(
        default="http://prometheus:9090",
        description="Prometheus server URL",
    )

    snapshot_prometheus_timeout: int = Field(
        default=10,
        ge=1,
        le=60,
        description="Prometheus query timeout (seconds)",
    )

    # Log collection settings
    snapshot_logs_enabled: bool = Field(
        default=False,
        description="Enable error log collection",
    )

    snapshot_logs_max_count: int = Field(
        default=50,
        ge=10,
        le=200,
        description="Maximum number of error logs to collect",
    )

    snapshot_logs_max_length: int = Field(
        default=500,
        ge=100,
        le=2000,
        description="Maximum log message length",
    )

    # Grafana dashboard link settings
    snapshot_grafana_base_url: str = Field(
        default="http://grafana:3000",
        description="Grafana server URL",
    )

    snapshot_grafana_dashboard_uid: str = Field(
        default="baldur",
        description="Grafana dashboard UID",
    )

    # ==========================================================================
    # Deployment Correlator - deployment correlation analysis
    # ==========================================================================
    deployment_correlator_enabled: bool = Field(
        default=False,
        description="Enable deployment correlation analysis",
    )

    deployment_adapter: str = Field(
        default="mock",
        description="Deployment adapter selection (mock/kubernetes)",
    )

    deployment_pre_window_minutes: int = Field(
        default=60,
        ge=10,
        le=180,
        description="Pre-incident deployment lookup window (minutes)",
    )

    deployment_post_window_minutes: int = Field(
        default=30,
        ge=5,
        le=60,
        description="Post-incident deployment lookup window (minutes)",
    )

    # ==========================================================================
    # Revision/Versioning - post-mortem revision management
    # ==========================================================================
    versioning_enabled: bool = Field(
        default=False,
        description="Enable postmortem versioning",
    )

    max_revisions: int = Field(
        default=50,
        ge=10,
        le=200,
        description="Maximum revisions per postmortem",
    )

    auto_seal_days: int = Field(
        default=30,
        ge=0,
        le=365,
        description="Auto-seal after N days (0=disabled)",
    )

    revision_storage: str = Field(
        default="hybrid",
        description="Revision storage type (redis/postgresql/hybrid)",
    )

    # ==========================================================================
    # Deep Links - post-mortem deep link URL settings
    # ==========================================================================
    deep_links_enabled: bool = Field(
        default=False,
        description="Enable deep link generation",
    )

    postmortem_base_url: str = Field(
        default="",
        description="Postmortem detail page base URL",
    )

    postmortem_timeline_url: str = Field(
        default="",
        description="Postmortem timeline view URL",
    )

    audit_log_base_url: str = Field(
        default="",
        description="Audit log UI base URL",
    )

    audit_evidence_base_url: str = Field(
        default="",
        description="CascadeEvent evidence page base URL",
    )

    # ==========================================================================
    # Notification Channels - notification channel settings
    # ==========================================================================
    slack_webhook_url: str = Field(
        default="",
        description="Slack Webhook URL for postmortem notifications",
    )

    # ==========================================================================
    # CascadeEvent Integration - audit evidence linking
    # ==========================================================================
    cascade_event_integration_enabled: bool = Field(
        default=False,
        description="Enable CascadeEvent audit evidence linking",
    )


def get_postmortem_settings() -> PostmortemSettings:
    from baldur.settings.root import get_config

    return get_config().slo_group.postmortem


# ==========================================================================
# Module Exports
# ==========================================================================

__all__ = [
    "PostmortemSettings",
    "get_postmortem_settings",
    "reset_postmortem_settings",
]


def reset_postmortem_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().slo_group.__dict__["postmortem"]
    except KeyError:
        pass
