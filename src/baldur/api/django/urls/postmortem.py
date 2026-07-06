"""Post-mortem URL patterns (incident analysis + revisions + sealing)."""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views.postmortem import (
    GetHealingIncidentsView as PostmortemIncidentsView,
)
from baldur.api.django.views.postmortem import (
    PostmortemDetailView,
)
from baldur.api.django.views.postmortem import (
    PostmortemGeneratorView as PostmortemGenerateView,
)
from baldur.api.django.views.postmortem_revision import (
    PostmortemRevisionCompareView,
    PostmortemRevisionDetailView,
    PostmortemRevisionListView,
    PostmortemSealView,
)

urlpatterns = [
    # Post-mortem report generation (auth required, X-Test header NOT required)
    path(
        "postmortem/generate/",
        PostmortemGenerateView.as_view(),
        name="postmortem-generate",
    ),
    # Post-mortem incidents list
    path(
        "postmortem/incidents/",
        PostmortemIncidentsView.as_view(),
        name="postmortem-incidents",
    ),
    # Post-mortem single-incident detail
    path(
        "postmortem/incidents/<str:incident_id>/",
        PostmortemDetailView.as_view(),
        name="postmortem-incident-detail",
    ),
    # Post-mortem revision list / create
    path(
        "postmortem/<str:incident_id>/revisions/",
        PostmortemRevisionListView.as_view(),
        name="postmortem-revision-list",
    ),
    # Post-mortem revision compare
    path(
        "postmortem/<str:incident_id>/revisions/compare/",
        PostmortemRevisionCompareView.as_view(),
        name="postmortem-revision-compare",
    ),
    # Post-mortem specific revision detail
    path(
        "postmortem/<str:incident_id>/revisions/<int:revision_number>/",
        PostmortemRevisionDetailView.as_view(),
        name="postmortem-revision-detail",
    ),
    # Post-mortem seal / unseal
    path(
        "postmortem/<str:incident_id>/seal/",
        PostmortemSealView.as_view(),
        name="postmortem-seal",
    ),
]
