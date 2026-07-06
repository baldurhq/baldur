"""
Tests for Jitter utilities.
"""

import os
import time
from unittest.mock import patch

from baldur.utils.jitter import (
    JitterConfig,
    calculate_jitter,
    sleep_with_jitter,
    with_jitter,
)


class TestCalculateJitter:
    """Tests for calculate_jitter function."""

    def test_returns_value_within_range(self):
        """Jitter should be within min and max range."""
        for _ in range(100):
            jitter = calculate_jitter(max_delay_seconds=10.0, min_delay_seconds=5.0)
            assert 5.0 <= jitter <= 10.0

    def test_default_range_is_zero_to_sixty(self):
        """Default jitter range should be 0 to 60 seconds."""
        for _ in range(100):
            jitter = calculate_jitter()
            assert 0.0 <= jitter <= 60.0


class TestSleepWithJitter:
    """Tests for sleep_with_jitter function."""

    def test_actually_sleeps(self):
        """Should actually sleep for the calculated time."""
        start = time.monotonic()
        waited = sleep_with_jitter(max_delay_seconds=0.1, min_delay_seconds=0.05)
        elapsed = time.monotonic() - start

        assert 0.05 <= waited <= 0.1
        # Windows timer precision can cause slight undersleep, allow 50% margin
        # Windows timer resolution is ~15.6ms which can cause significant variance
        assert elapsed >= waited * 0.5


class TestWithJitterDecorator:
    """Tests for with_jitter decorator."""

    def test_adds_delay_to_function(self):
        """Decorated function should have added delay."""

        @with_jitter(max_delay_seconds=0.05, min_delay_seconds=0.01)
        def quick_function():
            return "done"

        start = time.monotonic()
        result = quick_function()
        elapsed = time.monotonic() - start

        assert result == "done"
        assert elapsed >= 0.01

    def test_preserves_function_return_value(self):
        """Decorator should preserve function's return value."""

        @with_jitter(max_delay_seconds=0.01)
        def return_value():
            return {"key": "value"}

        result = return_value()
        assert result == {"key": "value"}

    def test_preserves_function_arguments(self):
        """Decorator should pass arguments correctly."""

        @with_jitter(max_delay_seconds=0.01)
        def add_numbers(a, b):
            return a + b

        result = add_numbers(2, 3)
        assert result == 5


class TestJitterConfig:
    """Tests for JitterConfig class."""

    def test_default_values(self):
        """Default config should have sensible defaults."""
        config = JitterConfig()
        assert config.enabled is True
        assert config.max_delay_seconds == 60.0
        assert config.min_delay_seconds == 0.0

    def test_get_delay_returns_zero_when_disabled(self):
        """get_delay should return 0 when jitter is disabled."""
        config = JitterConfig(enabled=False)
        assert config.get_delay() == 0.0

    def test_get_delay_returns_value_in_range(self):
        """get_delay should return value within configured range."""
        config = JitterConfig(
            enabled=True,
            max_delay_seconds=10.0,
            min_delay_seconds=5.0,
        )
        for _ in range(100):
            delay = config.get_delay()
            assert 5.0 <= delay <= 10.0

    def test_from_env_loads_environment_variables(self):
        """from_settings should load config from environment (from_env deprecated)."""
        from baldur.settings.jitter import reset_jitter_settings

        with patch.dict(
            os.environ,
            {
                "BALDUR_JITTER_ENABLED": "false",
                "BALDUR_JITTER_MAX_DELAY_SECONDS": "30.0",
                "BALDUR_JITTER_MIN_DELAY_SECONDS": "5.0",
            },
        ):
            reset_jitter_settings()  # 싱글톤 리셋
            config = JitterConfig.from_settings()
            assert config.enabled is False
            assert config.max_delay_seconds == 30.0
            assert config.min_delay_seconds == 5.0
        reset_jitter_settings()  # 테스트 후 정리

    def test_sleep_respects_disabled_flag(self):
        """sleep should not wait when disabled."""
        config = JitterConfig(enabled=False)

        start = time.monotonic()
        delay = config.sleep()
        elapsed = time.monotonic() - start

        assert delay == 0.0
        assert elapsed < 0.01
