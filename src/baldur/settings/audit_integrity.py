"""
Audit Integrity Settings - Pydantic v2.

감사 무결성 및 Cold Storage 관련 설정입니다.

Replaces:
- audit/integrity/sequence.py:DEFAULT_PENDING_TTL_SECONDS, DEFAULT_ORPHAN_TTL_SECONDS
- audit/integrity/cold_storage.py:ARCHIVE_THRESHOLD_DAYS, DEFAULT_COLD_RETENTION_YEARS
- audit/config.py:integrity_check_interval, hash_chain_lock_timeout

Environment Variables:
    BALDUR_AUDIT_INTEGRITY_PENDING_TTL_SECONDS=30
    BALDUR_AUDIT_INTEGRITY_ORPHAN_TTL_SECONDS=86400
    BALDUR_AUDIT_INTEGRITY_ARCHIVE_THRESHOLD_DAYS=7

Reference:
- docs/baldur/middleware_system/92_CONFIG_IMPLEMENTATION_GUIDE.md (Week 4 [25])
- docs/baldur/middleware_system/91_CONFIG_INVENTORY.md §9.7, §9.8, §9.9
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import STANDARD_RETRY_COUNT, TinyCount


class AuditIntegritySettings(BaseSettings):
    """
    감사 무결성 및 Cold Storage 설정.

    시퀀스 TTL:
    - pending_ttl_seconds: 보류 중인 항목 TTL (30초)
    - orphan_ttl_seconds: 고아 항목 TTL (24시간)

    Cold Storage:
    - archive_threshold_days: 아카이브 임계치 (7일)
    - cold_retention_years: 콜드 보관 기간 (7년, 법적 요구사항)

    무결성 검사:
    - integrity_check_interval: 검사 간격 (1시간)
    - hash_chain_lock_timeout: 해시 체인 락 타임아웃 (5초)
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
    # Integrity Check - from audit/config.py
    # ==========================================================================
    integrity_check_interval: int = Field(
        default=3600,
        ge=300,
        le=86400,
        description="Integrity check interval (seconds). Default 1 hour.",
    )

    hash_chain_lock_timeout: float = Field(
        default=5.0,
        ge=1.0,
        le=30.0,
        description="Hash chain lock timeout (seconds)",
    )

    # ==========================================================================
    # Verification - from audit/integrity
    # ==========================================================================
    verification_batch_size: int = Field(
        default=100,
        ge=10,
        le=1000,
        description="Verification batch size",
    )

    max_verification_retries: TinyCount = Field(
        default=STANDARD_RETRY_COUNT,
        description="Maximum number of verification retries",
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
    # Integrity Triad - Background Verifier + Recovery Gate + Merkle
    # ==========================================================================

    background_verify_merkle_threshold: int = Field(
        default=10000,
        ge=1000,
        le=1000000,
        description="Switch to MerkleSpotChecker when entry count exceeds this. Default 10000.",
    )

    merkle_block_size: int = Field(
        default=1000,
        ge=100,
        le=10000,
        description="Merkle spot-check block size. Default 1000.",
    )

    integrity_gate_fail_open: bool = Field(
        default=True,
        description="Integrity gate fail-open policy. False for fail-secure (PCI-DSS).",
    )

    integrity_gate_max_entries: int = Field(
        default=50000,
        ge=1000,
        le=1000000,
        description="Maximum entries for gate verification. Uses MerkleSpotChecker when exceeded.",
    )

    @model_validator(mode="after")
    def validate_retention(self) -> "AuditIntegritySettings":
        """아카이브 임계치가 보관 기간보다 작은지 검증."""
        if self.archive_threshold_days > self.retention_days:
            raise ValueError(
                f"archive_threshold_days ({self.archive_threshold_days}) must be less than "
                f"retention_days ({self.retention_days})"
            )
        return self

    @model_validator(mode="after")
    def validate_health_thresholds(self) -> "AuditIntegritySettings":
        """Health score 임계값 순서 검증: healthy > warning > critical."""
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
        """Cross cluster TTL 순서 검증: global >= local."""
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
