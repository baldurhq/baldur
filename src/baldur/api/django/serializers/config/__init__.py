"""
Configuration Serializers Package.

Provides backward-compatible re-exports of all config serializers.

Serializers for validating and serializing runtime configuration updates.
Includes apply strategy support (immediate, delayed, graceful).

Adds Fail-Safe Default hardening.
"""

# Advanced configs
from .advanced_configs import (
    ForensicConfigSerializer,
    LoggingConfigSerializer,
    MetricsConfigSerializer,
)

# Base
from .base import ApplyStrategyMixin

# Core configs
from .core_configs import (
    CircuitBreakerConfigSerializer,
    DLQConfigSerializer,
    IdempotencyConfigSerializer,
    RateLimitConfigSerializer,
    RetryConfigSerializer,
)

# Pending configs
from .pending_configs import (
    CancelPendingChangeSerializer,
    PendingConfigChangeSerializer,
)

# Security configs
from .security_configs import (
    NotificationConfigSerializer,
    SecurityConfigSerializer,
)

# SLO configs
from .slo_configs import (
    ErrorBudgetConfigSerializer,
    SLAConfigSerializer,
    SLOConfigSerializer,
    SLODefinitionSerializer,
)

# Storage configs
from .storage_configs import (
    L2StorageConfigSerializer,
    L2StorageStatusSerializer,
    ReplayAutomationConfigSerializer,
    ShadowLogEntrySerializer,
    ShadowLogStatsSerializer,
)

__all__ = [
    # Base
    "ApplyStrategyMixin",
    # Core configs
    "CircuitBreakerConfigSerializer",
    "DLQConfigSerializer",
    "RetryConfigSerializer",
    "RateLimitConfigSerializer",
    "IdempotencyConfigSerializer",
    # SLO configs
    "SLAConfigSerializer",
    "SLODefinitionSerializer",
    "SLOConfigSerializer",
    "ErrorBudgetConfigSerializer",
    # Security configs
    "SecurityConfigSerializer",
    "NotificationConfigSerializer",
    # Advanced configs
    "ForensicConfigSerializer",
    "MetricsConfigSerializer",
    "LoggingConfigSerializer",
    # Pending configs
    "PendingConfigChangeSerializer",
    "CancelPendingChangeSerializer",
    # Storage configs
    "L2StorageConfigSerializer",
    "L2StorageStatusSerializer",
    "ShadowLogEntrySerializer",
    "ShadowLogStatsSerializer",
    "ReplayAutomationConfigSerializer",
]
