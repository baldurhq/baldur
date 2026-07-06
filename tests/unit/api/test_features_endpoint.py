"""/features/ endpoint unit tests — 530 Wave 6F.

Three layers:
1. ``features_summary`` framework-agnostic handler — composes manifest ×
   resolver × entitlement validator into the 530 D9 response shape.
2. ``FeatureEntrySerializer`` — DRF ``class``→``field_class`` reserved-key
   round-trip.
3. ``FeaturesView`` — admin-only auth (PermissionLevel.ADMIN per 530 D2).

Techniques applied:
- Side effects (no settings instantiation when env unset)
- Dependency interaction (load_feature_manifest + get_entitlement_status
  mocked at the call site)
- State transition (entitlement active / missing / invalid → license_status)
- Serialization roundtrip (class ↔ field_class)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("django")

import django

if not django.apps.apps.ready:
    django.setup()

from baldur.api.handlers.features import features_summary
from baldur.core.entitlement import (
    EntitlementClaims,
    EntitlementResult,
    EntitlementStatus,
)
from baldur.interfaces.web_framework import HttpMethod, RequestContext
from baldur.services.feature_manifest.loader import ManifestEntry

# =============================================================================
# Helpers
# =============================================================================


def _ctx() -> RequestContext:
    return RequestContext(
        method=HttpMethod.GET,
        path="/api/baldur/features/",
        query_params={},
        path_params={},
    )


def _entry(
    *,
    module="circuit_breaker.py",
    class_name="CircuitBreakerSettings",
    field="enabled",
    default=True,
    tier="Core",
    env_var="BALDUR_CB_ENABLED",
) -> ManifestEntry:
    return ManifestEntry(
        module=module,
        class_name=class_name,
        field=field,
        default=default,
        tier=tier,
        env_var=env_var,
    )


def _active_result() -> EntitlementResult:
    claims = EntitlementClaims(
        customer_id="cust_a1b2c3",
        org="acme",
        tier="PRO",
        plan="monthly",
        issued_at="2026-04-01",
        expires="2099-01-01",
    )
    return EntitlementResult(status=EntitlementStatus.ACTIVE, claims=claims)


def _missing_result() -> EntitlementResult:
    return EntitlementResult(status=EntitlementStatus.MISSING)


def _invalid_with_claims() -> EntitlementResult:
    """INVALID can carry claims (e.g., expired token) per 530 D9 rules."""
    claims = EntitlementClaims(
        customer_id="cust_xyz",
        org="acme",
        tier="PRO",
        plan="monthly",
        issued_at="2025-01-01",
        expires="2025-06-01",  # already expired
    )
    return EntitlementResult(status=EntitlementStatus.INVALID, claims=claims)


# =============================================================================
# features_summary handler — Behavior (530 D9 shape + count + overlay)
# =============================================================================


class TestFeaturesSummaryBehavior:
    """Handler composes manifest × entitlement into 530 D9's response shape."""

    def test_response_top_level_keys(self):
        """JSON body has exactly 'entitlement' + 'features' keys (530 D9)."""
        entries = (
            _entry(tier="Core", env_var="BALDUR_CORE"),
            _entry(tier="v1.0", env_var="BALDUR_V1"),
        )

        with (
            patch(
                "baldur.core.entitlement.get_entitlement_status",
                return_value=_active_result(),
            ),
            patch(
                "baldur.services.feature_manifest.load_feature_manifest",
                return_value=entries,
            ),
        ):
            response = features_summary(_ctx())

        assert response.status_code == 200
        assert set(response.body.keys()) == {"entitlement", "features"}

    def test_count_parity_with_manifest(self):
        """One entry per manifest row — no filtering, no dedup (530 D10)."""
        entries = tuple(
            _entry(module=f"x{i}.py", class_name=f"X{i}", env_var=f"BALDUR_X{i}")
            for i in range(7)
        )

        with (
            patch(
                "baldur.core.entitlement.get_entitlement_status",
                return_value=_missing_result(),
            ),
            patch(
                "baldur.services.feature_manifest.load_feature_manifest",
                return_value=entries,
            ),
        ):
            response = features_summary(_ctx())

        assert len(response.body["features"]) == 7

    def test_feature_entry_shape_matches_d9(self):
        """Each entry carries the 8 fields documented in 530 D9."""
        entries = (
            _entry(
                module="circuit_breaker.py",
                class_name="CircuitBreakerSettings",
                field="enabled",
                default=True,
                tier="Core",
                env_var="BALDUR_CB_ENABLED",
            ),
        )

        with (
            patch(
                "baldur.core.entitlement.get_entitlement_status",
                return_value=_missing_result(),
            ),
            patch(
                "baldur.services.feature_manifest.load_feature_manifest",
                return_value=entries,
            ),
        ):
            response = features_summary(_ctx())

        entry = response.body["features"][0]
        assert set(entry.keys()) == {
            "module",
            "class",
            "field",
            "tier",
            "default",
            "enabled",
            "env_var",
            "license_status",
        }
        assert entry["module"] == "circuit_breaker.py"
        assert entry["class"] == "CircuitBreakerSettings"
        assert entry["tier"] == "Core"
        assert entry["env_var"] == "BALDUR_CB_ENABLED"
        # Core entries are license-independent per 530 D9.
        assert entry["license_status"] == "active"

    def test_active_entitlement_block_includes_claims(self):
        """ACTIVE + claims → block carries customer_id/org/expires/days (530 D9)."""
        with (
            patch(
                "baldur.core.entitlement.get_entitlement_status",
                return_value=_active_result(),
            ),
            patch(
                "baldur.services.feature_manifest.load_feature_manifest",
                return_value=(),
            ),
        ):
            response = features_summary(_ctx())

        block = response.body["entitlement"]
        assert block["status"] == "active"
        assert block["customer_id"] == "cust_a1b2c3"
        assert block["org"] == "acme"
        assert block["expires"] == "2099-01-01"
        assert block["days_until_expiry"] >= 0

    def test_missing_entitlement_omits_claims_fields(self):
        """MISSING never carries claims → block has only 'status' (530 D9)."""
        with (
            patch(
                "baldur.core.entitlement.get_entitlement_status",
                return_value=_missing_result(),
            ),
            patch(
                "baldur.services.feature_manifest.load_feature_manifest",
                return_value=(),
            ),
        ):
            response = features_summary(_ctx())

        block = response.body["entitlement"]
        assert block == {"status": "missing"}

    def test_invalid_entitlement_with_claims_includes_claims_fields(self):
        """INVALID + claims (expired token) → claims fields present (530 D9)."""
        with (
            patch(
                "baldur.core.entitlement.get_entitlement_status",
                return_value=_invalid_with_claims(),
            ),
            patch(
                "baldur.services.feature_manifest.load_feature_manifest",
                return_value=(),
            ),
        ):
            response = features_summary(_ctx())

        block = response.body["entitlement"]
        assert block["status"] == "invalid"
        assert block["customer_id"] == "cust_xyz"
        assert block["days_until_expiry"] < 0  # past expiry

    def test_v1_entry_marked_requires_pro_when_no_active_license(self):
        """v1.0 tier + missing entitlement → license_status='requires_pro'."""
        entries = (_entry(tier="v1.0", env_var="BALDUR_PRO_FEATURE"),)

        with (
            patch(
                "baldur.core.entitlement.get_entitlement_status",
                return_value=_missing_result(),
            ),
            patch(
                "baldur.services.feature_manifest.load_feature_manifest",
                return_value=entries,
            ),
        ):
            response = features_summary(_ctx())

        assert response.body["features"][0]["license_status"] == "requires_pro"

    def test_v1_entry_marked_active_when_license_active(self):
        """v1.0 tier + active entitlement → license_status='active'."""
        entries = (_entry(tier="v1.0", env_var="BALDUR_PRO_FEATURE"),)

        with (
            patch(
                "baldur.core.entitlement.get_entitlement_status",
                return_value=_active_result(),
            ),
            patch(
                "baldur.services.feature_manifest.load_feature_manifest",
                return_value=entries,
            ),
        ):
            response = features_summary(_ctx())

        assert response.body["features"][0]["license_status"] == "active"

    def test_empty_manifest_returns_empty_features_array(self):
        """No manifest entries → features=[], block still present."""
        with (
            patch(
                "baldur.core.entitlement.get_entitlement_status",
                return_value=_missing_result(),
            ),
            patch(
                "baldur.services.feature_manifest.load_feature_manifest",
                return_value=(),
            ),
        ):
            response = features_summary(_ctx())

        assert response.body["features"] == []
        assert response.body["entitlement"] == {"status": "missing"}


