"""V1_LAUNCH_MANIFEST.yaml loader with 2-tier path resolution.

Resolution priority:
1. ``BALDUR_TIER_MANIFEST_PATH`` env override — same env var as
   ``bootstrap.py``'s ``_locate_v1_launch_manifest`` so operator overrides
   take effect uniformly.
2. Package resource — ``importlib.resources.files("baldur._data")``. The
   manifest is now package-native (``src/baldur/_data/V1_LAUNCH_MANIFEST.yaml``),
   so this tier resolves in editable installs too — no separate editable-repo
   path is needed (the prior cross-tree docs-dir traversal is gone).

Module-level ``@lru_cache`` keeps the YAML parse cost off the request
path. Tests reset via ``_cache_clear()``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger()

__all__ = [
    "ManifestEntry",
    "load_feature_manifest",
]


_MANIFEST_RESOURCE_PACKAGE = "baldur._data"
_MANIFEST_RESOURCE_NAME = "V1_LAUNCH_MANIFEST.yaml"


@dataclass(frozen=True)
class ManifestEntry:
    """One row of V1_LAUNCH_MANIFEST.yaml (per-entry schema in the YAML header)."""

    module: str
    class_name: str
    field: str
    default: bool
    tier: str
    env_var: str


def _locate_manifest() -> Path | None:
    """Return the manifest path per the 2-tier resolution.

    Returns None if no candidate is reachable — callers degrade to an
    empty inventory rather than crashing the API.
    """
    override = os.getenv("BALDUR_TIER_MANIFEST_PATH")
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None

    # Package resource: the manifest ships natively under ``baldur._data`` in
    # both editable and wheel installs, so this single tier resolves uniformly.
    try:
        resource = resource_files(_MANIFEST_RESOURCE_PACKAGE).joinpath(
            _MANIFEST_RESOURCE_NAME
        )
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    if resource.is_file():
        return Path(str(resource))
    return None


def _coerce_entry(raw: dict[str, Any]) -> ManifestEntry | None:
    """Build a ManifestEntry from a raw YAML row, or None if malformed.

    Drops rows missing any required key so a single bad entry can't
    poison the whole inventory — bootstrap.py already validates schema
    fitness via test_v1_default_enable.py.
    """
    try:
        return ManifestEntry(
            module=str(raw["module"]),
            class_name=str(raw["class"]),
            field=str(raw["field"]),
            default=bool(raw["default"]),
            tier=str(raw["tier"]),
            env_var=str(raw["env_var"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


@lru_cache(maxsize=1)
def load_feature_manifest() -> tuple[ManifestEntry, ...]:
    """Read and cache the manifest as an immutable tuple of entries.

    Returns an empty tuple when the manifest is unreachable or
    malformed — same fail-quiet posture as bootstrap.py's
    ``_emit_tier_setting_warnings``: an introspection endpoint must
    never crash startup.
    """
    path = _locate_manifest()
    if path is None:
        logger.debug("feature_manifest.path_unresolved")
        return ()

    try:
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(
            "feature_manifest.load_failed",
            path=str(path),
            error=str(exc),
        )
        return ()

    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        return ()

    return tuple(
        coerced
        for raw in raw_entries
        if isinstance(raw, dict) and (coerced := _coerce_entry(raw)) is not None
    )


def _cache_clear() -> None:
    """Reset the manifest cache — test helper."""
    load_feature_manifest.cache_clear()
