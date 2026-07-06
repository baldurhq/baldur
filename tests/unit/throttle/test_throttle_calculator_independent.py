"""
ThrottleAwareBackoffCalculator independence unit tests.

Test target: services/backoff_calculator/calculator.py
- Independent calculate() (no BackoffCalculator base class)
- Boundary: attempt < 1 returns min_delay
- Max delay cap
- Jitter application
- get_delays_sequence()

Reference:
    docs/baldur/middleware_system/310_FUNCTIONAL_DUPLICATION_ELIMINATION.md §3.4.2
"""

from __future__ import annotations

from baldur.services.backoff_calculator.calculator import (
    ThrottleAwareBackoffCalculator,
)
from baldur.services.backoff_calculator.models import BackoffConfig

# =============================================================================
# Contract Tests
# =============================================================================


class TestThrottleAwareCalculatorIndependenceContract:
    """ThrottleAwareBackoffCalculator is independent of BackoffCalculator."""

    def test_no_backoff_calculator_in_mro(self):
        """ThrottleAwareBackoffCalculator does not inherit from BackoffCalculator."""
        mro_names = [cls.__name__ for cls in ThrottleAwareBackoffCalculator.__mro__]
        assert "BackoffCalculator" not in mro_names

    def test_has_calculate_method(self):
        """ThrottleAwareBackoffCalculator has its own calculate() method."""
        assert hasattr(ThrottleAwareBackoffCalculator, "calculate")
        # Verify it's defined directly on the class, not inherited
        assert "calculate" in ThrottleAwareBackoffCalculator.__dict__

    def test_backoff_multipliers_contract_values(self):
        """BACKOFF_MULTIPLIERS contains expected multiplier mapping."""
        m = ThrottleAwareBackoffCalculator.BACKOFF_MULTIPLIERS
        assert m["normal"] == 1.0
        assert m["sla_warning"] == 1.5
        assert m["sla_critical"] == 2.0
        assert m["emergency_1_2"] == 2.5
        assert m["emergency_3"] == 4.0
        assert m["error_budget_critical"] == 3.0


# =============================================================================
# Behavior Tests — calculate()
# =============================================================================


class TestThrottleAwareCalculatorCalculateBehavior:
    """ThrottleAwareBackoffCalculator.calculate() behavior verification."""

    def _make_calculator(self, **config_overrides) -> ThrottleAwareBackoffCalculator:
        """Create calculator with custom config, no event subscriptions."""
        config = BackoffConfig(**config_overrides)
        return ThrottleAwareBackoffCalculator(
            config=config,
            enable_push_cache=False,
            error_budget_check_enabled=False,
        )

    def test_attempt_zero_returns_min_delay(self):
        """attempt < 1 returns config.min_delay."""
        calc = self._make_calculator(min_delay=2)
        assert calc.calculate(0) == 2

    def test_attempt_negative_returns_min_delay(self):
        """Negative attempt returns config.min_delay."""
        calc = self._make_calculator(min_delay=1)
        assert calc.calculate(-5) == 1

    def test_exponential_growth_without_jitter(self):
        """Delay grows exponentially: base^attempt (no jitter)."""
        calc = self._make_calculator(base=4, max_delay=10000, jitter_percent=0)
        assert calc.calculate(1, with_jitter=False) == 4  # 4^1
        assert calc.calculate(2, with_jitter=False) == 16  # 4^2
        assert calc.calculate(3, with_jitter=False) == 64  # 4^3

    def test_max_delay_caps_result(self):
        """Delay is capped at config.max_delay."""
        calc = self._make_calculator(base=4, max_delay=50, jitter_percent=0)
        result = calc.calculate(3, with_jitter=False)  # 4^3=64 > 50
        assert result == 50

    def test_min_delay_floor(self):
        """Result is never below config.min_delay."""
        calc = self._make_calculator(base=4, min_delay=5, jitter_percent=0)
        result = calc.calculate(1, with_jitter=False)  # 4^1=4 < 5
        assert result >= 5

    def test_jitter_changes_result(self):
        """With jitter enabled, result varies from base calculation."""
        calc = self._make_calculator(base=4, max_delay=10000, jitter_percent=25)

        # Run multiple times; with 25% jitter, at least one should differ
        results = {calc.calculate(2, with_jitter=True) for _ in range(20)}
        assert len(results) > 1, "Jitter should produce varying results"

    def test_jitter_disabled_produces_deterministic_result(self):
        """With jitter_percent=0 and with_jitter=False, result is deterministic."""
        calc = self._make_calculator(base=4, max_delay=10000, jitter_percent=0)
        results = {calc.calculate(2, with_jitter=False) for _ in range(10)}
        assert len(results) == 1

    def test_get_delays_sequence_length(self):
        """get_delays_sequence returns correct number of delays."""
        calc = self._make_calculator(base=4, max_delay=10000, jitter_percent=0)
        seq = calc.get_delays_sequence(5, with_jitter=False)
        assert len(seq) == 5

    def test_get_delays_sequence_monotonic_without_jitter(self):
        """Without jitter, delay sequence is non-decreasing."""
        calc = self._make_calculator(base=2, max_delay=10000, jitter_percent=0)
        seq = calc.get_delays_sequence(5, with_jitter=False)
        for i in range(1, len(seq)):
            assert seq[i] >= seq[i - 1]
