"""
Standard error code definitions.

The standardized error code scheme used in API exception responses.
Each error code maps to an HTTP status code and a retryable flag.

Code format: {CATEGORY}_{SUBCATEGORY}_{DETAIL}

Categories:
    - VALIDATION: input validation failure
    - AUTH: authentication failure
    - AUTHZ: authorization (permission) failure
    - RESOURCE: resource-related
    - RATE: request throttling
    - CONFIG: configuration-related
    - SYSTEM: internal system error
    - SERVICE: external service related
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """
    Standard error codes.

    Every API exception response uses one of these codes.
    """

    # =========================================================================
    # VALIDATION: input validation failure (400 Bad Request)
    # =========================================================================
    VALIDATION_FIELD_REQUIRED = "VALIDATION_FIELD_REQUIRED"
    """Required field missing."""

    VALIDATION_FIELD_INVALID = "VALIDATION_FIELD_INVALID"
    """Field value has an invalid format or type."""

    VALIDATION_INVALID_VALUE = "VALIDATION_INVALID_VALUE"
    """Value is outside the allowed range."""

    VALIDATION_SERIALIZER_ERROR = "VALIDATION_SERIALIZER_ERROR"
    """DRF serializer validation failed."""

    VALIDATION_PARSE_ERROR = "VALIDATION_PARSE_ERROR"
    """Failed to parse the request body (JSON, etc.)."""

    # =========================================================================
    # AUTH: authentication failure (401 Unauthorized)
    # =========================================================================
    AUTH_NOT_AUTHENTICATED = "AUTH_NOT_AUTHENTICATED"
    """No authentication credentials provided."""

    AUTH_TOKEN_INVALID = "AUTH_TOKEN_INVALID"
    """Token is not valid."""

    AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"
    """Token has expired."""

    AUTH_CREDENTIALS_INVALID = "AUTH_CREDENTIALS_INVALID"
    """Credentials do not match."""

    # =========================================================================
    # AUTHZ: authorization (permission) failure (403 Forbidden)
    # =========================================================================
    AUTHZ_PERMISSION_DENIED = "AUTHZ_PERMISSION_DENIED"
    """No permission."""

    AUTHZ_GOVERNANCE_BLOCKED = "AUTHZ_GOVERNANCE_BLOCKED"
    """Blocked by a governance policy."""

    AUTHZ_ERROR_BUDGET_BLOCKED = "AUTHZ_ERROR_BUDGET_BLOCKED"
    """Automation blocked because the error budget is exhausted."""

    # =========================================================================
    # RESOURCE: resource-related (404 Not Found, 409 Conflict)
    # =========================================================================
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    """Resource not found."""

    RESOURCE_ALREADY_EXISTS = "RESOURCE_ALREADY_EXISTS"
    """Resource already exists."""

    RESOURCE_CONFLICT = "RESOURCE_CONFLICT"
    """Resource state conflict."""

    # =========================================================================
    # RATE: request throttling (429 Too Many Requests)
    # =========================================================================
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    """Rate limit exceeded."""

    RATE_THROTTLED = "RATE_THROTTLED"
    """Temporarily throttled."""

    # =========================================================================
    # CONFIG: configuration-related (409 Conflict)
    # =========================================================================
    CONFIG_LOCKED = "CONFIG_LOCKED"
    """Configuration locked by another operation (canary rollout, etc.)."""

    CONFIG_INVALID = "CONFIG_INVALID"
    """Configuration value is not valid."""

    # =========================================================================
    # SYSTEM: internal system error (500 Internal Server Error)
    # =========================================================================
    SYSTEM_INTERNAL_ERROR = "SYSTEM_INTERNAL_ERROR"
    """Unexpected internal error."""

    SYSTEM_DATABASE_ERROR = "SYSTEM_DATABASE_ERROR"
    """Database error."""

    SYSTEM_DLQ_ERROR = "SYSTEM_DLQ_ERROR"
    """DLQ store/lookup failure."""

    # =========================================================================
    # SERVICE: external service related (502, 503, 504)
    # =========================================================================
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    """External service unavailable."""

    SERVICE_CIRCUIT_OPEN = "SERVICE_CIRCUIT_OPEN"
    """Circuit breaker is OPEN."""

    SERVICE_TIMEOUT = "SERVICE_TIMEOUT"
    """External service timed out."""

    SERVICE_BAD_GATEWAY = "SERVICE_BAD_GATEWAY"
    """External service returned an invalid response."""


# =============================================================================
# HTTP status code mapping
# =============================================================================

ERROR_CODE_TO_HTTP_STATUS: dict[ErrorCode, int] = {
    # VALIDATION → 400
    ErrorCode.VALIDATION_FIELD_REQUIRED: 400,
    ErrorCode.VALIDATION_FIELD_INVALID: 400,
    ErrorCode.VALIDATION_INVALID_VALUE: 400,
    ErrorCode.VALIDATION_SERIALIZER_ERROR: 400,
    ErrorCode.VALIDATION_PARSE_ERROR: 400,
    # AUTH → 401
    ErrorCode.AUTH_NOT_AUTHENTICATED: 401,
    ErrorCode.AUTH_TOKEN_INVALID: 401,
    ErrorCode.AUTH_TOKEN_EXPIRED: 401,
    ErrorCode.AUTH_CREDENTIALS_INVALID: 401,
    # AUTHZ → 403
    ErrorCode.AUTHZ_PERMISSION_DENIED: 403,
    ErrorCode.AUTHZ_GOVERNANCE_BLOCKED: 403,
    ErrorCode.AUTHZ_ERROR_BUDGET_BLOCKED: 403,
    # RESOURCE → 404, 409
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    ErrorCode.RESOURCE_ALREADY_EXISTS: 409,
    ErrorCode.RESOURCE_CONFLICT: 409,
    # RATE → 429
    ErrorCode.RATE_LIMIT_EXCEEDED: 429,
    ErrorCode.RATE_THROTTLED: 429,
    # CONFIG → 409
    ErrorCode.CONFIG_LOCKED: 409,
    ErrorCode.CONFIG_INVALID: 400,
    # SYSTEM → 500
    ErrorCode.SYSTEM_INTERNAL_ERROR: 500,
    ErrorCode.SYSTEM_DATABASE_ERROR: 500,
    ErrorCode.SYSTEM_DLQ_ERROR: 500,
    # SERVICE → 502, 503, 504
    ErrorCode.SERVICE_UNAVAILABLE: 503,
    ErrorCode.SERVICE_CIRCUIT_OPEN: 503,
    ErrorCode.SERVICE_TIMEOUT: 504,
    ErrorCode.SERVICE_BAD_GATEWAY: 502,
}


# =============================================================================
# Retryability mapping
# =============================================================================

ERROR_CODE_RETRYABLE: dict[ErrorCode, bool] = {
    # VALIDATION → not retryable (input must be corrected)
    ErrorCode.VALIDATION_FIELD_REQUIRED: False,
    ErrorCode.VALIDATION_FIELD_INVALID: False,
    ErrorCode.VALIDATION_INVALID_VALUE: False,
    ErrorCode.VALIDATION_SERIALIZER_ERROR: False,
    ErrorCode.VALIDATION_PARSE_ERROR: False,
    # AUTH → not retryable (re-authentication required)
    ErrorCode.AUTH_NOT_AUTHENTICATED: False,
    ErrorCode.AUTH_TOKEN_INVALID: False,
    ErrorCode.AUTH_TOKEN_EXPIRED: False,
    ErrorCode.AUTH_CREDENTIALS_INVALID: False,
    # AUTHZ → not retryable (no permission)
    ErrorCode.AUTHZ_PERMISSION_DENIED: False,
    ErrorCode.AUTHZ_GOVERNANCE_BLOCKED: False,
    ErrorCode.AUTHZ_ERROR_BUDGET_BLOCKED: False,
    # RESOURCE → not retryable
    ErrorCode.RESOURCE_NOT_FOUND: False,
    ErrorCode.RESOURCE_ALREADY_EXISTS: False,
    ErrorCode.RESOURCE_CONFLICT: False,
    # RATE → retryable (after waiting)
    ErrorCode.RATE_LIMIT_EXCEEDED: True,
    ErrorCode.RATE_THROTTLED: True,
    # CONFIG → conditional (possible once the lock is released)
    ErrorCode.CONFIG_LOCKED: True,
    ErrorCode.CONFIG_INVALID: False,
    # SYSTEM → conditional (may be a transient error)
    ErrorCode.SYSTEM_INTERNAL_ERROR: True,
    ErrorCode.SYSTEM_DATABASE_ERROR: True,
    ErrorCode.SYSTEM_DLQ_ERROR: True,
    # SERVICE → retryable (after the service recovers)
    ErrorCode.SERVICE_UNAVAILABLE: True,
    ErrorCode.SERVICE_CIRCUIT_OPEN: True,
    ErrorCode.SERVICE_TIMEOUT: True,
    ErrorCode.SERVICE_BAD_GATEWAY: True,
}


# =============================================================================
# User-friendly default messages
# =============================================================================

ERROR_CODE_DEFAULT_MESSAGES: dict[ErrorCode, str] = {
    # VALIDATION
    ErrorCode.VALIDATION_FIELD_REQUIRED: "Required field is missing.",
    ErrorCode.VALIDATION_FIELD_INVALID: "Invalid field format.",
    ErrorCode.VALIDATION_INVALID_VALUE: "Value is out of allowed range.",
    ErrorCode.VALIDATION_SERIALIZER_ERROR: "Input validation failed.",
    ErrorCode.VALIDATION_PARSE_ERROR: "Unable to parse request body.",
    # AUTH
    ErrorCode.AUTH_NOT_AUTHENTICATED: "Authentication required.",
    ErrorCode.AUTH_TOKEN_INVALID: "Invalid authentication token.",
    ErrorCode.AUTH_TOKEN_EXPIRED: "Authentication token has expired.",
    ErrorCode.AUTH_CREDENTIALS_INVALID: "Invalid credentials.",
    # AUTHZ
    ErrorCode.AUTHZ_PERMISSION_DENIED: "You do not have permission to perform this action.",
    ErrorCode.AUTHZ_GOVERNANCE_BLOCKED: "Blocked by governance policy.",
    ErrorCode.AUTHZ_ERROR_BUDGET_BLOCKED: "Automation blocked due to error budget exhaustion.",
    # RESOURCE
    ErrorCode.RESOURCE_NOT_FOUND: "Requested resource not found.",
    ErrorCode.RESOURCE_ALREADY_EXISTS: "Resource already exists.",
    ErrorCode.RESOURCE_CONFLICT: "Resource state conflict.",
    # RATE
    ErrorCode.RATE_LIMIT_EXCEEDED: "Rate limit exceeded. Please retry later.",
    ErrorCode.RATE_THROTTLED: "Request temporarily throttled.",
    # CONFIG
    ErrorCode.CONFIG_LOCKED: "Configuration is locked by another operation.",
    ErrorCode.CONFIG_INVALID: "Invalid configuration value.",
    # SYSTEM
    ErrorCode.SYSTEM_INTERNAL_ERROR: "Internal server error.",
    ErrorCode.SYSTEM_DATABASE_ERROR: "Database error.",
    ErrorCode.SYSTEM_DLQ_ERROR: "Error occurred during DLQ processing.",
    # SERVICE
    ErrorCode.SERVICE_UNAVAILABLE: "Service temporarily unavailable.",
    ErrorCode.SERVICE_CIRCUIT_OPEN: "Service temporarily blocked. Please retry later.",
    ErrorCode.SERVICE_TIMEOUT: "Service response timed out.",
    ErrorCode.SERVICE_BAD_GATEWAY: "External service response error.",
}


# =============================================================================
# Utility functions
# =============================================================================


def get_http_status(code: ErrorCode) -> int:
    """Return the HTTP status code for an error code."""
    return ERROR_CODE_TO_HTTP_STATUS.get(code, 500)


def is_retryable(code: ErrorCode) -> bool:
    """Return whether an error code is retryable."""
    return ERROR_CODE_RETRYABLE.get(code, False)


def get_default_message(code: ErrorCode) -> str:
    """Return the default user-facing message for an error code."""
    return ERROR_CODE_DEFAULT_MESSAGES.get(code, "An error occurred.")


def get_error_info(code: ErrorCode) -> tuple[int, bool, str]:
    """
    Return the full information for an error code.

    Returns:
        Tuple of (http_status, retryable, default_message)
    """
    return (
        get_http_status(code),
        is_retryable(code),
        get_default_message(code),
    )


__all__ = [
    "ErrorCode",
    "ERROR_CODE_TO_HTTP_STATUS",
    "ERROR_CODE_RETRYABLE",
    "ERROR_CODE_DEFAULT_MESSAGES",
    "get_http_status",
    "is_retryable",
    "get_default_message",
    "get_error_info",
]
