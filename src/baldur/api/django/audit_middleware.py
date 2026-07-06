"""
AuditMiddleware - centralized audit middleware

Records audit events raised by every middleware and service as a 'single hash chain'.
Just before returning the response, it 'grabs' the events from the RequestAuditBuffer
and forwards them to the ContinuousAuditRecorder.

Core design principles (56_AUDIT_MIDDLEWARE_DESIGN.md):
-------------------------------------------------
1. "The angler (Middleware) must stand last"
   - AuditMiddleware must be last so it can grab all the CB-open, RateLimit-block,
     and DLQ-store events raised upstream.

2. "The event buffer is a 'receipt'"
   - RequestAuditBuffer is like a receipt that records the entire lifecycle of one request.

3. "Unifying the integrity hash chain"
   - Every log passes through the single channel that is the ContinuousAuditRecorder.
   - For enterprise audits, it proves that "not a single log was dropped or tampered with".

CRITICAL: this Middleware must be placed last in the MIDDLEWARE list!

Usage in settings.py:
    MIDDLEWARE = [
        "baldur.api.django.middleware.HealthBridgeMiddleware",  # topmost
        # ... other middlewares ...
        "baldur.api.django.audit_middleware.AuditMiddleware",  # last!
    ]

Pipeline flow:
    Request → [Entrance Middlewares] → [View] → [AuditMiddleware] → Response
                      │                    │              │
                      │                    │              ▼
                      ▼                    ▼      ┌────────────────┐
               events staged into                │ Event buffer:  │
               request.META["X-AUDIT-EVENTS"]     │ collect &      │
                                                  │ batch record   │
                                                  └───────┬────────┘
                                                          │
                                                          ▼
                                            ContinuousAuditRecorder
                                                   + HashChain
                                                   + WAL (optional)

Author: Baldur Team
Version: 1.0.0
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

import structlog

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

    from baldur.audit.continuous_audit import ContinuousAuditRecorder
    from baldur.audit.event_buffer import (
        AuditEvent,
        RequestAuditBuffer,
    )

logger = structlog.get_logger()


# CRITICAL event types that must be sent immediately
# Circuit Breaker state change, emergency mode activation, security violation, Error Budget depletion, etc.
CRITICAL_AUDIT_EVENT_TYPES: set[str] = {
    "circuit_breaker_state_change",
    "emergency_mode_activated",
    "security_violation",
    "error_budget_depleted",
}


class AuditMiddleware:
    """
    Centralized audit middleware.

    Features:
    1. Generate request_id and initialize the buffer at request start
    2. Collect all buffer events before returning the response
    3. Batch-record via ContinuousAuditRecorder (including HashChain)

    Fail-Open policy:
    - An audit recording failure does not interrupt business logic
    - Falls back to stderr output on failure
    - Tracks the failure count as a metric

    Settings (environment variables):
    - AUDIT_MIDDLEWARE_ENABLED: True/False (default: True)
    - AUDIT_CAPTURE_ERROR_RESPONSES: also record 4xx/5xx responses (default: True)
    - AUDIT_MIN_EVENTS_TO_RECORD: minimum event count (default: 1)
    """

    # Excluded paths (no audit needed)
    EXCLUDED_PATHS = [
        "/api/baldur/health/",
        "/health/",
        "/api/baldur/metrics/",
        "/metrics/",
        "/favicon.ico",
        "/static/",
    ]

    # Config-based read-audit paths
    # GET requests to these paths are recorded as DATA_ACCESS events
    # Overridable via BALDUR_AUDIT["read_paths"] in Django settings
    DEFAULT_READ_AUDIT_PATHS: list[str] = [
        "/api/admin/",
        "/api/payments/",
        "/api/users/personal/",
    ]

    def __init__(self, get_response: Callable):
        """Initialize AuditMiddleware."""

        self.get_response = get_response
        self._recorder: ContinuousAuditRecorder | None = None
        self._initialized = False
        self._read_audit_paths: list[str] = []

        # Statistics
        self._total_requests = 0
        self._total_events_recorded = 0
        self._failed_recordings = 0

    def _ensure_initialized(self) -> None:
        """Lazy initialization - runs after Django is fully loaded."""
        if self._initialized:
            return

        try:
            from baldur.adapters.audit.singleton import get_audit_adapter
            from baldur.audit.continuous_audit import ContinuousAuditRecorder

            adapter = get_audit_adapter()

            # Load the Checkpoint Strategy
            checkpoint_strategy = None
            if self._is_checkpoint_enabled():
                try:
                    from baldur.audit.checkpoint import (
                        get_default_checkpoint_strategy,
                    )

                    checkpoint_strategy = get_default_checkpoint_strategy()
                    logger.info("audit_middleware.checkpoint_strategy_loaded")
                except Exception as e:
                    logger.warning(
                        "audit_middleware.checkpoint_strategy_failed",
                        error=e,
                    )

            # WAL + Checkpoint settings
            wal_enabled = self._is_wal_enabled()

            self._recorder = ContinuousAuditRecorder(
                audit_adapter=adapter,
                fail_open=True,
                fallback_to_stdout=True,
                wal_enabled=wal_enabled,
                checkpoint_strategy=checkpoint_strategy,
                checkpoint_namespace=self._get_checkpoint_namespace(),
            )
            logger.info("audit_middleware.initialized_continuousauditrecorder")
        except Exception as e:
            logger.warning(
                "audit_middleware.recorder_init_failed",
                error=e,
            )
            self._recorder = None

        # Load config-based read-audit paths
        self._load_read_audit_config()

        self._initialized = True

    def _is_wal_enabled(self) -> bool:
        """Whether WAL is enabled."""
        import os

        return os.environ.get("AUDIT_WAL_ENABLED", "FALSE").upper() == "TRUE"

    def _is_checkpoint_enabled(self) -> bool:
        """Whether Checkpoint is enabled."""
        import os

        return os.environ.get("AUDIT_CHECKPOINT_ENABLED", "TRUE").upper() == "TRUE"

    def _get_checkpoint_namespace(self) -> str:
        """Checkpoint namespace."""
        import os

        return os.environ.get("AUDIT_CHECKPOINT_NAMESPACE", "audit_middleware")

    def _load_read_audit_config(self) -> None:
        """Load read-audit config from Django settings."""
        try:
            from django.conf import settings

            audit_config = getattr(settings, "BALDUR_AUDIT", {})
            self._read_audit_paths = audit_config.get(
                "read_paths", self.DEFAULT_READ_AUDIT_PATHS
            )

            logger.debug(
                "audit_middleware.read_audit_paths",
                read_audit_paths=self._read_audit_paths,
            )
        except Exception as e:
            logger.debug(
                "audit_middleware.load_audit_config_failed",
                error=e,
            )
            self._read_audit_paths = self.DEFAULT_READ_AUDIT_PATHS

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Process request/response."""
        self._ensure_initialized()
        self._total_requests += 1

        # === Excluded-path check ===
        if self._should_skip(request):
            return cast("HttpResponse", self.get_response(request))

        # === Buffer initialization ===
        buffer = self._init_buffer(request)

        # === Read audit (GET requests to configured paths) ===
        self._capture_read_access(request, buffer)

        # === Request handling ===
        response: HttpResponse = self.get_response(request)

        # === Response meta collection ===
        self._capture_response_meta(request, response, buffer)

        # === Event recording (grab the buffer) ===
        if buffer.has_events():
            # If async mode is enabled, send via AsyncHealingLogger
            if self._is_async_mode_enabled():
                self._flush_events_to_async_logger(buffer, request, response)
            else:
                # Synchronous mode (existing approach)
                self._record_events(buffer, request, response)

        return response

    def _capture_read_access(
        self, request: HttpRequest, buffer: RequestAuditBuffer
    ) -> None:
        """
        Record read (GET) requests to configured paths as DATA_ACCESS.

        Adds a DATA_ACCESS event to the buffer for GET requests matching the
        path patterns configured in BALDUR_AUDIT["read_paths"].
        """
        method = getattr(request, "method", "").upper()
        if method != "GET":
            return

        path = getattr(request, "path", "")
        if not self._should_audit_read(path):
            return

        from baldur.audit.event_buffer import AuditEventType

        buffer.add(
            event_type=AuditEventType.DATA_ACCESS,
            source="AuditMiddleware",
            details={
                "path": path,
                "method": method,
                "query_string": getattr(request, "META", {}).get("QUERY_STRING", ""),
            },
            success=True,
            actor_id=self._get_user_id(request),
        )

    def _should_audit_read(self, path: str) -> bool:
        """Check whether the path is a read-audit target."""
        if not path or not self._read_audit_paths:
            return False

        return any(path.startswith(audit_path) for audit_path in self._read_audit_paths)

    def _should_skip(self, request: HttpRequest) -> bool:
        """Excluded-path check."""
        path = getattr(request, "path", "")
        return any(path.startswith(excluded) for excluded in self.EXCLUDED_PATHS)

    def _init_buffer(self, request: HttpRequest) -> RequestAuditBuffer:
        """Initialize the buffer and generate request_id."""
        from baldur.audit.event_buffer import RequestAuditBuffer

        buffer = RequestAuditBuffer.get_or_create(request)

        # Generate or extract request_id
        buffer.request_id = self._get_or_create_request_id(request)

        # Set request metadata
        buffer.set_request_metadata(
            path=getattr(request, "path", None),
            method=getattr(request, "method", None),
            user_id=self._get_user_id(request),
        )

        return buffer

    def _get_or_create_request_id(self, request: HttpRequest) -> str:
        """Generate or extract the request ID."""
        # Use the X-Request-ID header if present
        request_id = getattr(request, "META", {}).get("HTTP_X_REQUEST_ID")
        if request_id:
            return str(request_id)

        # Generate one if absent
        return str(uuid.uuid4())

    def _get_user_id(self, request: HttpRequest) -> str | None:
        """Extract the user ID."""
        try:
            user = getattr(request, "user", None)
            if user and hasattr(user, "is_authenticated") and user.is_authenticated:
                return str(getattr(user, "id", None) or getattr(user, "pk", None))
        except Exception:
            pass
        return None

    def _capture_response_meta(
        self,
        request: HttpRequest,
        response: HttpResponse,
        buffer: RequestAuditBuffer,
    ) -> None:
        """
        Capture response metadata - add an event on an error response.

        If ExceptionHandler already recorded the exception, ERROR_DETECTED is not added.
        This prevents duplicate audit recording for the same exception.
        """
        from baldur.audit.event_buffer import AuditEventType

        status_code = getattr(response, "status_code", 200)
        elapsed = buffer.get_elapsed_seconds()

        # Add an event for a 4xx/5xx error response
        if status_code >= 400:
            # Skip if ExceptionHandler already recorded the exception (dedup)
            if buffer.has_event_from_source("ExceptionHandler"):
                return

            buffer.add(
                event_type=AuditEventType.ERROR_DETECTED,
                source="AuditMiddleware",
                details={
                    "status_code": status_code,
                    "path": getattr(request, "path", ""),
                    "method": getattr(request, "method", ""),
                    "elapsed_seconds": round(elapsed, 4),
                },
                success=False,
                error_message=f"HTTP {status_code}",
            )

    def _record_events(
        self,
        buffer: RequestAuditBuffer,
        request: HttpRequest,
        response: HttpResponse,
    ) -> None:
        """
        Batch-record events - grab the buffer.

        Fail-Open: no interruption of the main flow on a recording failure.
        """
        if self._recorder is None:
            # Fall back to logging if there is no recorder
            self._fallback_log_events(buffer)
            return

        try:
            # Get the actor context
            actor_id, actor_type = self._get_actor_context()

            # Request context
            request_context = {
                "request_id": buffer.request_id,
                "path": getattr(request, "path", ""),
                "method": getattr(request, "method", ""),
                "status_code": getattr(response, "status_code", 200),
                "actor_id": actor_id,
                "actor_type": actor_type,
                "elapsed_seconds": round(buffer.get_elapsed_seconds(), 4),
                "event_count": buffer.event_count(),
            }

            # Record each event (chained into the hash chain)
            for event in buffer.get_events():
                self._record_single_event(event, request_context)
                self._total_events_recorded += 1

        except Exception as e:
            # An audit failure must not block the main flow (Fail-Open)
            self._failed_recordings += 1
            logger.warning(
                "audit_middleware.recording_failed_fail_open",
                error=e,
                failed_recordings=self._failed_recordings,
            )
            # Attempt fallback
            self._fallback_log_events(buffer)

    def _is_async_mode_enabled(self) -> bool:
        """
        Check whether async audit mode is enabled.

        Controlled by the AUDIT_ASYNC_MODE_ENABLED environment variable (default: True).
        In async mode, events are sent non-blocking via AsyncHealingLogger.
        """
        import os

        return os.environ.get("AUDIT_ASYNC_MODE_ENABLED", "TRUE").upper() == "TRUE"

    def _flush_events_to_async_logger(
        self,
        buffer: RequestAuditBuffer,
        request: HttpRequest,
        response: HttpResponse,
    ) -> None:
        """
        Send events via AsyncHealingLogger (non-blocking).

        Regular events: batch-processed (flushed roughly every 5 seconds)
        CRITICAL events: sent immediately (CB state change, emergency mode, etc.)

        A logging failure does not affect the response under the Fail-Open policy.
        """
        try:
            from baldur.utils.async_logger import AsyncHealingLogger, EventSeverity

            # Get the actor context
            actor_id, actor_type = self._get_actor_context()

            # Request context
            request_context = {
                "request_id": buffer.request_id,
                "path": getattr(request, "path", ""),
                "method": getattr(request, "method", ""),
                "status_code": getattr(response, "status_code", 200),
                "actor_id": actor_id,
                "actor_type": actor_type,
                "elapsed_seconds": round(buffer.get_elapsed_seconds(), 4),
                "event_count": buffer.event_count(),
            }

            for event in buffer.get_events():
                # AuditEvent -> dict conversion
                event_dict = self._convert_event_to_dict(event, request_context)

                # Determine whether it is a CRITICAL event (CB state change, emergency mode, etc.)
                severity = EventSeverity.INFO
                event_type_value = (
                    event.event_type.value
                    if hasattr(event.event_type, "value")
                    else str(event.event_type)
                )
                if event_type_value in CRITICAL_AUDIT_EVENT_TYPES:
                    severity = EventSeverity.CRITICAL

                # Non-blocking send (~0.01ms)
                AsyncHealingLogger.log(event_dict, severity=severity)
                self._total_events_recorded += 1

        except Exception as e:
            # Fail-open: a logging failure does not affect the response
            self._failed_recordings += 1
            logger.warning(
                "audit_middleware.async_logging_failed_fail",
                error=e,
            )
            # Fall back to stderr output
            self._fallback_log_events(buffer)

    def _convert_event_to_dict(
        self,
        event: AuditEvent,
        request_context: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Convert an AuditEvent to a dict (for AsyncHealingLogger sending).

        Converts to a format usable by AuditAdapter.log().
        """
        from baldur.audit.event_buffer import AuditEventType
        from baldur.interfaces.audit_adapter import AuditAction

        # Event type -> AuditAction mapping
        action_map = {
            AuditEventType.DLQ_STORE: AuditAction.DLQ_STORE,
            AuditEventType.DLQ_REPLAY: AuditAction.DLQ_REPLAY_SUCCESS,
            AuditEventType.DLQ_ESCALATE: AuditAction.DLQ_ESCALATE,
            AuditEventType.DLQ_FORCE_REDRIVE: AuditAction.DLQ_FORCE_REDRIVE,
            AuditEventType.CB_STATE_CHANGE: AuditAction.CB_AUTO_OPEN,
            AuditEventType.CB_REJECTION: AuditAction.CB_FORCE_OPEN,
            AuditEventType.CB_RECOVERY: AuditAction.CB_AUTO_CLOSE,
            AuditEventType.GOVERNANCE_BLOCKED: AuditAction.GOVERNANCE_BLOCKED,
            AuditEventType.GOVERNANCE_KILL_SWITCH: AuditAction.GOVERNANCE_KILL_SWITCH,
            AuditEventType.RATE_LIMITED: AuditAction.GOVERNANCE_BLOCKED,
            AuditEventType.POOL_CB_REJECTION: AuditAction.CB_FORCE_OPEN,
            AuditEventType.POOL_CB_STATE_CHANGE: AuditAction.CB_AUTO_OPEN,
            AuditEventType.ERROR_DETECTED: AuditAction.SECURITY_ALERT,
            AuditEventType.CONFIG_CHANGE: AuditAction.CONFIG_CHANGE,
            AuditEventType.MANUAL_OVERRIDE: AuditAction.MANUAL_OVERRIDE,
            AuditEventType.GENERIC: AuditAction.CONFIG_CHANGE,
        }

        action = action_map.get(event.event_type, AuditAction.CONFIG_CHANGE)

        return {
            "action": action.value if hasattr(action, "value") else str(action),
            "event_type": event.event_type.value
            if hasattr(event.event_type, "value")
            else str(event.event_type),
            "source": event.source,
            "target_type": event.target_type or event.source,
            "target_id": event.target_id or request_context.get("request_id", ""),
            "actor_id": event.actor_id or request_context.get("actor_id"),
            "actor_type": event.actor_type,
            "domain": event.domain,
            "reason": event.reason,
            "details": {
                **event.details,
                "request_context": request_context,
            },
            "success": event.success,
            "error_message": event.error_message,
            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
        }

    def _get_actor_context(self) -> tuple[str | None, str]:
        """Get actor info from ActorContext."""
        try:
            from baldur.context.actor_context import ActorContext

            if ActorContext.is_set():
                actor = ActorContext.get_current()
                return actor.actor_id, actor.actor_type
        except ImportError:
            pass
        return None, "system"

    def _record_single_event(
        self,
        event: AuditEvent,
        request_context: dict[str, Any],
    ) -> None:
        """Record a single event - applies HashChain via ContinuousAuditRecorder."""
        try:
            from baldur.audit.event_buffer import AuditEventType
            from baldur.interfaces.audit_adapter import (
                AuditAction,
                AuditEntry,
                ContextType,
            )

            # Event type -> AuditAction mapping
            action_map = {
                AuditEventType.DLQ_STORE: AuditAction.DLQ_STORE,
                AuditEventType.DLQ_REPLAY: AuditAction.DLQ_REPLAY_SUCCESS,
                AuditEventType.DLQ_ESCALATE: AuditAction.DLQ_ESCALATE,
                AuditEventType.DLQ_FORCE_REDRIVE: AuditAction.DLQ_FORCE_REDRIVE,
                AuditEventType.CB_STATE_CHANGE: AuditAction.CB_AUTO_OPEN,
                AuditEventType.CB_REJECTION: AuditAction.CB_FORCE_OPEN,
                AuditEventType.CB_RECOVERY: AuditAction.CB_AUTO_CLOSE,
                AuditEventType.GOVERNANCE_BLOCKED: AuditAction.GOVERNANCE_BLOCKED,
                AuditEventType.GOVERNANCE_KILL_SWITCH: AuditAction.GOVERNANCE_KILL_SWITCH,
                AuditEventType.RATE_LIMITED: AuditAction.GOVERNANCE_BLOCKED,
                AuditEventType.POOL_CB_REJECTION: AuditAction.CB_FORCE_OPEN,
                AuditEventType.POOL_CB_STATE_CHANGE: AuditAction.CB_AUTO_OPEN,
                AuditEventType.ERROR_DETECTED: AuditAction.SECURITY_ALERT,
                AuditEventType.CONFIG_CHANGE: AuditAction.CONFIG_CHANGE,
                AuditEventType.MANUAL_OVERRIDE: AuditAction.MANUAL_OVERRIDE,
                AuditEventType.GENERIC: AuditAction.CONFIG_CHANGE,
            }

            action = action_map.get(event.event_type, AuditAction.CONFIG_CHANGE)

            # Create the AuditEntry
            entry = AuditEntry(
                action=action,
                actor_id=event.actor_id or request_context.get("actor_id"),
                actor_type=event.actor_type,
                context_type=ContextType.REQUEST,  # Middleware context
                target_type=event.target_type or event.source,
                target_id=event.target_id or request_context.get("request_id", ""),
                domain=event.domain,
                reason=event.reason,
                details={
                    **event.details,
                    "request_context": request_context,
                    "original_event_type": event.event_type.value,
                },
                success=event.success,
                error_message=event.error_message,
            )

            # Record via ContinuousAuditRecorder (applies HashChain)
            assert self._recorder is not None  # caller already guarded
            self._recorder.audit_adapter.log(entry)

        except Exception as e:
            logger.debug(
                "audit_middleware.event_record_failed",
                error=e,
            )

    def _fallback_log_events(self, buffer: RequestAuditBuffer) -> None:
        """Fallback: print events to stderr."""
        import sys

        for event in buffer.get_events():
            try:
                print(
                    f"[FALLBACK_AUDIT_LOG] {event.event_type.value}: {event.to_dict()}",
                    file=sys.stderr,
                )
            except Exception:
                pass

    # =========================================================================
    # Statistics & Monitoring
    # =========================================================================

    @classmethod
    def get_stats(cls) -> dict[str, Any]:
        """Return middleware statistics."""
        # Not a singleton, so it cannot be accessed at the class level
        # Query per-instance statistics from the instance
        return {
            "note": "Use instance._total_requests etc. for stats",
        }


# =============================================================================
# Utility Functions
# =============================================================================


def get_audit_middleware_from_settings() -> AuditMiddleware | None:
    """
    Get the AuditMiddleware instance from Django settings.

    Note: Django middleware is instantiated, making direct access difficult.
    This function is for reference only.
    """
    return None  # Django middleware is not directly accessible


def is_audit_middleware_enabled() -> bool:
    """Check whether AuditMiddleware is enabled."""
    import os

    return os.environ.get("AUDIT_MIDDLEWARE_ENABLED", "TRUE").upper() == "TRUE"
