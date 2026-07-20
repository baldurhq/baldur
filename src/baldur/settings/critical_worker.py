"""
Critical Worker Settings - Pydantic v2.

P0 priority dedicated Worker settings.

Replaces:
- services/coordination/critical_worker.py:CriticalPathDedicatedWorkerConfig defaults

Environment Variables:
    BALDUR_CRITICAL_WORKER_CRITICAL_QUEUE_NAME=baldur.critical
    BALDUR_CRITICAL_WORKER_CRITICAL_WORKER_COUNT=2
    BALDUR_CRITICAL_WORKER_DEPLOYMENT_ENV=STANDARD
    BALDUR_CRITICAL_WORKER_POOL_MINIMAL_WORKER_COUNT=2
    BALDUR_CRITICAL_WORKER_POOL_STANDARD_WORKER_COUNT=4
    BALDUR_CRITICAL_WORKER_POOL_HIGH_AVAILABILITY_WORKER_COUNT=4
    BALDUR_CRITICAL_WORKER_POOL_BURST_WORKER_COUNT=2
    BALDUR_CRITICAL_WORKER_POOL_ENTERPRISE_WORKER_COUNT=8
"""

from enum import Enum

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import SmallCount, TinyCount

logger = structlog.get_logger()


class DeploymentEnvironment(str, Enum):
    """
    Deployment environment type.

    Automatically adjusts Worker Pool settings by environment.
    """

    MINIMAL = "MINIMAL"
    """Minimal resource environment (dev, test)"""

    STANDARD = "STANDARD"
    """Standard operational environment"""

    HIGH_AVAILABILITY = "HIGH_AVAILABILITY"
    """High availability environment (critical services)"""

    BURST = "BURST"
    """Burst environment (traffic spike preparation)"""

    ENTERPRISE = "ENTERPRISE"
    """Enterprise environment (large-scale traffic)"""


