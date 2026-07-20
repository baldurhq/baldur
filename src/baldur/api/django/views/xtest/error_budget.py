"""
X-Test-Mode Error Budget Views

Error Budget-related test APIs:
- InjectErrorBudgetView: inject Error Budget consumption
"""

import structlog
from django.utils import timezone
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .base import XTestModeMixin

logger = structlog.get_logger()


class InjectErrorBudgetView(XTestModeMixin, APIView):
    """
    API for injecting Error Budget consumption.

    POST /api/baldur/xtest/inject-error-budget/

    Request:
        {
            "error_type": "critical",  // critical, major, minor
            "count": 10
        }
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        error_type = request.data.get("error_type", "critical")
        count = int(request.data.get("count", 10))

        # Cap the injection count
        max_injection = 100
        if count > max_injection:
            return Response(
                {
                    "status": "error",
                    "error": "injection_limit_exceeded",
                    "message": f"Maximum injection count is {max_injection}",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Exceptions are handled by the exception handler (ImportError is
        # handled separately as the module-not-installed case)
        try:
            from baldur_pro.services.error_budget import (
                get_error_budget_service,
            )
        except ImportError:
            return Response(
                {
                    "status": "warning",
                    "message": "Error Budget service not available",
                    "hint": "Error Budget may not be configured in this environment",
                }
            )

        eb_service = get_error_budget_service()

        # Previous state — ``ErrorBudgetStatus`` is the canonical surface and
        # exposes ``budget_remaining_percent`` directly.
        initial_budget = eb_service.get_budget_status().budget_remaining_percent

        # Inject errors — ``record_error`` API is (error_count, error_type,
        # service_name); the previous ``context`` kwarg never existed.
        for _ in range(count):
            eb_service.record_error(
                error_type=error_type,
                service_name="x-test-mode",
            )

        # Current state
        budget_status = eb_service.get_budget_status()
        current_budget = budget_status.budget_remaining_percent

        logger.info(
            "test.mode_error_budget",
            error_type=error_type,
            count=count,
            initial_budget=initial_budget,
            current_budget=current_budget,
            request_user=request.user,
        )

        return Response(
            {
                "status": "success",
                "error_type": error_type,
                "injected_count": count,
                "initial_budget_percent": initial_budget,
                "current_budget_percent": current_budget,
                "budget_consumed": initial_budget - current_budget,
                "budget_status": budget_status,
                "timestamp": timezone.now().isoformat(),
            }
        )


__all__ = [
    "InjectErrorBudgetView",
]
