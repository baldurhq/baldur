"""Unit tests for the circuit-breaker config admission clamp (744 D16/D17).

``from_settings()`` is the single admission point for every circuit-breaker
consumer. A value stored through the runtime-config write path runs no Pydantic
validator, so a window or count field can hold a ``0`` — or an unreachably large
number — that silently disables a protection trigger on every worker that builds
a config from it. The clamp is the last gate before such a value reaches the
protection logic.

The bounds it applies are DERIVED from ``CircuitBreakerSettings``' own field
declarations, so the tests below re-derive them from the same declarations
(walking the ``annotated_types`` metadata independently) rather than restating a
hand-written table: a field that gains or loses a bound is covered without a
test edit, and no authored list can drift.

Targets:
  - ``CircuitBreakerConfig.from_settings`` through BOTH source branches — the
    runtime-config-manager branch and the static-settings fallback — so the two
    paths are proven to admit identically.
  - the ``circuit_breaker.config_value_clamped`` WARNING and the report-only
    ``circuit_breaker.config_rate_trigger_unreachable`` WARNING.

Verification techniques (§8):
  - Boundary analysis — below ``ge`` / at ``ge`` / inside / at ``le`` / above ``le``
  - Side effects — the clamp is reported, not silent
  - Exception/edge — a stored ``0`` never reaches a divisor or a ring length
"""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest.mock import patch

import pytest

from baldur.services.circuit_breaker import config as config_module
from baldur.services.circuit_breaker.config import (
    CircuitBreakerConfig,
    reset_circuit_breaker_config,
)

# =============================================================================
# Derived row generation
# =============================================================================


def _mapped_bounded_fields() -> list[Any]:
    """Every ``CircuitBreakerConfig`` field whose settings field declares both
    an inclusive lower and upper bound, re-derived from the settings model."""
    from annotated_types import Ge, Le

    from baldur.settings.circuit_breaker import CircuitBreakerSettings

    mapped = {f.name for f in dataclasses.fields(CircuitBreakerConfig)}
    rows = []
    for name, field in CircuitBreakerSettings.model_fields.items():
        if name not in mapped:
            continue
        lower = upper = None
        for constraint in field.metadata:
            if isinstance(constraint, Ge):
                lower = constraint.ge
            elif isinstance(constraint, Le):
                upper = constraint.le
        if lower is None or upper is None:
            continue
        rows.append(pytest.param(name, lower, upper, field.annotation, id=name))
    return rows


_BOUNDED_FIELDS = _mapped_bounded_fields()

# Both operator-writable sources funnel through the same admission point; every
# boundary row runs through each of them.
_SOURCES = ["runtime_manager", "static_settings"]


class _StubRuntimeConfigManager:
    """Minimal stand-in for the PRO runtime-config manager.

    Only the one method ``from_settings`` calls is implemented, so a rename on
    the real manager surfaces as a failure here rather than as an
    auto-generated attribute that quietly answers.
    """

    def __init__(self, stored: dict[str, Any]) -> None:
        self._stored = stored

    def get_circuit_breaker_config(self) -> dict[str, Any]:
        return dict(self._stored)


def _build_config(source: str, stored: dict[str, Any]) -> CircuitBreakerConfig:
    """Build a config from ``stored`` through one of the two source branches."""
    from baldur.factory.registry import ProviderRegistry

    if source == "runtime_manager":
        with patch.object(
            ProviderRegistry.runtime_config_manager,
            "safe_get",
            return_value=_StubRuntimeConfigManager(stored),
        ):
            return CircuitBreakerConfig.from_settings()

    from baldur.settings.circuit_breaker import CircuitBreakerSettings
    from baldur.settings.root import get_config

    settings = CircuitBreakerSettings()
    # Written past the model's validators on purpose: the runtime-config write
    # path does exactly that, and reproducing it is the only way to place an
    # out-of-range value in front of the static branch.
    settings.__dict__.update(stored)
    get_config().core.__dict__["circuit_breaker"] = settings

    with patch.object(
        ProviderRegistry.runtime_config_manager, "safe_get", return_value=None
    ):
        return CircuitBreakerConfig.from_settings()


def _coerce(value: Any, python_type: Any) -> Any:
    """Cast a probe value to the field's declared numeric type."""
    return int(value) if python_type is int else float(value)


@pytest.fixture(autouse=True)
def _isolate_settings_and_clamp_log():
    """Reset the settings cache and the once-per-triple clamp-warning memo."""
    from baldur.settings.circuit_breaker import reset_circuit_breaker_settings
    from baldur.settings.root import reset_config

    reset_circuit_breaker_settings()
    reset_config()
    reset_circuit_breaker_config()
    yield
    reset_circuit_breaker_settings()
    reset_config()
    reset_circuit_breaker_config()


# =============================================================================
# Admission clamp (Behavior)
# =============================================================================


