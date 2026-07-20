"""
Standard error response builder.

Builds the standardized response format used by every API exception response.

Response structure:
    {
        "success": false,
        "error": {
            "code": "VALIDATION_FIELD_REQUIRED",
            "message": "A required field is missing.",
            "detail": "The 'amount' field is required.",
            "field": "amount",
            "retryable": false
        },
        "meta": {
            "request_id": "abc-123",
            "timestamp": "2024-01-26T12:00:00Z",
            "path": "/api/payments/",
            "method": "POST"
        }
    }
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now

from .classifier import ClassifiedError
from .codes import ErrorCode


def _get_current_region() -> str | None:
    """
    Look up the current region.

    Uses the ClusterIdentity value if available, otherwise reads the environment
    variable directly.

    Returns:
        Region identifier (seoul, tokyo, etc.) or None
    """
    try:
        from baldur.core.cluster_identity import get_cluster_identity

        identity = get_cluster_identity()
        return identity.region
    except ImportError:
        # ClusterIdentity module unavailable - read the environment variable
        return os.environ.get("BALDUR_NAMESPACE_REGION")
    except Exception:
        return os.environ.get("BALDUR_NAMESPACE_REGION")


@dataclass
class ErrorInfo(SerializableMixin):
    """
    Error detail information.

    Corresponds to the "error" field of the response.
    """

    exclude_none = True

    code: str
    """Standard error code string."""

    message: str
    """User-friendly message."""

    detail: str | None = None
    """Technical detail information."""

    field: str | None = None
    """Field name, for field-related errors."""

    retryable: bool = False
    """Whether the operation is retryable."""


@dataclass
class ResponseMeta(SerializableMixin):
    """
    Response metadata.

    Corresponds to the "meta" field of the response.
    In multi-region environments the region field identifies where the error
    occurred.
    """

    exclude_none = True

    request_id: str | None = None
    """Request tracing ID."""

    timestamp: datetime = field(default_factory=lambda: utc_now())
    """Time the error occurred."""

    path: str | None = None
    """Request path."""

    method: str | None = None
    """HTTP method."""

    causation_id: str | None = None
    """Cascade ID for causation tracing (links API and Celery causation)."""

    region: str | None = None
    """Region where the error occurred (BALDUR_NAMESPACE_REGION in multi-region)."""


@dataclass
class StandardErrorResponse:
    """
    Standard error response.

    Every API exception is returned in this format.
    """

    success: bool = False
    """Always False."""

    error: ErrorInfo = field(
        default_factory=lambda: ErrorInfo(
            code=ErrorCode.SYSTEM_INTERNAL_ERROR.value,
            message="An error occurred.",
        )
    )
    """Error detail information."""

    meta: ResponseMeta = field(default_factory=ResponseMeta)
    """Response metadata."""

    http_status: int = 500
    """HTTP status code (used when building the response object)."""

    extra: dict[str, Any] | None = None
    """Extra information (e.g. current_owner of ConfigLockError)."""

    def to_dict(self) -> dict[str, Any]:
        """
        Convert to a dictionary (for JSON serialization).

        http_status and extra are not included in the response body.
        """
        result: dict[str, Any] = {
            "success": self.success,
            "error": self.error.to_dict(),
            "meta": self.meta.to_dict(),
        }

        # Merge extra information into the error object when present
        if self.extra:
            result["error"].update(self.extra)

        return result

    @classmethod
    def from_classified_error(
        cls,
        classified: ClassifiedError,
        request_id: str | None = None,
        path: str | None = None,
        method: str | None = None,
        causation_id: str | None = None,
        region: str | None = None,
    ) -> StandardErrorResponse:
        """
        Build a standard response from a ClassifiedError.

        Args:
            classified: Classified exception information
            request_id: Request tracing ID
            path: Request path
            method: HTTP method
            causation_id: Cascade ID for causation tracing
            region: Region where the error occurred (None uses the
                BALDUR_NAMESPACE_REGION environment variable)

        Returns:
            StandardErrorResponse instance
        """
        # Resolve region automatically (read from the environment variable)
        resolved_region = region
        if resolved_region is None:
            resolved_region = _get_current_region()

        error_info = ErrorInfo(
            code=classified.code.value,
            message=classified.message,
            detail=classified.detail,
            field=classified.field,
            retryable=classified.retryable,
        )

        meta = ResponseMeta(
            request_id=request_id,
            path=path,
            method=method,
            causation_id=causation_id,
            region=resolved_region,
        )

        return cls(
            success=False,
            error=error_info,
            meta=meta,
            http_status=classified.http_status,
            extra=classified.extra,
        )

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        request_id: str | None = None,
        path: str | None = None,
        method: str | None = None,
    ) -> StandardErrorResponse:
        """
        Build a standard response from an exception (classification included).

        Args:
            exc: The raised exception
            request_id: Request tracing ID
            path: Request path
            method: HTTP method

        Returns:
            StandardErrorResponse instance
        """
        from .classifier import get_exception_classifier

        classifier = get_exception_classifier()
        classified = classifier.classify(exc)

        return cls.from_classified_error(
            classified=classified,
            request_id=request_id,
            path=path,
            method=method,
        )


def create_error_response(
    code: ErrorCode,
    message: str | None = None,
    detail: str | None = None,
    field: str | None = None,
    request_id: str | None = None,
    path: str | None = None,
    method: str | None = None,
    extra: dict[str, Any] | None = None,
    region: str | None = None,
) -> StandardErrorResponse:
    """
    Build a standard response from an error code (convenience function).

    Args:
        code: Error code
        message: User-facing message (falls back to the default message)
        detail: Technical detail information
        field: Field name
        request_id: Request ID
        path: Request path
        method: HTTP method
        extra: Extra information
        region: Region where the error occurred (None uses the
            BALDUR_NAMESPACE_REGION environment variable)

    Returns:
        StandardErrorResponse instance
    """
    from .codes import get_default_message, get_http_status, is_retryable

    # Resolve region automatically
    resolved_region = region if region is not None else _get_current_region()

    if message is None:
        message = get_default_message(code)

    error_info = ErrorInfo(
        code=code.value,
        message=message,
        detail=detail,
        field=field,
        retryable=is_retryable(code),
    )

    meta = ResponseMeta(
        request_id=request_id,
        path=path,
        method=method,
        region=resolved_region,
    )

    return StandardErrorResponse(
        success=False,
        error=error_info,
        meta=meta,
        http_status=get_http_status(code),
        extra=extra,
    )


__all__ = [
    "ErrorInfo",
    "ResponseMeta",
    "StandardErrorResponse",
    "create_error_response",
]