# =============================================================================
# FeatureEntrySerializer — Behavior (DRF 'class' reserved-key round-trip)
# =============================================================================


class TestFeatureEntrySerializerBehavior:
    """The wire field is 'class' — DRF reserves the name, so the serializer
    uses 'field_class' internally and translates on the way out (530 D6)."""

    def _wire_entry(self) -> dict:
        return {
            "module": "circuit_breaker.py",
            "class": "CircuitBreakerSettings",
            "field": "enabled",
            "tier": "Core",
            "default": True,
            "enabled": True,
            "env_var": "BALDUR_CB_ENABLED",
            "license_status": "active",
        }

    def test_to_representation_preserves_class_key_on_wire(self):
        from baldur.api.django.serializers.features import FeatureEntrySerializer

        serializer = FeatureEntrySerializer(instance=self._wire_entry())
        data = serializer.data

        assert "class" in data
        assert data["class"] == "CircuitBreakerSettings"
        # field_class internal alias is NOT exposed on the wire.
        assert "field_class" not in data

    def test_full_response_serializer_round_trip(self):
        from baldur.api.django.serializers.features import FeaturesResponseSerializer

        payload = {
            "entitlement": {"status": "missing"},
            "features": [self._wire_entry()],
        }
        serializer = FeaturesResponseSerializer(instance=payload)
        data = serializer.data

        assert data["entitlement"]["status"] == "missing"
        assert len(data["features"]) == 1
        assert data["features"][0]["class"] == "CircuitBreakerSettings"
        assert data["features"][0]["license_status"] == "active"


# =============================================================================
# FeaturesView auth — Contract (PermissionLevel.ADMIN per 530 D2)
# =============================================================================


class TestFeaturesViewAuthContract:
    """530 D2: /features/ is admin-only — never authenticated-user or public."""

    def test_view_declares_admin_permission_level(self):
        from baldur.api.django.views.features import FeaturesView
        from baldur.interfaces.web_framework import PermissionLevel

        assert FeaturesView.permission_level is PermissionLevel.ADMIN

    def test_view_handler_is_features_summary(self):
        """View dispatches to the framework-agnostic handler (530 wiring)."""
        from baldur.api.django.views.features import FeaturesView

        assert FeaturesView.handler is features_summary

    def test_get_permissions_resolves_to_isbaldur_admin(self, monkeypatch):
        # get_permission_instances doesn't depend on request state for ADMIN;
        # permission_map is None, so resolve via the class-level helper.
        from baldur.api.django.permissions import (
            IsBaldurAdmin,
            get_permission_instances,
        )
        from baldur.interfaces.web_framework import PermissionLevel

        instances = get_permission_instances(PermissionLevel.ADMIN)
        assert len(instances) == 1
        assert isinstance(instances[0], IsBaldurAdmin)
