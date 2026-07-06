"""API tiering URL patterns (definitions, mappings, overrides, dry-run)."""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views.tiering import (
    TierDefinitionsView,
    TierDryRunView,
    TierExportView,
    TierImportView,
    TierMappingsView,
    TierOverridesView,
    TierResetView,
    TierResolveLookupView,
)

urlpatterns = [
    path("config/tiers/", TierDefinitionsView.as_view(), name="config-tiers"),
    path("config/tiers/reset/", TierResetView.as_view(), name="config-tiers-reset"),
    path(
        "config/tiers/dry-run/", TierDryRunView.as_view(), name="config-tiers-dry-run"
    ),
    path("config/tiers/export/", TierExportView.as_view(), name="config-tiers-export"),
    path("config/tiers/import/", TierImportView.as_view(), name="config-tiers-import"),
    path(
        "config/tiers/resolve/",
        TierResolveLookupView.as_view(),
        name="config-tiers-resolve",
    ),
    path(
        "config/tier-mappings/", TierMappingsView.as_view(), name="config-tier-mappings"
    ),
    path(
        "config/tier-overrides/",
        TierOverridesView.as_view(),
        name="config-tier-overrides",
    ),
]
