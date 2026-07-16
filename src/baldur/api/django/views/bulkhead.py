"""
Bulkhead Status API - bulkhead state inspection endpoints.

Provides the REST API for inspecting the current state of the bulkhead
pattern: all compartments or a single one, with utilization and rejection
statistics.

Endpoints:
    GET /api/baldur/bulkhead/status/ - all bulkhead states
    GET /api/baldur/bulkhead/status/?name=database - a specific bulkhead's state

Note:
    Exception handling is delegated to baldur_exception_handler.
    See the REST_FRAMEWORK.EXCEPTION_HANDLER setting in settings.py.
"""

from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.bulkhead import bulkhead_status
from baldur.interfaces.web_framework import PermissionLevel
from baldur.utils.time import utc_now


class BulkheadStatusView(HandlerAPIView):
    """
    Bulkhead status API.

    Returns the current state of every bulkhead, or of a single named one:
    utilization, active request count, rejection statistics, etc.

    GET /api/baldur/bulkhead/status/
        - all bulkhead states

    GET /api/baldur/bulkhead/status/?name=database
        - a specific bulkhead's state
    """

    permission_level = PermissionLevel.PUBLIC
    handler = bulkhead_status


class BulkheadDetailView(APIView):
    """
    Single-bulkhead detail API.

    GET /api/baldur/bulkhead/{name}/
        - detailed state of the named bulkhead
    """

    permission_classes: list = []  # Public endpoint

    def get(self, request: Request, name: str) -> Response:
        """Get detail for a specific bulkhead."""
        from baldur.services.bulkhead.registry import get_bulkhead_registry

        registry = get_bulkhead_registry()

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
