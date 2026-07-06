"""
Baldur Services - Public API
===================================

Version: 2.0.0 (Breaking Change)
Updated: 2026-01-04

This module exposes only the **core Public API** of the Baldur system.
Enterprise users access every primary feature through this path.

Usage:
    from baldur.services import (
        get_circuit_breaker_service,
        get_replay_service,
        record_sla_breach,
    )

    # Circuit Breaker
    cb = get_circuit_breaker_service("payment_gateway")
    if cb.is_open():
        return handle_fallback()

    # DLQ (PRO — requires baldur-pro)
    from baldur.factory.registry import ProviderRegistry

    dlq = ProviderRegistry.dlq_service.safe_get()
    if dlq is not None:
        dlq.push(failed_operation)

=============================================================================
MIGRATION GUIDE (v1.x → v2.0.0)
=============================================================================

v2.0.0 narrows 62 exports down to 15 core APIs. Removed symbols are reachable
via their canonical import paths below.

Before (v1.x):
    from baldur.services import RetryHandler, RetryConfig
    from baldur.services import IdempotencyService
    from baldur.services import ControlAPIService

After (v2.0.0):
    from baldur.services.retry_handler import RetryHandler, RetryConfig
    from baldur.services.idempotency import IdempotencyService
    from baldur.services.control_api_service import ControlAPIService

Removed symbols and their new import paths:

    # Retry (→ retry_handler.py)
    RetryHandler         → from baldur.services.retry_handler import RetryHandler
    RetryConfig          → from baldur.services.retry_handler import RetryConfig
    RetryResult          → from baldur.services.retry_handler import RetryResult
    RetryAction          → from baldur.services.retry_handler import RetryAction
    MaxRetriesExceededError → from baldur.services.retry_handler import MaxRetriesExceededError

    # Idempotency (→ idempotency/)
    IdempotencyService   → from baldur.services.idempotency import IdempotencyService
    IdempotencyKey       → from baldur.services.idempotency import IdempotencyKey
    IdempotencyDomain    → from baldur.services.idempotency import IdempotencyDomain
    get_idempotency_service → from baldur.services.idempotency import get_idempotency_service

    # Control API (→ control_api_service.py)
    ControlAPIService    → from baldur.services.control_api_service import ControlAPIService
    ControlRequest       → from baldur.services.control_api_service import ControlRequest
    ControlResponse      → from baldur.services.control_api_service import ControlResponse

    # Circuit Breaker detail (→ circuit_breaker/)
    CircuitBreakerConfig → from baldur.services.circuit_breaker import CircuitBreakerConfig
    CircuitBreakerResult → from baldur.services.circuit_breaker import CircuitBreakerResult
    CircuitState         → from baldur.services.circuit_breaker import CircuitState
    should_allow_request → from baldur.services.circuit_breaker import should_allow_request
    force_open_circuit   → from baldur.services.circuit_breaker import force_open_circuit
    force_close_circuit  → from baldur.services.circuit_breaker import force_close_circuit

    # Rate Limit (→ circuit_breaker/)
    RateLimitTracker     → from baldur.services.circuit_breaker import RateLimitTracker
    get_rate_limit_tracker → from baldur.services.circuit_breaker import get_rate_limit_tracker
    record_rate_limit    → from baldur.services.circuit_breaker import record_rate_limit
    should_allow_with_protection → from baldur.services.circuit_breaker import should_allow_with_protection
    get_protection_status → from baldur.services.circuit_breaker import get_protection_status

    # Metrics detail (→ metrics/ subpackage)
    record_dlq_item_created → from baldur.services.metrics.recorders import record_dlq_item_created
    record_retry_attempt → from baldur.services.metrics.recorders import record_retry_attempt
    record_recovery_time → from baldur.services.metrics.recorders import record_recovery_time
    record_circuit_breaker_state_change → from baldur.services.metrics.recorders import record_circuit_breaker_state_change
    record_circuit_breaker_open_duration → from baldur.services.metrics.recorders import record_circuit_breaker_open_duration
    record_replay_attempt → from baldur.services.metrics.recorders import record_replay_attempt
    track_recovery_time  → from baldur.services.metrics.updaters import track_recovery_time
    DEFAULT_DOMAINS      → from baldur.metrics.registry import DEFAULT_DOMAINS

    # Security (→ security_*.py)
    SecurityViolationResult → from baldur.services.security import SecurityViolationResult
    SecurityConfig       → from baldur.services.security import SecurityConfig
    ViolationType        → from baldur.services.security import ViolationType
    Severity             → from baldur.services.security import Severity
    SEVERITY_BY_VIOLATION_TYPE → from baldur.services.security import SEVERITY_BY_VIOLATION_TYPE
    get_security_violation_service → from baldur.services.security import get_security_violation_service
    handle_security_violation → from baldur.services.security import handle_security_violation
    SecurityNotificationResult → from baldur.services.security_notification import SecurityNotificationResult
    ChannelDeliveryResult    → from baldur.services.security_notification import ChannelDeliveryResult
    NotificationConfig   → from baldur.services.security_notification import NotificationConfig
    NotificationChannel  → from baldur.services.security_notification import NotificationChannel
    # Transports moved to PRO (OSS observes; PRO notifies):
    SecurityNotificationService → from baldur_pro.services.security_notification import SecurityNotificationService
    get_security_notification_service → from baldur_pro.services.security_notification import get_security_notification_service

    # DLQ detail (→ dlq/)
    DLQConfig            → from baldur_pro.services.dlq import DLQConfig
    DLQEntryResult       → from baldur_pro.services.dlq import DLQEntryResult

    # Replay detail (→ replay_service.py)
    ReplayService        → from baldur.services.replay_service import ReplayService
    ReplayResult         → from baldur.services.replay_service import ReplayResult
    BatchReplayResult    → from baldur.services.replay_service import BatchReplayResult

=============================================================================

Status: Public
"""

