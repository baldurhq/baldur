"""Unit tests for testapp Celery tasks.

Tests task functions directly via .run() to bypass Celery broker.
No Django or Celery infrastructure required.

Verification techniques applied:
- Contract: return value structure for success tasks
- Behavior: exception raising for failure tasks
- Boundary: failure_rate thresholds (0.0, 1.0, intermediate)
- Side effect: time.sleep call in slow_task (via mock_sleep)
"""

from __future__ import annotations

import pytest

from tests.factories.time_helpers import mock_sleep
from tests.testapp.tasks import (
    DEFAULT_SLOW_TASK_DELAY,
    SLOW_TASK_SOFT_TIME_LIMIT,
    always_failing_task,
    deterministic_failing_task,
    dummy_order_task,
    dummy_payment_task,
    slow_task,
)

# ============================================================
# Contract: Success task return values
# ============================================================


class TestDummyPaymentTaskContract:
    """dummy_payment_task return value contract."""

    def test_returns_order_id_and_processed_status(self):
        """Returns dict with order_id and status='processed'."""
        result = dummy_payment_task.run(order_id=42)

        assert result == {"order_id": 42, "status": "processed"}

    def test_preserves_order_id_value(self):
        """Returned order_id matches the input argument."""
        result = dummy_payment_task.run(order_id=999)

        assert result["order_id"] == 999


class TestDummyOrderTaskContract:
    """dummy_order_task return value contract."""

    def test_returns_order_id_and_completed_status(self):
        """Returns dict with order_id and status='completed'."""
        result = dummy_order_task.run(order_id=7)

        assert result == {"order_id": 7, "status": "completed"}


# ============================================================
# Behavior: Failure tasks
# ============================================================


class TestAlwaysFailingTaskBehavior:
    """always_failing_task exception behavior."""

    def test_raises_runtime_error(self):
        """Always raises RuntimeError regardless of input."""
        with pytest.raises(RuntimeError):
            always_failing_task.run(order_id=1)

    def test_error_message_contains_order_id(self):
        """Error message includes the order_id for debugging."""
        with pytest.raises(RuntimeError, match="order 123"):
            always_failing_task.run(order_id=123)


class TestDeterministicFailingTaskBehavior:
    """deterministic_failing_task failure rate behavior."""

    def test_failure_rate_one_always_raises(self):
        """failure_rate=1.0 raises RuntimeError on every call."""
        for _ in range(5):
            with pytest.raises(RuntimeError):
                deterministic_failing_task.run(order_id=1, failure_rate=1.0)

    def test_failure_rate_zero_always_succeeds(self):
        """failure_rate=0.0 returns success on every call."""
        for _ in range(5):
            result = deterministic_failing_task.run(order_id=1, failure_rate=0.0)
            assert result["status"] == "processed"

    def test_failure_rate_zero_returns_order_id(self):
        """failure_rate=0.0 includes order_id in response."""
        result = deterministic_failing_task.run(order_id=55, failure_rate=0.0)

        assert result["order_id"] == 55

    def test_default_failure_rate_raises(self):
        """Default failure_rate (1.0) raises RuntimeError."""
        with pytest.raises(RuntimeError):
            deterministic_failing_task.run(order_id=1)

    def test_intermediate_failure_rate_uses_counter(self):
        """Intermediate failure_rate uses modular counter for deterministic behavior."""
        # Reset counter
        deterministic_failing_task._call_counter = 0

        # failure_rate=0.5 → cycle_length=2 → fails every 2nd call
        results = []
        errors = []
        for i in range(4):
            try:
                result = deterministic_failing_task.run(order_id=i, failure_rate=0.5)
                results.append(result)
            except RuntimeError:
                errors.append(i)

        # With cycle_length=2, counter % 2 == 0 fails at counter 2 and 4
        assert len(errors) == 2
        assert len(results) == 2


# ============================================================
# Contract: Slow task configuration
# ============================================================


class TestSlowTaskContract:
    """slow_task configuration contract."""

    def test_soft_time_limit_configured(self):
        """Task has soft_time_limit matching SLOW_TASK_SOFT_TIME_LIMIT."""
        assert slow_task.soft_time_limit == SLOW_TASK_SOFT_TIME_LIMIT


# ============================================================
# Behavior: Slow task
# ============================================================


class TestSlowTaskBehavior:
    """slow_task sleep and return behavior."""

    def test_calls_sleep_with_default_delay(self):
        """Calls time.sleep with default delay value from source."""
        with mock_sleep() as sleep_mock:
            slow_task.run(order_id=1)

        sleep_mock.assert_called_with(DEFAULT_SLOW_TASK_DELAY)

    def test_calls_sleep_with_custom_delay(self):
        """Calls time.sleep with the specified delay value."""
        with mock_sleep() as sleep_mock:
            slow_task.run(order_id=1, delay=3.0)

        sleep_mock.assert_called_with(3.0)
        assert sleep_mock.call_count == 1

    def test_returns_slow_completed_status(self):
        """Returns dict with status='slow_completed' and delay value."""
        with mock_sleep():
            result = slow_task.run(order_id=10, delay=2.5)

        assert result == {
            "order_id": 10,
            "status": "slow_completed",
            "delay": 2.5,
        }
