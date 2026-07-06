"""
Baldur API Middleware Package.

Provides middleware components for security, logging, and performance.

Features:
- HealthBridgeMiddleware: DB-independent health endpoints (Worker Saturation 방지)
- SensitiveEndpointAccessLogger: Logs access to sensitive endpoints
- BaldurMiddleware: Automatic failure detection and DLQ storage
- BaldurRecoveryLogger: Recovery event chain logging
- Fail-Secure Permission Classes
"""

from __future__ import annotations

# ============================================================
# Access Logging
# ============================================================
from baldur.api.django.middleware.access_logging import (
    SENSITIVE_ENDPOINT_PATTERNS,
    AccessLogEntry,
    SensitiveAccessLoggingMiddleware,
    SensitiveEndpointAccessLogger,
)

# ============================================================
# Actor Context
# ============================================================
from baldur.api.django.middleware.actor_context import ActorContextMiddleware

# ============================================================
# Baldur
# ============================================================
from baldur.api.django.middleware.baldur import BaldurMiddleware

# ============================================================
# Drain-Aware (471)
# ============================================================
from baldur.api.django.middleware.drain_aware import DrainAwareMiddleware

# ============================================================
# Health Bridge
# ============================================================
from baldur.api.django.middleware.health_bridge import HealthBridgeMiddleware

# ============================================================
# HTTP Metrics
# ============================================================
from baldur.api.django.middleware.http_metrics import (
    AsyncHttpMetricsMiddleware,
    HttpMetricsMiddleware,
)

# ============================================================
# IP Ban Enforcement
# ============================================================
from baldur.api.django.middleware.ip_ban import IPBanMiddleware

# ============================================================
# Fail-Secure Permissions
# ============================================================
from baldur.api.django.middleware.permissions import (
    FailSecureIsAdminUser,
    FailSecureIsAuthenticated,
)

# ============================================================
# Recovery Logger
# ============================================================
from baldur.api.django.middleware.recovery_logger import (
    BaldurRecoveryLogger,
    get_recovery_logger,
)

# ============================================================
# Request Tracking (471)
# ============================================================
from baldur.api.django.middleware.request_tracking import RequestTrackingMiddleware

# ============================================================
# Public API
# ============================================================
__all__ = [
    # Health Bridge
    "HealthBridgeMiddleware",
    # HTTP Metrics
    "HttpMetricsMiddleware",
    "AsyncHttpMetricsMiddleware",
    # IP Ban Enforcement
    "IPBanMiddleware",
    # Access Logging
    "SensitiveEndpointAccessLogger",
    "SensitiveAccessLoggingMiddleware",
    "AccessLogEntry",
    "SENSITIVE_ENDPOINT_PATTERNS",
    # Baldur
    "BaldurMiddleware",
    "BaldurRecoveryLogger",
    "get_recovery_logger",
    # Actor Context
    "ActorContextMiddleware",
    # Fail-Secure Permissions
    "FailSecureIsAuthenticated",
    "FailSecureIsAdminUser",
    # Drain-Aware (471)
    "DrainAwareMiddleware",
    # Request Tracking (471)
    "RequestTrackingMiddleware",
]
