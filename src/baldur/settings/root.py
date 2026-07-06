"""
Root Settings - BaldurSettings.

Unified Pydantic Settings replacing core/config.py:BaldurConfig.

All sub-settings are composed here for single-point access.
"""

from dotenv import load_dotenv

load_dotenv(
    override=False
)  # .env -> os.environ (1 time, existing env vars take priority)

import os
from enum import Enum
from functools import cached_property
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import COMMON_SETTINGS_CONFIG

if TYPE_CHECKING:
    from baldur.settings.circuit_breaker import CircuitBreakerSettings
    from baldur.settings.groups import (
        AdaptersGroup,
        AuditGroup,
        CoordinationGroup,
        CoreGroup,
        MetaGroup,
        MetricsGroup,
        MultiRegionGroup,
        ObservabilityGroup,
        ResilienceGroup,
        ScalingGroup,
        SecurityGroup,
        ServicesGroup,
        SLOGroup,
        TestingGroup,
    )
    from baldur.settings.retry import RetrySettings
    from baldur.settings.security import SecuritySettings
    from baldur.settings.sla import SLASettings


class FallbackPolicy(str, Enum):
    """DI fallback policy for in-memory adapter usage.

    Controls how services behave when ProviderRegistry is unavailable.

    - ALLOW: InMemory fallback silently (dev/test)
    - WARN_AND_ALLOW: Fallback with metrics + warning (staging)
    - FAIL_FAST: Crash immediately for K8s pod restart (production)
    """

    ALLOW = "allow"
    WARN_AND_ALLOW = "warn"
    FAIL_FAST = "fail_fast"


_root_logger = structlog.get_logger()


class BaldurSettings(BaseSettings):
    """
    Root configuration for the baldur system.

    Unified Pydantic Settings replacing legacy BaldurConfig dataclass.

    Usage:
        from baldur.settings import get_config
        config = get_config()
        print(config.core.circuit_breaker.failure_threshold)
    """

    model_config = COMMON_SETTINGS_CONFIG

    # ==========================================================================
    # Multi-Cluster Configuration
    # ==========================================================================
    cluster_id: str = Field(
        default="default",
        description="Cluster identifier (REQUIRED for multi-cluster deployments)",
    )

    # ==========================================================================
    # Feature flags
    # ==========================================================================
    debug_mode: bool = Field(
        default=False,
        description="Enable debug mode",
    )
    fallback_policy: FallbackPolicy = Field(
        default=FallbackPolicy.ALLOW,
        description="DI fallback policy: allow (dev), warn (staging), fail_fast (prod)",
    )

    # ==========================================================================
    # Site configuration
    # ==========================================================================
    site_url: str = Field(
        default="http://localhost:8000",
        description=(
            "Base URL for operator-facing links (security-incident admin "
            "links, actionable-alert buttons). Relative alert URLs are "
            "absolutized against it only when it is explicitly configured "
            "(SITE_URL env var or set_config)."
        ),
    )

    # ==========================================================================
    # Domain-specific overrides
    # ==========================================================================
    domain_configs: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Per-domain configuration overrides",
    )

    # ==========================================================================
    # Validators
    # ==========================================================================
    @model_validator(mode="after")
    def warn_default_cluster_id(self) -> "BaldurSettings":
        """
        Warn if using default cluster_id in multi-cluster mode.

        This validator logs a warning when:
        - namespace is enabled (multi-cluster mode)
        - cluster_id is still "default"

        This helps prevent data conflicts in multi-cluster deployments.
        """
        # Only warn if namespace is enabled and cluster_id is default
        if (
            self.multi_region.namespace.namespace_enabled
            and self.cluster_id == "default"
        ):
            # Check if environment variable is set
            env_cluster_id = os.environ.get("BALDUR_CLUSTER_ID")
            if not env_cluster_id or env_cluster_id == "default":
                _root_logger.warning(
                    "⚠️ BALDUR_CLUSTER_ID not set while namespace is enabled. "
                    "Using 'default' - this may cause data conflicts in multi-cluster. "
                    "Set BALDUR_CLUSTER_ID environment variable to your cluster name."
                )
        return self

    @model_validator(mode="after")
    def _run_cross_validation(self) -> "BaldurSettings":
        """
        Run cross-settings conflict checks at startup.

        Delegates to cross_validation.check_all() which logs warnings for
        dangerous setting combinations.
        """
        # Reference: 420
        from baldur.settings.cross_validation import check_all

        check_all(self)
        return self

    # ==========================================================================
    # Domain groups (cached_property, lazy initialization)
    # ==========================================================================
    @cached_property
    def core(self) -> "CoreGroup":
        from baldur.settings.groups import CoreGroup

        return CoreGroup()

    @cached_property
    def scaling(self) -> "ScalingGroup":
        from baldur.settings.groups import ScalingGroup

        return ScalingGroup()

    @cached_property
    def audit_group(self) -> "AuditGroup":
        from baldur.settings.groups import AuditGroup

        return AuditGroup()

    @cached_property
    def coordination(self) -> "CoordinationGroup":
        from baldur.settings.groups import CoordinationGroup

        return CoordinationGroup()

    @cached_property
    def multi_region(self) -> "MultiRegionGroup":
        from baldur.settings.groups import MultiRegionGroup

        return MultiRegionGroup()

    @cached_property
    def metrics_group(self) -> "MetricsGroup":
        from baldur.settings.groups import MetricsGroup

        return MetricsGroup()

    @cached_property
    def resilience(self) -> "ResilienceGroup":
        from baldur.settings.groups import ResilienceGroup

        return ResilienceGroup()

    @cached_property
    def obs(self) -> "ObservabilityGroup":
        from baldur.settings.groups import ObservabilityGroup

        return ObservabilityGroup()

    @cached_property
    def adapters(self) -> "AdaptersGroup":
        from baldur.settings.groups import AdaptersGroup

        return AdaptersGroup()

    @cached_property
    def security_group(self) -> "SecurityGroup":
        from baldur.settings.groups import SecurityGroup

        return SecurityGroup()

    @cached_property
    def slo_group(self) -> "SLOGroup":
        from baldur.settings.groups import SLOGroup

        return SLOGroup()

    @cached_property
    def meta(self) -> "MetaGroup":
        from baldur.settings.groups import MetaGroup

        return MetaGroup()

    @cached_property
    def testing(self) -> "TestingGroup":
        from baldur.settings.groups import TestingGroup

        return TestingGroup()

    @cached_property
    def services_group(self) -> "ServicesGroup":
        from baldur.settings.groups import ServicesGroup

        return ServicesGroup()

    # ==========================================================================
    # Full serialization (model_dump supplement for cached_property groups)
    # ==========================================================================
    def to_full_dict(self) -> dict[str, Any]:
        """model_dump() + cached_property groups for full serialization.

        Used by CLI --inspect, Admin API, etc. to view all settings.
        Only includes already-initialized cached_properties (lazy principle).
        """
        result = self.model_dump()
        for name in self._cached_property_names():
            if name in self.__dict__:
                val = self.__dict__[name]
                result[name] = self._group_to_dict(val)
        return result

    @classmethod
    def _cached_property_names(cls) -> list[str]:
        return [
            name for name, val in vars(cls).items() if isinstance(val, cached_property)
        ]

    @staticmethod
    def _group_to_dict(group: Any) -> dict[str, Any]:
        """Serialize only initialized cached_properties of a group object."""
        result: dict[str, Any] = {}
        for name, val in vars(type(group)).items():
            if isinstance(val, cached_property) and name in group.__dict__:
                prop_val = group.__dict__[name]
                if hasattr(prop_val, "model_dump"):
                    result[name] = prop_val.model_dump()
                else:
                    result[name] = prop_val
        return result

    # ==========================================================================
    # Convenience methods for backward compatibility
    # ==========================================================================
    def get_circuit_breaker_config(
        self, domain: str | None = None
    ) -> "CircuitBreakerSettings":
        """Get circuit breaker config, with optional domain overrides."""
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        if domain and domain in self.domain_configs:
            domain_cb = self.domain_configs[domain].get("circuit_breaker", {})
            if domain_cb:
                return CircuitBreakerSettings(
                    **{
                        **self.core.circuit_breaker.model_dump(),
                        **domain_cb,
                    }
                )
        return self.core.circuit_breaker

    def get_retry_config(self, domain: str | None = None) -> "RetrySettings":
        """Get retry config, with optional domain overrides."""
        from baldur.settings.retry import RetrySettings

        if domain and domain in self.domain_configs:
            domain_retry = self.domain_configs[domain].get("retry", {})
            if domain_retry:
                return RetrySettings(
                    **{
                        **self.core.retry.model_dump(),
                        **domain_retry,
                    }
                )
        return self.core.retry


