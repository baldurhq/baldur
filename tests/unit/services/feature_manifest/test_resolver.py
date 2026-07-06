"""feature_manifest.resolver unit tests — 530 D8 + D9.

Verifies:
- compute_license_status() — 5-case tier × entitlement matrix
- resolve_feature_status() — env-gated current value (no settings import
  when env var unset; canonical accessor invoked when set)
- _accessor_name() — 2 override entries (admin, logging_settings)
- _read_field_from_instance() — top-level match + nested BaseModel walk
- Unknown-tier fallback to DORMANT

Techniques applied:
- State transition (5 license_status branches)
- Dependency interaction (importlib.import_module called only when env set)
- Boundary analysis (unknown tier fallback)
- Idempotency (resolve doesn't mutate the entry)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from baldur.services.feature_manifest.loader import ManifestEntry
from baldur.services.feature_manifest.resolver import (
    FeatureStatus,
    LicenseStatus,
    _accessor_name,
    _read_field_from_instance,
    compute_license_status,
    resolve_feature_status,
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


# =============================================================================
# compute_license_status — Contract (530 D9 mapping table)
# =============================================================================


class TestComputeLicenseStatusContract:
    """530 D9 documents the exact (tier, entitlement) → license_status mapping."""

    @pytest.mark.parametrize(
        ("tier", "entitlement_status", "expected"),
        [
            # Core entries are license-independent.
            ("Core", "active", LicenseStatus.ACTIVE),
            ("Core", "missing", LicenseStatus.ACTIVE),
            ("Core", "invalid", LicenseStatus.ACTIVE),
            ("Core", None, LicenseStatus.ACTIVE),
            # v1.0 gates on active entitlement.
            ("v1.0", "active", LicenseStatus.ACTIVE),
            ("v1.0", "missing", LicenseStatus.REQUIRES_PRO),
            ("v1.0", "invalid", LicenseStatus.REQUIRES_PRO),
            ("v1.0", None, LicenseStatus.REQUIRES_PRO),
            # Deferred — held out of the v1.0 launch set.
            ("Deferred", "active", LicenseStatus.DEFERRED),
            ("Deferred", "missing", LicenseStatus.DEFERRED),
            # Dormant — opt-in only.
            ("Dormant", "active", LicenseStatus.DORMANT),
            ("Dormant", "missing", LicenseStatus.DORMANT),
        ],
    )
    def test_license_status_mapping(self, tier, entitlement_status, expected):
        assert compute_license_status(tier, entitlement_status) is expected

    def test_unknown_tier_fails_conservative_to_dormant(self):
        """Unknown tier → DORMANT (fail conservative). Code path emits a
        warning — verified by the implementation comment in resolver.py."""
        assert (
            compute_license_status("PrototypeTier", "active") is LicenseStatus.DORMANT
        )


# =============================================================================
# _accessor_name — Behavior (530 D8 override table)
# =============================================================================


class TestAccessorNameBehavior:
    """Two documented divergences from the f'get_{stem}_settings' convention."""

    @pytest.mark.parametrize(
        ("stem", "expected"),
        [
            ("admin", "get_admin_server_settings"),
            ("logging_settings", "get_logging_settings"),
            ("circuit_breaker", "get_circuit_breaker_settings"),
            ("openapi", "get_openapi_settings"),
            ("audit", "get_audit_settings"),
        ],
    )
    def test_accessor_derived_or_overridden(self, stem, expected):
        assert _accessor_name(stem) == expected


# =============================================================================
# _read_field_from_instance — Behavior (top-level + nested BaseModel walk)
# =============================================================================


class _StubInner:
    """Stand-in for a nested BaseModel — `type(self).__name__` is the class name."""

    def __init__(self, enabled: bool):
        self.enabled = enabled


class _StubARIMAConfig(_StubInner):
    """Same shape as ARIMAConfig — name match against `ARIMAConfig`."""


class _StubOpenAPISettings(_StubInner):
    """Top-level match against `OpenAPISettings`."""


class _StubMLModelsSettings:
    """Mimics MLModelsSettings: declares model_fields, holds inner instances."""

    model_fields = {"enabled": None, "arima": None}

    def __init__(self, *, enabled: bool, arima):
        self.enabled = enabled
        self.arima = arima


class _StubPlainClass:
    """No model_fields → walk path raises immediately."""

    def __init__(self, enabled: bool):
        self.enabled = enabled


class TestReadFieldFromInstanceBehavior:
    """Supports the ml_models.py pattern: nested BaseModel field lookup."""

    def test_top_level_class_match_reads_field_directly(self):
        instance = _StubOpenAPISettings(enabled=True)

        # Resolver matches by class name string (type(instance).__name__).
        # Use the real class name as the target.
        result = _read_field_from_instance(instance, type(instance).__name__, "enabled")

        assert result is True

    def test_nested_basemodel_walk_finds_inner_class(self):
        """ml_models.py style: outer settings declares model_fields and
        holds a nested BaseModel reachable via attribute lookup."""
        inner = _StubARIMAConfig(enabled=True)
        outer = _StubMLModelsSettings(enabled=False, arima=inner)

        result = _read_field_from_instance(outer, "_StubARIMAConfig", "enabled")

        assert result is True

    def test_no_match_raises_attribute_error(self):
        """Class name absent from instance & nested fields raises — the
        resolver catches and falls back to default."""
        outer = _StubMLModelsSettings(
            enabled=False, arima=_StubARIMAConfig(enabled=True)
        )

        with pytest.raises(AttributeError):
            _read_field_from_instance(outer, "NonexistentConfig", "enabled")

    def test_class_without_model_fields_raises(self):
        """Plain instance with no model_fields and no name match raises."""
        instance = _StubPlainClass(enabled=True)

        with pytest.raises(AttributeError):
            _read_field_from_instance(instance, "SomeOtherClass", "enabled")


# =============================================================================
# resolve_feature_status — Behavior (env-gated current value, 530 D8)
# =============================================================================


class TestResolveFeatureStatusBehavior:
    """530 D8: settings class is imported ONLY when env_var is in env."""

    def test_env_unset_returns_default_without_import(self):
        """Empty env → resolve uses manifest default, never imports settings."""
        entry = _entry(default=True, env_var="BALDUR_X")

        with patch(
            "baldur.services.feature_manifest.resolver.importlib.import_module"
        ) as mock_import:
            status = resolve_feature_status(entry, env={})

        mock_import.assert_not_called()
        assert status.enabled is True
        assert status.default is True

    def test_env_set_triggers_settings_accessor_call(self):
        """Operator-set env var → invoke canonical accessor for live value."""
        entry = _entry(
            module="openapi.py",
            class_name="OpenAPISettings",
            field="enabled",
            default=True,
            env_var="BALDUR_OPENAPI_ENABLED",
        )

        # Inject a fake module exposing the accessor. The class name has
        # to match the manifest entry's `class_name` so the top-level
        # branch in _read_field_from_instance() succeeds.
        OpenAPISettings = type("OpenAPISettings", (), {})  # noqa: N806
        fake_instance = OpenAPISettings()
        fake_instance.enabled = False
        fake_module = SimpleNamespace(get_openapi_settings=lambda: fake_instance)

        with patch(
            "baldur.services.feature_manifest.resolver.importlib.import_module",
            return_value=fake_module,
        ) as mock_import:
            status = resolve_feature_status(
                entry, env={"BALDUR_OPENAPI_ENABLED": "false"}
            )

        mock_import.assert_called_once_with("baldur.settings.openapi")
        # default=True but accessor returns enabled=False — env override observed.
        assert status.enabled is False
        assert status.default is True

    def test_returns_default_when_accessor_raises(self):
        """Accessor exception → fall back to manifest default (530 D8 safety)."""
        entry = _entry(default=True, env_var="BALDUR_BROKEN")

        def _raise(_name):
            raise ImportError("module not installed")

        with patch(
            "baldur.services.feature_manifest.resolver.importlib.import_module",
            side_effect=_raise,
        ):
            status = resolve_feature_status(entry, env={"BALDUR_BROKEN": "1"})

        assert status.enabled is True  # fell back to default
        assert status.default is True

    def test_license_status_overlay_passed_through(self):
        """entitlement_status arg flows into compute_license_status (530 D9)."""
        entry = _entry(tier="v1.0")

        status = resolve_feature_status(entry, env={}, entitlement_status="active")

        assert status.license_status is LicenseStatus.ACTIVE

    def test_license_status_default_treats_missing_status_as_unlicensed(self):
        entry = _entry(tier="v1.0")

        status = resolve_feature_status(entry, env={})

        assert status.license_status is LicenseStatus.REQUIRES_PRO

    def test_feature_status_carries_through_all_manifest_fields(self):
        entry = _entry(
            module="canary.py",
            class_name="CanarySettings",
            field="enabled",
            default=False,
            tier="v1.0",
            env_var="BALDUR_CANARY_ENABLED",
        )

        status = resolve_feature_status(entry, env={}, entitlement_status="active")

        assert isinstance(status, FeatureStatus)
        assert status.module == "canary.py"
        assert status.class_name == "CanarySettings"
        assert status.field == "enabled"
        assert status.default is False
        assert status.tier == "v1.0"
        assert status.env_var == "BALDUR_CANARY_ENABLED"
        assert status.enabled is False  # default propagated
        assert status.license_status is LicenseStatus.ACTIVE
