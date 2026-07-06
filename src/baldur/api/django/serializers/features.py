"""DRF serializers for the /features/ inventory endpoint — 530 Wave 6F.

Per 530 D6 exception, the new ``/features/`` view is fully annotated
with ``@extend_schema(responses=FeaturesResponseSerializer)`` so AI
tooling has a worked example of the response shape. The other
~50 ``HandlerAPIView`` subclasses stay on the paths-only baseline per
OOS #530-3.

Field ``help_text`` and enum ``ChoiceField`` are surfaced through to the
OpenAPI 3.0 schema verbatim.
"""

from __future__ import annotations

from rest_framework import serializers

from baldur.core.entitlement import EntitlementStatus
from baldur.services.feature_manifest import LicenseStatus

__all__ = [
    "EntitlementBlockSerializer",
    "FeatureEntrySerializer",
    "FeaturesResponseSerializer",
]


# Tier values mirror baldur/_data/V1_LAUNCH_MANIFEST.yaml's `tier` column.
_TIER_CHOICES = (
    ("Core", "OSS infrastructure / always-on resilience primitives."),
    ("v1.0", "PRO v1.0 launch-set feature (Revision 5)."),
    (
        "Deferred",
        "PRO feature held out of the v1.0 launch set (default False; promoted individually post-v1.0).",
    ),
    ("Dormant", "AI-tuning / Compliance-Security cluster; opt-in only."),
)

_ENTITLEMENT_STATUS_CHOICES = tuple(
    (status.value, status.name.title()) for status in EntitlementStatus
)

_LICENSE_STATUS_CHOICES = tuple(
    (status.value, status.name.replace("_", " ").title()) for status in LicenseStatus
)


class EntitlementBlockSerializer(serializers.Serializer):
    """Top-level entitlement summary in the /features/ response."""

    status = serializers.ChoiceField(
        choices=_ENTITLEMENT_STATUS_CHOICES,
        help_text=(
            "Entitlement validation result. 'active' indicates a verified "
            "PRO license; 'missing' indicates no token is configured "
            "(default for OSS installs); 'invalid' indicates a parse or "
            "signature failure."
        ),
    )
    customer_id = serializers.CharField(
        required=False,
        help_text="License customer id (present when claims were parsed).",
    )
    org = serializers.CharField(
        required=False,
        help_text="License organization name (present when claims were parsed).",
    )
    expires = serializers.CharField(
        required=False,
        help_text="License expiry date in YYYY-MM-DD form.",
    )
    days_until_expiry = serializers.IntegerField(
        required=False,
        help_text="Days until expiry (negative when past due).",
    )


class FeatureEntrySerializer(serializers.Serializer):
    """One row of the feature inventory, per V1_LAUNCH_MANIFEST.yaml."""

    module = serializers.CharField(
        help_text="Settings module filename, e.g. 'circuit_breaker.py'.",
    )
    field_class = serializers.CharField(
        source="class",
        help_text="Pydantic class owning the field (BaseSettings or nested BaseModel).",
    )
    field = serializers.CharField(
        help_text="Boolean field name on the owning Pydantic class.",
    )
    tier = serializers.ChoiceField(
        choices=_TIER_CHOICES,
        help_text="Launch tier per baldur/_data/V1_LAUNCH_MANIFEST.yaml.",
    )
    default = serializers.BooleanField(
        help_text="Pydantic Field(default=...) at v1.0 launch.",
    )
    enabled = serializers.BooleanField(
        help_text=(
            "Currently-resolved value. Equals 'default' unless the env var "
            "is set (then the canonical accessor is invoked and the live "
            "Pydantic value is read)."
        ),
    )
    env_var = serializers.CharField(
        help_text="Operator-facing environment variable name.",
    )
    license_status = serializers.ChoiceField(
        choices=_LICENSE_STATUS_CHOICES,
        help_text=(
            "License overlay per 530 D9: Core entries are always 'active'; "
            "v1.0 entries report 'requires_pro' without an active license; "
            "Deferred reports 'deferred'; Dormant reports 'dormant'."
        ),
    )

    def to_representation(self, instance: dict) -> dict:
        """Translate the wire 'class' key — DRF reserves the name."""
        if "class" in instance and "field_class" not in instance:
            instance = {**instance, "field_class": instance["class"]}
        data: dict = super().to_representation(instance)
        if "field_class" in data:
            data["class"] = data.pop("field_class")
        return data


class FeaturesResponseSerializer(serializers.Serializer):
    """Response shape for ``GET /api/baldur/features/``."""

    entitlement = EntitlementBlockSerializer(
        help_text="Top-level entitlement status + claims-derived fields.",
    )
    features = FeatureEntrySerializer(
        many=True,
        help_text="One entry per row in V1_LAUNCH_MANIFEST.yaml.",
    )
