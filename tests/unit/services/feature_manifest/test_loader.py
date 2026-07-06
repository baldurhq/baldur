"""feature_manifest.loader unit tests.

Verifies:
- 2-tier path resolution (env override → package resource)
- `@lru_cache` semantics and `_cache_clear()` reset
- Malformed-row drop behavior (single bad entry can't poison the whole list)
- Empty/missing-file degradation returns ()

Techniques applied:
- Dependency interaction (env var override)
- Idempotency (cache hit returns same tuple)
- Exception/edge cases (missing keys, bad types, file absent)
- Data immutability (return type is tuple, frozen dataclass)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from baldur.services.feature_manifest import loader as loader_module
from baldur.services.feature_manifest.loader import (
    ManifestEntry,
    _coerce_entry,
    _locate_manifest,
    load_feature_manifest,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    loader_module._cache_clear()
    yield
    loader_module._cache_clear()


def _write_manifest(path: Path, entries: list[dict]) -> None:
    """Write a minimal V1_LAUNCH_MANIFEST.yaml-compatible file."""
    import yaml

    path.write_text(yaml.safe_dump({"entries": entries}), encoding="utf-8")


# =============================================================================
# _coerce_entry — Behavior (malformed row handling)
# =============================================================================


class TestCoerceEntryBehavior:
    """Single bad entry must not poison the whole inventory (530 D7)."""

    def test_valid_row_produces_manifest_entry(self):
        entry = _coerce_entry(
            {
                "module": "openapi.py",
                "class": "OpenAPISettings",
                "field": "enabled",
                "default": True,
                "tier": "Core",
                "env_var": "BALDUR_OPENAPI_ENABLED",
            }
        )

        assert entry is not None
        assert entry.module == "openapi.py"
        assert entry.class_name == "OpenAPISettings"
        assert entry.field == "enabled"
        assert entry.default is True
        assert entry.tier == "Core"
        assert entry.env_var == "BALDUR_OPENAPI_ENABLED"

    @pytest.mark.parametrize(
        "missing_key",
        ["module", "class", "field", "default", "tier", "env_var"],
    )
    def test_missing_required_key_returns_none(self, missing_key):
        row = {
            "module": "x.py",
            "class": "X",
            "field": "enabled",
            "default": True,
            "tier": "Core",
            "env_var": "BALDUR_X",
        }
        row.pop(missing_key)

        assert _coerce_entry(row) is None

    def test_default_coerces_truthy_values_to_bool(self):
        """Non-bool truthy `default` is coerced via bool() per loader contract."""
        entry = _coerce_entry(
            {
                "module": "x.py",
                "class": "X",
                "field": "enabled",
                "default": 1,  # truthy non-bool
                "tier": "Core",
                "env_var": "BALDUR_X",
            }
        )

        assert entry is not None
        assert entry.default is True

    def test_extra_fields_are_silently_ignored(self):
        """Forward-compat: unknown keys don't break parsing."""
        entry = _coerce_entry(
            {
                "module": "x.py",
                "class": "X",
                "field": "enabled",
                "default": False,
                "tier": "Core",
                "env_var": "BALDUR_X",
                "future_extension": "ignored",
            }
        )

        assert entry is not None
        assert entry.default is False


# =============================================================================
# _locate_manifest — Behavior (2-tier path priority: env override → package)
# =============================================================================


class TestLocateManifestBehavior:
    """Path resolution priority: env override → package resource."""

    def test_env_override_takes_precedence(self, tmp_path, monkeypatch):
        """BALDUR_TIER_MANIFEST_PATH env var has highest priority (step 1)."""
        custom = tmp_path / "custom_manifest.yaml"
        _write_manifest(custom, [])
        monkeypatch.setenv("BALDUR_TIER_MANIFEST_PATH", str(custom))

        located = _locate_manifest()

        assert located == custom

    def test_env_override_pointing_to_nonexistent_falls_through_to_none(
        self, tmp_path, monkeypatch
    ):
        """When env override points to a missing file, _locate returns None."""
        # Use a guaranteed-missing path; also clear editable repo path so we
        # don't accidentally fall through to the real on-disk YAML.
        monkeypatch.setenv(
            "BALDUR_TIER_MANIFEST_PATH",
            str(tmp_path / "does_not_exist.yaml"),
        )

        assert _locate_manifest() is None

    def test_package_resource_used_when_no_env_override(self, monkeypatch):
        """Package resource finds baldur._data/V1_LAUNCH_MANIFEST.yaml (step 2).

        The manifest is package-native, so ``importlib.resources`` resolves it
        in editable installs too — no separate editable-repo traversal needed.
        """
        monkeypatch.delenv("BALDUR_TIER_MANIFEST_PATH", raising=False)

        located = _locate_manifest()

        # The package-native resource is present in both editable and wheel runs.
        assert located is not None
        assert located.name == "V1_LAUNCH_MANIFEST.yaml"
        assert located.is_file()


# =============================================================================
# load_feature_manifest — Behavior (cache + parse + degradation)
# =============================================================================