# =============================================================================
# Singleton pattern
# =============================================================================
_settings: BaldurSettings | None = None


def get_config() -> BaldurSettings:
    """
    Get the global BaldurSettings instance.

    Creates a default instance if none exists.

    Returns:
        BaldurSettings singleton
    """
    global _settings
    if _settings is None:
        _settings = BaldurSettings()
    return _settings


def set_config(config: BaldurSettings | None) -> None:
    """
    Set the global configuration.

    Args:
        config: BaldurSettings instance or None to reset
    """
    global _settings
    _settings = config


def reset_config() -> None:
    """Reset the global configuration (for testing)."""
    global _settings
    _settings = None


def reload_config() -> BaldurSettings:
    """Force reload of configuration."""
    global _settings
    _settings = BaldurSettings()
    return _settings


def configure(**kwargs: Any) -> BaldurSettings:
    """
    Configure the baldur system with the given parameters.

    Args:
        **kwargs: Configuration parameters

    Returns:
        Configured BaldurSettings instance
    """
    global _settings
    _settings = BaldurSettings(**kwargs)
    return _settings


# =============================================================================
# Convenience getters for sub-configurations
# These provide shortcuts to access specific settings without going through get_config()
#
# The per-domain env accessors that collided with RuntimeConfigManager methods of
# the same name (get_circuit_breaker_config / get_dlq_config / get_retry_config /
# get_rate_limit_config / get_notification_config / get_forensic_config) were
# removed; the canonical env accessors are the per-module get_*_settings() getters
# (settings/circuit_breaker.py, settings/dlq.py, ...). The two below stay because
# they never collided with a manager method (_thresholds, not _config).
# =============================================================================


def get_sla_thresholds() -> "SLASettings":
    """Get SLA thresholds configuration."""
    return get_config().slo_group.sla


def get_security_thresholds() -> "SecuritySettings":
    """Get security thresholds configuration."""
    return get_config().security_group.security
