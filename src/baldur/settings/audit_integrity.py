"""
Audit Integrity Settings - Pydantic v2.

Audit integrity and Cold Storage settings.

Replaces:
- audit/integrity/sequence.py:DEFAULT_PENDING_TTL_SECONDS, DEFAULT_ORPHAN_TTL_SECONDS
- audit/integrity/cold_storage.py:ARCHIVE_THRESHOLD_DAYS, DEFAULT_COLD_RETENTION_YEARS

Environment Variables:
    BALDUR_AUDIT_INTEGRITY_PENDING_TTL_SECONDS=30
    BALDUR_AUDIT_INTEGRITY_ORPHAN_TTL_SECONDS=86400
    BALDUR_AUDIT_INTEGRITY_ARCHIVE_THRESHOLD_DAYS=7
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class AuditIntegritySettings(BaseSettings):
    """
    Audit integrity and Cold Storage settings.

    Sequence TTL:
    - pending_ttl_seconds: TTL for pending entries (30 seconds)
    - orphan_ttl_seconds: TTL for orphaned entries (24 hours)

    Cold Storage:
    - archive_threshold_days: hot-to-cold archive threshold (7 days)
    - cold_retention_years: cold storage retention period (7 years, legal requirement)
    """

    model_config = make_settings_config("BALDUR_AUDIT_INTEGRITY_")

    # ==========================================================================
    # Sequence TTL - from audit/integrity/sequence.py
    # ==========================================================================
    pending_ttl_seconds: int = Field(
        default=30,
        ge=10,
        le=300,
        description="Pending entry TTL (seconds)",
    )

    orphan_ttl_seconds: int = Field(
        default=86400,
        ge=3600,
        le=604800,  # 7일
        description="Orphan entry TTL (seconds). Default 24 hours.",
    )

    # ==========================================================================
    # Cold Storage - from audit/integrity/cold_storage.py
    # ==========================================================================
    archive_threshold_days: int = Field(
        default=7,
        ge=1,
        le=30,
        description="Hot-to-cold archive threshold (days)",
    )

    cold_retention_years: int = Field(
        default=7,
        ge=1,
        le=10,
        description="Cold storage retention period (years). Legal requirement.",
    )

    # ==========================================================================
    # Retention - additional
    # ==========================================================================
    retention_days: int = Field(
        default=365,
        ge=90,
        le=3650,
        description="General audit log retention period (days). Default 1 year.",
    )

    # ==========================================================================
    # Anchor - from audit/integrity/anchor.py
    # ==========================================================================
    anchor_retention_days: int = Field(
        default=90,
        ge=30,
        le=365,
        description="Daily hash anchor retention period (days). Default 90 days.",
    )

    # ==========================================================================
    # Cross Cluster - from audit/integrity/cross_cluster_linker.py
    # ==========================================================================
    cross_cluster_local_ttl_days: int = Field(
        default=90,
        ge=30,
        le=365,
        description="Local cluster anchor TTL (days). Default 90 days.",
    )

    cross_cluster_global_ttl_days: int = Field(
        default=365,
        ge=90,
        le=730,
        description="Global cluster anchor TTL (days). Default 1 year.",
    )

    # ==========================================================================
    # Health Score - from audit/integrity/health_score.py
    # ==========================================================================
    health_healthy_threshold: float = Field(
        default=95.0,
        ge=80.0,
        le=100.0,
        description="Integrity healthy threshold (%). Healthy if >= 95%.",
    )

    health_warning_threshold: float = Field(
        default=80.0,
        ge=50.0,
        le=95.0,
        description="Integrity warning threshold (%). Warning if >= 80%.",
    )

    health_critical_threshold: float = Field(
        default=50.0,
        ge=0.0,
        le=80.0,
        description="Integrity critical threshold (%). Critical if < 50%.",
    )

    # ==========================================================================
    # S3 WORM - from audit/backends/s3_worm.py
    # ==========================================================================
    s3_worm_retention_days: int = Field(
        default=365,
        ge=90,
        le=2555,
        description="S3 WORM object retention period (days). Default 1 year, set per legal requirements.",
    )

    # ==========================================================================
    # Health Score Cache (audit/integrity/health_score.py) — 339
    # ==========================================================================
    health_score_max_events: int = Field(
        default=1000,
        ge=10,
        le=100000,
        description="Maximum recovery events to keep in IntegrityHealthScore buffer.",
    )
    health_score_cache_ttl_seconds: float = Field(
        default=10.0,
        ge=1.0,
        le=300.0,
        description="IntegrityHealthScore metrics cache TTL (seconds).",
    )

    # ==========================================================================
    # Integrity Gate - Recovery Gate
    # ==========================================================================

    integrity_gate_fail_open: bool = Field(
        default=True,
        description="Integrity gate fail-open policy. False for fail-secure (PCI-DSS).",
    )

    @model_validator(mode="after")
    def validate_retention(self) -> "AuditIntegritySettings":
        """Validate that the archive threshold is shorter than the retention period."""
        if self.archive_threshold_days > self.retention_days:
            raise ValueError(
                f"archive_threshold_days ({self.archive_threshold_days}) must be less than "
                f"retention_days ({self.retention_days})"
            )
        return self

    @model_validator(mode="after")
    def validate_health_thresholds(self) -> "AuditIntegritySettings":
        """Validate health score threshold order: healthy > warning > critical."""
        if self.health_healthy_threshold <= self.health_warning_threshold:
            raise ValueError(
                f"health_healthy_threshold ({self.health_healthy_threshold}) must be greater than "
                f"health_warning_threshold ({self.health_warning_threshold})"
            )
        if self.health_warning_threshold <= self.health_critical_threshold:
            raise ValueError(
                f"health_warning_threshold ({self.health_warning_threshold}) must be greater than "
                f"health_critical_threshold ({self.health_critical_threshold})"
            )
        return self

    @model_validator(mode="after")
    def validate_cross_cluster_ttl(self) -> "AuditIntegritySettings":
        """Validate cross-cluster TTL order: global >= local."""
        if self.cross_cluster_global_ttl_days < self.cross_cluster_local_ttl_days:
            raise ValueError(
                f"cross_cluster_global_ttl_days ({self.cross_cluster_global_ttl_days}) must be >= "
                f"cross_cluster_local_ttl_days ({self.cross_cluster_local_ttl_days})"
            )
        return self


def get_audit_integrity_settings() -> "AuditIntegritySettings":
    from baldur.settings.root import get_config

    return get_config().audit_group.audit_integrity


def reset_audit_integrity_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().audit_group.__dict__["audit_integrity"]
    except KeyError:
        pass