# =============================================================================
# PUBLIC API — 15 core exports only
# =============================================================================

import importlib as _importlib
from typing import TYPE_CHECKING

# --- Configuration ---
from baldur.metrics.registry import DEFAULT_DOMAINS

from ..core.config import get_sla_thresholds

# --- Core Services (ordered by usage frequency) ---
from .circuit_breaker import (  # Canonical package; Convenience functions; Rate limit tracking
    CircuitBreakerConfig,
    CircuitBreakerResult,
    CircuitBreakerService,
    CircuitState,
    RateLimitTracker,
    force_close_circuit,
    force_open_circuit,
    get_circuit_breaker_service,
    get_rate_limit_tracker,
    should_allow_request,
)

# --- Backward Compatibility - Control API ---
from .control_api_service import (
    ControlAPIService,
    ControlRequest,
    ControlResponse,
)

# --- Idempotency ---
from .idempotency import (
    IdempotencyDomain,
    IdempotencyKey,
    IdempotencyService,
    get_idempotency_service,
)

# --- Metrics (PEP 562 lazy) ---
# These names resolve through __getattr__ so importing baldur.services does
# not eagerly load services/metrics/{recorders,updaters}, which depend on
# prometheus_client at module load. Users without the [prometheus] extra hit
# a loud ImportError on first attribute access.
#
# The TYPE_CHECKING block below completes the PEP 562 pattern: it mirrors the
# lazy map so static analyzers (IDE autocomplete + the mkdocstrings/griffe
# reference build) resolve these names WITHOUT triggering the heavy
# prometheus_client load — the same convention baldur/__init__.py applies to
# its lazy framework-extra symbols. The block is a no-op at runtime
# (TYPE_CHECKING is False), so __getattr__ remains the sole runtime resolver.
if TYPE_CHECKING:
    from baldur.services.metrics.recorders import (
        record_sla_breach as record_sla_breach,
    )
    from baldur.services.metrics.updaters import (
        collect_all_metrics as collect_all_metrics,
    )

_LAZY_METRIC_IMPORTS: dict[str, tuple[str, str]] = {
    "record_sla_breach": ("baldur.services.metrics.recorders", "record_sla_breach"),
    "collect_all_metrics": ("baldur.services.metrics.updaters", "collect_all_metrics"),
}


def __getattr__(name: str):
    if name in _LAZY_METRIC_IMPORTS:
        module_path, attr_name = _LAZY_METRIC_IMPORTS[name]
        module = _importlib.import_module(module_path)
        value = getattr(module, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


from .replay_service import (
    BatchReplayResult,
    ReplayResult,
    ReplayService,
    get_replay_service,
)

# --- Retry ---
from .retry_handler import (
    MaxRetriesExceededError,
    RetryAction,
    RetryConfig,
    RetryPolicy,
    RetryPolicyConfig,
    RetryResult,
)

# --- Backward Compatibility - Security ---
from .security import (
    SEVERITY_BY_VIOLATION_TYPE,
    SecurityConfig,
    SecurityViolationResult,
    SecurityViolationService,
    Severity,
    ViolationType,
    get_security_violation_service,
    handle_security_violation,
)

# --- Security Notification (OSS value surface only; transports are PRO — OSS observes, PRO notifies) ---
from .security_notification import (
    ChannelDeliveryResult,
    NotificationChannel,
    NotificationConfig,
    SecurityNotificationResult,
)

# =============================================================================
# __all__ — optimized for IDE IntelliSense
# =============================================================================

__all__ = [
    # === Core Service Getters (most frequently used) ===
    "get_circuit_breaker_service",
    "get_replay_service",
    "get_sla_thresholds",
    # === Core Service Classes ===
    "CircuitBreakerService",
    "ReplayService",
    "BatchReplayResult",
    # === Backward Compatibility - Circuit Breaker ===
    "CircuitBreakerConfig",
    "CircuitBreakerResult",
    "CircuitState",
    "should_allow_request",
    "force_open_circuit",
    "force_close_circuit",
    "RateLimitTracker",
    "get_rate_limit_tracker",
    # === Backward Compatibility - Replay ===
    "ReplayResult",
    # === Backward Compatibility - Idempotency ===
    "IdempotencyKey",
    "IdempotencyService",
    "IdempotencyDomain",
    "get_idempotency_service",
    # === Retry ===
    "RetryPolicy",
    "RetryPolicyConfig",
    "RetryConfig",
    "RetryResult",
    "RetryAction",
    "MaxRetriesExceededError",
    # === Backward Compatibility - Control API ===
    "ControlAPIService",
    "ControlRequest",
    "ControlResponse",
    # === Backward Compatibility - Security ===
    "SecurityViolationService",
    "SecurityViolationResult",
    "SecurityConfig",
    "ViolationType",
    "Severity",
    "SEVERITY_BY_VIOLATION_TYPE",
    "get_security_violation_service",
    "handle_security_violation",
    # === Security Notification (OSS value surface; transports are PRO) ===
    "SecurityNotificationResult",
    "NotificationConfig",
    "NotificationChannel",
    "ChannelDeliveryResult",
    # === Metrics ===
    "record_sla_breach",
    "collect_all_metrics",
]