class CriticalWorkerSettings(BaseSettings):
    """
    Critical Path dedicated Worker settings.

    Operates a small dedicated Worker group that processes only P0 tasks,
    separate from general Worker groups.
    """

    model_config = make_settings_config("BALDUR_CRITICAL_WORKER_")

    # ==========================================================================
    # Queue Names (from critical_worker.py)
    # ==========================================================================
    critical_queue_name: str = Field(
        default="baldur.critical",
        description="P0 dedicated queue (Abort, Kill Switch only)",
    )

    high_priority_queue_name: str = Field(
        default="baldur.high",
        description="P1-P2 high priority queue (Escalation, Recovery)",
    )

    default_queue_name: str = Field(
        default="baldur.default",
        description="P3+ general queue (Alert, Audit, Archive)",
    )

    recovery_queue_name: str = Field(
        default="baldur.recovery",
        description="Recovery dedicated queue",
    )

    notification_queue_name: str = Field(
        default="baldur.notifications",
        description="Notification dedicated queue",
    )

    maintenance_queue_name: str = Field(
        default="baldur.maintenance",
        description="Maintenance task queue",
    )

    # ==========================================================================
    # Worker Counts
    # ==========================================================================
    critical_worker_count: TinyCount = Field(
        default=2,
        description="Dedicated Worker count (minimum 1 guaranteed)",
    )

    high_priority_worker_count: SmallCount = Field(
        default=4,
        description="High priority Worker count",
    )

    default_worker_count: int = Field(
        default=8,
        ge=1,
        le=50,
        description="General Worker count",
    )

    # ==========================================================================
    # Concurrency Settings (per queue)
    # ==========================================================================
    critical_concurrency: TinyCount = Field(
        default=2,
        description="Critical queue concurrency (throughput per Worker)",
    )

    high_priority_concurrency: SmallCount = Field(
        default=4,
        description="High Priority queue concurrency",
    )

    default_concurrency: int = Field(
        default=8,
        ge=1,
        le=50,
        description="Default queue concurrency",
    )

    # ==========================================================================
    # Prefetch Settings
    # ==========================================================================
    critical_prefetch_multiplier: int = Field(
        default=1,
        ge=1,
        le=4,
        description="Critical queue prefetch multiplier",
    )

    high_priority_prefetch_multiplier: int = Field(
        default=2,
        ge=1,
        le=8,
        description="High Priority queue prefetch multiplier",
    )

    default_prefetch_multiplier: int = Field(
        default=4,
        ge=1,
        le=16,
        description="Default queue prefetch multiplier",
    )

    # ==========================================================================
    # Task Timeout
    # ==========================================================================
    task_timeout_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Task timeout (seconds)",
    )

    # ==========================================================================
    # Deployment Environment
    # ==========================================================================
    deployment_env: DeploymentEnvironment = Field(
        default=DeploymentEnvironment.STANDARD,
        description="Deployment environment (MINIMAL, STANDARD, HIGH_AVAILABILITY, BURST, ENTERPRISE)",
    )

    # ==========================================================================
    # Environment-specific Worker Pool Settings (MINIMAL)
    # ==========================================================================
    pool_minimal_worker_count: int = Field(
        default=2,
        ge=1,
        le=4,
        description="MINIMAL environment Worker count",
    )
    pool_minimal_concurrency: int = Field(
        default=2,
        ge=1,
        le=4,
        description="MINIMAL environment concurrency",
    )
    pool_minimal_prefetch_multiplier: int = Field(
        default=1,
        ge=1,
        le=2,
        description="MINIMAL environment prefetch multiplier",
    )

    # ==========================================================================
    # Environment-specific Worker Pool Settings (STANDARD)
    # ==========================================================================
    pool_standard_worker_count: int = Field(
        default=4,
        ge=2,
        le=8,
        description="STANDARD environment Worker count",
    )
    pool_standard_concurrency: int = Field(
        default=4,
        ge=2,
        le=8,
        description="STANDARD environment concurrency",
    )
    pool_standard_prefetch_multiplier: int = Field(
        default=2,
        ge=1,
        le=4,
        description="STANDARD environment prefetch multiplier",
    )

    # ==========================================================================
    # Environment-specific Worker Pool Settings (HIGH_AVAILABILITY)
    # ==========================================================================
    pool_high_availability_worker_count: int = Field(
        default=4,
        ge=2,
        le=12,
        description="HIGH_AVAILABILITY environment Worker count",
    )
    pool_high_availability_concurrency: int = Field(
        default=4,
        ge=2,
        le=12,
        description="HIGH_AVAILABILITY environment concurrency",
    )
    pool_high_availability_prefetch_multiplier: int = Field(
        default=2,
        ge=1,
        le=4,
        description="HIGH_AVAILABILITY environment prefetch multiplier",
    )

    # ==========================================================================
    # Environment-specific Worker Pool Settings (BURST)
    # ==========================================================================
    pool_burst_worker_count: int = Field(
        default=2,
        ge=1,
        le=8,
        description="BURST environment Worker count (fewer workers, higher concurrency)",
    )
    pool_burst_concurrency: int = Field(
        default=4,
        ge=2,
        le=16,
        description="BURST environment concurrency",
    )
    pool_burst_prefetch_multiplier: int = Field(
        default=4,
        ge=1,
        le=8,
        description="BURST environment prefetch multiplier",
    )

    # ==========================================================================
    # Environment-specific Worker Pool Settings (ENTERPRISE)
    # ==========================================================================
    pool_enterprise_worker_count: int = Field(
        default=8,
        ge=4,
        le=32,
        description="ENTERPRISE environment Worker count",
    )
    pool_enterprise_concurrency: int = Field(
        default=8,
        ge=4,
        le=32,
        description="ENTERPRISE environment concurrency",
    )
    pool_enterprise_prefetch_multiplier: int = Field(
        default=4,
        ge=1,
        le=8,
        description="ENTERPRISE environment prefetch multiplier",
    )

    @field_validator("critical_worker_count")
    @classmethod
    def validate_critical_worker_count(cls, v: int) -> int:
        """Ensure minimum 1 Worker."""
        if v < 1:
            logger.warning(
                "safe_default.invalid_using",
                setting_value=v,
            )
            return 1
        return v

    def get_pool_config_for_env(
        self, env: DeploymentEnvironment | None = None
    ) -> dict[str, int]:
        """
        Return Worker Pool settings for the environment.

        Args:
            env: Deployment environment (default: self.deployment_env)

        Returns:
            {"worker_count": N, "concurrency": N, "prefetch_multiplier": N}
        """
        env = env or self.deployment_env

        pool_configs = {
            DeploymentEnvironment.MINIMAL: {
                "worker_count": self.pool_minimal_worker_count,
                "concurrency": self.pool_minimal_concurrency,
                "prefetch_multiplier": self.pool_minimal_prefetch_multiplier,
            },
            DeploymentEnvironment.STANDARD: {
                "worker_count": self.pool_standard_worker_count,
                "concurrency": self.pool_standard_concurrency,
                "prefetch_multiplier": self.pool_standard_prefetch_multiplier,
            },
            DeploymentEnvironment.HIGH_AVAILABILITY: {
                "worker_count": self.pool_high_availability_worker_count,
                "concurrency": self.pool_high_availability_concurrency,
                "prefetch_multiplier": self.pool_high_availability_prefetch_multiplier,
            },
            DeploymentEnvironment.BURST: {
                "worker_count": self.pool_burst_worker_count,
                "concurrency": self.pool_burst_concurrency,
                "prefetch_multiplier": self.pool_burst_prefetch_multiplier,
            },
            DeploymentEnvironment.ENTERPRISE: {
                "worker_count": self.pool_enterprise_worker_count,
                "concurrency": self.pool_enterprise_concurrency,
                "prefetch_multiplier": self.pool_enterprise_prefetch_multiplier,
            },
        }

        return pool_configs.get(env, pool_configs[DeploymentEnvironment.STANDARD])


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_critical_worker_settings() -> "CriticalWorkerSettings":
    """Get cached CriticalWorkerSettings instance."""
    from baldur.settings.root import get_config

    return get_config().services_group.critical_worker


def reset_critical_worker_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["critical_worker"]
    except KeyError:
        pass