class TestLoadFeatureManifestBehavior:
    """Cache + parse + degradation behavior per 530 D7."""

    def test_returns_tuple_of_manifest_entries(self, tmp_path, monkeypatch):
        manifest = tmp_path / "manifest.yaml"
        _write_manifest(
            manifest,
            [
                {
                    "module": "openapi.py",
                    "class": "OpenAPISettings",
                    "field": "enabled",
                    "default": True,
                    "tier": "Core",
                    "env_var": "BALDUR_OPENAPI_ENABLED",
                },
                {
                    "module": "audit.py",
                    "class": "AuditSettings",
                    "field": "enabled",
                    "default": False,
                    "tier": "Core",
                    "env_var": "BALDUR_AUDIT_ENABLED",
                },
            ],
        )
        monkeypatch.setenv("BALDUR_TIER_MANIFEST_PATH", str(manifest))

        entries = load_feature_manifest()

        assert isinstance(entries, tuple)
        assert len(entries) == 2
        assert all(isinstance(e, ManifestEntry) for e in entries)
        assert entries[0].module == "openapi.py"
        assert entries[1].field == "enabled"

    def test_malformed_row_dropped_without_crashing_inventory(
        self, tmp_path, monkeypatch
    ):
        """One bad entry doesn't poison the rest (530 D7)."""
        manifest = tmp_path / "manifest.yaml"
        _write_manifest(
            manifest,
            [
                {
                    "module": "openapi.py",
                    "class": "OpenAPISettings",
                    "field": "enabled",
                    "default": True,
                    "tier": "Core",
                    "env_var": "BALDUR_OPENAPI_ENABLED",
                },
                # malformed: missing `field`
                {
                    "module": "broken.py",
                    "class": "BrokenSettings",
                    "default": True,
                    "tier": "Core",
                    "env_var": "BALDUR_BROKEN",
                },
            ],
        )
        monkeypatch.setenv("BALDUR_TIER_MANIFEST_PATH", str(manifest))

        entries = load_feature_manifest()

        assert len(entries) == 1
        assert entries[0].module == "openapi.py"

    def test_missing_file_returns_empty_tuple_quietly(
        self, tmp_path, monkeypatch, caplog
    ):
        """Manifest unreachable → fail-quiet empty tuple (introspection endpoint
        must never crash startup, 530 D7)."""
        monkeypatch.setenv(
            "BALDUR_TIER_MANIFEST_PATH",
            str(tmp_path / "absent.yaml"),
        )

        result = load_feature_manifest()

        assert result == ()

    def test_malformed_yaml_returns_empty_tuple(self, tmp_path, monkeypatch):
        """Invalid YAML → fail-quiet empty tuple."""
        broken = tmp_path / "broken.yaml"
        broken.write_text("this is: not: valid: yaml: [", encoding="utf-8")
        monkeypatch.setenv("BALDUR_TIER_MANIFEST_PATH", str(broken))

        assert load_feature_manifest() == ()

    def test_empty_entries_list_returns_empty_tuple(self, tmp_path, monkeypatch):
        manifest = tmp_path / "manifest.yaml"
        _write_manifest(manifest, [])
        monkeypatch.setenv("BALDUR_TIER_MANIFEST_PATH", str(manifest))

        assert load_feature_manifest() == ()

    def test_entries_key_missing_returns_empty_tuple(self, tmp_path, monkeypatch):
        """YAML without the top-level `entries:` key degrades to ()."""
        unrelated = tmp_path / "unrelated.yaml"
        unrelated.write_text("something_else: 1\n", encoding="utf-8")
        monkeypatch.setenv("BALDUR_TIER_MANIFEST_PATH", str(unrelated))

        assert load_feature_manifest() == ()


# =============================================================================
# Cache lifecycle — Behavior
# =============================================================================


class TestLoadFeatureManifestCacheBehavior:
    """@lru_cache(maxsize=1) keeps YAML parse cost off the request path."""

    def test_repeated_calls_return_identical_object(self, tmp_path, monkeypatch):
        manifest = tmp_path / "manifest.yaml"
        _write_manifest(
            manifest,
            [
                {
                    "module": "x.py",
                    "class": "X",
                    "field": "enabled",
                    "default": True,
                    "tier": "Core",
                    "env_var": "BALDUR_X",
                }
            ],
        )
        monkeypatch.setenv("BALDUR_TIER_MANIFEST_PATH", str(manifest))

        first = load_feature_manifest()
        second = load_feature_manifest()

        assert first is second  # cached tuple identity

    def test_cache_clear_picks_up_file_changes(self, tmp_path, monkeypatch):
        """_cache_clear() forces re-read on next call (530 D7 test helper)."""
        manifest = tmp_path / "manifest.yaml"
        _write_manifest(
            manifest,
            [
                {
                    "module": "x.py",
                    "class": "X",
                    "field": "enabled",
                    "default": True,
                    "tier": "Core",
                    "env_var": "BALDUR_X",
                }
            ],
        )
        monkeypatch.setenv("BALDUR_TIER_MANIFEST_PATH", str(manifest))

        first = load_feature_manifest()
        assert len(first) == 1

        # Rewrite the file with 2 entries; without clear, we still get 1.
        _write_manifest(
            manifest,
            [
                {
                    "module": "x.py",
                    "class": "X",
                    "field": "enabled",
                    "default": True,
                    "tier": "Core",
                    "env_var": "BALDUR_X",
                },
                {
                    "module": "y.py",
                    "class": "Y",
                    "field": "enabled",
                    "default": False,
                    "tier": "v1.0",
                    "env_var": "BALDUR_Y",
                },
            ],
        )
        assert load_feature_manifest() is first  # still cached

        loader_module._cache_clear()
        second = load_feature_manifest()
        assert len(second) == 2
        assert second is not first


# =============================================================================
# ManifestEntry — Contract (frozen dataclass — data immutability)
# =============================================================================


class TestManifestEntryContract:
    """ManifestEntry is frozen — protects the cached tuple from mutation."""

    def test_entry_is_immutable(self):
        entry = ManifestEntry(
            module="x.py",
            class_name="X",
            field="enabled",
            default=True,
            tier="Core",
            env_var="BALDUR_X",
        )

        with pytest.raises(AttributeError):
            entry.module = "y.py"  # type: ignore[misc]
