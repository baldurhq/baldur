"""Configuration versioning + rollback URL patterns."""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views.config_history import (
    ConfigCompareView,
    ConfigHistoryView,
    ConfigRollbackView,
    ConfigVersionDetailView,
)

urlpatterns = [
    path(
        "config/<str:config_type>/history/",
        ConfigHistoryView.as_view(),
        name="config-history",
    ),
    path(
        "config/<str:config_type>/history/<int:version>/",
        ConfigVersionDetailView.as_view(),
        name="config-version-detail",
    ),
    path(
        "config/<str:config_type>/rollback/",
        ConfigRollbackView.as_view(),
        name="config-rollback",
    ),
    path(
        "config/<str:config_type>/compare/",
        ConfigCompareView.as_view(),
        name="config-compare",
    ),
]
