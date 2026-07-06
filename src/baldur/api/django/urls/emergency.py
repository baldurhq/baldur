"""Emergency mode URL patterns."""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views.emergency import (
    EmergencyConfigView,
    EmergencyHistoryView,
    EmergencyLevelsView,
    EmergencyReleaseView,
    EmergencyStatusView,
    EmergencyTriggerView,
    GradualRecoveryStartView,
    GradualRecoveryStopView,
)

urlpatterns = [
    path("emergency/status/", EmergencyStatusView.as_view(), name="emergency-status"),
    path(
        "emergency/trigger/", EmergencyTriggerView.as_view(), name="emergency-trigger"
    ),
    path(
        "emergency/release/", EmergencyReleaseView.as_view(), name="emergency-release"
    ),
    path(
        "emergency/gradual-recovery/",
        GradualRecoveryStartView.as_view(),
        name="emergency-gradual-recovery",
    ),
    path(
        "emergency/stop-recovery/",
        GradualRecoveryStopView.as_view(),
        name="emergency-stop-recovery",
    ),
    path(
        "emergency/history/", EmergencyHistoryView.as_view(), name="emergency-history"
    ),
    path("emergency/config/", EmergencyConfigView.as_view(), name="emergency-config"),
    path("emergency/levels/", EmergencyLevelsView.as_view(), name="emergency-levels"),
]
