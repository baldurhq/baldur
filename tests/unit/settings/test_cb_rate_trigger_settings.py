"""Unit tests for the promoted circuit-breaker failure-rate settings (719 D7).

``failure_rate_threshold``, ``sliding_window_size`` and ``minimum_calls`` moved
from dataclass-only defaults reached through ``getattr`` fallbacks to real
``BALDUR_CB_*`` environment variables. These tests pin the defaults an operator
inherits, the bounds the field types enforce, and the cross-field validator that
warns — rather than raises — when ``minimum_calls`` puts the rate trigger out of
reach.

Verification techniques applied:
- Contract: design-doc defaults (50.0 / 100 / 10) and declared field presence
- Boundary analysis: ``Percentage`` 0-100 and ``LargeCount`` 1-1000 edges
- Side effects: the model validator emits a warning log and never raises
"""

from __future__ import annotations

import os
from unittest import mock

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from baldur.settings.circuit_breaker import (
    CircuitBreakerSettings,
    reset_circuit_breaker_settings,
)

RATE_TRIGGER_UNREACHABLE_EVENT = "settings.cb_rate_trigger_unreachable"


def _settings(**overrides) -> CircuitBreakerSettings:
    """Build settings from an empty environment plus explicit overrides."""
    reset_circuit_breaker_settings()
    with mock.patch.dict(os.environ, {}, clear=True):
        return CircuitBreakerSettings(**overrides)


# =============================================================================
# Contract — defaults and bounds an operator inherits
# =============================================================================


class TestCircuitBreakerSettingsContract:
    """The three promoted fields, their defaults, and their env vars.

    The defaults ship the rate trigger ON, so they are the semantics every
    install gets without configuring anything.
    """

    def test_failure_rate_threshold_default_is_50_percent(self):
        """failure_rate_threshold default: 50.0% (rate trigger enabled)."""
        assert _settings().failure_rate_threshold == 50.0

    def test_sliding_window_size_default_is_100(self):
        """sliding_window_size default: the last 100 CLOSED calls."""
        assert _settings().sliding_window_size == 100

    def test_minimum_calls_default_is_10(self):
        """minimum_calls default: 10 observations before the rate is trusted."""
        assert _settings().minimum_calls == 10

    def test_promoted_fields_are_declared_on_the_model(self):
        """All three are real settings fields, not getattr fallbacks."""
        fields = CircuitBreakerSettings.model_fields

        assert "failure_rate_threshold" in fields
        assert "sliding_window_size" in fields
        assert "minimum_calls" in fields

    @pytest.mark.parametrize(
        ("env_var", "raw", "attribute", "expected"),
        [
            (
                "BALDUR_CB_FAILURE_RATE_THRESHOLD",
                "0",
                "failure_rate_threshold",
                0.0,
            ),
            (
                "BALDUR_CB_FAILURE_RATE_THRESHOLD",
                "75.5",
                "failure_rate_threshold",
                75.5,
            ),
            ("BALDUR_CB_SLIDING_WINDOW_SIZE", "250", "sliding_window_size", 250),
            ("BALDUR_CB_MINIMUM_CALLS", "3", "minimum_calls", 3),
        ],
        ids=["rate_disabled", "rate_custom", "window_size", "minimum_calls"],
    )
    def test_env_var_overrides_the_default(self, env_var, raw, attribute, expected):
        """Each promoted field is reachable through its BALDUR_CB_ variable."""
        reset_circuit_breaker_settings()
        with mock.patch.dict(os.environ, {env_var: raw}, clear=True):
            assert getattr(CircuitBreakerSettings(), attribute) == expected

    @pytest.mark.parametrize(
        ("value", "accepted"),
        [(-0.1, False), (0.0, True), (100.0, True), (100.1, False)],
        ids=["below_min", "at_min", "at_max", "above_max"],
    )
    def test_failure_rate_threshold_percentage_bounds(self, value, accepted):
        """failure_rate_threshold is a Percentage: 0.0 <= value <= 100.0."""
        if accepted:
            assert _settings(failure_rate_threshold=value).failure_rate_threshold == (
                value
            )
        else:
            with pytest.raises(ValidationError):
                _settings(failure_rate_threshold=value)

    @pytest.mark.parametrize(
        ("field_name", "value", "accepted"),
        [
            ("sliding_window_size", 0, False),
            ("sliding_window_size", 1, True),
            ("sliding_window_size", 1000, True),
            ("sliding_window_size", 1001, False),
            ("minimum_calls", 0, False),
            ("minimum_calls", 1, True),
            ("minimum_calls", 1000, True),
            ("minimum_calls", 1001, False),
        ],
        ids=[
            "window_below_min",
            "window_at_min",
            "window_at_max",
            "window_above_max",
            "minimum_below_min",
            "minimum_at_min",
            "minimum_at_max",
            "minimum_above_max",
        ],
    )
    def test_count_fields_respect_large_count_bounds(self, field_name, value, accepted):
        """Both counts are LargeCount: 1 <= value <= 1000.

        The upper bound is load-bearing beyond input hygiene — it caps the
        failure-path ``sum(window)`` cost, which is why the window carries no
        incremental counters.
        """
        # sliding_window_size must stay >= minimum_calls' default to keep the
        # validator quiet; pin both so only the field under test varies.
        overrides = {"sliding_window_size": 1000, "minimum_calls": 1}
        overrides[field_name] = value

        if accepted:
            assert getattr(_settings(**overrides), field_name) == value
        else:
            with pytest.raises(ValidationError):
                _settings(**overrides)


