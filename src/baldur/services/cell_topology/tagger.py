"""
Cell Tagger — 요청/태스크에 cell_id 태깅.

CellRegistry.get_cell_for_key()를 사용하여
일관된 Cell 할당을 수행합니다.

태깅 키 우선순위:
1. tenant_id (멀티테넌트 환경)
2. user_id (인증된 사용자)
3. session_id (세션 기반)
4. client_ip (최후의 수단)
5. trace_id (Fallback — 분산 해시 기반 균등 분배)
"""

from __future__ import annotations

import time
from typing import Any

import structlog

logger = structlog.get_logger()


class CellTagger:
    """
    Cell Tagger — 요청 컨텍스트에서 cell_id를 결정.

    CellRegistry와 연동하여 Consistent Hash 기반
    Cell 할당을 수행합니다.
    """

    # 태깅 키 우선순위 (높은 순)
    TAG_KEY_PRIORITY = [
        "tenant_id",
        "user_id",
        "session_id",
        "client_ip",
    ]

    def __init__(self):
        self._cell_registry = None

    def _get_registry(self):
        """CellRegistry 지연 로딩."""
        if self._cell_registry is None:
            from baldur.services.cell_topology import get_cell_registry

            self._cell_registry = get_cell_registry()
        return self._cell_registry

    def resolve_cell_id(self, context: dict[str, Any]) -> str:
        """
        컨텍스트에서 cell_id 결정.

        Args:
            context: 태깅 컨텍스트
                - tenant_id: 테넌트 식별자
                - user_id: 사용자 식별자
                - session_id: 세션 식별자
                - client_ip: 클라이언트 IP
                - trace_id: 분산 추적 ID (Fallback용)

        Returns:
            cell_id (예: "cell-3")
        """
        registry = self._get_registry()
        settings = registry._settings

        if not settings.enabled or not settings.tagging_enabled:
            return f"{settings.cell_prefix}-0"

        # 우선순위 순으로 태깅 키 탐색
        for key_name in self.TAG_KEY_PRIORITY:
            value = context.get(key_name)
            if value:
                return registry.get_cell_for_key(f"{key_name}:{value}")

        # ── Fallback: 분산 해시 기반 균등 분배 ──
        # cell-0 고정 할당 대신 Hash Ring을 재사용하여 ACTIVE Cell에 균등 분배.
        # trace_id가 있으면 사용 (동일 요청 재시도 시 동일 Cell 보장),
        # 없으면 monotonic_ns로 분산.
        trace_id = context.get("trace_id")
        fallback_key = f"fallback:{trace_id or time.monotonic_ns()}"
        return registry.get_cell_for_key(fallback_key)

    def resolve_cell_id_from_request(self, request: Any) -> str:
        """
        Django HttpRequest에서 cell_id 결정.

        Args:
            request: Django HttpRequest

        Returns:
            cell_id

        Note:
            request.user, request.session 접근을 위해
            반드시 AuthenticationMiddleware, SessionMiddleware
            이후에 실행되어야 한다.
        """
        context: dict[str, Any] = {}

        # tenant_id (멀티테넌트 미들웨어에서 설정)
        tenant_id = getattr(request, "tenant_id", None)
        if tenant_id:
            context["tenant_id"] = str(tenant_id)

        # user_id (set by the authentication middleware)
        if hasattr(request, "user") and hasattr(request.user, "pk") and request.user.pk:
            context["user_id"] = str(request.user.pk)

        # session_id
        session = getattr(request, "session", None)
        if session and hasattr(session, "session_key") and session.session_key:
            context["session_id"] = session.session_key

        # client_ip
        context["client_ip"] = self._get_client_ip(request)

        # trace_id (Fallback용 — trace_id_middleware가 선행 설정)
        trace_id = getattr(request, "trace_id", None)
        if trace_id:
            context["trace_id"] = trace_id

        return self.resolve_cell_id(context)

    @staticmethod
    def _get_client_ip(request: Any) -> str:
        """클라이언트 IP 추출 (TieringMiddleware._get_client_ip와 동일 패턴)."""
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            return x_forwarded_for.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "unknown")
