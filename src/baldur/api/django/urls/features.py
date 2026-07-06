"""Features summary URL pattern — 530 Wave 6F."""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views.features import FeaturesView

__all__ = ["urlpatterns"]

urlpatterns = [
    path("features/", FeaturesView.as_view(), name="features-summary"),
]
