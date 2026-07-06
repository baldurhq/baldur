"""
Cell Topology Settings - Pydantic v2.

Cell topology-based logical traffic bulkhead settings.
Uses Consistent Hash Ring to assign services/tenants to Cells,
and manages per-Cell Bulkhead isolation.

Environment Variables:
    BALDUR_CELL_TOPOLOGY_ENABLED=false
    BALDUR_CELL_TOPOLOGY_CELL_COUNT=8
    BALDUR_CELL_TOPOLOGY_CELL_PREFIX=cell
    BALDUR_CELL_TOPOLOGY_BULKHEAD_ISOLATION_ENABLED=false
    BALDUR_CELL_TOPOLOGY_BULKHEAD_MAX_CONCURRENT_PER_CELL=100
    BALDUR_CELL_TOPOLOGY_EVACUATION_HEALTH_THRESHOLD=0.3
    BALDUR_CELL_TOPOLOGY_RECOVERY_HEALTH_THRESHOLD=0.7
    BALDUR_CELL_TOPOLOGY_EVACUATION_CONSECUTIVE_COUNT=3
    BALDUR_CELL_TOPOLOGY_RECOVERY_CONSECUTIVE_COUNT=5
    BALDUR_CELL_TOPOLOGY_EVACUATION_DRAIN_GRACE_SECONDS=2.0
    BALDUR_CELL_TOPOLOGY_MAX_EVACUATED_RATIO=0.25
    BALDUR_CELL_TOPOLOGY_ISOLATION_NOTIFICATION_DURATION_SECONDS=3600
    BALDUR_CELL_TOPOLOGY_EVACUATION_HISTORY_MAX_SIZE=1000
    BALDUR_CELL_TOPOLOGY_WARMUP_INITIAL_PERCENTAGE=10.0
    BALDUR_CELL_TOPOLOGY_RECONCILIATION_INTERVAL_SECONDS=15.0
    BALDUR_CELL_TOPOLOGY_SERVICE_HEARTBEAT_TTL_SECONDS=300.0
"""

from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    HugeCount,
    LongDuration,
    MediumDuration,
    Percentage,
    Probability,
    SmallCount,
    StrictProbability,
)


