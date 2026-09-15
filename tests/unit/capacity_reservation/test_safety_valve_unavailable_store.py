"""The safety valve stands down at WARNING on an unreadable breaker store.

793 D12. The valve's error-rate half now reads an aggregate in which a
tripped dependency counts, and that aggregate propagates the shared store's
unavailability instead of reading ``0.0``. An error rate the store cannot
supply is not a healthy reading and not an incident of the valve's own: the
valve reports no breach at WARNING with the store's reason — the same
fail-safe direction its CPU half takes on an unreadable sample — while every
other exception keeps the traceback-carrying ERROR it had.

Verification techniques applied:
- Error path: the typed store error -> ``False`` at WARNING with the reason,
  no traceback; another exception -> ``False`` through ``logger.exception``
- Fire precondition: the fault is raised from the attribute the valve reads
  (``get_error_rate``), and its touch is asserted
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from baldur.services.capacity_reservation.event_calendar import EventCalendar
from baldur.services.capacity_reservation.pre_warmer import PreWarmer
from baldur.services.circuit_breaker.exceptions import (
    CircuitBreakerStateUnavailableError,
)
from baldur.settings.capacity_reservation import CapacityReservationSettings


class _ProviderRaisingOnErrorRate:
    """A provider whose error-rate read raises; CPU reads healthy."""

    def __init__(self, error: Exception):
        self._error = error
        self.touched = False

    def get_cpu_usage(self) -> float:
        return 0.0

    def get_error_rate(self) -> float:
        self.touched = True
        raise self._error


def _pre_warmer(provider) -> PreWarmer:
    return PreWarmer(
        calendar=MagicMock(spec=EventCalendar),
        graceful_degradation=None,
        metrics_provider=provider,
        settings=CapacityReservationSettings(),
    )


class TestSafetyValveUnavailableStoreBehavior:
    """Two error exits, told apart by their log level and their payload."""

    @pytest.mark.parametrize(
        "reason", ["l2_quarantined", "degraded_backend", "l2_timeout"]
    )
    def test_unavailable_store_stands_down_at_warning_with_the_reason(self, reason):
        provider = _ProviderRaisingOnErrorRate(
            CircuitBreakerStateUnavailableError("get_cluster_states", reason)
        )
        warmer = _pre_warmer(provider)

        with capture_logs() as logs:
            fired = warmer.check_safety_valve()

        assert fired is False
        assert provider.touched is True
        entries = [
            entry
            for entry in logs
            if entry.get("event") == "capacity_reservation.safety_valve_check_failed"
        ]
        assert len(entries) == 1
        assert entries[0]["log_level"] == "warning"
        assert entries[0]["reason"] == reason
        assert not any(
            entry.get("event") == "capacity_reservation.safety_valve_check_error"
            for entry in logs
        )

    def test_any_other_exception_keeps_the_error_level_exit(self):
        """Control: the typed branch is narrow — a bug still reports as one."""
        provider = _ProviderRaisingOnErrorRate(RuntimeError("provider bug"))
        warmer = _pre_warmer(provider)

        with capture_logs() as logs:
            fired = warmer.check_safety_valve()

        assert fired is False
        assert provider.touched is True
        entries = [
            entry
            for entry in logs
            if entry.get("event") == "capacity_reservation.safety_valve_check_error"
        ]
        assert len(entries) == 1
        assert entries[0]["log_level"] == "error"
        assert not any(
            entry.get("event") == "capacity_reservation.safety_valve_check_failed"
            for entry in logs
        )
