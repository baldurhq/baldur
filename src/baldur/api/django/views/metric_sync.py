"""
Metric Sync API Endpoints.

Manual metric synchronization and drift reporting.

Handlers extracted to api/handlers/metric_sync.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.metric_sync import drift_report, metric_sync
from baldur.interfaces.web_framework import PermissionLevel

__all__ = [
    "MetricSyncView",
    "DriftReportView",
]


class MetricSyncView(HandlerAPIView):
    """Manual metric synchronization endpoint."""

    permission_level = PermissionLevel.ADMIN
    handler = metric_sync


class DriftReportView(HandlerAPIView):
    """Drift status report endpoint."""

    permission_level = PermissionLevel.ADMIN
    handler = drift_report
