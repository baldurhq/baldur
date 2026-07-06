"""
Backpressure Middleware for Django.

과부하 시 503 응답을 반환하고 커스텀 헤더를 추가합니다.
RateController와 GracefulDegradation을 통합합니다.

헤더 규약:
- X-Baldur-Backpressure-Level: 현재 Backpressure 레벨
- X-Baldur-Degraded-Features: 비활성화된 기능 목록 (콤마 구분)
- Retry-After: 재시도 권장 시간 (초)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import structlog
from django.http import HttpRequest, HttpResponse

from baldur.settings.backpressure import get_backpressure_settings

try:
    from baldur.scaling.graceful_degradation import get_graceful_degradation
    from baldur.scaling.rate_controller import get_rate_controller

    _SCALING_AVAILABLE = True
except ImportError:
    _SCALING_AVAILABLE = False

logger = structlog.get_logger()


class BackpressureMiddleware:
    """
    Backpressure 미들웨어.

    기능:
    - 과부하 시 503 응답 반환
    - 커스텀 메시지 지원 (다국어/브랜딩)
    - 비활성화된 기능 목록을 헤더로 전달
    - Retry-After 헤더 제공

    설정 (환경변수):
        BALDUR_BACKPRESSURE_ENABLED=true
        BALDUR_BACKPRESSURE_REJECT_MESSAGE="..."
        BALDUR_BACKPRESSURE_REJECT_RETRY_AFTER_SECONDS=5

    Usage (settings.py):
        MIDDLEWARE = [
            ...
            'baldur.api.django.middleware.backpressure.BackpressureMiddleware',
            ...
        ]
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        """
        Args:
            get_response: 다음 미들웨어/뷰 호출 함수
        """
        self.get_response = get_response
        if _SCALING_AVAILABLE:
            self._controller = get_rate_controller()
            self._degradation = get_graceful_degradation()

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """
        요청 처리.

        과부하 시 503을 반환하고, 정상 시 응답에 헤더를 추가합니다.
        """
        if not _SCALING_AVAILABLE:
            return cast(HttpResponse, self.get_response(request))

        settings = get_backpressure_settings()
        if not settings.backpressure_enabled:
            return cast(HttpResponse, self.get_response(request))

        # Rate 체크
        if not self._controller.should_process():
            return self._create_overload_response()

        # 정상 처리
        response: HttpResponse = self.get_response(request)

        # 비활성화된 기능 헤더 추가
        disabled_features = self._degradation.get_disabled_features()
        if disabled_features:
            response["X-Baldur-Degraded-Features"] = ",".join(disabled_features)

        # 현재 레벨 헤더 추가
        current_level = self._controller.get_state().level
        response["X-Baldur-Backpressure-Level"] = current_level.value

        return response

    def _create_overload_response(self) -> HttpResponse:
        """과부하 시 503 응답 생성."""
        settings = get_backpressure_settings()
        current_level = self._controller.get_state().level

        logger.warning(
            "backpressure_middleware.request_rejected",
            current_level=current_level.value,
        )

        return HttpResponse(
            content=settings.reject_message,
            status=503,
            content_type="text/plain; charset=utf-8",
            headers={
                "Retry-After": str(settings.reject_retry_after_seconds),
                "X-Baldur-Backpressure-Level": current_level.value,
            },
        )


class AsyncBackpressureMiddleware:
    """
    비동기 Backpressure 미들웨어.

    ASGI 환경에서 사용합니다.

    Usage (settings.py):
        MIDDLEWARE = [
            ...
            'baldur.api.django.middleware.backpressure.AsyncBackpressureMiddleware',
            ...
        ]
    """

    async_capable = True
    sync_capable = False

    def __init__(self, get_response: Callable[[HttpRequest], Any]):
        """
        Args:
            get_response: 다음 미들웨어/뷰 호출 함수 (async)
        """
        self.get_response = get_response
        if _SCALING_AVAILABLE:
            self._controller = get_rate_controller()
            self._degradation = get_graceful_degradation()

    async def __call__(self, request: HttpRequest) -> HttpResponse:
        """요청 처리 (비동기)."""
        if not _SCALING_AVAILABLE:
            return cast(HttpResponse, await self.get_response(request))

        settings = get_backpressure_settings()
        if not settings.backpressure_enabled:
            return cast(HttpResponse, await self.get_response(request))

        # Rate 체크 (동기 호출이지만 빠름)
        if not self._controller.should_process():
            return self._create_overload_response()

        # 정상 처리
        response: HttpResponse = await self.get_response(request)

        # 비활성화된 기능 헤더 추가
        disabled_features = self._degradation.get_disabled_features()
        if disabled_features:
            response["X-Baldur-Degraded-Features"] = ",".join(disabled_features)

        # 현재 레벨 헤더 추가
        current_level = self._controller.get_state().level
        response["X-Baldur-Backpressure-Level"] = current_level.value

        return response

    def _create_overload_response(self) -> HttpResponse:
        """과부하 시 503 응답 생성."""
        settings = get_backpressure_settings()
        current_level = self._controller.get_state().level

        logger.warning(
            "async_backpressure_middleware.request_rejected",
            current_level=current_level.value,
        )

        return HttpResponse(
            content=settings.reject_message,
            status=503,
            content_type="text/plain; charset=utf-8",
            headers={
                "Retry-After": str(settings.reject_retry_after_seconds),
                "X-Baldur-Backpressure-Level": current_level.value,
            },
        )
