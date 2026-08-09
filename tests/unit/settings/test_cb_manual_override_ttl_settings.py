"""741 D3 — the manual-override lifetime becomes a real setting.

``CircuitBreakerConfig`` had read ``manual_override_ttl_minutes`` through a
``getattr(..., 90)`` fallback for as long as the field did not exist on
``CircuitBreakerSettings``, so no ``BALDUR_CB_*`` variable could change the
lifetime of a manual Block. The field now exists, and
``MAX_MANUAL_OVERRIDE_TTL_MINUTES`` is the single source for its upper bound —
the control-API validator and the REST handler validator both import it rather
than restating a literal.

Verification techniques applied:
- Contract: the shipped default (90), the shipped bound (1440), the env var
- Boundary analysis: ``ge=1`` / ``le=MAX_MANUAL_OVERRIDE_TTL_MINUTES`` edges
- Dependency interaction: the resolved config reads the settings value
"""

from __future__ import annotations

import os
from unittest import mock

import pytest
from pydantic import ValidationError

from baldur.settings.circuit_breaker import (
    MAX_MANUAL_OVERRIDE_TTL_MINUTES,
    CircuitBreakerSettings,
    reset_circuit_breaker_settings,
)


def _settings(**overrides) -> CircuitBreakerSettings:
    """Build settings from an empty environment plus explicit overrides."""
    reset_circuit_breaker_settings()
    with mock.patch.dict(os.environ, {}, clear=True):
        return CircuitBreakerSettings(**overrides)


# =============================================================================
# Contract — what an operator inherits and what they may configure
# =============================================================================


class TestManualOverrideTTLSettingsContract:
    """The field, its default, its bound, and its environment variable."""

    def test_manual_override_ttl_minutes_default_is_90(self):
        """Unchanged from the literal it replaces — no install shifts."""
        assert _settings().manual_override_ttl_minutes == 90

    def test_manual_override_ttl_minutes_is_declared_on_the_model(self):
        """A real settings field, not a getattr fallback."""
        assert "manual_override_ttl_minutes" in CircuitBreakerSettings.model_fields

    def test_max_manual_override_ttl_minutes_is_a_day(self):
        """24h: the shift-handover boundary the dead-man's switch is sized to."""
        assert MAX_MANUAL_OVERRIDE_TTL_MINUTES == 1440

    def test_manual_override_ttl_minutes_reads_its_env_var(self):
        """``BALDUR_CB_MANUAL_OVERRIDE_TTL_MINUTES`` feeds the field."""
        reset_circuit_breaker_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_CB_MANUAL_OVERRIDE_TTL_MINUTES": "30"},
            clear=True,
        ):
            assert CircuitBreakerSettings().manual_override_ttl_minutes == 30

    @pytest.mark.parametrize(
        ("value", "accepted"),
        [
            (0, False),
            (1, True),
            (MAX_MANUAL_OVERRIDE_TTL_MINUTES, True),
            (MAX_MANUAL_OVERRIDE_TTL_MINUTES + 1, False),
        ],
        ids=["below_min", "at_min", "at_max", "above_max"],
    )
    def test_manual_override_ttl_minutes_bounds(self, value, accepted):
        """The field's own bound is the same constant the API validators use.

        A non-expiring pin is unreachable by construction: zero is refused
        here as it is at every other surface.
        """
        if accepted:
            assert _settings(manual_override_ttl_minutes=value) is not None
        else:
            with pytest.raises(ValidationError):
                _settings(manual_override_ttl_minutes=value)


# =============================================================================
# Behavior — the resolved config carries the configured value
# =============================================================================


class TestManualOverrideTTLConfigResolution:
    """The setting is what force_open / force_close resolve ``None`` to."""

    def test_resolved_config_reads_the_settings_value(self):
        """Without this hop the env var would be dead on the tier-absent path.

        ``from_settings`` prefers the runtime-config manager when one is
        registered and only falls through to the settings hop when it is not,
        so the manager slot is neutralised here. Otherwise this asserts the
        stored blob's defaults rather than the environment, and the variable
        this suite covers is the documented tier-absent surface.

        The reset belongs *inside* the patched environment. It cascades into an
        eager rebuild, which refills the settings cache it has just cleared,
        from whichever environment is in force at that moment. Reset outside
        and that refill happens a statement before the fake environment is
        installed, so the assertion reads the shipped default and the hop this
        test exists for never runs. Which environment refills it also depends
        on whether a runtime-config manager is registered, so the same
        placement passes with the private tier installed and fails without it.
        """
        from baldur.factory.registry import ProviderRegistry
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig

        with (
            mock.patch.object(
                ProviderRegistry.runtime_config_manager, "safe_get", return_value=None
            ),
            mock.patch.dict(
                os.environ,
                {"BALDUR_CB_MANUAL_OVERRIDE_TTL_MINUTES": "17"},
                clear=True,
            ),
        ):
            reset_circuit_breaker_settings()
            config = CircuitBreakerConfig.from_settings()

        reset_circuit_breaker_settings()
        assert config.manual_override_ttl_minutes == 17


# =============================================================================
# Contract — the dict-merge write path enforces the same bound
# =============================================================================


class TestManualOverrideTTLRuntimeConfigGuardContract:
    """The runtime-config write path refuses what the Field bound refuses.

    PRO runtime-config edits merge plain dicts through
    ``is_valid_value`` — the Pydantic ``ge/le`` never runs there. Without a
    ``VALIDATION_RULES`` row, a console edit could store ``0``, and the
    blank-TTL default would then mint a pin with no expiry — one that the
    sweep skips and only a Reset lifts (741 verify, refutation pass).
    """

    def test_validation_rules_row_matches_the_settings_bound(self):
        """One bound, one source — the rule row must track the constant."""
        from baldur.core.safe_defaults import VALIDATION_RULES

        assert VALIDATION_RULES["circuit_breaker"]["manual_override_ttl_minutes"] == (
            1,
            MAX_MANUAL_OVERRIDE_TTL_MINUTES,
        )

    @pytest.mark.parametrize(
        ("value", "accepted"),
        [(0, False), (-5, False), (1, True), (90, True), (1440, True), (100000, False)],
        ids=["zero", "negative", "at_min", "default", "at_max", "way_above"],
    )
    def test_is_valid_value_enforces_the_bound(self, value, accepted):
        """The gate the PRO ``_apply_kwargs`` merge actually consults."""
        from baldur.core.safe_defaults import is_valid_value

        assert (
            is_valid_value("circuit_breaker", "manual_override_ttl_minutes", value)
            is accepted
        )
