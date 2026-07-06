"""
Continuous Audit Configuration.

환경변수 기반 설정으로 하드코딩 문제 방지.
보안에 민감한 값(해시 시드)은 반드시 환경변수로 설정.
"""

import os
from dataclasses import dataclass, field
from typing import Any

import structlog

from baldur.core.serializable import SerializableMixin

logger = structlog.get_logger()


@dataclass
class AuditConfig(SerializableMixin):
    """
    감사 로그 설정 - 환경변수 우선.

    설정 우선순위:
    1. 환경변수 (AUDIT_*)
    2. DNA 선언 (서비스별 설정)
    3. 설정 파일
    4. 코드 기본값

    Environment Variables:
        AUDIT_HASH_SEED: 해시 체인 시드 (필수)
        AUDIT_RETENTION_DAYS: 로그 보존 기간 (기본: 365)
        AUDIT_STORAGE: 스토리지 백엔드 (기본: file)
        AUDIT_S3_BUCKET: S3 버킷 이름 (선택)
        AUDIT_S3_WORM: WORM 모드 활성화 (기본: false)
        AUDIT_ALERT_CHANNELS: 알림 채널 (쉼표 구분)
    """

    # 해시 체인 시드 (환경변수 필수)
    hash_seed: str = field(
        default_factory=lambda: os.environ.get("AUDIT_HASH_SEED", "")
    )

    # 보존 기간 (규정별 다름)
    retention_days: int = field(
        default_factory=lambda: int(os.environ.get("AUDIT_RETENTION_DAYS", "365"))
    )

    # 스토리지 백엔드: file, s3, loki
    storage_backend: str = field(
        default_factory=lambda: os.environ.get("AUDIT_STORAGE", "file")
    )

    # S3 설정 (선택)
    s3_bucket: str | None = field(
        default_factory=lambda: os.environ.get("AUDIT_S3_BUCKET")
    )
    s3_worm_enabled: bool = field(
        default_factory=lambda: (
            os.environ.get("AUDIT_S3_WORM", "false").lower() == "true"
        )
    )

    # 알림 채널
    alert_channels: list[str] = field(default_factory=list)

    # 민감 데이터 마스킹
    mask_sensitive_data: bool = True

    # 무결성 검증 주기 (초)
    integrity_check_interval: int = field(
        default_factory=lambda: int(
            os.environ.get("AUDIT_INTEGRITY_CHECK_INTERVAL", "3600")
        )
    )

    # 배치 저장 설정
    batch_size: int = field(
        default_factory=lambda: int(os.environ.get("AUDIT_BATCH_SIZE", "100"))
    )
    batch_flush_interval: int = field(
        default_factory=lambda: int(os.environ.get("AUDIT_BATCH_FLUSH_INTERVAL", "10"))
    )

    # Distributed hash chain settings
    hash_chain_distributed: bool = field(
        default_factory=lambda: (
            os.environ.get("AUDIT_HASH_CHAIN_DISTRIBUTED", "false").lower() == "true"
        )
    )
    # Per-feature override only; the canonical BALDUR_REDIS_URL fallback is
    # resolved lazily in get_redis_client() so the None default (opt-in
    # distributed hash chain) is preserved.
    hash_chain_redis_url: str | None = field(
        default_factory=lambda: os.environ.get("AUDIT_HASH_CHAIN_REDIS_URL")
    )
    hash_chain_key_prefix: str = field(
        default_factory=lambda: os.environ.get("AUDIT_HASH_CHAIN_KEY_PREFIX", "baldur:")
    )
    hash_chain_lock_timeout: float = field(
        default_factory=lambda: float(
            os.environ.get("AUDIT_HASH_CHAIN_LOCK_TIMEOUT", "5.0")
        )
    )

    def __post_init__(self) -> None:
        """환경변수에서 알림 채널 로드 및 해시 시드 검증."""
        # 알림 채널 파싱
        channels_str = os.environ.get("AUDIT_ALERT_CHANNELS", "")
        if channels_str and not self.alert_channels:
            self.alert_channels = [
                ch.strip() for ch in channels_str.split(",") if ch.strip()
            ]

        # Hash seed validation: required in production, dev seed otherwise.
        if not self.hash_seed:
            # Lazy import keeps baldur.runtime out of the audit module's
            # import-time graph (audit is imported very early via settings).
            from baldur.runtime import is_production

            if is_production():
                raise ValueError(
                    "AUDIT_HASH_SEED environment variable is not set. "
                    "It is mandatory in production for hash chain integrity."
                )
            self.hash_seed = "dev-seed-not-for-production"
            logger.warning("audit_config.dev_seed_used")

    @classmethod
    def from_dna(cls, dna_config: dict) -> "AuditConfig":
        """
        DNA 선언에서 설정 로드 (환경변수가 우선).

        Args:
            dna_config: DNA에서 로드된 설정 딕셔너리

        Returns:
            AuditConfig 인스턴스
        """
        return cls(
            hash_seed=os.environ.get(
                "AUDIT_HASH_SEED", dna_config.get("hash_seed", "")
            ),
            retention_days=int(
                os.environ.get(
                    "AUDIT_RETENTION_DAYS", dna_config.get("retention_days", 365)
                )
            ),
            storage_backend=os.environ.get(
                "AUDIT_STORAGE", dna_config.get("storage", "file")
            ),
            s3_bucket=os.environ.get("AUDIT_S3_BUCKET", dna_config.get("s3_bucket")),
            s3_worm_enabled=os.environ.get(
                "AUDIT_S3_WORM", str(dna_config.get("s3_worm", False))
            ).lower()
            == "true",
            alert_channels=dna_config.get("alert_channels", []),
            mask_sensitive_data=dna_config.get("mask_sensitive_data", True),
        )

    @classmethod
    def get_default(cls) -> "AuditConfig":
        """환경변수에서 기본 설정 생성."""
        return cls()

    def _post_serialize(self, data: dict[str, Any]) -> dict[str, Any]:
        """Mask sensitive fields (hash_seed, hash_chain_redis_url)."""
        data["hash_seed"] = "***" if self.hash_seed else None
        data["hash_chain_redis_url"] = "***" if self.hash_chain_redis_url else None
        return super()._post_serialize(data)

    def get_redis_client(self) -> Any | None:
        """
        Get Redis client for distributed hash chain.

        When distributed mode is enabled and no per-feature override
        (AUDIT_HASH_CHAIN_REDIS_URL) is set, the URL is resolved from the
        canonical BALDUR_REDIS_URL (RedisSettings.url) rather than a bare
        localhost default.

        Returns:
            Redis client if distributed mode enabled, None otherwise.
        """
        if not self.hash_chain_distributed:
            return None

        try:
            from baldur.adapters.redis.connection_factory import (
                get_redis_connection_factory,
            )
            from baldur.settings.redis import get_redis_settings

            redis_url = self.hash_chain_redis_url or get_redis_settings().url
            return get_redis_connection_factory().create(redis_url)
        except ImportError:
            logger.warning("audit_config.redis_factory_unavailable")
            return None
        except Exception as e:
            logger.warning(
                "audit_config.create_redis_client_failed",
                error=e,
            )
            return None


# 규정별 최소 보존 기간 (법적 요구사항 기반 기본값)
COMPLIANCE_RETENTION_DAYS: dict[str, int | None] = {
    "DORA": 1825,  # 5년
    "PCI-DSS": 365,  # 1년
    "SOC2": 365,  # 1년
    "GDPR": None,  # 목적 달성 시까지 (서비스별 판단)
    "HIPAA": 2190,  # 6년
}


def _get_default_max_retention() -> int:
    """Get default max retention days from settings."""
    from baldur.settings.audit import get_audit_settings

    return get_audit_settings().compliance_max_retention_days


def get_recommended_retention(standards: list[str]) -> int:
    """
    규정 목록에 따른 권장 보존 기간 반환.

    Args:
        standards: 준수해야 할 규정 목록 (예: ["DORA", "PCI-DSS"])

    Returns:
        최대 보존 기간 (일)
    """
    max_days = _get_default_max_retention()

    for std in standards:
        days = COMPLIANCE_RETENTION_DAYS.get(std.upper())
        if days and days > max_days:
            max_days = days

    return max_days
