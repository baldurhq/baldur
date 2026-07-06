"""
Unit tests for make_settings_config helper and COMMON_SETTINGS_CONFIG.

Verification targets:
- COMMON_SETTINGS_CONFIG contract values (341 doc §3.3)
- make_settings_config() basic call, override, return type
- Data immutability of COMMON_SETTINGS_CONFIG after helper calls
- Idempotency of make_settings_config()
- Nested model env override via env_nested_delimiter propagation

Test subject: baldur.settings.base
"""

import os
from unittest import mock

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

from baldur.settings.base import COMMON_SETTINGS_CONFIG, make_settings_config


class TestCommonSettingsConfigContract:
    """COMMON_SETTINGS_CONFIG design contract verification.

    Validates values specified in 341 doc §3.3.
    """

    def test_env_file_is_none(self):
        """env_file must be None (no .env file loading)."""
        assert COMMON_SETTINGS_CONFIG["env_file"] is None

    def test_extra_is_ignore(self):
        """extra must be 'ignore' (unknown env vars are silently ignored)."""
        assert COMMON_SETTINGS_CONFIG["extra"] == "ignore"

    def test_validate_default_is_true(self):
        """validate_default must be True (all defaults are validated)."""
        assert COMMON_SETTINGS_CONFIG["validate_default"] is True

    def test_env_nested_delimiter_is_double_underscore(self):
        """env_nested_delimiter must be '__' (project standard for nested model env override)."""
        assert COMMON_SETTINGS_CONFIG["env_nested_delimiter"] == "__"

    def test_common_config_has_exactly_four_keys(self):
        """COMMON_SETTINGS_CONFIG defines exactly 4 common attributes."""
        expected_keys = {
            "env_file",
            "extra",
            "validate_default",
            "env_nested_delimiter",
        }
        actual_keys = set(COMMON_SETTINGS_CONFIG.keys())
        assert actual_keys == expected_keys


class TestMakeSettingsConfigContract:
    """make_settings_config() return value contract verification.

    Validates that the helper produces correct SettingsConfigDict values
    as specified in 341 doc §3.3.
    """

    def test_basic_call_includes_env_prefix(self):
        """make_settings_config() includes the given env_prefix."""
        result = make_settings_config("BALDUR_TEST_")
        assert result["env_prefix"] == "BALDUR_TEST_"

    def test_basic_call_includes_all_common_keys(self):
        """make_settings_config() result contains all COMMON_SETTINGS_CONFIG keys."""
        result = make_settings_config("BALDUR_TEST_")
        for key in COMMON_SETTINGS_CONFIG:
            assert key in result
            assert result[key] == COMMON_SETTINGS_CONFIG[key]

    def test_basic_call_has_five_keys(self):
        """make_settings_config() result has 4 common keys + env_prefix = 5 keys."""
        result = make_settings_config("BALDUR_TEST_")
        assert len(result) == 5


class TestMakeSettingsConfigBehavior:
    """make_settings_config() behavioral verification."""

    # === Override ===

    def test_override_replaces_common_value(self):
        """extra='forbid' override takes priority over common 'ignore'."""
        result = make_settings_config("BALDUR_STRICT_", extra="forbid")
        assert result["extra"] == "forbid"

    def test_override_preserves_non_overridden_keys(self):
        """Override only affects the specified key, not others."""
        result = make_settings_config("BALDUR_STRICT_", extra="forbid")
        assert result["env_file"] == COMMON_SETTINGS_CONFIG["env_file"]
        assert result["validate_default"] == COMMON_SETTINGS_CONFIG["validate_default"]
        assert (
            result["env_nested_delimiter"]
            == COMMON_SETTINGS_CONFIG["env_nested_delimiter"]
        )

    def test_multiple_overrides_all_applied(self):
        """Multiple overrides are all applied correctly."""
        result = make_settings_config(
            "BALDUR_CUSTOM_",
            extra="forbid",
            validate_default=False,
        )
        assert result["extra"] == "forbid"
        assert result["validate_default"] is False

    # === Data Immutability (§8.6) ===

    def test_call_does_not_mutate_common_config(self):
        """Calling make_settings_config does not modify COMMON_SETTINGS_CONFIG."""
        original_snapshot = dict(COMMON_SETTINGS_CONFIG)

        make_settings_config("BALDUR_TEST_", extra="forbid")

        assert dict(COMMON_SETTINGS_CONFIG) == original_snapshot

    # === Idempotency (§8.3) ===

    def test_same_args_produce_equal_result(self):
        """Identical calls produce equal results."""
        result1 = make_settings_config("BALDUR_FOO_")
        result2 = make_settings_config("BALDUR_FOO_")
        assert result1 == result2

    def test_same_args_with_override_produce_equal_result(self):
        """Identical calls with overrides produce equal results."""
        result1 = make_settings_config("BALDUR_FOO_", extra="forbid")
        result2 = make_settings_config("BALDUR_FOO_", extra="forbid")
        assert result1 == result2

    # === Real Settings Class Usage ===

    def test_result_usable_as_model_config(self):
        """make_settings_config result can be used as BaseSettings.model_config."""

        class DemoSettings(BaseSettings):
            model_config = make_settings_config("BALDUR_DEMO_")

            my_value: int = 42

        with mock.patch.dict(os.environ, {}, clear=True):
            settings = DemoSettings()
            assert settings.my_value == 42

    def test_env_prefix_applied_in_real_settings(self):
        """env_prefix from make_settings_config works for actual env var loading."""

        class DemoSettings(BaseSettings):
            model_config = make_settings_config("BALDUR_DEMO_")

            my_value: int = 42

        with mock.patch.dict(
            os.environ,
            {"BALDUR_DEMO_MY_VALUE": "99"},
            clear=True,
        ):
            settings = DemoSettings()
            assert settings.my_value == 99

    def test_env_nested_delimiter_propagated_to_real_settings(self):
        """env_nested_delimiter='__' from COMMON_SETTINGS_CONFIG enables nested env override."""

        class NestedModel(BaseModel):
            x: int = 1
            y: int = 2

        class DemoSettings(BaseSettings):
            model_config = make_settings_config("BALDUR_NESTED_")

            sub: NestedModel = Field(default_factory=NestedModel)

        # Given: env var using __ delimiter to target nested field
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_NESTED_SUB__X": "10",
                "BALDUR_NESTED_SUB__Y": "20",
            },
            clear=True,
        ):
            # When
            settings = DemoSettings()

            # Then
            assert settings.sub.x == 10
            assert settings.sub.y == 20

    def test_nested_env_override_partial_fields(self):
        """Partial nested env override only changes specified sub-fields."""

        class NestedModel(BaseModel):
            a: str = "default_a"
            b: str = "default_b"

        class DemoSettings(BaseSettings):
            model_config = make_settings_config("BALDUR_PARTIAL_")

            nested: NestedModel = Field(default_factory=NestedModel)

        # Given: only one sub-field overridden via env
        with mock.patch.dict(
            os.environ,
            {"BALDUR_PARTIAL_NESTED__A": "overridden"},
            clear=True,
        ):
            # When
            settings = DemoSettings()

            # Then
            assert settings.nested.a == "overridden"
            assert settings.nested.b == "default_b"
