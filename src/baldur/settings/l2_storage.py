"""
L2 Storage Settings - Pydantic v2.

Single Source of Truth for L2 storage configuration.

Replaces:
- core/config.py:L2StorageConfig (lines 470-512)
- core/safe_defaults.py:SAFE_DEFAULTS["l2_storage"]
- core/safe_defaults.py:VALIDATION_RULES["l2_storage"]
- config.py:L2StorageConfig (lines 525-614)
- config.py:L2StorageRuntimeConfig (lines 616-860)

Environment Variables:
    BALDUR_L2_STORAGE_REDIS_TIMEOUT_MS=50
    BALDUR_L2_STORAGE_RECONCILIATION_INTERVAL_SECONDS=300
"""

from __future__ import annotations

import threading

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    STANDARD_POOL_SIZE,
    STANDARD_RETRY_COUNT,
    MediumCount,
    TinyCount,
)
from baldur.utils.time import utc_now


class L2StorageSettings(BaseSettings):
    """
    L2 Storage runtime configuration with validation.

    Timeouts, shadow logging, and health check settings.

    All defaults match core/config.py:L2StorageConfig
    All validation rules match core/safe_defaults.py:VALIDATION_RULES["l2_storage"]
    """

    model_config = make_settings_config("BALDUR_L2_STORAGE_")

    # ==========================================================================
    # Enable/Disable (from safe_defaults l2_storage)
    # ==========================================================================
    enabled: bool = Field(
        default=False,
        description="Enable L2 storage (disabled by default, requires explicit activation)",
    )

    # ==========================================================================
    # Timeouts (ms) (from core/config.py lines 492-494)
    # Validation rules from core/safe_defaults.py lines 333-337
    # ==========================================================================
    redis_timeout_ms: int = Field(
        default=1000,
        ge=10,
        le=1000,
        description=(
            "Redis timeout in milliseconds for the L2 future budget "
            "(LayeredRepository). LAN p99 is typically <5ms; the 1000ms "
            "default reserves headroom for cold-start bursts that are "
            "absorbed by LayeredRepository._ensure_l2_warmup_once. "
            "Lowering below ~500ms forfeits the Cat 6.4 cold-start "
            "cluster-cap guarantee unless the deployment uses a known-fast "
            "Redis with a custom warmup."
        ),
    )
    database_timeout_ms: int = Field(
        default=200,
        ge=50,
        le=5000,
        description="Database connection timeout in milliseconds",
    )
    fallback_timeout_ms: int = Field(
        default=100,
        ge=10,
        le=1000,
        description="Fallback timeout in milliseconds",
    )

    # ==========================================================================
    # Shadow Logging (from core/config.py lines 497-498)
    # ==========================================================================
    shadow_log_max_entries: int = Field(
        default=1000,
        ge=100,
        le=10000,
        description="Maximum shadow log entries",
    )

    # ==========================================================================
    # Reconciliation (from core/config.py lines 501-502)
    # ==========================================================================
    reconciliation_interval_seconds: int = Field(
        default=300,
        ge=60,
        le=3600,
        description="Reconciliation check interval in seconds",
    )
    reconciliation_jitter_percent: int = Field(
        default=20,
        ge=0,
        le=50,
        description="Jitter percentage for reconciliation",
    )
    reconciliation_jitter_min_seconds: float = Field(
        default=0.0,
        ge=0.0,
        le=60.0,
        description="Minimum jitter in seconds",
    )
    reconciliation_jitter_max_seconds: float = Field(
        default=5.0,
        ge=0.0,
        le=60.0,
        description="Maximum jitter in seconds",
    )

    # ==========================================================================
    # Health Check (from core/config.py lines 508-510)
    # ==========================================================================
    health_check_interval_seconds: float = Field(
        default=30.0,
        ge=5.0,
        le=300.0,
        description="Health check interval in seconds",
    )
    health_check_timeout_ms: int = Field(
        default=100,
        ge=50,
        le=5000,
        description="Health check timeout in milliseconds",
    )

    # ==========================================================================
    # Connection Pool (from safe_defaults l2_storage)
    # ==========================================================================
    max_retry_on_failure: TinyCount = Field(
        default=STANDARD_RETRY_COUNT,
        description="Maximum retries on failure",
    )
    connection_pool_size: MediumCount = Field(
        default=STANDARD_POOL_SIZE,
        description="Connection pool size",
    )
    executor_max_workers: int = Field(
        default=16,
        ge=1,
        le=64,
        description=(
            "ThreadPoolExecutor size for LayeredRepository L2 sync (478 D3). "
            "Startup-only — not exposed via L2StorageRuntimeConfig because "
            "ThreadPoolExecutor is fixed-size at construction. Tune up for "
            "PRO-tier burst workloads."
        ),
    )


