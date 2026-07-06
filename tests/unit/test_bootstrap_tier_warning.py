"""Unit tests for ``baldur.bootstrap`` 527 D9 tier-warning emitter.

Covers:

- ``_locate_v1_launch_manifest()`` — manifest path resolution:
  - package-resource resolution (``baldur._data``, present in editable + wheel)
  - ``BALDUR_TIER_MANIFEST_PATH`` override honored when file exists
  - override pointing at a non-existent path → ``None`` (no fallback)
  - package resource non-resolvable → ``None``
- ``_emit_tier_setting_warnings()`` — operator WARNINGs for Deferred/Dormant
  env-var overrides:
  - emits ``baldur.tier_setting_overridden`` for each Deferred/Dormant entry
    whose ``env_var`` resolves truthy in ``os.environ``
  - ``BALDUR_SUPPRESS_TIER_WARNING=true`` silences the entire emitter
  - Core / v1.0 entries are never warned about, even when set truthy
  - manifest unreachable (``_locate_v1_launch_manifest()`` returns ``None``)
    → silent no-op (startup must never fail on a diagnostic)
  - malformed manifest YAML / missing ``entries`` list → silent no-op

Verification techniques (per UNIT_TEST_GUIDELINES §8):

- §8.4 Side effects (WARNING log capture via ``structlog.testing.capture_logs``).
- §8.5 Dependency interaction (env-var fixture + ``monkeypatch.setenv``).
- §8.11 Time independence — N/A (no time-based behavior).

These tests live at ``tests/unit/test_bootstrap_tier_warning.py`` because
``baldur.bootstrap`` is a top-level module (no parent package); the sibling
files ``test_bootstrap*.py`` follow the same convention.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def manifest_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Write a YAML manifest at a temp path and bind it via env-var override.

    Returns a callable that takes the list of ``entries`` to write and
    yields the resolved manifest Path. Tests use this to drive
    ``_emit_tier_setting_warnings()`` against a controlled manifest rather
    than the production manifest at ``baldur/_data/V1_LAUNCH_MANIFEST.yaml``.
    """
    import yaml

    def _write(entries: list[dict]) -> Path:
        manifest_path = tmp_path / "V1_LAUNCH_MANIFEST.yaml"
        manifest_path.write_text(
            yaml.safe_dump({"entries": entries}),
            encoding="utf-8",
        )
        monkeypatch.setenv("BALDUR_TIER_MANIFEST_PATH", str(manifest_path))
        return manifest_path

    return _write


# =============================================================================
# _locate_v1_launch_manifest — Behavior tests
# =============================================================================


class TestLocateV1LaunchManifestBehavior:
    """Manifest path resolution honors override + resolves the package resource."""

    def test_package_resource_resolves_when_installed_editable(self):
        # The manifest is package-native (``baldur._data/V1_LAUNCH_MANIFEST.yaml``),
        # so ``importlib.resources`` resolves it in editable installs too — no
        # docs-tree traversal. Resolution should succeed.
        from baldur.bootstrap import _locate_v1_launch_manifest

        result = _locate_v1_launch_manifest()

        assert result is not None
        assert result.name == "V1_LAUNCH_MANIFEST.yaml"
        assert result.is_file()

    def test_env_override_returns_path_when_file_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Given: a real file at a custom path bound via env-var override
        from baldur.bootstrap import _locate_v1_launch_manifest

        custom_manifest = tmp_path / "custom_manifest.yaml"
        custom_manifest.write_text("entries: []\n", encoding="utf-8")
        monkeypatch.setenv("BALDUR_TIER_MANIFEST_PATH", str(custom_manifest))

        # When
        result = _locate_v1_launch_manifest()

        # Then: override path returned (not the default project-root path)
        assert result == custom_manifest

    def test_env_override_returns_none_when_path_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # An explicit override that points at a non-existent file MUST NOT
        # silently fall back to the default — operators asked for a specific
        # manifest and got nothing, that's the answer.
        from baldur.bootstrap import _locate_v1_launch_manifest

        missing = tmp_path / "does_not_exist.yaml"
        monkeypatch.setenv("BALDUR_TIER_MANIFEST_PATH", str(missing))

        result = _locate_v1_launch_manifest()

        assert result is None

    def test_package_resource_unreachable_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # When the package resource is unreachable, resolution returns None.
        # Simulate by patching ``Path.is_file`` for the manifest name only —
        # the override branch is skipped because BALDUR_TIER_MANIFEST_PATH is
        # unset.
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_TIER_MANIFEST_PATH", raising=False)

        real_is_file = Path.is_file

        def _fake_is_file(self: Path) -> bool:
            if self.name == "V1_LAUNCH_MANIFEST.yaml":
                return False
            return real_is_file(self)

        monkeypatch.setattr(Path, "is_file", _fake_is_file)

        result = bootstrap._locate_v1_launch_manifest()

        assert result is None


