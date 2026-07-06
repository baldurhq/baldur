"""
예외 분류기.

발생한 예외를 카테고리와 표준 에러 코드로 분류합니다.
DRF, Django, 커스텀 예외 및 일반 Python 예외를 처리합니다.

분류 기준:
    - VALIDATION: ValidationError, ValueError, Serializer 에러
    - AUTH: AuthenticationFailed
    - AUTHZ: PermissionDenied
    - NOT_FOUND: Http404, NotFound
    - CONFLICT: ConfigLockError, IntegrityError
    - RATE_LIMIT: Throttled
    - INTERNAL: Exception (기타)
    - SERVICE: 외부 서비스 에러
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .codes import ErrorCode, get_default_message, is_retryable


class ExceptionCategory(str, Enum):
    """예외 카테고리."""

    VALIDATION = "validation"
    """입력값 검증 실패."""

    AUTH = "auth"
    """인증 실패."""

    AUTHZ = "authz"
    """인가(권한) 실패."""

    NOT_FOUND = "not_found"
    """리소스 없음."""

    CONFLICT = "conflict"
    """리소스 상태 충돌."""

    RATE_LIMIT = "rate_limit"
    """요청 제한."""

    INTERNAL = "internal"
    """시스템 내부 오류."""

    SERVICE = "service"
    """외부 서비스 오류."""


@dataclass
class ClassifiedError:
    """
    분류된 예외 정보.

    예외 분류기가 반환하는 구조화된 에러 정보입니다.
    """

    category: ExceptionCategory
    """예외 카테고리."""

    code: ErrorCode
    """표준 에러 코드."""

    http_status: int
    """HTTP 상태 코드."""

    message: str
    """사용자 친화적 메시지."""

    detail: str | None = None
    """기술적 상세 정보 (str(exception))."""

    field: str | None = None
    """필드 관련 에러 시 필드명."""

    retryable: bool = False
    """재시도 가능 여부."""

    exception_class: str = ""
    """원본 예외 클래스명."""

    extra: dict[str, Any] | None = None
    """추가 메타데이터 (ConfigLockError의 current_owner 등)."""


class ExceptionClassifier:
    """
    예외 분류기.

    다양한 예외 유형을 표준화된 에러 코드와 카테고리로 분류합니다.
    DRF 예외 → Django 예외 → 커스텀 예외 → Python 예외 순으로 검사합니다.

    사용 예시:
        classifier = ExceptionClassifier()
        classified = classifier.classify(exception)
        # classified.code, classified.http_status 등 사용
    """

    def classify(self, exc: BaseException) -> ClassifiedError:
        """
        예외를 분류하여 표준화된 에러 정보 반환.

        Args:
            exc: 분류할 예외

        Returns:
            ClassifiedError 인스턴스
        """
        exception_class = type(exc).__name__

        # 1. DRF 예외 체크
        result = self._classify_drf_exception(exc)
        if result:
            return self._with_exception_class(result, exception_class)

        # 2. Django 예외 체크
        result = self._classify_django_exception(exc)
        if result:
            return self._with_exception_class(result, exception_class)

        # 3. 커스텀 예외 체크 (baldur 패키지)
        result = self._classify_custom_exception(exc)
        if result:
            return self._with_exception_class(result, exception_class)

        # 4. 일반 Python 예외
        result = self._classify_python_exception(exc)
        return self._with_exception_class(result, exception_class)

    def _with_exception_class(
        self,
        result: ClassifiedError,
        exception_class: str,
    ) -> ClassifiedError:
        """예외 클래스명을 결과에 추가."""
        result.exception_class = exception_class
        return result

    def _classify_drf_exception(self, exc: BaseException) -> ClassifiedError | None:
        """DRF 예외 분류."""
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

        # ValidationError 특수 처리
        if isinstance(exc, ValidationError):
            return self._handle_validation_error(exc)

        # 예외 타입별 핸들러 매핑
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

        # 기타 DRF 예외 → 상태 코드 기반 분류
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
        """DRF 예외 타입별 핸들러 시도."""
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
        """ValidationError 상세 처리."""
        detail = getattr(exc, "detail", str(exc))
        field = None
        message = get_default_message(ErrorCode.VALIDATION_SERIALIZER_ERROR)

        # DRF ValidationError는 detail이 dict 또는 list일 수 있음
        if isinstance(detail, dict):
            # 첫 번째 필드 에러 추출
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
        """Django 예외 분류."""
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

        # IntegrityError (unique constraint 등)
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
        """baldur 패키지 커스텀 예외 분류."""
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

        # PaymentRecoveryError (shopping 패키지)
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
        """일반 Python 예외 분류."""

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

        # 기본: 내부 서버 오류
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
        """HTTP 상태 코드 기반 분류 (fallback)."""
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


# 싱글톤 인스턴스
_classifier: ExceptionClassifier | None = None
_classifier_lock = threading.Lock()


def get_exception_classifier() -> ExceptionClassifier:
    """ExceptionClassifier 싱글톤 인스턴스 반환."""
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
