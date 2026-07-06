"""
Bulkhead Status API - 격벽 상태 조회 엔드포인트.

격벽 패턴의 현재 상태를 조회하는 REST API를 제공합니다.
모든 격벽 또는 특정 격벽의 상태, 사용률, 거부 통계를 조회할 수 있습니다.

Endpoints:
    GET /api/baldur/bulkhead/status/ - 모든 격벽 상태 조회
    GET /api/baldur/bulkhead/status/?name=database - 특정 격벽 상태 조회

Note:
    예외 처리는 baldur_exception_handler로 위임됩니다.
    settings.py의 REST_FRAMEWORK.EXCEPTION_HANDLER 설정 참조.
"""

from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.bulkhead import bulkhead_status
from baldur.factory.registry import ProviderRegistry
from baldur.interfaces.web_framework import PermissionLevel
from baldur.utils.time import utc_now


class BulkheadStatusView(HandlerAPIView):
    """
    격벽 상태 조회 API.

    모든 격벽의 현재 상태를 조회하거나, 특정 격벽의 상태를 조회합니다.
    각 격벽의 사용률, 활성 요청 수, 거부 통계 등을 반환합니다.

    GET /api/baldur/bulkhead/status/
        - 모든 격벽 상태 조회

    GET /api/baldur/bulkhead/status/?name=database
        - 특정 격벽 상태 조회
    """

    permission_level = PermissionLevel.PUBLIC
    handler = bulkhead_status


class BulkheadDetailView(APIView):
    """
    특정 격벽 상세 조회 API.

    GET /api/baldur/bulkhead/{name}/
        - 특정 격벽의 상세 상태 조회
    """

    permission_classes: list = []  # Public endpoint

    def get(self, request: Request, name: str) -> Response:
        """Get detail for a specific bulkhead."""
        registry = ProviderRegistry.bulkhead_registry.safe_get()
        if registry is None:
            raise NotFound(
                detail={
                    "error": "Bulkhead registry is unavailable (baldur_pro required)"
                }
            )

        try:
            bulkhead = registry.get(name)
        except KeyError as _err:
            raise NotFound(
                detail={
                    "error": f"Bulkhead '{name}' not found",
                    "available_bulkheads": registry.list_names(),
                }
            ) from _err

        state = bulkhead.get_state()

        return Response(
            {
                "name": state.name,
                "type": state.bulkhead_type.value,
                "max_concurrent": state.max_concurrent,
                "active_count": state.active_count,
                "waiting_count": state.waiting_count,
                "rejected_count": state.rejected_count,
                "available_permits": state.available_permits,
                "utilization_percent": round(state.utilization_percent, 2),
                "last_rejection_time": (
                    state.last_rejection_time.isoformat()
                    if state.last_rejection_time
                    else None
                ),
                "timestamp": utc_now().isoformat(),
            }
        )
