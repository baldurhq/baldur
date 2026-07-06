"""Impl 527 D5 — V1.0 default-enable contract.

Strict fitness function over every Pydantic ``*enabled*`` / ``enable_*``
boolean field under ``src/baldur/settings/``. Each discovered field MUST
appear in ``src/baldur/_data/V1_LAUNCH_MANIFEST.yaml`` AND its current
``Field(default=...)`` MUST equal the manifest's ``default``. The manifest
``env_var`` MUST equal the env var computed from the owning class's
``env_prefix`` (plus ``env_nested_delimiter="__"`` paths for nested
``BaseModel`` sub-configs).

Rule registry: ``ARCHITECTURE.md#g18-v1-default-enable``
"""

from __future__ import annotations

from typing import Any

import yaml

from tests.architecture._helpers import discover_enable_fields
from tests.architecture.conftest import PROJECT_ROOT

_MANIFEST_PATH = PROJECT_ROOT / "src" / "baldur" / "_data" / "V1_LAUNCH_MANIFEST.yaml"
_VALID_TIERS = frozenset({"Core", "v1.0", "Deferred", "Dormant"})


def _discover_fields() -> dict[tuple[str, str, str], dict[str, Any]]:
    """Return ``{(module, class, field): {default, env_var}}`` for every enable-shape field.

    Thin adapter over the shared ``discover_enable_fields()`` enumerator (impl
    doc 575 D3) — the single source of truth co-owned with G32. G18's downstream
    manifest cross-check keys on ``(module, class, field)``; the enumerator's
    extra ``source_file`` field (which G32 needs) is dropped here.
    """
    return {
        (ef.module, ef.cls, ef.field): {"default": ef.default, "env_var": ef.env_var}
        for ef in discover_enable_fields()
    }


def _load_manifest() -> list[dict[str, Any]]:
    with _MANIFEST_PATH.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    entries = data.get("entries") or []
    if not isinstance(entries, list):
        raise AssertionError(
            f"{_MANIFEST_PATH.name}: top-level `entries:` must be a list, got {type(entries).__name__}"
        )
    return entries


def _manifest_index(
    entries: list[dict[str, Any]],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in entries:
        key = (entry["module"], entry["class"], entry["field"])
        if key in index:
            raise AssertionError(f"{_MANIFEST_PATH.name}: duplicate entry {key}")
        index[key] = entry
    return index


class TestV1DefaultEnableContract:
    """527 D5 — every `*enabled*` / `enable_*` field is tier-classified at v1.0."""

    def test_manifest_schema_well_formed(self):
        """Manifest entries declare all required keys with valid tier values."""
        entries = _load_manifest()
        problems: list[str] = []
        required_keys = {"module", "class", "field", "default", "tier", "env_var"}
        for idx, entry in enumerate(entries):
            missing = required_keys - entry.keys()
            if missing:
                problems.append(f"entry #{idx}: missing keys {sorted(missing)}")
                continue
            if entry["tier"] not in _VALID_TIERS:
                problems.append(
                    f"entry #{idx} ({entry['module']}.{entry['class']}.{entry['field']}): "
                    f"tier={entry['tier']!r} not in {sorted(_VALID_TIERS)}"
                )
            if not isinstance(entry["default"], bool):
                problems.append(
                    f"entry #{idx} ({entry['module']}.{entry['class']}.{entry['field']}): "
                    f"default={entry['default']!r} must be a bool"
                )
        assert not problems, "Manifest schema violations:\n  " + "\n  ".join(problems)

    def test_every_discovered_field_has_manifest_entry(self):
        """Strict enumeration — new `*enabled*` fields require a manifest line."""
        discovered = _discover_fields()
        index = _manifest_index(_load_manifest())
        missing = sorted(set(discovered) - set(index))
        assert not missing, (
            f"{len(missing)} `*enabled*` field(s) without a manifest entry. "
            f"Add to {_MANIFEST_PATH.name}:\n  "
            + "\n  ".join(f"{m}.{c}.{f}" for m, c, f in missing)
        )

    def test_no_stale_manifest_entries(self):
        """Manifest must not retain rows for fields that no longer exist in code."""
        discovered = _discover_fields()
        index = _manifest_index(_load_manifest())
        stale = sorted(set(index) - set(discovered))
        assert not stale, (
            f"{len(stale)} manifest entry/entries refer to fields that no longer "
            f"exist in code. Remove from {_MANIFEST_PATH.name}:\n  "
            + "\n  ".join(f"{m}.{c}.{f}" for m, c, f in stale)
        )

    def test_field_defaults_match_manifest(self):
        """Every field's `Field(default=...)` must match the manifest's expected default."""
        discovered = _discover_fields()
        index = _manifest_index(_load_manifest())
        drift: list[str] = []
        for key, entry in sorted(index.items()):
            actual = discovered.get(key)
            if actual is None:
                continue
            if actual["default"] != entry["default"]:
                module, klass, field = key
                drift.append(
                    f"{module}.{klass}.{field}: code default={actual['default']!r} "
                    f"!= manifest default={entry['default']!r} (tier={entry['tier']})"
                )
        assert not drift, (
            f"{len(drift)} default-value drift between code and v1.0 manifest:\n  "
            + "\n  ".join(drift)
        )

    def test_env_var_binding(self):
        """Manifest's env_var must equal env_prefix + nested-path + field name (uppercase).

        Pinned to Pydantic v2 BaseSettings auto-env contract — catches future
        regressions where someone overrides env_prefix, adds `Field(alias=...)`,
        or renames a field without updating the operator-facing env var.
        """
        discovered = _discover_fields()
        index = _manifest_index(_load_manifest())
        problems: list[str] = []
        for key, entry in sorted(index.items()):
            actual = discovered.get(key)
            if actual is None:
                continue
            if actual["env_var"] != entry["env_var"]:
                module, klass, field = key
                problems.append(
                    f"{module}.{klass}.{field}: computed env_var={actual['env_var']!r} "
                    f"!= manifest env_var={entry['env_var']!r}"
                )
        assert not problems, (
            f"{len(problems)} env-var binding mismatch between Pydantic auto-derived "
            f"name and manifest:\n  " + "\n  ".join(problems)
        )
