"""Safety-valve metrics provider and the pre-warmer hard-limit override wiring.

Regression coverage for the structurally-disabled safety valve: the
``SafetyValveMetricsProvider`` Protocol had no implementation tree-wide and the
production construction passed no provider and no graceful-degradation handle,
so ``check_safety_valve()`` permanently returned False and
``emergency_override()`` was a no-op even with a provider (a two-layer false
guarantee). The provider now reads the existing system-metrics cache (CPU) and
the aggregate circuit-breaker failure rate (error rate), and the override
reaches graceful degradation — except under ``dry_run`` (the default), which
logs the would-be transition instead of applying it.

Test targets:
    - services.capacity_reservation.safety_valve_provider.
      SystemMetricsSafetyValveProvider: fraction conversion, fail-safe raises,
      and the error-rate read reflecting traffic admitted through ``protect()``
      (a default-built breaker records on the process-shared service the
      provider reads).
    - services.capacity_reservation.pre_warmer.PreWarmer.check_safety_valve /
      emergency_override: threshold breach detection, dry_run guard, CRITICAL
      transition delivery.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.protect_facade import protect
from baldur.services.capacity_reservation.pre_warmer import PreWarmer
from baldur.services.capacity_reservation.safety_valve_provider import (
    SystemMetricsSafetyValveProvider,
)
from baldur.services.circuit_breaker.convenience import (
    configure_circuit_breaker_service,
    reset_circuit_breaker_service,
)
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.settings.capacity_reservation import CapacityReservationSettings


class _StubMetricsProvider:
    """Scriptable SafetyValveMetricsProvider implementation."""

    def __init__(
        self, cpu: float = 0.0, error_rate: float = 0.0, error: Exception | None = None
    ):
        self._cpu = cpu
        self._error_rate = error_rate
        self._error = error

    def get_cpu_usage(self) -> float:
        if self._error is not None:
            raise self._error
        return self._cpu

    def get_error_rate(self) -> float:
        if self._error is not None:
            raise self._error
        return self._error_rate


def _settings(**overrides) -> CapacityReservationSettings:
    return CapacityReservationSettings(**overrides)


def _pre_warmer(provider=None, graceful=None, **settings_overrides) -> PreWarmer:
    return PreWarmer(
        calendar=MagicMock(),
        graceful_degradation=graceful,
        metrics_provider=provider,
        settings=_settings(**settings_overrides),
    )


class TestSafetyValveProviderBehavior:
    """Provider reads live OSS sources and fails safe when they are unreadable."""

    def test_cpu_usage_converts_cache_percent_to_fraction(self):
        """cpu_percent (0-100) is returned as the valve's 0.0-1.0 fraction."""
        cache = MagicMock()
        cache.is_running.return_value = True
        cache.get_metrics.return_value = MagicMock(cpu_percent=42.0, source="cache")

        with patch(
            "baldur.services.system_metrics_cache.get_system_metrics_cache",
            return_value=cache,
        ):
            assert SystemMetricsSafetyValveProvider().get_cpu_usage() == 0.42

    def test_cpu_usage_raises_when_cache_not_running(self):
        """An unreadable CPU signal raises so the valve degrades to no-override."""
        cache = MagicMock()
        cache.is_running.return_value = False

        with patch(
            "baldur.services.system_metrics_cache.get_system_metrics_cache",
            return_value=cache,
        ):
            with pytest.raises(RuntimeError):
                SystemMetricsSafetyValveProvider().get_cpu_usage()

    def test_cpu_usage_raises_when_sample_is_stale(self):
        """A stale sample must not feed a CRITICAL-transition decision."""
        cache = MagicMock()
        cache.is_running.return_value = True
        cache.get_metrics.return_value = MagicMock(cpu_percent=99.0, source="stale")

        with patch(
            "baldur.services.system_metrics_cache.get_system_metrics_cache",
            return_value=cache,
        ):
            with pytest.raises(RuntimeError):
                SystemMetricsSafetyValveProvider().get_cpu_usage()

    def test_error_rate_delegates_to_aggregate_failure_rate(self):
        """Error rate is the circuit-breaker service's aggregate failure fraction."""
        cb_service = MagicMock()
        cb_service.get_aggregate_failure_rate.return_value = 0.25

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=cb_service,
        ):
            assert SystemMetricsSafetyValveProvider().get_error_rate() == 0.25


@pytest.fixture
def shared_breaker_service() -> CircuitBreakerService:
    """The runtime singleton at shipped thresholds over an in-memory repository.

    No ``config=``: the instance reads the process-shared configuration, so
    the trip triggers in force are the shipped ones. ``protect()``'s
    default-built breaker resolves this instance per call.
    """
    service = CircuitBreakerService(repository=InMemoryCircuitBreakerStateRepository())
    configure_circuit_breaker_service(service)
    yield service
    reset_circuit_breaker_service()


def _raise_downstream() -> None:
    raise RuntimeError("downstream down")


def _drive_protected(name: str, pattern: str) -> None:
    """Replay ``pattern`` through ``protect(name, …)``: ``f`` raises, ``s`` returns."""
    for outcome in pattern:
        if outcome == "f":
            with pytest.raises(RuntimeError):
                protect(
                    name,
                    _raise_downstream,
                    circuit_breaker=True,
                    retry=False,
                    timeout=None,
                )
        else:
            protect(name, lambda: "ok", circuit_breaker=True, retry=False, timeout=None)