class CellTopologySettings(BaseSettings):
    """Cell topology settings."""

    model_config = make_settings_config("BALDUR_CELL_TOPOLOGY_")

    # ==========================================================================
    # Master toggle
    # ==========================================================================
    enabled: bool = Field(
        default=False,
        description="Cell topology master switch",
    )

    tagging_enabled: bool = Field(
        default=False,
        description="Enable cell_id tagging on requests/tasks",
    )

    bulkhead_isolation_enabled: bool = Field(
        default=False,
        description="Enable Cell-level Bulkhead isolation",
    )

    evacuation_enabled: bool = Field(
        default=False,
        description="Enable Cell evacuation",
    )

    # ==========================================================================
    # Cell configuration
    # ==========================================================================
    cell_count: int = Field(
        default=8,
        ge=1,
        le=256,
        description="Number of Cells",
    )

    cell_prefix: str = Field(
        default="cell",
        description="Cell name prefix (e.g. 'cell' -> 'cell-0', 'cell-1')",
    )

    # ==========================================================================
    # Bulkhead settings
    # ==========================================================================
    bulkhead_max_concurrent_per_cell: HugeCount = Field(
        default=100,
        description="Per-Cell Bulkhead max concurrent requests",
    )

    bulkhead_type: str = Field(
        default="semaphore",
        description="Bulkhead type ('semaphore' or 'thread_pool')",
    )

    # ==========================================================================
    # Evacuation/Health check
    # ==========================================================================
    evacuation_health_threshold: Probability = Field(
        default=0.3,
        description="Evacuation trigger health threshold (increment counter below this)",
    )

    recovery_health_threshold: Probability = Field(
        default=0.7,
        description="Recovery trigger health threshold (increment counter above this)",
    )

    evacuation_consecutive_count: int = Field(
        default=3,
        ge=1,
        le=30,
        description="Consecutive N below threshold triggers evacuation (hysteresis)",
    )

    recovery_consecutive_count: int = Field(
        default=5,
        ge=1,
        le=30,
        description="Consecutive N above threshold triggers recovery (hysteresis)",
    )

    evacuation_traffic_drain_seconds: int = Field(
        default=30,
        ge=1,
        le=600,
        description="Traffic drain wait time (seconds)",
    )

    evacuation_drain_grace_seconds: float = Field(
        default=2.0,
        ge=0.0,
        le=30.0,
        description="NTP Drift tolerance buffer (seconds). Extra margin for drain time check.",
    )

    max_evacuated_ratio: Probability = Field(
        default=0.25,
        description=(
            "Max isolation ratio of total Cells (Cascading Failure prevention). "
            "e.g. 0.25 -> max 2 of 8 Cells isolated"
        ),
    )

    isolation_notification_duration_seconds: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="Expected isolation duration passed to RegionalIsolationGate (seconds)",
    )

    evacuation_history_max_size: int = Field(
        default=1000,
        ge=10,
        le=100000,
        description="CellEvacuationPolicy in-memory evacuation history max entries",
    )

    health_check_interval_seconds: int = Field(
        default=10,
        ge=1,
        le=300,
        description="Health check interval (seconds)",
    )

    # ==========================================================================
    # Metrics / Prometheus
    # ==========================================================================
    metrics_enabled: bool = Field(
        default=True,
        description="Prometheus metrics collection enabled",
    )

    prometheus_url: str = Field(
        default="http://localhost:9090",
        description="Prometheus HTTP API endpoint URL",
    )

    # ==========================================================================
    # Dynamic scaling
    # ==========================================================================
    warmup_initial_percentage: Percentage = Field(
        default=10.0,
        description="Initial traffic ratio for new Cell deployment (%)",
    )

    warmup_step_percentage: float = Field(
        default=20.0,
        ge=1.0,
        le=100.0,
        description="Per-promotion step increment (%)",
    )

    warmup_step_interval_seconds: LongDuration = Field(
        default=60.0,
        description="Wait time between promotion steps (seconds)",
    )

    # ==========================================================================
    # Anti-entropy Reconciliation
    # ==========================================================================
    reconciliation_interval_seconds: float = Field(
        default=15.0,
        ge=1.0,
        le=300.0,
        description="Anti-entropy Reconciliation interval (seconds)",
    )

    # ==========================================================================
    # Service Heartbeat
    # ==========================================================================
    service_heartbeat_interval_seconds: MediumDuration = Field(
        default=30.0,
        description="Service Heartbeat renewal interval (seconds)",
    )

    service_heartbeat_ttl_seconds: float = Field(
        default=300.0,
        ge=10.0,
        le=3600.0,
        description="Service Heartbeat TTL (seconds). Default 5min.",
    )

    # ==========================================================================
    # Trust Boundary control
    # ==========================================================================
    internal_dns_suffixes: list[str] = Field(
        default=[".svc.cluster.local", ".internal"],
        description=(
            "DNS suffixes considered as internal services. "
            "OTel Baggage (cell_id etc.) is propagated only to hosts matching these suffixes. "
            "e.g. Kubernetes: .svc.cluster.local"
        ),
    )

    trusted_source_cidrs: list[str] = Field(
        default=[
            "10.0.0.0/8",  # RFC 1918 Class A — K8s Pod/Service CIDR default
            "172.16.0.0/12",  # RFC 1918 Class B
            "192.168.0.0/16",  # RFC 1918 Class C
            "127.0.0.0/8",  # Loopback (dev environment)
        ],
        description=(
            "Source CIDR list trusted for cell_id propagation. "
            "Only requests from these ranges accept upstream cell_id. "
            "Public internet requests fall back to local hashing."
        ),
    )

    # ==========================================================================
    # Prometheus API (services/cell_topology/health.py) — 339
    # ==========================================================================
    prometheus_timeout_seconds: float = Field(
        default=3.0,
        ge=0.5,
        le=30.0,
        description="Prometheus API query timeout (seconds).",
    )
    prometheus_max_consecutive_failures: SmallCount = Field(
        default=3,
        description="Consecutive Prometheus failures before fallback mode.",
    )
    prometheus_retry_after_seconds: float = Field(
        default=60.0,
        ge=5.0,
        le=600.0,
        description="Wait time before half-open probe after fallback (seconds).",
    )

    # ==========================================================================
    # Health Score Weights (services/cell_topology/health.py) — 339
    # ==========================================================================
    health_weight_error_rate: Probability = Field(
        default=0.35,
        description="Health score weight for error rate component.",
    )
    health_weight_latency: Probability = Field(
        default=0.25,
        description="Health score weight for latency P99 component.",
    )
    health_weight_bulkhead: Probability = Field(
        default=0.20,
        description="Health score weight for bulkhead utilization component.",
    )
    health_weight_cb_open: Probability = Field(
        default=0.20,
        description="Health score weight for circuit breaker open ratio component.",
    )

    # ==========================================================================
    # Health Score Normalization (services/cell_topology/health.py) — 339
    # ==========================================================================
    health_max_error_rate: StrictProbability = Field(
        default=0.5,
        description="Error rate normalization ceiling (1.0 = 100%).",
    )
    health_max_latency_p99: float = Field(
        default=5.0,
        ge=0.1,
        le=60.0,
        description="Latency P99 normalization ceiling (seconds).",
    )
    health_min_samples_for_penalty: int = Field(
        default=10,
        ge=1,
        le=1000,
        description="Minimum request count before error rate penalty applies.",
    )
    health_ewma_alpha: StrictProbability = Field(
        default=0.3,
        description="EWMA smoothing factor for health score.",
    )

    @model_validator(mode="after")
    def _validate_health_weights_sum(self) -> CellTopologySettings:
        """Validate that health score weights sum to 1.0."""
        total = (
            self.health_weight_error_rate
            + self.health_weight_latency
            + self.health_weight_bulkhead
            + self.health_weight_cb_open
        )
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"Health weights must sum to 1.0, got {total}")
        return self


def get_cell_topology_settings() -> CellTopologySettings:
    from baldur.settings.root import get_config

    return get_config().multi_region.cell_topology


def reset_cell_topology_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().multi_region.__dict__["cell_topology"]
    except KeyError:
        pass
