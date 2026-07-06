"""Feature manifest service — runtime reader for V1_LAUNCH_MANIFEST.yaml.

Loads the authoritative tier/default contract from the package resource
``baldur._data/V1_LAUNCH_MANIFEST.yaml`` and joins it with the entitlement
state to answer "what features are enabled, what tier do they belong to, and
is my license active for them?" — the data backing
``GET /api/baldur/features/``.

Public API:
    load_feature_manifest()    — read + cache the manifest entries
    resolve_feature_status()   — env-gated per-entry status resolution
    compute_license_status()   — overlay tier × entitlement → status enum
    FeatureStatus              — dataclass returned per entry
    ManifestEntry              — raw YAML entry dataclass
    LicenseStatus              — enum for the per-entry license overlay
"""

from baldur.services.feature_manifest.loader import (
    ManifestEntry,
    load_feature_manifest,
)
from baldur.services.feature_manifest.resolver import (
    FeatureStatus,
    LicenseStatus,
    compute_license_status,
    resolve_feature_status,
)

__all__ = [
    "FeatureStatus",
    "LicenseStatus",
    "ManifestEntry",
    "compute_license_status",
    "load_feature_manifest",
    "resolve_feature_status",
]