# =============================================================================
# Behavior — the unreachable-rate-trigger validator (719 D7)
# =============================================================================


class TestCircuitBreakerSettingsWarningBehavior:
    """``minimum_calls > sliding_window_size`` warns instead of raising.

    The combination is inert, not invalid: it disables the rate trigger while
    the consecutive-failure trigger keeps protecting. Raising would turn a
    suboptimal setting into a boot failure, against graceful degradation.
    """

    def test_minimum_calls_above_window_size_emits_a_warning(self):
        """The out-of-reach combination is surfaced at load time."""
        with capture_logs() as logs:
            _settings(sliding_window_size=50, minimum_calls=100)

        warnings = [
            entry
            for entry in logs
            if entry.get("event") == RATE_TRIGGER_UNREACHABLE_EVENT
        ]
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"
        assert warnings[0]["minimum_calls"] == 100
        assert warnings[0]["sliding_window_size"] == 50

    def test_minimum_calls_above_window_size_does_not_raise(self):
        """Loading still succeeds — the settings object is usable."""
        settings = _settings(sliding_window_size=50, minimum_calls=100)

        assert settings.minimum_calls == 100
        assert settings.sliding_window_size == 50

    @pytest.mark.parametrize(
        ("window_size", "minimum_calls"),
        [(100, 10), (100, 100), (100, 99)],
        ids=["well_below", "at_boundary", "just_below"],
    )
    def test_reachable_combinations_stay_silent(self, window_size, minimum_calls):
        """At or below the window size the rate trigger is reachable — no warning.

        The equal case is the boundary that matters: a ``>`` to ``>=`` drift
        would warn on the perfectly valid "rate over the whole window" setup.
        """
        with capture_logs() as logs:
            _settings(sliding_window_size=window_size, minimum_calls=minimum_calls)

        assert not [
            entry
            for entry in logs
            if entry.get("event") == RATE_TRIGGER_UNREACHABLE_EVENT
        ]

    def test_warning_fires_for_env_supplied_values(self):
        """The validator runs on env-sourced settings, not only kwargs."""
        reset_circuit_breaker_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_CB_SLIDING_WINDOW_SIZE": "20",
                "BALDUR_CB_MINIMUM_CALLS": "40",
            },
            clear=True,
        ):
            with capture_logs() as logs:
                settings = CircuitBreakerSettings()

        assert settings.minimum_calls == 40
        assert [
            entry
            for entry in logs
            if entry.get("event") == RATE_TRIGGER_UNREACHABLE_EVENT
        ]
