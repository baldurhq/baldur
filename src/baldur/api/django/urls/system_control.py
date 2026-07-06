"""System kill-switch and dry-run mode URL patterns."""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views.system_control import (
    DryRunDisableView,
    DryRunEnableView,
    SystemDisableView,
    SystemEnableView,
    SystemStatusView,
)

urlpatterns = [
    path("system/status/", SystemStatusView.as_view(), name="system-status"),
    path("system/enable/", SystemEnableView.as_view(), name="system-enable"),
    path("system/disable/", SystemDisableView.as_view(), name="system-disable"),
    # Dry Run Mode
    path("system/dry-run/enable/", DryRunEnableView.as_view(), name="dry-run-enable"),
    path(
        "system/dry-run/disable/", DryRunDisableView.as_view(), name="dry-run-disable"
    ),
]
