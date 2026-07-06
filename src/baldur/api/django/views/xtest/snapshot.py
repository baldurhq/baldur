"""
X-Test-Mode Snapshot Views

시스템 스냅샷 관련 테스트 API:
- SystemSnapshotView: 시스템 스냅샷 조회
"""

import structlog
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .base import XTestModeMixin, collect_system_snapshot

logger = structlog.get_logger()


class SystemSnapshotView(XTestModeMixin, APIView):
    """
    시스템 스냅샷 조회 API.

    GET /api/baldur/xtest/snapshot/
    """

    def get(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        snapshot = collect_system_snapshot()

        # CB 상태 추가
        try:
            from baldur.services.circuit_breaker import (
                get_circuit_breaker_service,
            )

            cb_service = get_circuit_breaker_service()
            all_states = cb_service.repository.get_all_states()

            snapshot["circuit_breakers"] = {
                state.service_name: {
                    "state": state.state,
                    "failure_count": state.failure_count,
                }
                for state in all_states
            }
        except Exception as e:
            snapshot["circuit_breakers"] = {"error": str(e)}

        # Error Budget 상태 추가
        try:
            from baldur_pro.services.error_budget import (
                get_error_budget_service,
            )

            eb_service = get_error_budget_service()
            budget_status = eb_service.get_budget_status()

            snapshot["error_budget"] = {
                "remaining_percent": budget_status.budget_remaining_percent,
                "status": budget_status,
            }
        except Exception as e:
            snapshot["error_budget"] = {"error": str(e)}

        return Response({"status": "success", "snapshot": snapshot})


__all__ = [
    "SystemSnapshotView",
]
