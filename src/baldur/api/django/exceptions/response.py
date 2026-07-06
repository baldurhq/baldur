"""
표준 에러 응답 생성기.

모든 API 예외 응답에 사용되는 표준화된 응답 포맷을 생성합니다.

응답 구조:
    {
        "success": false,
        "error": {
            "code": "VALIDATION_FIELD_REQUIRED",
            "message": "필수 필드가 누락되었습니다.",
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
    현재 리전 정보 조회.

    ClusterIdentity가 있으면 해당 값 사용, 없으면 환경변수 직접 조회.

    Returns:
        리전 식별자 (seoul, tokyo 등) 또는 None
    """
    try:
        from baldur.core.cluster_identity import get_cluster_identity

        identity = get_cluster_identity()
        return identity.region
    except ImportError:
        # ClusterIdentity 모듈 없음 - 환경변수 직접 조회
        return os.environ.get("BALDUR_NAMESPACE_REGION")
    except Exception:
        return os.environ.get("BALDUR_NAMESPACE_REGION")


@dataclass
class ErrorInfo(SerializableMixin):
    """
    에러 상세 정보.

    응답의 "error" 필드에 해당합니다.
    """

    exclude_none = True

    code: str
    """표준 에러 코드 문자열."""

    message: str
    """사용자 친화적 메시지."""

    detail: str | None = None
    """기술적 상세 정보."""

    field: str | None = None
    """필드 관련 에러 시 필드명."""

    retryable: bool = False
    """재시도 가능 여부."""


@dataclass
class ResponseMeta(SerializableMixin):
    """
    응답 메타데이터.

    응답의 "meta" 필드에 해당합니다.
    멀티 리전 환경에서는 region 필드로 에러 발생 리전을 식별합니다.
    """

    exclude_none = True

    request_id: str | None = None
    """요청 추적 ID."""

    timestamp: datetime = field(default_factory=lambda: utc_now())
    """에러 발생 시간."""

    path: str | None = None
    """요청 경로."""

    method: str | None = None
    """HTTP 메서드."""

    causation_id: str | None = None
    """인과관계 추적용 Cascade ID (API-Celery 인과관계 연결)."""

    region: str | None = None
    """에러 발생 리전 (멀티 리전 환경에서 BALDUR_NAMESPACE_REGION 값)."""


@dataclass
class StandardErrorResponse:
    """
    표준 에러 응답.

    모든 API 예외가 이 형식으로 응답됩니다.
    """

    success: bool = False
    """항상 False."""

    error: ErrorInfo = field(
        default_factory=lambda: ErrorInfo(
            code=ErrorCode.SYSTEM_INTERNAL_ERROR.value,
            message="An error occurred.",
        )
    )
    """에러 상세 정보."""

    meta: ResponseMeta = field(default_factory=ResponseMeta)
    """응답 메타데이터."""

    http_status: int = 500
    """HTTP 상태 코드 (응답 객체 생성 시 사용)."""

    extra: dict[str, Any] | None = None
    """추가 정보 (ConfigLockError의 current_owner 등)."""

    def to_dict(self) -> dict[str, Any]:
        """
        딕셔너리로 변환 (JSON 직렬화용).

        http_status와 extra는 응답 본문에 포함되지 않습니다.
        """
        result: dict[str, Any] = {
            "success": self.success,
            "error": self.error.to_dict(),
            "meta": self.meta.to_dict(),
        }

        # extra 정보가 있으면 error 객체에 병합
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
        ClassifiedError로부터 표준 응답 생성.

        Args:
            classified: 분류된 예외 정보
            request_id: 요청 추적 ID
            path: 요청 경로
            method: HTTP 메서드
            causation_id: 인과관계 추적용 Cascade ID
            region: 에러 발생 리전 (None이면 BALDUR_NAMESPACE_REGION 환경변수 사용)

        Returns:
            StandardErrorResponse 인스턴스
        """
        # region 자동 설정 (환경변수에서 읽기)
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
        예외로부터 표준 응답 생성 (분류 포함).

        Args:
            exc: 발생한 예외
            request_id: 요청 추적 ID
            path: 요청 경로
            method: HTTP 메서드

        Returns:
            StandardErrorResponse 인스턴스
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
    에러 코드로부터 표준 응답 생성 (편의 함수).

    Args:
        code: 에러 코드
        message: 사용자 메시지 (없으면 기본 메시지 사용)
        detail: 기술적 상세 정보
        field: 필드명
        request_id: 요청 ID
        path: 요청 경로
        method: HTTP 메서드
        extra: 추가 정보
        region: 에러 발생 리전 (None이면 BALDUR_NAMESPACE_REGION 환경변수 사용)

    Returns:
        StandardErrorResponse 인스턴스
    """
    from .codes import get_default_message, get_http_status, is_retryable

    # region 자동 설정
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
