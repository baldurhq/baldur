"""Unit tests for baldur.cli._config (429 Part 7 D10 resolution chain).

Covers:
- resolve_config precedence: CLI flag > BALDUR_CONFIG > cwd > XDG > env_only
- apply_config_to_env: TOML flattening, env preservation, invalid TOML
- load_dotenv_if_requested: explicit flag vs BALDUR_DOTENV env var
- _flatten_baldur_section / _walk / _stringify: value projection rules
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from baldur.cli import _config
from baldur.cli._config import (
    ConfigResolution,
    apply_config_to_env,
    load_dotenv_if_requested,
    resolve_config,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every BALDUR_* env var plus the XDG trigger, before and after.

    resolve_config reads os.environ and Path.cwd(); leftover BALDUR_CONFIG or
    BALDUR_DOTENV from an outer shell would mask the chain we want to test.

    The teardown is defensive: apply_config_to_env() writes directly to
    os.environ (bypassing monkeypatch), so anything it projected into env
    would outlive the test and pollute sibling tests under pytest-xdist -
    specifically admin-server tests that instantiate AdminServerSettings
    afterward and would pick up a stray BALDUR_ADMIN_BIND.
    """

    def _strip() -> None:
        for key in list(os.environ.keys()):
            if key.startswith("BALDUR_") or key == "XDG_CONFIG_HOME":
                os.environ.pop(key, None)

    _strip()
    yield monkeypatch
    _strip()


@pytest.fixture
def isolated_cwd(tmp_path, monkeypatch):
    """Run resolve_config() against a fresh tmp_path cwd with no baldur.toml."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# =============================================================================
# Contract — ConfigResolution dataclass
# =============================================================================


class TestConfigResolutionContract:
    """ConfigResolution is the observability record of which source won."""

    def test_slots_prevent_attribute_mutation(self):
        """__slots__ guarantees we don't accidentally add runtime fields."""
        resolution = ConfigResolution(None, "env_only")
        with pytest.raises(AttributeError):
            resolution.something_new = "oops"

    def test_repr_exposes_path_and_source(self):
        """__repr__ is used in debug logs - both fields must be visible."""
        resolution = ConfigResolution(Path("/x/y.toml"), "cli_flag")
        rendered = repr(resolution)
        assert "cli_flag" in rendered
        assert "y.toml" in rendered


# =============================================================================
# Behavior — resolve_config chain order
# =============================================================================


class TestResolveConfigBehavior:
    """D10 precedence chain: CLI flag > env var > cwd > XDG > env_only."""

    def test_explicit_cli_flag_wins_over_env_var(
        self, clean_env, isolated_cwd, tmp_path
    ):
        # Given: both a CLI flag and BALDUR_CONFIG env var point to real files
        flag_file = tmp_path / "flag.toml"
        flag_file.write_text("")
        env_file = tmp_path / "env.toml"
        env_file.write_text("")
        clean_env.setenv("BALDUR_CONFIG", str(env_file))

        # When
        resolution = resolve_config(str(flag_file))

        # Then: flag path wins
        assert resolution.source == "cli_flag"
        assert resolution.path == flag_file

    def test_explicit_cli_flag_missing_raises(self, clean_env, isolated_cwd):
        """An explicit --config that does not resolve is a user error."""
        with pytest.raises(FileNotFoundError) as excinfo:
            resolve_config("/definitely/not/a/real/path.toml")
        assert "--config" in str(excinfo.value)

    def test_env_var_used_when_no_flag(self, clean_env, isolated_cwd, tmp_path):
        env_file = tmp_path / "env.toml"
        env_file.write_text("")
        clean_env.setenv("BALDUR_CONFIG", str(env_file))

        resolution = resolve_config(None)

        assert resolution.source == "env_var"
        assert resolution.path == env_file

    def test_env_var_missing_file_falls_through_with_warning(
        self, clean_env, isolated_cwd, caplog
    ):
        """BALDUR_CONFIG pointing to a missing file warns but does not raise."""
        clean_env.setenv("BALDUR_CONFIG", "/nowhere/baldur.toml")

        resolution = resolve_config(None)

        # Falls through to cwd (empty) -> xdg (none with clean_env) -> env_only
        assert resolution.source == "env_only"
        assert resolution.path is None

    def test_cwd_baldur_toml_preferred_over_dot_baldur_toml(
        self, clean_env, isolated_cwd
    ):
        """Resolution walks _CWD_CANDIDATES in order; baldur.toml comes first."""
        (isolated_cwd / "baldur.toml").write_text("")
        (isolated_cwd / ".baldur.toml").write_text("")

        resolution = resolve_config(None)

        assert resolution.source == "cwd"
        assert resolution.path.name == "baldur.toml"

    def test_cwd_dot_baldur_toml_used_when_baldur_toml_absent(
        self, clean_env, isolated_cwd
    ):
        (isolated_cwd / ".baldur.toml").write_text("")

        resolution = resolve_config(None)

        assert resolution.source == "cwd"
        assert resolution.path.name == ".baldur.toml"

    def test_xdg_used_when_cwd_empty(self, clean_env, isolated_cwd, tmp_path):
        xdg_home = tmp_path / "xdg"
        xdg_dir = xdg_home / "baldur"
        xdg_dir.mkdir(parents=True)
        (xdg_dir / "config.toml").write_text("")

        if sys.platform == "win32":
            clean_env.setenv("APPDATA", str(xdg_home))
        else:
            clean_env.setenv("XDG_CONFIG_HOME", str(xdg_home))

        resolution = resolve_config(None)

        assert resolution.source == "xdg"
        assert resolution.path.parent.name == "baldur"

    def test_env_only_when_nothing_resolves(self, clean_env, isolated_cwd):
        """All chain entries miss -> env_only sentinel with path=None."""
        resolution = resolve_config(None)
        assert resolution.source == "env_only"
        assert resolution.path is None

    def test_empty_string_env_var_treated_as_unset(self, clean_env, isolated_cwd):
        """BALDUR_CONFIG='' (set but empty) must not short-circuit the chain."""
        clean_env.setenv("BALDUR_CONFIG", "   ")
        resolution = resolve_config(None)
        assert resolution.source == "env_only"


