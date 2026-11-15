"""Features summary view — 530 Wave 6F.

Admin-only DRF view that wraps the framework-agnostic ``features_summary``
handler. Decorated with ``@extend_schema`` per 530 D6 exception so the
response shape (D9) is fully discoverable through the OpenAPI surface
without bleeding into the OOS #530-3 campaign across the other ~50 views.

When drf-spectacular is not installed the import falls back to an
identity decorator so the view module remains importable in OSS installs
that don't add ``baldur[openapi]``.
"""

from __future__ import annotations

from baldur.api.django.base import HandlerAPIView
from baldur.api.django.serializers.features import FeaturesResponseSerializer
from baldur.api.handlers.features import features_summary
from baldur.interfaces.web_framework import PermissionLevel

try:
    from drf_spectacular.utils import extend_schema
except ImportError:  # drf-spectacular is an optional extras dep

    def extend_schema(**_kwargs):  # type: ignore[no-redef,misc]
        def _identity(view):
            return view

        return _identity


__all__ = ["FeaturesView"]


@extend_schema(
    responses=FeaturesResponseSerializer,
    description=(
        "Consolidated feature inventory: every Pydantic *enabled* field "
        "from V1_LAUNCH_MANIFEST.yaml, joined with the active entitlement "
        "to report per-feature license status. Admin-only — leaks the "
        "complete set of operator-toggleable surface names."
    ),
    summary="Feature inventory + entitlement overlay",
)
class FeaturesView(HandlerAPIView):
    """GET /api/baldur/features/ — admin-only inventory + license overlay."""

    permission_level = PermissionLevel.ADMIN
    handler = features_summary
