"""
DRF custom exception handler.

Extends Django REST Framework's exception handling to return standardized error responses.
All exceptions are classified by ExceptionClassifier and responded via StandardErrorResponse.
On an exception, an event is appended to RequestAuditBuffer.

Key features:
    - Standardized error response format
    - Audit buffer integration (integrated with AuditMiddleware)
    - Automatic sensitive-information masking
    - Prometheus metric collection
    - Pool Timeout (SQLAlchemy) detection and 503 return

Configuration (settings.py):
    REST_FRAMEWORK = {
        'EXCEPTION_HANDLER': 'baldur.api.django.exceptions.handler.baldur_exception_handler',
    }
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import structlog

from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from rest_framework.request import Request
    from rest_framework.response import Response

    from baldur.api.django.exceptions.classifier import ClassifiedError
    from baldur.api.django.exceptions.response import StandardErrorResponse
    from baldur.audit.event_buffer import AuditEventType

logger = structlog.get_logger()


# =============================================================================
# Prometheus metrics (optional dependency)
# =============================================================================

_METRICS_INITIALIZED = False
_api_exception_total = None
_api_exception_by_code = None
_api_exception_by_category = None


def _init_metrics():
    """Initialize Prometheus metrics (only when prometheus_client is present)."""
    global \
        _METRICS_INITIALIZED, \
        _api_exception_total, \
        _api_exception_by_code, \
        _api_exception_by_category

    if _METRICS_INITIALIZED:
        return

    try:
        from prometheus_client import REGISTRY, Counter

        # Check whether a metric is already registered
        try:
            _api_exception_total = REGISTRY._names_to_collectors.get(
                "baldur_api_exception_total"
            )
        except (AttributeError, KeyError):
            _api_exception_total = None

        if _api_exception_total is None:
            _api_exception_total = Counter(
                "baldur_api_exception_total",
                "Total number of API exceptions",
                ["path", "method", "status_code"],
            )

        try:
            _api_exception_by_code = REGISTRY._names_to_collectors.get(
                "baldur_api_exception_by_code"
            )
        except (AttributeError, KeyError):
            _api_exception_by_code = None

        if _api_exception_by_code is None:
            _api_exception_by_code = Counter(
                "baldur_api_exception_by_code",
                "API exception count by error code",
                ["error_code"],
            )

        try:
            _api_exception_by_category = REGISTRY._names_to_collectors.get(
                "baldur_api_exception_by_category"
            )
        except (AttributeError, KeyError):
            _api_exception_by_category = None

        if _api_exception_by_category is None:
            _api_exception_by_category = Counter(
                "baldur_api_exception_by_category",
                "API exception count by category",
                ["category"],
            )

        _METRICS_INITIALIZED = True
        logger.debug("exception_handler.prometheus_metrics_initialized")

    except ImportError:
        logger.debug("exception_handler.available_metrics_disabled")
        _METRICS_INITIALIZED = True


def _record_metrics(
    path: str | None,
    method: str | None,
    status_code: int,
    error_code: str,
    category: str,
) -> None:
    """Record Prometheus metrics."""
    _init_metrics()

    try:
        if _api_exception_total is not None:
            _api_exception_total.labels(
                path=path or "unknown",
                method=method or "unknown",
                status_code=str(status_code),
            ).inc()

        if _api_exception_by_code is not None:
            _api_exception_by_code.labels(error_code=error_code).inc()

        if _api_exception_by_category is not None:
            _api_exception_by_category.labels(category=category).inc()

    except Exception as e:
        logger.debug(
            "adaptive_throttle.metrics_failed",
            error=e,
        )


# =============================================================================
# Pool Timeout detection (SQLAlchemy compatible)
# =============================================================================

try:
    from sqlalchemy.exc import TimeoutError as SATimeoutError

    SQLALCHEMY_AVAILABLE = True
except ImportError:
    # Sentinel class: never matches at runtime because no exception inherits it.
    class SATimeoutError(Exception):  # type: ignore[no-redef]
        """Never-matching placeholder when sqlalchemy is unavailable."""

    SQLALCHEMY_AVAILABLE = False


def _is_pool_timeout(exc: Exception) -> bool:
    """Check whether the exception is a SQLAlchemy Pool Timeout."""
    error_str = str(exc).lower()
    error_type = type(exc).__name__

    return (
        (SQLALCHEMY_AVAILABLE and isinstance(exc, SATimeoutError))
        or "queuepool limit" in error_str
        or "connection timed out" in error_str
        or "pool exhausted" in error_str
        or ("timeout" in error_type.lower() and "pool" in error_str)
    )


def baldur_exception_handler(
    exc: Exception,
    context: dict[str, Any],
) -> Response | None:
    """
    DRF custom exception handler.

    Converts all exceptions into a standardized format and records them in the Audit buffer.
    Returns 503 Service Unavailable on Pool Timeout detection.
    If CausationContext is unset, sets it using request_id as the trigger_event_id.

    Args:
        exc: the raised exception
        context: DRF context (view, request, format, args, kwargs)

    Returns:
        A Response object, or None (None re-raises the exception)
    """
    from rest_framework.response import Response
    from rest_framework.views import exception_handler as drf_exception_handler

    from .classifier import ClassifiedError, get_exception_classifier
    from .codes import ErrorCode
    from .response import StandardErrorResponse

    # Extract request information
    request = context.get("request")
    request_id = _extract_request_id(request)
    path = _extract_path(request)
    method = _extract_method(request)

    # Initialize CausationContext (use request_id as trigger if unset)
    causation_id = _init_causation_context(request_id)

    # Handle Pool Timeout first (SQLAlchemy integration)
    if _is_pool_timeout(exc):
        logger.error(
            "exception_handler.pool_timeout_detected",
            adapter_type=type(exc).__name__,
            error=exc,
        )

        # Build the standard response (SERVICE_UNAVAILABLE)
        from .classifier import ClassifiedError, ExceptionCategory

        pool_classified = ClassifiedError(
            category=ExceptionCategory.SERVICE,
            code=ErrorCode.SERVICE_UNAVAILABLE,
            http_status=503,
            message="Service temporarily unavailable.",
            detail="Database connection pool exhausted",
            retryable=True,
            exception_class=type(exc).__name__,
            extra={"retry_after": 10},
        )

        standard_response = StandardErrorResponse.from_classified_error(
            classified=pool_classified,
            request_id=request_id,
            path=path,
            method=method,
            causation_id=causation_id,
        )

        # Record audit and metrics
        _record_audit_event(
            request, exc, pool_classified, standard_response, causation_id
        )
        _record_metrics(
            path, method, 503, ErrorCode.SERVICE_UNAVAILABLE.value, "service"
        )

        response = Response(
            data=standard_response.to_dict(),
            status=503,
        )
        response["Retry-After"] = "10"
        return response

    # Call the DRF default handler first (check whether DRF can handle the exception)
    drf_response = drf_exception_handler(exc, context)

    # Classify the exception
    classifier = get_exception_classifier()
    classified = classifier.classify(exc)

    # Build the standard response (includes causation_id)
    standard_response = StandardErrorResponse.from_classified_error(
        classified=classified,
        request_id=request_id,
        path=path,
        method=method,
        causation_id=causation_id,
    )

    # Append the event to the Audit buffer (includes sensitive-information masking)
    _record_audit_event(request, exc, classified, standard_response, causation_id)

    # Record Prometheus metrics
    _record_metrics(
        path=path,
        method=method,
        status_code=classified.http_status,
        error_code=classified.code.value,
        category=classified.category.value,
    )

    # Logging
    _log_exception(exc, classified, request_id, path, method)

    # Build the DRF Response
    response = Response(
        data=standard_response.to_dict(),
        status=standard_response.http_status,
    )

    # Copy headers from the DRF response (e.g., Throttled's Retry-After)
    if drf_response is not None:
        for header_name, header_value in drf_response.items():
            response[header_name] = header_value

    return response


def _init_causation_context(request_id: str | None) -> str | None:
    """
    Initialize the CausationContext.

    When CausationContext is unset, starts a new Cascade using request_id as the
    trigger_event_id.

    Args:
        request_id: request tracking ID

    Returns:
        cascade_id (when CausationContext is set), or None
    """
    try:
        from baldur.context.causation_context import CausationContext

        if CausationContext.is_set():
            # Already set - return the existing cascade_id
            return CausationContext.get_current_cascade_id()

        # Start a new Cascade if unset (use request_id as trigger)
        # Set directly without a context manager
        import uuid as uuid_module

        from baldur.context.causation_context import (
            CausationInfo,
            _current_causation,
        )

        cascade_id = f"cascade-{uuid_module.uuid4().hex[:12]}"
        trigger_id = request_id or f"evt-{uuid_module.uuid4().hex[:8]}"

        info = CausationInfo(
            cascade_id=cascade_id,
            parent_event_id=trigger_id,
            chain_depth=0,
            namespace="global",
            metadata={
                "source": "exception_handler",
                "request_id": request_id,
                "created_at": utc_now().isoformat(),
            },
        )

        _current_causation.set(info)

        logger.debug(
            "exception_handler.initialized_causationcontext",
            cascade_id=cascade_id,
            trigger_id=trigger_id,
        )

        return cascade_id

    except ImportError:
        return None
    except Exception as e:
        logger.debug(
            "exception_handler.init_causationcontext_failed",
            error=e,
        )
        return None


def _extract_request_id(request: Request | None) -> str | None:
    """Extract or generate request_id from the request."""
    if request is None:
        return str(uuid.uuid4())

    # Find an existing request_id in META
    if hasattr(request, "META"):
        # Common header patterns
        for header in [
            "HTTP_X_REQUEST_ID",
            "HTTP_X_CORRELATION_ID",
            "HTTP_REQUEST_ID",
        ]:
            if request.META.get(header):
                return str(request.META[header])

        # request_id generated by AuditMiddleware
        from baldur.audit.event_buffer import RequestAuditBuffer

        buffer = RequestAuditBuffer.get(request)
        if buffer and buffer.request_id:
            return buffer.request_id

    return str(uuid.uuid4())


def _extract_path(request: Request | None) -> str | None:
    """Extract the path from the request."""
    if request is None:
        return None
    return getattr(request, "path", None)


def _extract_method(request: Request | None) -> str | None:
    """Extract the HTTP method from the request."""
    if request is None:
        return None
    return getattr(request, "method", None)


def _record_audit_event(
    request: Request | None,
    exc: Exception,
    classified: ClassifiedError,
    response: StandardErrorResponse,
    causation_id: str | None = None,
) -> None:
    """
    Append the exception event to the Audit buffer.

    AuditMiddleware collects and records this event when the response is returned.
    Sensitive information is masked at different levels by RBAC role.
    An audit failure does not block the response (fail-open).

    Args:
        request: DRF Request object
        exc: the raised exception
        classified: classified exception info
        response: the standard error response
        causation_id: Cascade ID for causality tracking
    """
    if request is None:
        return

    try:
        from baldur.audit.event_buffer import (
            RequestAuditBuffer,
        )

        buffer = RequestAuditBuffer.get_or_create(request)

        # Determine the event type by exception category
        event_type = _get_audit_event_type(classified)

        # Compose the details
        details: dict[str, Any] = {
            "error_code": classified.code.value,
            "exception_class": classified.exception_class,
            "category": classified.category.value,
            "http_status": classified.http_status,
            "path": response.meta.path,
            "method": response.meta.method,
        }

        # Include cascade_id (causality tracking)
        if causation_id:
            details["cascade_id"] = causation_id

        # Additional metadata
        if classified.field:
            details["field"] = classified.field

        if classified.extra:
            details["extra"] = classified.extra

        # Mask sensitive info in the error message (for Audit - hashed, identity-checkable)
        error_message = _mask_error_message_for_audit(str(exc)[:500])

        buffer.add(
            event_type=event_type,
            source="ExceptionHandler",
            details=details,
            success=False,
            error_message=error_message,
        )

    except Exception as e:
        # An audit failure does not block the response
        logger.debug(
            "exception_handler.record_audit_event_failed",
            error=e,
        )


def _get_audit_event_type(classified: ClassifiedError) -> AuditEventType:
    """
    Return the AuditEventType corresponding to the classified exception.

    Uses API-exception-specific event types to prevent duplicate
    ERROR_DETECTED recording in AuditMiddleware.
    """
    from baldur.audit.event_buffer import AuditEventType

    from .classifier import ExceptionCategory

    # Mapping by category - use API-exception-specific event types
    category_to_event_type = {
        ExceptionCategory.VALIDATION: AuditEventType.API_VALIDATION_ERROR,
        ExceptionCategory.AUTH: AuditEventType.API_AUTH_ERROR,
        ExceptionCategory.AUTHZ: AuditEventType.API_AUTH_ERROR,
        ExceptionCategory.NOT_FOUND: AuditEventType.API_NOT_FOUND,
        ExceptionCategory.CONFLICT: AuditEventType.API_EXCEPTION,
        ExceptionCategory.RATE_LIMIT: AuditEventType.API_THROTTLED,
        ExceptionCategory.INTERNAL: AuditEventType.API_EXCEPTION,
        ExceptionCategory.SERVICE: AuditEventType.API_EXCEPTION,
    }

    return category_to_event_type.get(
        classified.category,
        AuditEventType.API_EXCEPTION,
    )


def _mask_error_message(message: str) -> str:
    """
    Mask the error message for the client response.

    Detects patterns such as passwords, tokens, and API keys and hides them completely.
    Masks at the MaskingLevel.CLIENT level.
    """
    try:
        from baldur.audit.masking import MaskingLevel, mask_with_level

        # Detect sensitive patterns
        sensitive_patterns = [
            "password",
            "token",
            "api_key",
            "apikey",
            "secret",
            "authorization",
            "credential",
        ]

        message_lower = message.lower()
        for pattern in sensitive_patterns:
            if pattern in message_lower:
                # Hide completely if it appears to contain sensitive info
                return mask_with_level(message, MaskingLevel.CLIENT)

        return message

    except ImportError:
        # fallback - default masking on sensitive-pattern detection
        sensitive_patterns = [
            "password",
            "token",
            "api_key",
            "apikey",
            "secret",
            "authorization",
            "credential",
        ]
        message_lower = message.lower()
        if any(pattern in message_lower for pattern in sensitive_patterns):
            return "***REDACTED***"
        return message
    except Exception:
        return message


def _mask_error_message_for_audit(message: str) -> str:
    """
    Mask the error message for Audit recording.

    Protects sensitive info via SHA-256 hashing while still allowing identity checks.
    Identical error messages have identical hashes, so pattern analysis is possible.
    """
    try:
        from baldur.audit.masking import MaskingLevel, mask_with_level

        # Detect sensitive patterns
        sensitive_patterns = [
            "password",
            "token",
            "api_key",
            "apikey",
            "secret",
            "authorization",
            "credential",
        ]

        message_lower = message.lower()
        for pattern in sensitive_patterns:
            if pattern in message_lower:
                # For Audit - hashed (identity-checkable)
                return mask_with_level(message, MaskingLevel.AUDIT)

        return message

    except ImportError:
        # fallback - default masking on sensitive-pattern detection
        sensitive_patterns = [
            "password",
            "token",
            "api_key",
            "apikey",
            "secret",
            "authorization",
            "credential",
        ]
        message_lower = message.lower()
        if any(pattern in message_lower for pattern in sensitive_patterns):
            return "[MASKED_FOR_AUDIT]"
        return message
    except Exception:
        return message


def _log_exception(
    exc: Exception,
    classified: ClassifiedError,
    request_id: str | None,
    path: str | None,
    method: str | None,
) -> None:
    """Log the exception."""
    log_msg = (
        f"[ExceptionHandler] {classified.exception_class}: "
        f"code={classified.code.value}, "
        f"status={classified.http_status}, "
        f"path={path}, method={method}, "
        f"request_id={request_id}"
    )

    # 5xx errors at ERROR level, 4xx at WARNING
    if classified.http_status >= 500:
        logger.error(log_msg, exc_info=exc)
    else:
        logger.warning(log_msg)


__all__ = [
    "baldur_exception_handler",
]
