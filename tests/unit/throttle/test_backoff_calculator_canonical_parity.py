"""Site-1 parity: ThrottleAwareBackoffCalculator.calculate() composes the canonical
ExponentialBackoff yet preserves the historical ``base ** attempt`` jitterless curve,
the int-seconds contract, and the min_delay clamp (D3 site 1)."""

from __future__ import annotations

import pytest

from baldur.services.backoff_calculator.calculator import ThrottleAwareBackoffCalculator
from baldur.services.backoff_calculator.models import BackoffConfig


def _calc(base=4, max_delay=180, jitter_percent=25, min_delay=1):
    cfg = BackoffConfig(
        base=base,
        max_delay=max_delay,
        jitter_percent=jitter_percent,
        min_delay=min_delay,
    )
    # enable_push_cache=False avoids EventBus subscription side effects.
    return ThrottleAwareBackoffCalculator(config=cfg, enable_push_cache=False)


class TestBackoffCalculatorCanonicalParity:
    @pytest.mark.parametrize(
        ("attempt", "expected"),
        [(1, 4), (2, 16), (3, 64), (4, 180)],  # 4**4=256 -> capped at 180
    )
    def test_jitterless_curve_is_base_power_attempt(self, attempt, expected):
        calc = _calc(base=4, max_delay=180)
        assert calc.calculate(attempt, with_jitter=False) == expected

    def test_min_delay_clamps_small_delays_up(self):
        calc = _calc(base=2, min_delay=10, max_delay=180)
        assert calc.calculate(1, with_jitter=False) == 10  # 2**1=2 -> 10

    def test_attempt_below_one_returns_min_delay(self):
        calc = _calc(min_delay=3)
        assert calc.calculate(0, with_jitter=False) == 3

    def test_result_is_int(self):
        calc = _calc()
        assert isinstance(calc.calculate(2, with_jitter=False), int)

    def test_jitter_stays_in_symmetric_band(self):
        calc = _calc(base=4, max_delay=1000, jitter_percent=25)
        # attempt 3 -> 64; +/-25% jitter -> [48, 80] after int truncation.
        values = {calc.calculate(3, with_jitter=True) for _ in range(200)}
        assert all(48 <= v <= 80 for v in values)
        assert len(values) > 1  # jitter actually varies the delay
