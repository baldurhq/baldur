"""
Baldur Exception Handler.

A unified exception handling system that standardizes API error responses and
records them in the audit log.

Key components:
    - ErrorCode: standard error code Enum
    - ExceptionClassifier: classifies exceptions into a category and a code
    - StandardErrorResponse: standardized error response format
    - baldur_exception_handler: DRF exception handler

Usage:
    # Configure the DRF exception handler in settings.py
    REST_FRAMEWORK = {
        'EXCEPTION_HANDLER': 'baldur.api.django.exceptions.baldur_exception_handler',
    }

    # Build a standard response directly in code
    from baldur.api.django.exceptions import (
        ErrorCode,
        StandardErrorResponse,
        create_error_response,
    )

    # Build a response straight from an error code
    response = create_error_response(
        code=ErrorCode.VALIDATION_FIELD_REQUIRED,
        field="amount",
        detail="The 'amount' field is required.",
    )
    return Response(response.to_dict(), status=response.http_status)

    # Build a response from an exception
    try:
        ...
    except Exception as e:
        response = StandardErrorResponse.from_exception(e, request_id="abc-123")
        return Response(response.to_dict(), status=response.http_status)
"""

from .classifier import (
    ClassifiedError,
    ExceptionCategory,
    ExceptionClassifier,
    get_exception_classifier,
)
from .codes import (
    ERROR_CODE_DEFAULT_MESSAGES,
    ERROR_CODE_RETRYABLE,
    ERROR_CODE_TO_HTTP_STATUS,
    ErrorCode,
    get_default_message,
    get_error_info,
    get_http_status,
    is_retryable,
)
from .handler import (
    baldur_exception_handler,
)
from .response import (
    ErrorInfo,
    ResponseMeta,
    StandardErrorResponse,
    create_error_response,
)

__all__ = [
    # === Error codes ===
    "ErrorCode",
    "ERROR_CODE_TO_HTTP_STATUS",
    "ERROR_CODE_RETRYABLE",
    "ERROR_CODE_DEFAULT_MESSAGES",
    "get_http_status",
    "is_retryable",
    "get_default_message",
    "get_error_info",
    # === Exception classifier ===
    "ExceptionCategory",
    "ClassifiedError",
    "ExceptionClassifier",
    "get_exception_classifier",
    # === Standard response ===
    "ErrorInfo",
    "ResponseMeta",
    "StandardErrorResponse",
    "create_error_response",
    # === DRF handler ===
    "baldur_exception_handler",
]
