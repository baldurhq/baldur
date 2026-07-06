"""
Baldur Exception Handler.

통합 예외 처리 시스템으로, API 예외 응답을 표준화하고 Audit 로그에 기록합니다.

주요 컴포넌트:
    - ErrorCode: 표준 에러 코드 Enum
    - ExceptionClassifier: 예외를 카테고리와 코드로 분류
    - StandardErrorResponse: 표준화된 에러 응답 포맷
    - baldur_exception_handler: DRF 예외 핸들러

사용 방법:
    # settings.py에서 DRF 예외 핸들러 설정
    REST_FRAMEWORK = {
        'EXCEPTION_HANDLER': 'baldur.api.django.exceptions.baldur_exception_handler',
    }

    # 코드에서 직접 표준 응답 생성
    from baldur.api.django.exceptions import (
        ErrorCode,
        StandardErrorResponse,
        create_error_response,
    )

    # 에러 코드로 직접 응답 생성
    response = create_error_response(
        code=ErrorCode.VALIDATION_FIELD_REQUIRED,
        field="amount",
        detail="The 'amount' field is required.",
    )
    return Response(response.to_dict(), status=response.http_status)

    # 예외로부터 응답 생성
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
    # === 에러 코드 ===
    "ErrorCode",
    "ERROR_CODE_TO_HTTP_STATUS",
    "ERROR_CODE_RETRYABLE",
    "ERROR_CODE_DEFAULT_MESSAGES",
    "get_http_status",
    "is_retryable",
    "get_default_message",
    "get_error_info",
    # === 예외 분류기 ===
    "ExceptionCategory",
    "ClassifiedError",
    "ExceptionClassifier",
    "get_exception_classifier",
    # === 표준 응답 ===
    "ErrorInfo",
    "ResponseMeta",
    "StandardErrorResponse",
    "create_error_response",
    # === DRF 핸들러 ===
    "baldur_exception_handler",
]