# =============================================================================
# Behavior — _xdg_config_path platform-specific resolution
# =============================================================================


class TestXdgConfigPathBehavior:
    """XDG path computation is the only piece of _config with platform branching."""

    def test_win32_uses_appdata(self, clean_env, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        clean_env.setenv("APPDATA", r"C:\Users\test\AppData\Roaming")

        path = _config._xdg_config_path()

        assert path is not None
        assert path.name == "config.toml"
        assert path.parent.name == "baldur"

    def test_win32_without_appdata_returns_none(self, clean_env, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        clean_env.delenv("APPDATA", raising=False)

        assert _config._xdg_config_path() is None

    def test_posix_prefers_xdg_config_home(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "linux")
        clean_env.setenv("XDG_CONFIG_HOME", str(tmp_path))
        clean_env.setenv("HOME", "/home/elsewhere")

        path = _config._xdg_config_path()

        assert path is not None
        assert path == tmp_path / "baldur" / "config.toml"

    def test_posix_falls_back_to_home_dot_config(self, clean_env, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        clean_env.delenv("XDG_CONFIG_HOME", raising=False)
        clean_env.setenv("HOME", "/home/u")

        path = _config._xdg_config_path()

        assert path == Path("/home/u/.config/baldur/config.toml")

    def test_posix_without_home_returns_none(self, clean_env, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        clean_env.delenv("XDG_CONFIG_HOME", raising=False)
        clean_env.delenv("HOME", raising=False)

        assert _config._xdg_config_path() is None


# =============================================================================
# Behavior — apply_config_to_env TOML projection
# =============================================================================


class TestApplyConfigToEnvBehavior:
    """TOML [baldur] sections are flattened and exported as BALDUR_* env vars."""

    def test_null_resolution_returns_empty(self, clean_env):
        """ConfigResolution(None, 'env_only') is a valid no-op."""
        applied = apply_config_to_env(ConfigResolution(None, "env_only"))
        assert applied == {}

    def test_top_level_baldur_scalars_are_rejected(self, clean_env, tmp_path, caplog):
        """Top-level [baldur] scalars have no owning settings class; reject them.

        Every settings class uses ``make_settings_config("BALDUR_<SECTION>_")`` -
        there is no owner for bare ``BALDUR_<KEY>`` projection. Silently emitting
        those env vars would look like they took effect but reach no class.
        """
        toml_file = tmp_path / "c.toml"
        toml_file.write_text(
            """
            [baldur]
            service_name = "cart-api"
            debug = true
            """.strip()
        )

        applied = apply_config_to_env(ConfigResolution(toml_file, "cwd"))

        assert applied == {}
        assert "BALDUR_SERVICE_NAME" not in os.environ
        assert "BALDUR_DEBUG" not in os.environ

    def test_section_fields_use_single_underscore_class_prefix(
        self, clean_env, tmp_path
    ):
        """[baldur.admin] bind -> BALDUR_ADMIN_BIND (class env_prefix + field).

        Settings classes use ``make_settings_config("BALDUR_ADMIN_")`` so the
        projection must emit exactly ``BALDUR_ADMIN_<FIELD>`` for Pydantic to
        pick it up.
        """
        toml_file = tmp_path / "c.toml"
        toml_file.write_text(
            """
            [baldur.admin]
            bind = "0.0.0.0"
            port = 8080
            """.strip()
        )

        applied = apply_config_to_env(ConfigResolution(toml_file, "cwd"))

        assert applied["BALDUR_ADMIN_BIND"] == "0.0.0.0"
        assert applied["BALDUR_ADMIN_PORT"] == "8080"

    def test_sub_basemodel_fields_use_double_underscore_delimiter(
        self, clean_env, tmp_path
    ):
        """[baldur.admin.retry] max -> BALDUR_ADMIN_RETRY__MAX.

        Nested sub-BaseModel fields inside a settings class use the Pydantic
        ``env_nested_delimiter="__"`` (``settings/base.py:16``). The section
        prefix still joins with a single underscore, so only the depth-2+
        levels use ``__``.
        """
        toml_file = tmp_path / "c.toml"
        toml_file.write_text(
            """
            [baldur.dlq.retention]
            days = 7
            """.strip()
        )

        applied = apply_config_to_env(ConfigResolution(toml_file, "cwd"))

        assert applied["BALDUR_DLQ_RETENTION__DAYS"] == "7"

    def test_existing_env_vars_win_over_toml(self, clean_env, tmp_path):
        """Process env always wins - users can override committed baldur.toml."""
        clean_env.setenv("BALDUR_ADMIN_BIND", "shell-override")
        toml_file = tmp_path / "c.toml"
        toml_file.write_text('[baldur.admin]\nbind = "from-toml"\n')

        applied = apply_config_to_env(ConfigResolution(toml_file, "cwd"))

        assert "BALDUR_ADMIN_BIND" not in applied  # preserved, not applied
        assert os.environ["BALDUR_ADMIN_BIND"] == "shell-override"

    def test_non_baldur_sections_ignored(self, clean_env, tmp_path):
        """[tool.xyz] and similar shouldn't leak into BALDUR_* namespace."""
        toml_file = tmp_path / "c.toml"
        toml_file.write_text(
            """
            [tool.ruff]
            line_length = 100
            [other]
            thing = "no"
            [baldur.admin]
            bind = "127.0.0.1"
            """.strip()
        )

        applied = apply_config_to_env(ConfigResolution(toml_file, "cwd"))

        assert applied == {"BALDUR_ADMIN_BIND": "127.0.0.1"}

    def test_baldur_section_missing_returns_empty(self, clean_env, tmp_path):
        """TOML file without [baldur] is valid - just no env vars to apply."""
        toml_file = tmp_path / "c.toml"
        toml_file.write_text("[tool.ruff]\nline_length = 100\n")

        applied = apply_config_to_env(ConfigResolution(toml_file, "cwd"))

        assert applied == {}

    def test_invalid_toml_raises_value_error(self, clean_env, tmp_path):
        toml_file = tmp_path / "c.toml"
        toml_file.write_text("this is =not valid = toml [[[")

        with pytest.raises(ValueError) as excinfo:
            apply_config_to_env(ConfigResolution(toml_file, "cwd"))
        assert "Invalid TOML" in str(excinfo.value)


# =============================================================================
# End-to-end — TOML -> env -> Pydantic settings round-trip
# =============================================================================


class TestTomlToPydanticRoundTripBehavior:
    """Guards against projection/schema drift.

    The failure that motivated this class: unit tests asserted the correct
    env var was emitted, but Pydantic never actually read it because the
    delimiter used by the projection did not match the delimiter each
    settings class expects. Asserting the round-trip catches that silently.
    """

    def test_admin_section_projection_is_read_back_by_settings_class(
        self, clean_env, tmp_path
    ):
        # Given: a TOML file with a single admin bind override.
        from baldur.settings.admin import (
            AdminServerSettings,
            reset_admin_server_settings,
        )

        toml_file = tmp_path / "c.toml"
        toml_file.write_text('[baldur.admin]\nbind = "203.0.113.1"\n')

        # When: apply_config_to_env projects the TOML onto os.environ, then we
        # construct a fresh AdminServerSettings (singleton cache already cleared
        # by clean_env but reset explicitly to be safe across test ordering).
        reset_admin_server_settings()
        try:
            apply_config_to_env(ConfigResolution(toml_file, "cwd"))
            settings = AdminServerSettings()

            # Then: the bind value flows TOML -> env -> Pydantic field.
            assert settings.bind == "203.0.113.1"
        finally:
            reset_admin_server_settings()


# =============================================================================
# Behavior — _flatten / _walk / _stringify helpers
# =============================================================================


class TestStringifyBehavior:
    """_stringify maps TOML primitives onto env-var-safe strings."""

    def test_bool_true_becomes_one(self):
        assert _config._stringify(True) == "1"

    def test_bool_false_becomes_zero(self):
        assert _config._stringify(False) == "0"

    def test_list_becomes_comma_separated(self):
        assert _config._stringify(["a", "b", "c"]) == "a,b,c"

    def test_tuple_becomes_comma_separated(self):
        assert _config._stringify(("a", "b")) == "a,b"

    def test_nested_list_with_booleans_preserves_mapping(self):
        assert _config._stringify([True, False, 1]) == "1,0,1"

    def test_int_uses_str(self):
        assert _config._stringify(42) == "42"

    def test_float_uses_str(self):
        assert _config._stringify(3.14) == "3.14"

    def test_string_passes_through(self):
        assert _config._stringify("hello") == "hello"


class TestFlattenBaldurSectionBehavior:
    """_flatten_baldur_section walks recursively; non-dict roots return empty."""

    def test_non_dict_baldur_section_returns_empty(self):
        # TOML can give [baldur] as a table, but if somehow it's not a dict...
        assert _config._flatten_baldur_section({"baldur": "a_string"}) == {}

    def test_empty_baldur_section_returns_empty(self):
        assert _config._flatten_baldur_section({"baldur": {}}) == {}

    def test_mixed_top_level_scalars_rejected_nested_projected(self):
        """Top-level scalars skipped; sections project as class-prefix + field."""
        data = {
            "baldur": {
                "service_name": "api",  # top-level scalar - rejected
                "admin": {"port": 8080},
                "dlq": {"memory_cap": 10000, "enabled": True},
            }
        }

        flat = _config._flatten_baldur_section(data)

        assert "BALDUR_SERVICE_NAME" not in flat
        assert flat["BALDUR_ADMIN_PORT"] == "8080"
        assert flat["BALDUR_DLQ_MEMORY_CAP"] == "10000"
        assert flat["BALDUR_DLQ_ENABLED"] == "1"

    def test_deep_nesting_uses_double_underscore_from_depth_two(self):
        """Section -> field = '_'; field.sub_field.sub_sub = '__'."""
        data = {
            "baldur": {
                "admin": {
                    "retry": {"max": 5, "window": {"seconds": 30}},
                }
            }
        }

        flat = _config._flatten_baldur_section(data)

        assert flat["BALDUR_ADMIN_RETRY__MAX"] == "5"
        assert flat["BALDUR_ADMIN_RETRY__WINDOW__SECONDS"] == "30"


# =============================================================================
# Behavior — load_dotenv_if_requested
# =============================================================================


class TestLoadDotenvBehavior:
    """`.env` loading is explicit-only: --env-file or BALDUR_DOTENV=1."""

    def test_no_signal_returns_none(self, clean_env, isolated_cwd):
        """No flag, no env var -> never silently load anything."""
        assert load_dotenv_if_requested(None) is None

    def test_explicit_path_wins_over_env_flag(self, clean_env, isolated_cwd, tmp_path):
        flag_file = tmp_path / "flag.env"
        flag_file.write_text("FROM_FLAG=1\n")
        cwd_env = isolated_cwd / ".env"
        cwd_env.write_text("FROM_CWD=1\n")
        clean_env.setenv("BALDUR_DOTENV", "1")

        loaded = load_dotenv_if_requested(str(flag_file))

        assert loaded == flag_file
        assert os.environ.get("FROM_FLAG") == "1"
        assert "FROM_CWD" not in os.environ

    def test_explicit_path_missing_raises(self, clean_env, isolated_cwd):
        with pytest.raises(FileNotFoundError) as excinfo:
            load_dotenv_if_requested("/never/exists/.env")
        assert "--env-file" in str(excinfo.value)

    def test_baldur_dotenv_env_var_loads_cwd_env(self, clean_env, isolated_cwd):
        (isolated_cwd / ".env").write_text("BALDUR_FROM_DOTENV=value1\n")
        clean_env.setenv("BALDUR_DOTENV", "1")

        loaded = load_dotenv_if_requested(None)

        assert loaded is not None
        assert loaded.name == ".env"
        assert os.environ.get("BALDUR_FROM_DOTENV") == "value1"

    def test_baldur_dotenv_env_var_cwd_missing_returns_none(
        self, clean_env, isolated_cwd
    ):
        """BALDUR_DOTENV=1 with no ./.env just falls through, does not raise."""
        clean_env.setenv("BALDUR_DOTENV", "1")

        assert load_dotenv_if_requested(None) is None

    @pytest.mark.parametrize(
        "value",
        ["1", "true", "TRUE", "yes", "on", "  1  ", "Yes"],
    )
    def test_dotenv_truthy_values_activate(self, clean_env, isolated_cwd, value):
        (isolated_cwd / ".env").write_text("K=v\n")
        clean_env.setenv("BALDUR_DOTENV", value)

        assert load_dotenv_if_requested(None) is not None

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "random"])
    def test_dotenv_falsy_values_skip(self, clean_env, isolated_cwd, value):
        (isolated_cwd / ".env").write_text("K=v\n")
        clean_env.setenv("BALDUR_DOTENV", value)

        assert load_dotenv_if_requested(None) is None


class TestApplyDotenvBehavior:
    """Minimal KEY=value parser - comments, quoting, invalid lines."""

    def test_parses_basic_key_value(self, clean_env, tmp_path):
        env = tmp_path / ".env"
        env.write_text("BALDUR_KEY_ONE=hello\nBALDUR_KEY_TWO=world\n")

        _config._apply_dotenv(env)

        assert os.environ["BALDUR_KEY_ONE"] == "hello"
        assert os.environ["BALDUR_KEY_TWO"] == "world"

    def test_strips_double_quotes_around_value(self, clean_env, tmp_path):
        env = tmp_path / ".env"
        env.write_text('BALDUR_Q="quoted"\n')
        _config._apply_dotenv(env)
        assert os.environ["BALDUR_Q"] == "quoted"

    def test_strips_single_quotes_around_value(self, clean_env, tmp_path):
        env = tmp_path / ".env"
        env.write_text("BALDUR_Q='squoted'\n")
        _config._apply_dotenv(env)
        assert os.environ["BALDUR_Q"] == "squoted"

    def test_unmatched_leading_quote_kept_verbatim(self, clean_env, tmp_path):
        """Unbalanced quote is preserved - minimal parser, no repair attempt."""
        env = tmp_path / ".env"
        env.write_text('BALDUR_Q="dangling\n')
        _config._apply_dotenv(env)
        assert os.environ["BALDUR_Q"] == '"dangling'

    def test_unmatched_trailing_quote_kept_verbatim(self, clean_env, tmp_path):
        env = tmp_path / ".env"
        env.write_text("BALDUR_Q=dangling'\n")
        _config._apply_dotenv(env)
        assert os.environ["BALDUR_Q"] == "dangling'"

    def test_mixed_quote_types_only_outer_pair_stripped(self, clean_env, tmp_path):
        """Only one matching pair is stripped; inner mismatched quotes stay."""
        env = tmp_path / ".env"
        env.write_text(""" BALDUR_Q="'inner'"\n""".strip() + "\n")
        _config._apply_dotenv(env)
        # Outer pair is `"..."`, strip leaves `'inner'` untouched.
        assert os.environ["BALDUR_Q"] == "'inner'"

    def test_empty_quoted_value_becomes_empty_string(self, clean_env, tmp_path):
        env = tmp_path / ".env"
        env.write_text('BALDUR_Q=""\n')
        _config._apply_dotenv(env)
        assert os.environ["BALDUR_Q"] == ""

    def test_skips_comments_and_blank_lines(self, clean_env, tmp_path):
        env = tmp_path / ".env"
        env.write_text("# comment line\n\nBALDUR_VALID=yes\n# another\n")
        _config._apply_dotenv(env)
        assert os.environ["BALDUR_VALID"] == "yes"

    def test_skips_malformed_lines_without_equals(self, clean_env, tmp_path):
        env = tmp_path / ".env"
        env.write_text("no_equals_here\nBALDUR_OK=1\n")
        _config._apply_dotenv(env)
        assert os.environ["BALDUR_OK"] == "1"

    def test_existing_env_preserved_over_dotenv(self, clean_env, tmp_path):
        """Process env wins - .env cannot overwrite a real shell export."""
        clean_env.setenv("BALDUR_PRE_SET", "shell_wins")
        env = tmp_path / ".env"
        env.write_text("BALDUR_PRE_SET=from_dotenv\n")

        _config._apply_dotenv(env)

        assert os.environ["BALDUR_PRE_SET"] == "shell_wins"
