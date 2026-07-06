"""
Baldur Dashboard Views.

REST API endpoints for monitoring dashboard.

Endpoints:
- GET /api/baldur/dashboard/summary/ - Get system summary statistics

Note: Business logic has been extracted to DashboardService.
See: services/dashboard_service.py
"""

import structlog

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.dashboard import dashboard_summary
from baldur.interfaces.web_framework import PermissionLevel

logger = structlog.get_logger()


class DashboardSummaryView(HandlerAPIView):
    """
    Dashboard Summary API.

    GET /api/baldur/dashboard/summary/

    Returns a comprehensive summary of the baldur system status.

    Note: Read-only endpoint - Viewer role or higher can access.
    """

    permission_level = PermissionLevel.VIEWER
    handler = dashboard_summary
