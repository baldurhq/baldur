"""
Exception classifier.

Classifies raised exceptions into categories and standard error codes.
Handles DRF, Django, custom, and plain Python exceptions.

Classification criteria:
    - VALIDATION: ValidationError, ValueError, Serializer errors
    - AUTH: AuthenticationFailed
    - AUTHZ: PermissionDenied
    - NOT_FOUND: Http404, NotFound
    - CONFLICT: ConfigLockError, IntegrityError
    - RATE_LIMIT: Throttled
    - INTERNAL: Exception (other)
    - SERVICE: external service errors
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .codes import ErrorCode, get_default_message, is_retryable


class ExceptionCategory(str, Enum):
    """Exception category."""

    VALIDATION = "validation"
    """Input validation failure."""

    AUTH = "auth"
    """Authentication failure."""

    AUTHZ = "authz"
    """Authorization (permission) failure."""

    NOT_FOUND = "not_found"
    """Resource not found."""

    CONFLICT = "conflict"
    """Resource state conflict."""

    RATE_LIMIT = "rate_limit"
    """Request throttled."""

    INTERNAL = "internal"
    """Internal system error."""

    SERVICE = "service"
    """External service error."""


@dataclass
class ClassifiedError:
    """
    Classified exception information.

    The structured error information returned by the exception classifier.
    """

    category: ExceptionCategory
    """Exception category."""

    code: ErrorCode
    """Standard error code."""

    http_status: int
    """HTTP status code."""

    message: str
    """User-friendly message."""

    detail: str | None = None
    """Technical detail information (str(exception))."""

    field: str | None = None
    """Field name, for field-related errors."""

    retryable: bool = False
    """Whether the operation is retryable."""

    exception_class: str = ""
    """Original exception class name."""

    extra: dict[str, Any] | None = None
    """Extra metadata (e.g. current_owner of ConfigLockError)."""


class ExceptionClassifier:
    """
    Exception classifier.

    Classifies various exception types into standardized error codes and
    categories.
    Checks in the order DRF → Django → custom → plain Python exceptions.

    Example:
        classifier = ExceptionClassifier()
        classified = classifier.classify(exception)
        # use classified.code, classified.http_status, etc.
    """

    def classify(self, exc: BaseException) -> ClassifiedError:
        """
        Classify an exception and return standardized error information.

        Args:
            exc: The exception to classify

        Returns:
            ClassifiedError instance
        """
        exception_class = type(exc).__name__

        # 1. Check DRF exceptions
        result = self._classify_drf_exception(exc)
        if result:
            return self._with_exception_class(result, exception_class)

        # 2. Check Django exceptions
        result = self._classify_django_exception(exc)
        if result:
            return self._with_exception_class(result, exception_class)

        # 3. Check custom exceptions (baldur package)
        result = self._classify_custom_exception(exc)
        if result:
            return self._with_exception_class(result, exception_class)

        # 4. Plain Python exceptions
        result = self._classify_python_exception(exc)
        return self._with_exception_class(result, exception_class)

    def _with_exception_class(
        self,
        result: ClassifiedError,
        exception_class: str,
    ) -> ClassifiedError:
        """Add the exception class name to the result."""
        result.exception_class = exception_class
        return result

    def _classify_drf_exception(self, exc: BaseException) -> ClassifiedError | None:
        """Classify DRF exceptions."""
        try:
            from rest_framework.exceptions import (
                APIException,
                AuthenticationFailed,
                MethodNotAllowed,  # noqa: F401
                NotAcceptable,  # noqa: F401
                NotAuthenticated,
                NotFound,
                ParseError,
                PermissionDenied,
                Throttled,
                UnsupportedMediaType,  # noqa: F401
                ValidationError,
            )
        except ImportError:
            return None

        if not isinstance(exc, APIException):
            return None

        # Special handling for ValidationError
        if isinstance(exc, ValidationError):
            return self._handle_validation_error(exc)

        # Per-exception-type handler mapping
        handler_result = self._try_drf_exception_handlers(
            exc,
            ParseError,
            NotAuthenticated,
            AuthenticationFailed,
            PermissionDenied,
            NotFound,
            Throttled,
        )
        if handler_result:
            return handler_result

        # Other DRF exceptions → classify by status code
        status_code = getattr(exc, "status_code", 500)
        return self._classify_by_status_code(exc, status_code)

    def _try_drf_exception_handlers(
        self,
        exc: BaseException,
        ParseError,
        NotAuthenticated,
        AuthenticationFailed,
        PermissionDenied,
        NotFound,
        Throttled,
    ) -> ClassifiedError | None:
        """Try the per-type DRF exception handlers."""
        detail = str(exc.detail) if hasattr(exc, "detail") else str(exc)

        # ParseError
        if isinstance(exc, ParseError):
            return ClassifiedError(
                category=ExceptionCategory.VALIDATION,
                code=ErrorCode.VALIDATION_PARSE_ERROR,
                http_status=400,
                message=get_default_message(ErrorCode.VALIDATION_PARSE_ERROR),
                detail=detail,
                retryable=False,
            )

        # Authentication
        if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
            code = (
                ErrorCode.AUTH_CREDENTIALS_INVALID
                if isinstance(exc, AuthenticationFailed)
                else ErrorCode.AUTH_NOT_AUTHENTICATED
            )
            return ClassifiedError(
                category=ExceptionCategory.AUTH,
                code=code,
                http_status=401,
                message=get_default_message(code),
                detail=detail,
                retryable=False,
            )

        # Permission
        if isinstance(exc, PermissionDenied):
            return ClassifiedError(
                category=ExceptionCategory.AUTHZ,
                code=ErrorCode.AUTHZ_PERMISSION_DENIED,
                http_status=403,
                message=get_default_message(ErrorCode.AUTHZ_PERMISSION_DENIED),
                detail=detail,
                retryable=False,
            )

        # NotFound
        if isinstance(exc, NotFound):
            return ClassifiedError(
                category=ExceptionCategory.NOT_FOUND,
                code=ErrorCode.RESOURCE_NOT_FOUND,
                http_status=404,
                message=get_default_message(ErrorCode.RESOURCE_NOT_FOUND),
                detail=detail,
                retryable=False,
            )

        # Throttled
        if isinstance(exc, Throttled):
            return ClassifiedError(
                category=ExceptionCategory.RATE_LIMIT,
                code=ErrorCode.RATE_THROTTLED,
                http_status=429,
                message=get_default_message(ErrorCode.RATE_THROTTLED),
                detail=detail,
                retryable=True,
                extra={"wait": getattr(exc, "wait", None)},
            )

        return None

    def _handle_validation_error(self, exc: BaseException) -> ClassifiedError:
        """Detailed handling for ValidationError."""
        detail = getattr(exc, "detail", str(exc))
        field = None
        message = get_default_message(ErrorCode.VALIDATION_SERIALIZER_ERROR)

        # A DRF ValidationError detail may be a dict or a list
        if isinstance(detail, dict):
            # Extract the first field error
            for field_name, errors in detail.items():
                field = field_name
                if isinstance(errors, list) and errors:
                    message = str(errors[0])
                elif errors:
                    message = str(errors)
                break
            detail = str(detail)
        elif isinstance(detail, list):
            message = str(detail[0]) if detail else message
            detail = str(detail)
        else:
            detail = str(detail)

        return ClassifiedError(
            category=ExceptionCategory.VALIDATION,
            code=ErrorCode.VALIDATION_SERIALIZER_ERROR,
            http_status=400,
            message=message,
            detail=detail,
            field=field,
            retryable=False,
        )

    def _classify_django_exception(self, exc: BaseException) -> ClassifiedError | None:
        """Classify Django exceptions."""
        try:
            from django.core.exceptions import (
                PermissionDenied as DjangoPermissionDenied,
            )
            from django.core.exceptions import ValidationError as DjangoValidationError
            from django.db import DatabaseError, IntegrityError
            from django.http import Http404
        except ImportError:
            return None

        # Http404
        if isinstance(exc, Http404):
            return ClassifiedError(
                category=ExceptionCategory.NOT_FOUND,
                code=ErrorCode.RESOURCE_NOT_FOUND,
                http_status=404,
                message=get_default_message(ErrorCode.RESOURCE_NOT_FOUND),
                detail=str(exc),
                retryable=False,
            )

        # Django PermissionDenied
        if isinstance(exc, DjangoPermissionDenied):
            return ClassifiedError(
                category=ExceptionCategory.AUTHZ,
                code=ErrorCode.AUTHZ_PERMISSION_DENIED,
                http_status=403,
                message=get_default_message(ErrorCode.AUTHZ_PERMISSION_DENIED),
                detail=str(exc),
                retryable=False,
            )

        # Django ValidationError
        if isinstance(exc, DjangoValidationError):
            messages = getattr(exc, "messages", [str(exc)])
            message = messages[0] if messages else str(exc)
            return ClassifiedError(
                category=ExceptionCategory.VALIDATION,
                code=ErrorCode.VALIDATION_INVALID_VALUE,
                http_status=400,
                message=message,
                detail=str(messages),
                retryable=False,
            )

        # IntegrityError (unique constraint, etc.)
        if isinstance(exc, IntegrityError):
            return ClassifiedError(
                category=ExceptionCategory.CONFLICT,
                code=ErrorCode.RESOURCE_CONFLICT,
                http_status=409,
                message=get_default_message(ErrorCode.RESOURCE_CONFLICT),
                detail=str(exc),
                retryable=False,
            )

        # DatabaseError
        if isinstance(exc, DatabaseError):
            return ClassifiedError(
                category=ExceptionCategory.INTERNAL,
                code=ErrorCode.SYSTEM_DATABASE_ERROR,
                http_status=500,
                message=get_default_message(ErrorCode.SYSTEM_DATABASE_ERROR),
                detail=str(exc),
                retryable=True,
            )

        return None

    def _classify_custom_exception(self, exc: BaseException) -> ClassifiedError | None:
        """Classify baldur package custom exceptions."""
        exception_class = type(exc).__name__

        # ConfigLockError
        if exception_class == "ConfigLockError":
            current_owner = getattr(exc, "current_owner", None)
            config_type = getattr(exc, "config_type", "")
            return ClassifiedError(
                category=ExceptionCategory.CONFLICT,
                code=ErrorCode.CONFIG_LOCKED,
                http_status=409,
                message=get_default_message(ErrorCode.CONFIG_LOCKED),
                detail=str(exc),
                retryable=True,
                extra={
                    "current_owner": current_owner,
                    "config_type": config_type,
                },
            )

        # AutomationBlockedError
        if exception_class == "AutomationBlockedError":
            error_budget_percent = getattr(exc, "error_budget_percent", None)
            threshold_percent = getattr(exc, "threshold_percent", None)
            return ClassifiedError(
                category=ExceptionCategory.AUTHZ,
                code=ErrorCode.AUTHZ_ERROR_BUDGET_BLOCKED,
                http_status=403,
                message=get_default_message(ErrorCode.AUTHZ_ERROR_BUDGET_BLOCKED),
                detail=str(exc),
                retryable=False,
                extra={
                    "error_budget_percent": error_budget_percent,
                    "threshold_percent": threshold_percent,
                },
            )

        # CircuitBreakerOpenError
        if exception_class == "CircuitBreakerOpenError":
            service_name = getattr(exc, "service_name", None)
            return ClassifiedError(
                category=ExceptionCategory.SERVICE,
                code=ErrorCode.SERVICE_CIRCUIT_OPEN,
                http_status=503,
                message=get_default_message(ErrorCode.SERVICE_CIRCUIT_OPEN),
                detail=str(exc),
                retryable=True,
                extra={"service_name": service_name},
            )

        # PaymentRecoveryError (shopping package)
        if exception_class == "PaymentRecoveryError":
            code_attr = getattr(exc, "code", "RECOVERY_ERROR")
            recoverable = getattr(exc, "recoverable", True)
            return ClassifiedError(
                category=ExceptionCategory.SERVICE,
                code=ErrorCode.SERVICE_UNAVAILABLE,
                http_status=503,
                message=str(exc),
                detail=str(exc),
                retryable=recoverable,
                extra={"error_code": code_attr},
            )

        return None

    def _classify_python_exception(self, exc: BaseException) -> ClassifiedError:
        """Classify plain Python exceptions."""

        # ValueError
        if isinstance(exc, ValueError):
            return ClassifiedError(
                category=ExceptionCategory.VALIDATION,
                code=ErrorCode.VALIDATION_INVALID_VALUE,
                http_status=400,
                message=get_default_message(ErrorCode.VALIDATION_INVALID_VALUE),
                detail=str(exc),
                retryable=False,
            )

        # TypeError
        if isinstance(exc, TypeError):
            return ClassifiedError(
                category=ExceptionCategory.VALIDATION,
                code=ErrorCode.VALIDATION_FIELD_INVALID,
                http_status=400,
                message=get_default_message(ErrorCode.VALIDATION_FIELD_INVALID),
                detail=str(exc),
                retryable=False,
            )

        # KeyError
        if isinstance(exc, KeyError):
            return ClassifiedError(
                category=ExceptionCategory.VALIDATION,
                code=ErrorCode.VALIDATION_FIELD_REQUIRED,
                http_status=400,
                message=get_default_message(ErrorCode.VALIDATION_FIELD_REQUIRED),
                detail=f"Missing key: {exc}",
                field=str(exc).strip("'\""),
                retryable=False,
            )

        # TimeoutError
        if isinstance(exc, TimeoutError):
            return ClassifiedError(
                category=ExceptionCategory.SERVICE,
                code=ErrorCode.SERVICE_TIMEOUT,
                http_status=504,
                message=get_default_message(ErrorCode.SERVICE_TIMEOUT),
                detail=str(exc),
                retryable=True,
            )

        # ConnectionError
        if isinstance(exc, ConnectionError):
            return ClassifiedError(
                category=ExceptionCategory.SERVICE,
                code=ErrorCode.SERVICE_UNAVAILABLE,
                http_status=503,
                message=get_default_message(ErrorCode.SERVICE_UNAVAILABLE),
                detail=str(exc),
                retryable=True,
            )

        # Default: internal server error
        return ClassifiedError(
            category=ExceptionCategory.INTERNAL,
            code=ErrorCode.SYSTEM_INTERNAL_ERROR,
            http_status=500,
            message=get_default_message(ErrorCode.SYSTEM_INTERNAL_ERROR),
            detail=str(exc),
            retryable=True,
        )

    def _classify_by_status_code(  # noqa: C901, PLR0912
        self,
        exc: BaseException,
        status_code: int,
    ) -> ClassifiedError:
        """Classify by HTTP status code (fallback)."""
        detail = str(exc)

        if 400 <= status_code < 500:
            if status_code == 400:
                code = ErrorCode.VALIDATION_INVALID_VALUE
                category = ExceptionCategory.VALIDATION
            elif status_code == 401:
                code = ErrorCode.AUTH_NOT_AUTHENTICATED
                category = ExceptionCategory.AUTH
            elif status_code == 403:
                code = ErrorCode.AUTHZ_PERMISSION_DENIED
                category = ExceptionCategory.AUTHZ
            elif status_code == 404:
                code = ErrorCode.RESOURCE_NOT_FOUND
                category = ExceptionCategory.NOT_FOUND
            elif status_code == 409:
                code = ErrorCode.RESOURCE_CONFLICT
                category = ExceptionCategory.CONFLICT
            elif status_code == 429:
                code = ErrorCode.RATE_THROTTLED
                category = ExceptionCategory.RATE_LIMIT
            else:
                code = ErrorCode.VALIDATION_INVALID_VALUE
                category = ExceptionCategory.VALIDATION
        else:
            if status_code == 502:
                code = ErrorCode.SERVICE_BAD_GATEWAY
                category = ExceptionCategory.SERVICE
            elif status_code == 503:
                code = ErrorCode.SERVICE_UNAVAILABLE
                category = ExceptionCategory.SERVICE
            elif status_code == 504:
                code = ErrorCode.SERVICE_TIMEOUT
                category = ExceptionCategory.SERVICE
            else:
                code = ErrorCode.SYSTEM_INTERNAL_ERROR
                category = ExceptionCategory.INTERNAL

        return ClassifiedError(
            category=category,
            code=code,
            http_status=status_code,
            message=get_default_message(code),
            detail=detail,
            retryable=is_retryable(code),
        )


# Singleton instance
_classifier: ExceptionClassifier | None = None
_classifier_lock = threading.Lock()


def get_exception_classifier() -> ExceptionClassifier:
    """Return the ExceptionClassifier singleton instance."""
    global _classifier
    if _classifier is None:
        with _classifier_lock:
            if _classifier is None:
                _classifier = ExceptionClassifier()
    return _classifier


__all__ = [
    "ExceptionCategory",
    "ClassifiedError",
    "ExceptionClassifier",
    "get_exception_classifier",
]
