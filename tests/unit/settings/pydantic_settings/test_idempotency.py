"""
Tests for IdempotencySettings.
"""

import pytest
from pydantic import ValidationError


class TestIdempotencySettings:
    """Tests for IdempotencySettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.idempotency import reset_idempotency_settings

        reset_idempotency_settings()
        yield
        reset_idempotency_settings()

    def test_default_values(self):
        """Defaults match core/config.py:IdempotencyConfig."""
        from baldur.settings.idempotency import IdempotencySettings

        settings = IdempotencySettings()

        assert settings.default_cache_ttl == 60
        assert settings.extended_cache_ttl == 300
        assert settings.clock_skew_tolerance_seconds == 5.0

    def test_env_override(self, monkeypatch):
        """Values can be overridden via environment variables."""
        from baldur.settings.idempotency import IdempotencySettings

        monkeypatch.setenv("BALDUR_IDEMPOTENCY_DEFAULT_CACHE_TTL", "120")

        settings = IdempotencySettings()

        assert settings.default_cache_ttl == 120

    def test_validation_cache_ttl_range(self):
        """default_cache_ttl range (1-3600) validation."""
        from baldur.settings.idempotency import IdempotencySettings

        with pytest.raises(ValidationError):
            IdempotencySettings(default_cache_ttl=0)

        with pytest.raises(ValidationError):
            IdempotencySettings(default_cache_ttl=3601)

    def test_singleton_pattern(self):
        """The singleton pattern returns the same cached instance."""
        from baldur.settings.idempotency import get_idempotency_settings

        settings1 = get_idempotency_settings()
        settings2 = get_idempotency_settings()

        assert settings1 is settings2


class TestIdempotencySettingsContract:
    """461 D3: ``allow_inmemory_fallback`` field design contract.

    Per UNIT_TEST_GUIDELINES §0.1, contract tests (verifying values from the
    design spec) live in ``Test*Contract`` classes.
    """

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        from baldur.settings.idempotency import reset_idempotency_settings

        reset_idempotency_settings()
        yield
        reset_idempotency_settings()

    def test_allow_inmemory_fallback_default_is_false(self):
        """461 D3: defaults to False so production fails-closed by default
        when no cache adapter is registered."""
        from baldur.settings.idempotency import IdempotencySettings

        settings = IdempotencySettings()
        assert settings.allow_inmemory_fallback is False

    @pytest.mark.parametrize(
        ("env_value", "expected"),
        [
            ("true", True),
            ("True", True),
            ("1", True),
            ("false", False),
            ("False", False),
            ("0", False),
        ],
        ids=[
            "true_lower",
            "true_title",
            "one",
            "false_lower",
            "false_title",
            "zero",
        ],
    )
    def test_allow_inmemory_fallback_env_var_coercion(
        self, monkeypatch, env_value, expected
    ):
        """461 D3: BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK coerces the
        standard Pydantic bool literals to the field value."""
        from baldur.settings.idempotency import IdempotencySettings

        monkeypatch.setenv("BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK", env_value)
        settings = IdempotencySettings()
        assert settings.allow_inmemory_fallback is expected

    def test_fail_open_on_cache_error_default_is_false(self):
        """567 D9: defaults to False so a cache I/O error during an enabled,
        explicitly-requested idempotency check fails CLOSED — a transient blip
        cannot let a duplicate side effect through."""
        from baldur.settings.idempotency import IdempotencySettings

        settings = IdempotencySettings()
        assert settings.fail_open_on_cache_error is False

    @pytest.mark.parametrize(
        ("env_value", "expected"),
        [
            ("true", True),
            ("True", True),
            ("1", True),
            ("false", False),
            ("False", False),
            ("0", False),
        ],
        ids=[
            "true_lower",
            "true_title",
            "one",
            "false_lower",
            "false_title",
            "zero",
        ],
    )
    def test_fail_open_on_cache_error_env_var_coercion(
        self, monkeypatch, env_value, expected
    ):
        """567 D9: BALDUR_IDEMPOTENCY_FAIL_OPEN_ON_CACHE_ERROR coerces the
        standard Pydantic bool literals to the field value."""
        from baldur.settings.idempotency import IdempotencySettings

        monkeypatch.setenv("BALDUR_IDEMPOTENCY_FAIL_OPEN_ON_CACHE_ERROR", env_value)
        settings = IdempotencySettings()
        assert settings.fail_open_on_cache_error is expected

    def test_gate_memory_ttl_default_is_1800(self):
        """595 D5: the gate dedup memory window defaults to 1800 s (30 min),
        matching IDEMPOTENCY_DEFAULT_TTL_SECONDS."""
        from baldur.settings.idempotency import IdempotencySettings

        settings = IdempotencySettings()
        assert settings.gate_memory_ttl_seconds == 1800  # design contract

    @pytest.mark.parametrize(
        ("value", "should_pass"),
        [
            (59, False),  # below ge=60
            (60, True),  # at ge=60
            (86400, True),  # at le=86400
            (86401, False),  # above le=86400
        ],
        ids=["below_min", "at_min", "at_max", "above_max"],
    )
    def test_gate_memory_ttl_boundary_contract(self, value, should_pass):
        """595 D5: gate_memory_ttl_seconds bounds ge=60 / le=86400."""
        from baldur.settings.idempotency import IdempotencySettings

        if should_pass:
            settings = IdempotencySettings(gate_memory_ttl_seconds=value)
            assert settings.gate_memory_ttl_seconds == value
        else:
            with pytest.raises(ValidationError):
                IdempotencySettings(gate_memory_ttl_seconds=value)

    def test_gate_memory_ttl_env_var_override(self, monkeypatch):
        """595 D5: BALDUR_IDEMPOTENCY_GATE_MEMORY_TTL_SECONDS tunes the field."""
        from baldur.settings.idempotency import IdempotencySettings

        monkeypatch.setenv("BALDUR_IDEMPOTENCY_GATE_MEMORY_TTL_SECONDS", "7200")
        settings = IdempotencySettings()
        assert settings.gate_memory_ttl_seconds == 7200
