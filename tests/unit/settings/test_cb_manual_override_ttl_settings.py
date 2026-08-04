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
        """Without this hop the env var would be dead on every path."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig

        reset_circuit_breaker_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_CB_MANUAL_OVERRIDE_TTL_MINUTES": "17"},
            clear=True,
        ):
            config = CircuitBreakerConfig.from_settings()

        reset_circuit_breaker_settings()
        assert config.manual_override_ttl_minutes == 17