class TestCircuitBreakerConfigAdmissionBehavior:
    """Every bounded numeric field is admitted into its declared range, from
    either source, and the clamp is reported rather than silent."""

    @pytest.mark.parametrize("source", _SOURCES)
    @pytest.mark.parametrize(
        ("field", "lower", "upper", "python_type"), _BOUNDED_FIELDS
    )
    def test_value_below_the_declared_lower_bound_is_clamped_up(
        self, source, field, lower, upper, python_type
    ):
        stored = _coerce(lower - 1, python_type)

        config = _build_config(source, {field: stored})

        assert getattr(config, field) == _coerce(lower, python_type)

    @pytest.mark.parametrize("source", _SOURCES)
    @pytest.mark.parametrize(
        ("field", "lower", "upper", "python_type"), _BOUNDED_FIELDS
    )
    def test_value_at_the_declared_lower_bound_is_admitted_unchanged(
        self, source, field, lower, upper, python_type
    ):
        """The bound itself is legal — the clamp must not narrow the range the
        environment already accepts."""
        stored = _coerce(lower, python_type)

        config = _build_config(source, {field: stored})

        assert getattr(config, field) == stored

    @pytest.mark.parametrize("source", _SOURCES)
    @pytest.mark.parametrize(
        ("field", "lower", "upper", "python_type"), _BOUNDED_FIELDS
    )
    def test_value_inside_the_declared_range_is_admitted_unchanged(
        self, source, field, lower, upper, python_type
    ):
        stored = _coerce((lower + upper) / 2, python_type)

        config = _build_config(source, {field: stored})

        assert getattr(config, field) == stored

    @pytest.mark.parametrize("source", _SOURCES)
    @pytest.mark.parametrize(
        ("field", "lower", "upper", "python_type"), _BOUNDED_FIELDS
    )
    def test_value_at_the_declared_upper_bound_is_admitted_unchanged(
        self, source, field, lower, upper, python_type
    ):
        stored = _coerce(upper, python_type)

        config = _build_config(source, {field: stored})

        assert getattr(config, field) == stored

    @pytest.mark.parametrize("source", _SOURCES)
    @pytest.mark.parametrize(
        ("field", "lower", "upper", "python_type"), _BOUNDED_FIELDS
    )
    def test_value_above_the_declared_upper_bound_is_clamped_down(
        self, source, field, lower, upper, python_type
    ):
        stored = _coerce(upper + 1, python_type)

        config = _build_config(source, {field: stored})

        assert getattr(config, field) == _coerce(upper, python_type)

    @pytest.mark.parametrize("source", _SOURCES)
    def test_both_sources_admit_an_out_of_range_value_identically(self, source):
        """The two branches are separate call sites; the whole point of routing
        them through one admission point is that they cannot disagree."""
        stored = {"sliding_window_size": 0, "minimum_calls": 999_999}

        config = _build_config(source, stored)

        assert config.sliding_window_size == 1
        assert config.minimum_calls == 1000

    # -------------------------------------------------------------------------
    # Documented-disable and unreachable-value rows
    # -------------------------------------------------------------------------

    @pytest.mark.parametrize("source", _SOURCES)
    def test_failure_rate_threshold_zero_survives_untouched(self, source):
        """``0`` is the documented way to disable the failure-rate trigger, and
        it is the declared lower bound — the clamp must not take it away."""
        config = _build_config(source, {"failure_rate_threshold": 0})

        assert config.failure_rate_threshold == 0.0

    @pytest.mark.parametrize("source", _SOURCES)
    def test_unreachably_large_minimum_calls_is_capped(self, source):
        """An unbounded ``minimum_calls`` puts the failure-rate trigger out of
        reach process-wide; the cap keeps it evaluable."""
        config = _build_config(source, {"minimum_calls": 1_000_000})

        assert config.minimum_calls == 1000

    @pytest.mark.parametrize("source", _SOURCES)
    def test_self_ddos_window_zero_is_floored_so_no_divisor_is_zero(self, source):
        """The self-DDoS check divides by the window; a stored ``0`` would raise
        ``ZeroDivisionError`` on the admission path of every worker."""
        config = _build_config(source, {"self_ddos_window_seconds": 0})

        assert config.self_ddos_window_seconds == 1
        # The value is safe to divide by, which is the property that matters.
        assert config.self_ddos_rps_limit / config.self_ddos_window_seconds > 0

    @pytest.mark.parametrize("source", _SOURCES)
    def test_field_declaring_no_bound_is_passed_through_unchanged(self, source):
        """Guards the derivation itself: the clamp applies declared bounds and
        invents none, so an unbounded field keeps whatever was stored."""
        assert "fallback_cache_ttl_seconds" not in config_module._declared_bounds()

        config = _build_config(source, {"fallback_cache_ttl_seconds": 9999})

        assert config.fallback_cache_ttl_seconds == 9999

    # -------------------------------------------------------------------------
    # End-to-end: the admitted value is the one protection runs on
    # -------------------------------------------------------------------------

    def test_stored_zero_window_never_produces_a_zero_length_outcome_ring(self):
        """A ``deque(maxlen=0)`` discards every outcome as it is appended, so
        the failure-rate trigger would see permanent "no evidence"."""
        from baldur.services.circuit_breaker.outcome_window import OutcomeWindow

        config = _build_config("runtime_manager", {"sliding_window_size": 0})
        window = OutcomeWindow()

        window.record_failure("payments", config.sliding_window_size)

        assert window.read("payments") == (1, 1)

    def test_stored_zero_cascade_window_leaves_the_429_check_reachable(self):
        """The cascade check counts 429s over the configured window; a stored
        ``0`` makes every count zero and the condition unreachable."""
        from baldur.core.rate_limiting import SlidingWindowCounter

        config = _build_config(
            "runtime_manager", {"rate_limit_cascade_window_seconds": 0}
        )

        now = [1000.0]
        counter = SlidingWindowCounter(clock=lambda: now[0])
        counter.record("payments")
        now[0] += 0.5

        assert config.rate_limit_cascade_window_seconds == 1
        assert counter.count("payments", config.rate_limit_cascade_window_seconds) == 1

    # -------------------------------------------------------------------------
    # Reporting
    # -------------------------------------------------------------------------

    def test_clamped_value_is_reported_with_the_stored_and_applied_values(self):
        """A silent clamp is a configuration lie: the console still shows the
        stored value, so the log is the only place the difference appears."""
        with patch.object(config_module, "logger") as mock_logger:
            _build_config("runtime_manager", {"sliding_window_size": 0})

        events = [call.args[0] for call in mock_logger.warning.call_args_list]
        assert "circuit_breaker.config_value_clamped" in events

        clamp_call = next(
            call
            for call in mock_logger.warning.call_args_list
            if call.args[0] == "circuit_breaker.config_value_clamped"
        )
        assert clamp_call.kwargs["field"] == "sliding_window_size"
        assert clamp_call.kwargs["stored_value"] == 0
        assert clamp_call.kwargs["applied_value"] == 1

    def test_in_range_value_is_not_reported(self):
        with patch.object(config_module, "logger") as mock_logger:
            _build_config("runtime_manager", {"sliding_window_size": 50})

        events = [call.args[0] for call in mock_logger.warning.call_args_list]
        assert "circuit_breaker.config_value_clamped" not in events

    def test_repeated_rebuilds_report_one_clamp_per_distinct_triple(self):
        """A holder rebuilt on every settings reset must not turn one bad
        stored value into an unbounded log stream."""
        with patch.object(config_module, "logger") as mock_logger:
            _build_config("runtime_manager", {"sliding_window_size": 0})
            _build_config("runtime_manager", {"sliding_window_size": 0})

        clamps = [
            call
            for call in mock_logger.warning.call_args_list
            if call.args[0] == "circuit_breaker.config_value_clamped"
        ]
        assert len(clamps) == 1

    def test_minimum_calls_above_window_size_is_reported_as_unreachable(self):
        """Report-only: both values are individually in range, but together the
        window can never hold enough calls to evaluate the rate trigger."""
        with patch.object(config_module, "logger") as mock_logger:
            config = _build_config(
                "runtime_manager", {"minimum_calls": 900, "sliding_window_size": 100}
            )

        events = [call.args[0] for call in mock_logger.warning.call_args_list]
        assert "circuit_breaker.config_rate_trigger_unreachable" in events
        # Report-only — neither value is altered.
        assert config.minimum_calls == 900
        assert config.sliding_window_size == 100

    def test_minimum_calls_within_window_size_is_not_reported(self):
        with patch.object(config_module, "logger") as mock_logger:
            _build_config(
                "runtime_manager", {"minimum_calls": 10, "sliding_window_size": 100}
            )

        events = [call.args[0] for call in mock_logger.warning.call_args_list]
        assert "circuit_breaker.config_rate_trigger_unreachable" not in events


