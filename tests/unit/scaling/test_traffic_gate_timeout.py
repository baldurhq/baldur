"""
TrafficGate bulkhead_timeout passthrough + _check_bulkhead severity-split unit tests.

Test items:
- Behavior: should_allow()'s bulkhead_timeout is forwarded to Bulkhead.try_acquire()
- Behavior: None is forwarded when bulkhead_timeout is unset
- Behavior: _check_bulkhead distinguishes expected unavailability (registry absent
  → bulkhead_failed WARNING) from unexpected errors (try_acquire raises →
  bulkhead_error ERROR with exception info), staying fail-open in both cases (D4)
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.scaling.config import (
    BackpressureSettings,
    reset_backpressure_settings,
)
from baldur.scaling.rate_controller import (
    RateController,
    reset_rate_controller,
)
from baldur.scaling.traffic_gate import TrafficGate, reset_traffic_gate


def _make_gate() -> TrafficGate:
    """Build a TrafficGate whose rate controller allows traffic, so the test
    isolates the bulkhead step."""
    settings = BackpressureSettings(
        backpressure_enabled=True,
        max_rate_per_second=10000.0,
    )
    controller = RateController(settings=settings)
    return TrafficGate(rate_controller=controller)


class TestBulkheadTimeoutPassthroughBehavior:
    """bulkhead_timeout parameter passthrough verification.

    Under the widened try_acquire contract these forwarding assertions ARE the
    contract pin: the gate forwards bulkhead_timeout to try_acquire(timeout=).
    """

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        reset_rate_controller()
        reset_backpressure_settings()
        reset_traffic_gate()
        yield
        reset_rate_controller()
        reset_backpressure_settings()
        reset_traffic_gate()

    def test_timeout_forwarded_to_bulkhead(self):
        """bulkhead_timeout is forwarded to Bulkhead.try_acquire(timeout=)."""
        mock_bulkhead = MagicMock()
        mock_bulkhead.try_acquire.return_value = True

        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_bulkhead

        gate = _make_gate()

        with patch(
            "baldur_pro.services.bulkhead.get_bulkhead_registry",
            return_value=mock_registry,
        ):
            gate.should_allow(
                priority=0,
                bulkhead_name="tier:critical",
                bulkhead_timeout=0.05,
            )

        mock_bulkhead.try_acquire.assert_called_once_with(timeout=0.05)

    def test_none_timeout_forwarded_as_none(self):
        """None is forwarded to try_acquire() when bulkhead_timeout is unset."""
        mock_bulkhead = MagicMock()
        mock_bulkhead.try_acquire.return_value = True

        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_bulkhead

        gate = _make_gate()

        with patch(
            "baldur_pro.services.bulkhead.get_bulkhead_registry",
            return_value=mock_registry,
        ):
            gate.should_allow(
                priority=0,
                bulkhead_name="tier:critical",
            )

        mock_bulkhead.try_acquire.assert_called_once_with(timeout=None)


class TestCheckBulkheadSeveritySplitBehavior:
    """_check_bulkhead failure-class severity split (D4).

    Fail-open is retained on the allow/deny surface for every failure class, but
    the log severity distinguishes expected unavailability (registry absent) from
    an unexpected error (the class that swallowed the pre-616 TypeError).
    """

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        reset_rate_controller()
        reset_backpressure_settings()
        reset_traffic_gate()
        yield
        reset_rate_controller()
        reset_backpressure_settings()
        reset_traffic_gate()

    def test_registry_absent_fails_open_with_bulkhead_failed_warning(self):
        """Registry unavailable (PRO not installed) → fail-open, logged at WARNING
        as traffic_gate.bulkhead_failed (expected degradation)."""
        from baldur.factory.registry import ProviderRegistry

        gate = _make_gate()

        # Given — the bulkhead registry slot resolves to None
        with patch.object(
            ProviderRegistry.bulkhead_registry, "safe_get", return_value=None
        ):
            with capture_logs() as logs:
                decision = gate.should_allow(
                    priority=0,
                    bulkhead_name="external_api",
                )

        # Then — fail-open: the request proceeds ungated
        assert decision.allowed is True
        assert decision.bulkhead_acquired is False

        warns = [e for e in logs if e.get("event") == "traffic_gate.bulkhead_failed"]
        assert len(warns) == 1
        assert warns[0]["log_level"] == "warning"
        assert warns[0]["bulkhead_name"] == "external_api"

    def test_unexpected_try_acquire_error_fails_open_with_bulkhead_error_at_error(self):
        """An unexpected try_acquire error → fail-open, logged loudly at ERROR as
        traffic_gate.bulkhead_error with exception info."""
        # Given — a bulkhead whose try_acquire raises an unexpected error
        mock_bulkhead = MagicMock()
        mock_bulkhead.try_acquire.side_effect = RuntimeError("contract violated")

        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_bulkhead

        gate = _make_gate()

        # When — routed through the gate
        with patch(
            "baldur_pro.services.bulkhead.get_bulkhead_registry",
            return_value=mock_registry,
        ):
            with capture_logs() as logs:
                decision = gate.should_allow(
                    priority=0,
                    bulkhead_name="external_api",
                )

        # Then — fail-open on the allow/deny surface
        assert decision.allowed is True
        assert decision.bulkhead_acquired is False

        # And the error is loud: ERROR level, carries exception info
        errors = [e for e in logs if e.get("event") == "traffic_gate.bulkhead_error"]
        assert len(errors) == 1
        entry = errors[0]
        assert entry["log_level"] == "error"
        assert entry.get("exc_info")
        assert entry["bulkhead_name"] == "external_api"