# =============================================================================
# _emit_tier_setting_warnings — Behavior tests
# =============================================================================


_DORMANT_ENTRY = {
    "module": "compliance.py",
    "class": "ComplianceSettings",
    "field": "enabled",
    "default": False,
    "tier": "Dormant",
    "env_var": "BALDUR_TEST_DORMANT_COMPLIANCE",
}

_DEFERRED_ENTRY = {
    "module": "saga.py",
    "class": "SagaSettings",
    "field": "enabled",
    "default": False,
    "tier": "Deferred",
    "env_var": "BALDUR_TEST_DEFERRED_SAGA",
}

_V10_ENTRY = {
    "module": "audit.py",
    "class": "AuditSettings",
    "field": "enabled",
    "default": False,
    "tier": "v1.0",
    "env_var": "BALDUR_TEST_V10_AUDIT",
}

_CORE_ENTRY = {
    "module": "admin.py",
    "class": "AdminServerSettings",
    "field": "enabled",
    "default": True,
    "tier": "Core",
    "env_var": "BALDUR_TEST_CORE_ADMIN",
}


class TestEmitTierSettingWarningsBehavior:
    """One WARNING per Deferred/Dormant env-var override, suppressible wholesale."""

    def test_dormant_env_override_emits_one_warning(
        self, manifest_factory, monkeypatch: pytest.MonkeyPatch
    ):
        # Given: manifest with a Dormant entry + env var set truthy
        from baldur.bootstrap import _emit_tier_setting_warnings

        manifest_factory([_DORMANT_ENTRY])
        monkeypatch.setenv(_DORMANT_ENTRY["env_var"], "true")

        # When
        with capture_logs() as captured:
            _emit_tier_setting_warnings()

        # Then: exactly one tier_setting_overridden WARNING for the Dormant entry
        warnings = [
            entry
            for entry in captured
            if entry.get("event") == "baldur.tier_setting_overridden"
        ]
        assert len(warnings) == 1
        warning = warnings[0]
        assert warning["log_level"] == "warning"
        assert warning["tier"] == "Dormant"
        assert warning["env_var"] == _DORMANT_ENTRY["env_var"]
        assert warning["setting_path"] == "compliance.py.ComplianceSettings.enabled"
        assert warning["version_target"] == "post-v1.0"

    def test_deferred_env_override_emits_warning_with_post_v1_version_target(
        self, manifest_factory, monkeypatch: pytest.MonkeyPatch
    ):
        # Given: manifest with a Deferred entry + env var set truthy
        from baldur.bootstrap import _emit_tier_setting_warnings

        manifest_factory([_DEFERRED_ENTRY])
        monkeypatch.setenv(_DEFERRED_ENTRY["env_var"], "1")

        # When
        with capture_logs() as captured:
            _emit_tier_setting_warnings()

        # Then
        warnings = [
            entry
            for entry in captured
            if entry.get("event") == "baldur.tier_setting_overridden"
        ]
        assert len(warnings) == 1
        assert warnings[0]["tier"] == "Deferred"
        assert warnings[0]["version_target"] == "post-v1.0"

    @pytest.mark.parametrize(
        "suppress_value",
        ["true", "True", "TRUE", "1", "yes", "on", "y", "t"],
    )
    def test_suppress_env_var_silences_all_warnings(
        self, manifest_factory, monkeypatch: pytest.MonkeyPatch, suppress_value: str
    ):
        # Given: a Dormant + Deferred override that WOULD warn
        from baldur.bootstrap import _emit_tier_setting_warnings

        manifest_factory([_DORMANT_ENTRY, _DEFERRED_ENTRY])
        monkeypatch.setenv(_DORMANT_ENTRY["env_var"], "true")
        monkeypatch.setenv(_DEFERRED_ENTRY["env_var"], "true")
        # Wholesale suppression flag in any of the recognized truthy spellings
        monkeypatch.setenv("BALDUR_SUPPRESS_TIER_WARNING", suppress_value)

        # When
        with capture_logs() as captured:
            _emit_tier_setting_warnings()

        # Then: zero tier_setting_overridden events emitted
        warnings = [
            entry
            for entry in captured
            if entry.get("event") == "baldur.tier_setting_overridden"
        ]
        assert warnings == []

    def test_core_and_v10_tier_overrides_never_warn(
        self, manifest_factory, monkeypatch: pytest.MonkeyPatch
    ):
        # Given: Core + v1.0 entries with env vars set truthy. Per D9 the
        # tier-warning emitter is scoped to Deferred/Dormant only — Core and
        # v1.0 are launch-set features and need no operator nudge.
        from baldur.bootstrap import _emit_tier_setting_warnings

        manifest_factory([_CORE_ENTRY, _V10_ENTRY])
        monkeypatch.setenv(_CORE_ENTRY["env_var"], "true")
        monkeypatch.setenv(_V10_ENTRY["env_var"], "true")

        # When
        with capture_logs() as captured:
            _emit_tier_setting_warnings()

        # Then
        warnings = [
            entry
            for entry in captured
            if entry.get("event") == "baldur.tier_setting_overridden"
        ]
        assert warnings == []

    def test_no_env_var_set_no_warnings(
        self, manifest_factory, monkeypatch: pytest.MonkeyPatch
    ):
        # Given: Deferred/Dormant entries in manifest but NO env-var overrides.
        # Default state of an OSS install — no operator action, no warnings.
        from baldur.bootstrap import _emit_tier_setting_warnings

        manifest_factory([_DORMANT_ENTRY, _DEFERRED_ENTRY])
        monkeypatch.delenv(_DORMANT_ENTRY["env_var"], raising=False)
        monkeypatch.delenv(_DEFERRED_ENTRY["env_var"], raising=False)

        # When
        with capture_logs() as captured:
            _emit_tier_setting_warnings()

        # Then
        warnings = [
            entry
            for entry in captured
            if entry.get("event") == "baldur.tier_setting_overridden"
        ]
        assert warnings == []

    def test_falsy_env_var_value_does_not_warn(
        self, manifest_factory, monkeypatch: pytest.MonkeyPatch
    ):
        # `BALDUR_X_ENABLED=false` is an explicit disable, not an override.
        # Pydantic v2 BaseSettings treats it as False, so no warning.
        from baldur.bootstrap import _emit_tier_setting_warnings

        manifest_factory([_DORMANT_ENTRY])
        monkeypatch.setenv(_DORMANT_ENTRY["env_var"], "false")

        with capture_logs() as captured:
            _emit_tier_setting_warnings()

        warnings = [
            entry
            for entry in captured
            if entry.get("event") == "baldur.tier_setting_overridden"
        ]
        assert warnings == []

    def test_mixed_tier_manifest_emits_only_for_deferred_and_dormant(
        self, manifest_factory, monkeypatch: pytest.MonkeyPatch
    ):
        # Given: all four tiers represented, all env vars truthy
        from baldur.bootstrap import _emit_tier_setting_warnings

        manifest_factory([_CORE_ENTRY, _V10_ENTRY, _DEFERRED_ENTRY, _DORMANT_ENTRY])
        for entry in (_CORE_ENTRY, _V10_ENTRY, _DEFERRED_ENTRY, _DORMANT_ENTRY):
            monkeypatch.setenv(entry["env_var"], "true")

        # When
        with capture_logs() as captured:
            _emit_tier_setting_warnings()

        # Then: exactly two warnings — Deferred and Dormant
        warnings = [
            entry
            for entry in captured
            if entry.get("event") == "baldur.tier_setting_overridden"
        ]
        emitted_tiers = sorted(w["tier"] for w in warnings)
        assert emitted_tiers == ["Deferred", "Dormant"]

    def test_manifest_unreachable_is_silent_no_op(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # _locate_v1_launch_manifest() returning None must NOT raise — the
        # diagnostic is best-effort and must never break startup.
        from baldur import bootstrap

        monkeypatch.setattr(bootstrap, "_locate_v1_launch_manifest", lambda: None)

        with capture_logs() as captured:
            bootstrap._emit_tier_setting_warnings()

        warnings = [
            entry
            for entry in captured
            if entry.get("event") == "baldur.tier_setting_overridden"
        ]
        assert warnings == []

    def test_malformed_manifest_yaml_is_silent_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Manifest exists but is malformed (corrupt bytes) — yaml.safe_load
        # raises; the emitter must log a DEBUG breadcrumb and return without
        # raising. We assert no WARNING leaks out as a tier override.
        from baldur.bootstrap import _emit_tier_setting_warnings

        manifest_path = tmp_path / "V1_LAUNCH_MANIFEST.yaml"
        manifest_path.write_text(
            "entries: [unterminated\n  - {bad: yaml",
            encoding="utf-8",
        )
        monkeypatch.setenv("BALDUR_TIER_MANIFEST_PATH", str(manifest_path))

        with capture_logs() as captured:
            _emit_tier_setting_warnings()

        warnings = [
            entry
            for entry in captured
            if entry.get("event") == "baldur.tier_setting_overridden"
        ]
        assert warnings == []

    def test_manifest_without_entries_list_is_silent_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Valid YAML but `entries:` key missing → silent no-op.
        from baldur.bootstrap import _emit_tier_setting_warnings

        manifest_path = tmp_path / "V1_LAUNCH_MANIFEST.yaml"
        manifest_path.write_text("metadata:\n  version: 1\n", encoding="utf-8")
        monkeypatch.setenv("BALDUR_TIER_MANIFEST_PATH", str(manifest_path))

        with capture_logs() as captured:
            _emit_tier_setting_warnings()

        warnings = [
            entry
            for entry in captured
            if entry.get("event") == "baldur.tier_setting_overridden"
        ]
        assert warnings == []

    def test_malformed_entry_skipped_other_entries_still_emit(
        self, manifest_factory, monkeypatch: pytest.MonkeyPatch
    ):
        # Bad entry (not a dict) in the middle of the list MUST NOT block
        # subsequent entries from emitting their warnings.
        from baldur.bootstrap import _emit_tier_setting_warnings

        manifest_factory(["not_a_dict", _DORMANT_ENTRY])
        monkeypatch.setenv(_DORMANT_ENTRY["env_var"], "true")

        with capture_logs() as captured:
            _emit_tier_setting_warnings()

        warnings = [
            entry
            for entry in captured
            if entry.get("event") == "baldur.tier_setting_overridden"
        ]
        assert len(warnings) == 1
        assert warnings[0]["tier"] == "Dormant"

    def test_entry_missing_env_var_is_skipped(
        self, manifest_factory, monkeypatch: pytest.MonkeyPatch
    ):
        # Defensive: an entry without an `env_var` key (manifest drift)
        # must not crash the emitter — just skip the row.
        from baldur.bootstrap import _emit_tier_setting_warnings

        broken_entry = {
            "module": "x.py",
            "class": "X",
            "field": "enabled",
            "default": False,
            "tier": "Deferred",
            # env_var intentionally omitted
        }
        manifest_factory([broken_entry, _DORMANT_ENTRY])
        monkeypatch.setenv(_DORMANT_ENTRY["env_var"], "true")

        with capture_logs() as captured:
            _emit_tier_setting_warnings()

        warnings = [
            entry
            for entry in captured
            if entry.get("event") == "baldur.tier_setting_overridden"
        ]
        # Only the well-formed Dormant entry should warn
        assert len(warnings) == 1
        assert warnings[0]["env_var"] == _DORMANT_ENTRY["env_var"]
