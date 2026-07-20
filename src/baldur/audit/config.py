"""
Continuous Audit Configuration.

Env-var-based configuration to avoid hardcoding.
Security-sensitive values (the hash seed) MUST be set via environment variables.
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
    Audit log configuration - environment variables take precedence.

    Configuration precedence:
    1. Environment variables (AUDIT_*)
    2. DNA declaration (per-service configuration)
    3. Configuration file
    4. Code defaults

    Environment Variables:
        AUDIT_HASH_SEED: Hash chain seed (required)
        AUDIT_RETENTION_DAYS: Log retention period (default: 365)
        AUDIT_STORAGE: Storage backend (default: file)
        AUDIT_S3_BUCKET: S3 bucket name (optional)
        AUDIT_S3_WORM: Enable WORM mode (default: false)
        AUDIT_ALERT_CHANNELS: Alert channels (comma-separated)
    """

    # Hash chain seed (environment variable required)
    hash_seed: str = field(
        default_factory=lambda: os.environ.get("AUDIT_HASH_SEED", "")
    )

    # Retention period (varies per regulation)
    retention_days: int = field(
        default_factory=lambda: int(os.environ.get("AUDIT_RETENTION_DAYS", "365"))
    )

    # Storage backend: file, s3, loki
    storage_backend: str = field(
        default_factory=lambda: os.environ.get("AUDIT_STORAGE", "file")
    )

    # S3 configuration (optional)
    s3_bucket: str | None = field(
        default_factory=lambda: os.environ.get("AUDIT_S3_BUCKET")
    )
    s3_worm_enabled: bool = field(
        default_factory=lambda: (
            os.environ.get("AUDIT_S3_WORM", "false").lower() == "true"
        )
    )

    # Alert channels
    alert_channels: list[str] = field(default_factory=list)

    # Sensitive data masking
    mask_sensitive_data: bool = True

    # Integrity check interval (seconds)
    integrity_check_interval: int = field(
        default_factory=lambda: int(
            os.environ.get("AUDIT_INTEGRITY_CHECK_INTERVAL", "3600")
        )
    )

    # Batch persistence settings
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
        """Load alert channels from env vars and validate the hash seed."""
        # Parse alert channels
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
        Load configuration from a DNA declaration (env vars take precedence).

        Args:
            dna_config: Configuration dictionary loaded from DNA

        Returns:
            AuditConfig instance
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
        """Build the default configuration from environment variables."""
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


# Minimum retention period per regulation (defaults based on legal requirements)
COMPLIANCE_RETENTION_DAYS: dict[str, int | None] = {
    "DORA": 1825,  # 5 years
    "PCI-DSS": 365,  # 1 year
    "SOC2": 365,  # 1 year
    "GDPR": None,  # Until the purpose is fulfilled (decided per service)
    "HIPAA": 2190,  # 6 years
}


def _get_default_max_retention() -> int:
    """Get default max retention days from settings."""
    from baldur.settings.audit import get_audit_settings

    return get_audit_settings().compliance_max_retention_days


def get_recommended_retention(standards: list[str]) -> int:
    """
    Return the recommended retention period for a list of regulations.

    Args:
        standards: Regulations to comply with (e.g. ["DORA", "PCI-DSS"])

    Returns:
        Maximum retention period (days)
    """
    max_days = _get_default_max_retention()

    for std in standards:
        days = COMPLIANCE_RETENTION_DAYS.get(std.upper())
        if days and days > max_days:
            max_days = days

    return max_days
