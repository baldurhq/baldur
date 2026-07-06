"""
Actor Context Middleware.

모든 HTTP 요청에서 "누가" 이 작업을 수행하는지 자동 추적.

이 미들웨어를 사용하면:
1. 모든 AuditEntry에 자동으로 actor_id, actor_type이 채워짐
2. Admin 페이지에서 설정 변경 시 누가 변경했는지 기록
3. API 호출 시 어느 사용자가 호출했는지 추적
4. IP 주소, 세션 ID 등 보안 감사 정보도 자동 수집

Usage in settings.py:
    MIDDLEWARE = [
        ...
        'baldur.api.django.middleware.actor_context.ActorContextMiddleware',
        ...
    ]

비활성화:
    BALDUR_ACTOR_MIDDLEWARE_ENABLED = False (settings.py)
    또는
    BALDUR_ACTOR_MIDDLEWARE_ENABLED=false (환경변수)

설정 후 어디서든:
    from baldur.context import ActorContext

    actor = ActorContext.get_current()
    print(f"Current user: {actor.actor_id}")  # admin@example.com
    print(f"IP: {actor.ip_address}")  # 192.168.1.1
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = structlog.get_logger()


class ActorContextMiddleware:
    """
    Django Middleware for automatic actor context tracking.

    Extracts user information from request and makes it available
    throughout the request lifecycle for audit logging.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response
        self._enabled = self._check_enabled()

        status = "enabled" if self._enabled else "DISABLED"
        logger.info(
            "actor_context_middleware.initialized",
            status=status,
        )

    def _check_enabled(self) -> bool:
        """미들웨어 활성화 여부 확인."""
        try:
            from django.conf import settings

            return getattr(settings, "BALDUR_ACTOR_MIDDLEWARE_ENABLED", True)
        except Exception:
            # settings 접근 불가 시 환경변수 확인
            return os.getenv("BALDUR_ACTOR_MIDDLEWARE_ENABLED", "true").lower() in (
                "true",
                "1",
                "yes",
            )

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # 미들웨어 비활성화 시 바이패스
        if not self._enabled:
            return self.get_response(request)

        from baldur.context.actor_context import ActorContext

        # Use context manager to set actor for this request
        # Fail-Open: Actor 설정 실패 시 요청은 계속 처리 (500 방지)
        try:
            with ActorContext.set_actor_from_django_request(request):
                response = self.get_response(request)
        except Exception as e:
            logger.warning(
                "actor_context_middleware.actor_context_setup_failed",
                error=e,
            )
            response = self.get_response(request)

        return response