# =============================================================================
# Config dataclass defaults (Contract)
# =============================================================================


class TestCircuitBreakerConfigContract:
    """Values a design document states, asserted literally."""

    def test_half_open_stuck_timeout_seconds_default(self):
        """Rerouted through the config so the value the breaker reads is the one
        the console advertises; it must match the settings default."""
        assert CircuitBreakerConfig().half_open_stuck_timeout_seconds == 60

    def test_half_open_stuck_timeout_default_matches_the_settings_default(self):
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        assert (
            CircuitBreakerConfig().half_open_stuck_timeout_seconds
            == CircuitBreakerSettings().half_open_stuck_timeout_seconds
        )

    @pytest.mark.parametrize(
        ("field", "lower", "upper"),
        [
            ("sliding_window_size", 1, 1000),
            ("minimum_calls", 1, 1000),
            ("failure_rate_threshold", 0.0, 100.0),
            ("half_open_stuck_timeout_seconds", 1, 3600),
            ("self_ddos_window_seconds", 1, 300),
        ],
    )
    def test_declared_admission_bounds(self, field, lower, upper):
        """The ranges the clamp admits into, pinned as the spec states them —
        a silent widening or narrowing of any of them changes what a stored
        value can do to protection."""
        bounds = config_module._declared_bounds()

        assert bounds[field][0] == lower
        assert bounds[field][1] == upper

    def test_failure_rate_threshold_lower_bound_is_zero_not_one(self):
        """``0`` disables the rate trigger by design; a lower bound of ``1``
        would silently re-enable it on every deployment that disabled it."""
        assert config_module._declared_bounds()["failure_rate_threshold"][0] == 0.0