class TestSafetyValveProviderReadsProtectedTrafficBehavior:
    """The error-rate read sees the calls ``protect()`` admitted.

    Before the default-built breaker recorded on the shared service, a
    process whose traffic all ran through ``protect()`` read ``0.0`` here
    and the valve could never fire on error rate.
    """

    def test_error_rate_reflects_failures_admitted_through_protect(
        self, shared_breaker_service
    ):
        """One failure in ten calls reads 0.1 — no trigger fires on the way.

        Boundary: 1 consecutive failure is under the count threshold (5) and
        a 10 % rate is under the rate threshold (50 %) at the minimum sample
        (10), so no trip clears the window the read is taken from.
        """
        pattern = "f" + "s" * 9
        config = shared_breaker_service.config
        assert len(pattern) >= config.minimum_calls
        assert pattern.count("f") < config.failure_threshold
        assert pattern.count("f") / len(pattern) * 100 < config.failure_rate_threshold

        _drive_protected("valve-read", pattern)

        assert SystemMetricsSafetyValveProvider().get_error_rate() == pytest.approx(
            pattern.count("f") / len(pattern)
        )

    def test_error_rate_reads_zero_after_a_trip_on_the_only_protected_name(
        self, shared_breaker_service
    ):
        """CLOSED → OPEN clears the tripped name's window: the read is 0.0, not 1.0.

        The documented convention — a window with no evidence reads the same
        as no traffic at all — pinned rather than left to drift.
        """
        with (
            patch.object(shared_breaker_service, "_log_circuit_open_audit"),
            patch.object(shared_breaker_service, "_apply_burn_rate_multiplier"),
        ):
            _drive_protected(
                "valve-trip", "f" * shared_breaker_service.config.failure_threshold
            )

        assert (
            shared_breaker_service.repository.get_or_create("valve-trip").state
            == "open"
        )
        assert shared_breaker_service.get_window_evidence("valve-trip") == (0, 0)
        assert SystemMetricsSafetyValveProvider().get_error_rate() == 0.0


class TestSafetyValveWiringBehavior:
    """check_safety_valve fires on breach; emergency_override honors dry_run."""

    def test_cpu_breach_fires_the_valve(self):
        settings = _settings()
        provider = _StubMetricsProvider(
            cpu=settings.safety_valve_cpu_threshold + 0.01, error_rate=0.0
        )
        warmer = _pre_warmer(provider=provider)

        assert warmer.check_safety_valve() is True

    def test_error_rate_breach_fires_the_valve(self):
        settings = _settings()
        provider = _StubMetricsProvider(
            cpu=0.0, error_rate=settings.safety_valve_error_rate_threshold + 0.01
        )
        warmer = _pre_warmer(provider=provider)

        assert warmer.check_safety_valve() is True

    def test_metrics_at_threshold_do_not_fire(self):
        """Boundary: the thresholds are strict (>), so equal values do not fire."""
        settings = _settings()
        provider = _StubMetricsProvider(
            cpu=settings.safety_valve_cpu_threshold,
            error_rate=settings.safety_valve_error_rate_threshold,
        )
        warmer = _pre_warmer(provider=provider)

        assert warmer.check_safety_valve() is False

    def test_no_metrics_provider_never_fires(self):
        """Without a wired provider the valve reports no breach (the old inert state)."""
        warmer = _pre_warmer(provider=None)

        assert warmer.check_safety_valve() is False

    def test_provider_error_degrades_to_no_override(self):
        """An unreadable metrics source must never trigger the CRITICAL path."""
        provider = _StubMetricsProvider(error=RuntimeError("metrics unavailable"))
        warmer = _pre_warmer(provider=provider)

        assert warmer.check_safety_valve() is False

    def test_emergency_override_dry_run_logs_only(self):
        """Under dry_run (the default) the CRITICAL transition is not applied."""
        graceful = MagicMock()
        warmer = _pre_warmer(graceful=graceful, dry_run=True)

        warmer.emergency_override()

        graceful.update_level.assert_not_called()
        assert warmer.safety_valve_active is False

    def test_emergency_override_reaches_graceful_degradation(self):
        """With dry_run off, the override delivers a CRITICAL backpressure level."""
        from baldur.settings.backpressure import BackpressureLevel

        graceful = MagicMock()
        warmer = _pre_warmer(graceful=graceful, dry_run=False)

        warmer.emergency_override()

        graceful.update_level.assert_called_once_with(BackpressureLevel.CRITICAL)
        assert warmer.safety_valve_active is True

    def test_breach_to_override_end_to_end(self):
        """Threshold breach detected by the provider drives the real transition."""
        from baldur.settings.backpressure import BackpressureLevel

        settings = _settings()
        provider = _StubMetricsProvider(cpu=settings.safety_valve_cpu_threshold + 0.2)
        graceful = MagicMock()
        warmer = _pre_warmer(provider=provider, graceful=graceful, dry_run=False)

        if warmer.check_safety_valve():
            warmer.emergency_override()

        graceful.update_level.assert_called_once_with(BackpressureLevel.CRITICAL)