def get_l2_storage_settings() -> L2StorageSettings:
    """Get cached L2StorageSettings instance."""
    from baldur.settings.root import get_config

    return get_config().services_group.l2_storage


def reset_l2_storage_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["l2_storage"]
    except KeyError:
        pass


def get_l2_storage_settings_safe() -> L2StorageSettings:
    """Get L2 storage settings with environment variable drift detection."""
    from baldur.settings.drift_monitor import get_config_drift_monitor

    monitor = get_config_drift_monitor()
    if monitor.check_and_invalidate("l2_storage", "BALDUR_L2_STORAGE_"):
        reset_l2_storage_settings()
    return get_l2_storage_settings()


# =============================================================================
# L2 Storage Runtime Config (migrated from config.py)
# =============================================================================


class L2StorageRuntimeConfig:
    """
    Runtime-configurable L2 storage settings.

    Allows API-level settings adjustment so operators can change
    timeouts from dashboard/API without server restart.

    Singleton pattern for global configuration management.
    """

    _instance: L2StorageRuntimeConfig | None = None
    _lock = threading.Lock()

    def __new__(cls) -> L2StorageRuntimeConfig:
        """Singleton pattern for global configuration."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._init_defaults()
                    cls._instance = instance
        return cls._instance

    def _init_defaults(self) -> None:
        """Initialize default values from environment or hardcoded defaults."""
        self._runtime_lock = threading.Lock()
        self._runtime_config: dict = {}
        self._last_updated: dict = {}

        # L2StorageSettings (BaseSettings) auto-parses env vars
        try:
            _base = get_l2_storage_settings()
            self._env_defaults = {
                "redis_timeout_ms": _base.redis_timeout_ms,
                "database_timeout_ms": _base.database_timeout_ms,
                "fallback_timeout_ms": _base.fallback_timeout_ms,
                "shadow_log_max_entries": _base.shadow_log_max_entries,
                "reconciliation_jitter_min_seconds": _base.reconciliation_jitter_min_seconds,
                "reconciliation_jitter_max_seconds": _base.reconciliation_jitter_max_seconds,
                "health_check_interval_seconds": _base.health_check_interval_seconds,
                "health_check_timeout_ms": _base.health_check_timeout_ms,
            }
        except Exception:
            self._env_defaults = {}

        # Hardcoded defaults (industry best-practice baseline)
        self._hardcoded_defaults = {
            "redis_timeout_ms": 1000,
            "database_timeout_ms": 200,
            "fallback_timeout_ms": 100,
            "shadow_log_max_entries": 1000,
            "reconciliation_jitter_min_seconds": 0.0,
            "reconciliation_jitter_max_seconds": 5.0,
            "health_check_interval_seconds": 30.0,
            "health_check_timeout_ms": 100,
        }

    def _get_value(self, key: str) -> int | float | bool:
        """Get value with priority: runtime > env > hardcoded."""
        with self._runtime_lock:
            if key in self._runtime_config:
                value = self._runtime_config[key]
                assert isinstance(value, int | float | bool)
                return value
        env_value = self._env_defaults.get(key)
        if env_value is not None:
            return env_value
        hardcoded = self._hardcoded_defaults[key]
        assert isinstance(hardcoded, int | float | bool)
        return hardcoded

    # Field validation rules: (min, max, error_message)
    _FIELD_VALIDATORS: dict = {
        "redis_timeout_ms": (10, 1000, "redis_timeout_ms must be between 10 and 1000"),
        "database_timeout_ms": (
            50,
            5000,
            "database_timeout_ms must be between 50 and 5000",
        ),
        "fallback_timeout_ms": (
            10,
            1000,
            "fallback_timeout_ms must be between 10 and 1000",
        ),
        "shadow_log_max_entries": (
            100,
            10000,
            "shadow_log_max_entries must be between 100 and 10000",
        ),
        "reconciliation_jitter_min_seconds": (
            0.0,
            60.0,
            "reconciliation_jitter_min_seconds must be between 0 and 60",
        ),
        "reconciliation_jitter_max_seconds": (
            0.0,
            60.0,
            "reconciliation_jitter_max_seconds must be between 0 and 60",
        ),
        "health_check_interval_seconds": (
            5.0,
            300.0,
            "health_check_interval_seconds must be between 5 and 300",
        ),
        "health_check_timeout_ms": (
            10,
            1000,
            "health_check_timeout_ms must be between 10 and 1000",
        ),
    }

    def _validate_and_update_field(
        self, key: str, value: int | float | bool | None
    ) -> tuple[bool, int | float | bool | None]:
        """Validate and update a single config field. Returns (updated, value)."""
        if value is None:
            return False, None

        if key in self._FIELD_VALIDATORS:
            min_val, max_val, error_msg = self._FIELD_VALIDATORS[key]
            if not (min_val <= value <= max_val):
                raise ValueError(error_msg)

        self._runtime_config[key] = value
        return True, value

    def update(
        self,
        redis_timeout_ms: int | None = None,
        database_timeout_ms: int | None = None,
        fallback_timeout_ms: int | None = None,
        shadow_log_max_entries: int | None = None,
        reconciliation_jitter_min_seconds: float | None = None,
        reconciliation_jitter_max_seconds: float | None = None,
        health_check_interval_seconds: float | None = None,
        health_check_timeout_ms: int | None = None,
        updated_by: str = "api",
    ) -> dict:
        """
        Update L2 storage configuration at runtime.

        Returns:
            Updated configuration as dict
        """

        field_updates = {
            "redis_timeout_ms": redis_timeout_ms,
            "database_timeout_ms": database_timeout_ms,
            "fallback_timeout_ms": fallback_timeout_ms,
            "shadow_log_max_entries": shadow_log_max_entries,
            "reconciliation_jitter_min_seconds": reconciliation_jitter_min_seconds,
            "reconciliation_jitter_max_seconds": reconciliation_jitter_max_seconds,
            "health_check_interval_seconds": health_check_interval_seconds,
            "health_check_timeout_ms": health_check_timeout_ms,
        }

        updates = {}

        with self._runtime_lock:
            for key, value in field_updates.items():
                updated, validated_value = self._validate_and_update_field(key, value)
                if updated:
                    updates[key] = validated_value

            if updates:
                self._last_updated = {
                    "timestamp": utc_now().isoformat(),
                    "updated_by": updated_by,
                    "changes": updates,
                }

        return self.to_dict()

    def reset(self) -> None:
        """Reset to environment/default values (clear runtime config)."""
        with self._runtime_lock:
            self._runtime_config.clear()
            self._last_updated = {}

    def get_redis_timeout_ms(self) -> int:
        """Get Redis timeout in milliseconds."""
        return int(self._get_value("redis_timeout_ms"))

    def get_database_timeout_ms(self) -> int:
        """Get database timeout in milliseconds."""
        return int(self._get_value("database_timeout_ms"))

    def get_fallback_timeout_ms(self) -> int:
        """Get fallback timeout in milliseconds."""
        return int(self._get_value("fallback_timeout_ms"))

    def get_shadow_log_max_entries(self) -> int:
        """Get shadow log max entries."""
        return int(self._get_value("shadow_log_max_entries"))

    def get_timeout_for_adapter(self, adapter_type: str) -> float:
        """Get timeout for adapter type in seconds."""
        timeouts = {
            "redis": self.get_redis_timeout_ms(),
            "database": self.get_database_timeout_ms(),
            "django": self.get_database_timeout_ms(),
        }
        return (
            timeouts.get(adapter_type.lower(), self.get_fallback_timeout_ms()) / 1000.0
        )

    def to_dict(self) -> dict:
        """Export current configuration as dict."""
        return {
            "redis_timeout_ms": self.get_redis_timeout_ms(),
            "database_timeout_ms": self.get_database_timeout_ms(),
            "fallback_timeout_ms": self.get_fallback_timeout_ms(),
            "shadow_log_max_entries": self.get_shadow_log_max_entries(),
            "reconciliation_jitter_min_seconds": self._get_value(
                "reconciliation_jitter_min_seconds"
            ),
            "reconciliation_jitter_max_seconds": self._get_value(
                "reconciliation_jitter_max_seconds"
            ),
            "health_check_interval_seconds": self._get_value(
                "health_check_interval_seconds"
            ),
            "health_check_timeout_ms": self._get_value("health_check_timeout_ms"),
            "last_updated": self._last_updated,
        }


def get_l2_storage_runtime_config() -> L2StorageRuntimeConfig:
    """
    Get the singleton L2StorageRuntimeConfig instance.

    Use this for runtime-changeable settings via API.

    Returns:
        L2StorageRuntimeConfig singleton
    """
    return L2StorageRuntimeConfig()


def reset_l2_storage_runtime_config() -> None:
    """Reset the singleton L2StorageRuntimeConfig (for testing)."""
    L2StorageRuntimeConfig._instance = None
