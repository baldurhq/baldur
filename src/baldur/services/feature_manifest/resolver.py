"""Env-gated resolver — per-entry current value + license overlay.

Two pure functions over the loaded manifest:

* ``resolve_feature_status(entry, env)`` — implements 530 D8: when the
  entry's ``env_var`` is not in the supplied environment, returns the
  manifest default directly (no settings import, no instantiation). When
  set, dynamically imports the canonical accessor and re-reads the
  field — so Pydantic validators and env overrides take effect.

* ``compute_license_status(tier, entitlement_status)`` — implements 530
  D9: maps tier × entitlement to the per-entry license overlay enum.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

import structlog

from baldur.services.feature_manifest.loader import ManifestEntry

logger = structlog.get_logger()

__all__ = [
    "FeatureStatus",
    "LicenseStatus",
    "compute_license_status",
    "resolve_feature_status",
]


# Settings modules whose accessor name diverges from the f"get_{stem}_settings"
# convention. Audit run 2026-05-24: full scan of src/baldur/settings/*.py found
# exactly these two manifest-relevant divergences. Adding new exceptions
# requires updating both this table and the audit comment.
_ACCESSOR_OVERRIDES: dict[str, str] = {
    "admin": "get_admin_server_settings",
    "logging_settings": "get_logging_settings",
}


class LicenseStatus(str, Enum):
    """Per-feature license overlay status (530 D9)."""

    ACTIVE = "active"
    REQUIRES_PRO = "requires_pro"
    DEFERRED = "deferred"
    DORMANT = "dormant"


@dataclass(frozen=True)
class FeatureStatus:
    """Resolved status for a single manifest entry."""

    module: str
    class_name: str
    field: str
    tier: str
    default: bool
    enabled: bool
    env_var: str
    license_status: LicenseStatus


def _accessor_name(module_stem: str) -> str:
    return _ACCESSOR_OVERRIDES.get(module_stem, f"get_{module_stem}_settings")


def _read_field_from_instance(
    instance: Any, target_class_name: str, field: str
) -> bool:
    """Return ``instance.<field>`` if the instance's class matches, else
    walk nested BaseModel fields to find one whose class name matches.

    Supports the ml_models.py pattern where the manifest entry's
    ``class`` is a nested BaseModel (e.g. ARIMAConfig) reachable via a
    snake-cased attribute on the top-level BaseSettings.
    """
    if type(instance).__name__ == target_class_name:
        return bool(getattr(instance, field))

    model_fields = getattr(instance, "model_fields", None)
    if not model_fields:
        raise AttributeError(
            f"cannot resolve {target_class_name}.{field} on {type(instance).__name__}"
        )

    for attr in model_fields:
        try:
            value = getattr(instance, attr)
        except AttributeError:
            continue
        if type(value).__name__ == target_class_name:
            return bool(getattr(value, field))

    raise AttributeError(
        f"no nested attribute on {type(instance).__name__} matches "
        f"class {target_class_name}"
    )


def _resolve_current_value(entry: ManifestEntry) -> bool:
    """Instantiate the settings class and read the field. D8 env-set path."""
    stem = entry.module.removesuffix(".py")
    module_path = f"baldur.settings.{stem}"
    accessor_name = _accessor_name(stem)

    try:
        module = importlib.import_module(module_path)
        accessor = getattr(module, accessor_name)
        instance = accessor()
        return _read_field_from_instance(instance, entry.class_name, entry.field)
    except Exception as exc:
        logger.debug(
            "feature_manifest.resolve_fallback_to_default",
            module=entry.module,
            class_name=entry.class_name,
            field=entry.field,
            error=str(exc),
        )
        return entry.default


def compute_license_status(tier: str, entitlement_status: str | None) -> LicenseStatus:
    """Map (tier, entitlement.status) → per-entry license status (530 D9).

    ``entitlement_status`` is the string value of EntitlementStatus
    (``active``/``invalid``/``missing``). None is treated as missing —
    same as when ``baldur_pro`` is uninstalled.
    """
    if tier == "Core":
        return LicenseStatus.ACTIVE
    if tier == "v1.0":
        if entitlement_status == "active":
            return LicenseStatus.ACTIVE
        return LicenseStatus.REQUIRES_PRO
    if tier == "Deferred":
        return LicenseStatus.DEFERRED
    if tier == "Dormant":
        return LicenseStatus.DORMANT
    # Unknown tier — fail conservative: treat as dormant.
    logger.warning("feature_manifest.unknown_tier", tier=tier)
    return LicenseStatus.DORMANT


def resolve_feature_status(
    entry: ManifestEntry,
    env: Mapping[str, str],
    *,
    entitlement_status: str | None = None,
) -> FeatureStatus:
    """Build a FeatureStatus for a single manifest entry.

    Env-gated per 530 D8: when ``entry.env_var`` is not in ``env``,
    returns the manifest default directly without importing or
    instantiating the settings class. When set, dynamically imports the
    canonical accessor and re-reads the field so Pydantic validators
    and the operator's override take effect.
    """
    current = _resolve_current_value(entry) if entry.env_var in env else entry.default

    return FeatureStatus(
        module=entry.module,
        class_name=entry.class_name,
        field=entry.field,
        tier=entry.tier,
        default=entry.default,
        enabled=current,
        env_var=entry.env_var,
        license_status=compute_license_status(entry.tier, entitlement_status),
    )
