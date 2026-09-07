"""
Tests for Circuit Breaker Protection Mixin

Covers:
- Rate limit cascade detection
- Self-DDoS protection
- Adaptive backoff calculation

Refactored to use Factory Pattern (Phase 2):
- MockCircuitBreakerStateData → factories.MockCircuitBreakerStateData
- MockRepository → factories.InMemoryCircuitBreakerRepository
- MockRateLimitTracker → factories.InMemoryRateLimitTracker
"""

from unittest.mock import patch

import pytest

# Factory Pattern imports
from tests.factories import (
    InMemoryCircuitBreakerRepository,
    InMemoryRateLimitTracker,
    MockCircuitBreakerStateData,
)


class TestRateLimitCascadeDetection:
    """Tests for rate limit cascade detection."""

    def test_record_rate_limit_response_below_threshold(self):
        """Test recording rate limit response below threshold."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            rate_limit_cascade_threshold=10,
            rate_limit_cascade_window_seconds=60,
            rate_limit_cascade_rate=10.0,
            rate_limit_cascade_minimum_calls=20,
        )
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._rate_limits["test_service"] = 5  # Below threshold
        mock_tracker._requests["test_service"] = 100

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            result = service.record_rate_limit_response("test_service")

        # Should not trigger cascade
        assert result is None

    def test_record_rate_limit_response_triggers_cascade(self):
        """Test recording rate limit response triggers cascade."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            rate_limit_cascade_threshold=10,
            rate_limit_cascade_window_seconds=60,
            rate_limit_cascade_rate=10.0,
            rate_limit_cascade_minimum_calls=20,
        )
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._rate_limits["test_service"] = 15  # Above threshold
        mock_tracker._requests["test_service"] = 100  # 15/100 = 15% > 10%

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            with patch(
                "baldur.services.circuit_breaker.manual_control._is_system_enabled",
                return_value=True,
            ):
                result = service.record_rate_limit_response("test_service")

        # Should trigger cascade and open circuit
        assert result is not None
        assert result.success is True

    def test_record_rate_limit_response_when_disabled(self):
        """Test recording rate limit response when CB disabled."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=False)
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        result = service.record_rate_limit_response("test_service")
        assert result is None

    def test_check_rate_limit_cascade(self):
        """Test check_rate_limit_cascade method."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            rate_limit_cascade_threshold=10,
            rate_limit_cascade_rate=10.0,
            rate_limit_cascade_minimum_calls=20,
        )
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._rate_limits["test_service"] = 15
        mock_tracker._requests["test_service"] = 100  # 15% > 10%

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            is_cascade = service.check_rate_limit_cascade("test_service")

        assert is_cascade is True

    def test_check_rate_limit_cascade_no_cascade(self):
        """Test check_rate_limit_cascade when no cascade."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            rate_limit_cascade_threshold=10,
            rate_limit_cascade_rate=10.0,
            rate_limit_cascade_minimum_calls=20,
        )
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._rate_limits["test_service"] = 5
        mock_tracker._requests["test_service"] = 100

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            is_cascade = service.check_rate_limit_cascade("test_service")

        assert is_cascade is False

    def test_the_cascade_writes_no_request_of_its_own(self):
        """The rate's denominator belongs to the observation site, not to this.

        Writing it here as well counted the same call twice: in a pure storm the
        observed rate could never exceed 50%, putting the top half of the
        setting's range out of reach.
        """
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True, rate_limit_cascade_threshold=100)
        service = CircuitBreakerService(
            config=config, repository=InMemoryCircuitBreakerRepository()
        )
        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._requests["test_service"] = 7

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            service.record_rate_limit_response("test_service")

        assert mock_tracker._requests["test_service"] == 7
        assert mock_tracker._rate_limits["test_service"] == 1

    def test_a_zero_denominator_is_guarded_rather_than_divided_by(self):
        """A caller that reports a 429 without a request must not raise.

        The minimum-sample term no longer implies a non-zero denominator now
        that the request write moved out, so the division needs its own guard.
        """
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            rate_limit_cascade_threshold=1,
            rate_limit_cascade_rate=10.0,
            rate_limit_cascade_minimum_calls=0,
        )
        service = CircuitBreakerService(
            config=config, repository=InMemoryCircuitBreakerRepository()
        )
        mock_tracker = InMemoryRateLimitTracker()

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            result = service.record_rate_limit_response("test_service")

        assert result is None

    def test_the_opening_call_steps_the_backoff_and_returns_a_result(self):
        """``did_open`` gates the cluster-logical side effects of the trip."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            rate_limit_cascade_threshold=10,
            rate_limit_cascade_rate=10.0,
            rate_limit_cascade_minimum_calls=20,
        )
        service = CircuitBreakerService(
            config=config, repository=InMemoryCircuitBreakerRepository()
        )
        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._rate_limits["test_service"] = 15
        mock_tracker._requests["test_service"] = 100

        with (
            patch(
                "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
                return_value=mock_tracker,
            ),
            patch(
                "baldur.services.circuit_breaker.manual_control._is_system_enabled",
                return_value=True,
            ),
        ):
            result = service.record_rate_limit_response("test_service")

        assert result is not None
        assert mock_tracker.get_backoff_level("test_service") == 1

    def test_a_race_loser_owes_neither_the_backoff_step_nor_a_result(self):
        """An already-OPEN breaker performed no transition on this call.

        The backoff ladder is a cluster-logical side effect of the trip, so
        charging it per worker would multiply the delay by the fleet size.
        """
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            rate_limit_cascade_threshold=10,
            rate_limit_cascade_rate=10.0,
            rate_limit_cascade_minimum_calls=20,
        )
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)
        mock_repo.get_or_create("test_service").state = "open"

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._rate_limits["test_service"] = 15
        mock_tracker._requests["test_service"] = 100

        with (
            patch(
                "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
                return_value=mock_tracker,
            ),
            patch(
                "baldur.services.circuit_breaker.manual_control._is_system_enabled",
                return_value=True,
            ),
        ):
            result = service.record_rate_limit_response("test_service")

        assert result is None
        assert mock_tracker.get_backoff_level("test_service") == 0
        assert mock_tracker._rate_limits["test_service"] == 16

    def test_the_cascade_opens_an_automatic_row_not_an_operator_pin(self):
        """A cascade trip recovers through recovery_timeout like any automatic OPEN.

        It used to borrow the manual pin's expiry, so it admitted nothing until
        the TTL was due and ignored recovery entirely — a 90-minute outage from
        a storm that may have passed in seconds.
        """
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            rate_limit_cascade_threshold=10,
            rate_limit_cascade_rate=10.0,
            rate_limit_cascade_minimum_calls=20,
        )
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._rate_limits["test_service"] = 15
        mock_tracker._requests["test_service"] = 100

        with (
            patch(
                "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
                return_value=mock_tracker,
            ),
            patch(
                "baldur.services.circuit_breaker.manual_control._is_system_enabled",
                return_value=True,
            ),
        ):
            service.record_rate_limit_response("test_service")

        state = mock_repo.get_or_create("test_service")
        assert state.state == "open"
        assert state.manually_controlled is False
        assert state.manual_override_expires_at is None


class TestSelfDDoSProtection:
    """Tests for self-DDoS protection."""

    def test_should_allow_with_ddos_protection_normal(self):
        """Test should_allow_with_ddos_protection under normal load."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            self_ddos_protection_enabled=True,
            self_ddos_rps_limit=10,
            self_ddos_window_seconds=10,
        )
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._requests["test_service"] = 50  # 50/10 = 5 RPS < 10 limit

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            allowed, backoff = service.should_allow_with_ddos_protection("test_service")

        assert allowed is True
        assert backoff == 0.0

    def test_should_allow_with_ddos_protection_high_load(self):
        """Test should_allow_with_ddos_protection under high load."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            self_ddos_protection_enabled=True,
            self_ddos_rps_limit=10,
            self_ddos_window_seconds=10,
        )
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._requests["test_service"] = 150  # 150/10 = 15 RPS > 10 limit

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            allowed, backoff = service.should_allow_with_ddos_protection("test_service")

        # Still allowed but with backoff suggestion
        assert allowed is True
        assert backoff > 0

    def test_should_allow_with_ddos_protection_circuit_open(self):
        """Test should_allow_with_ddos_protection when circuit is open."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        mock_repo._states["test_service"] = MockCircuitBreakerStateData(
            service_name="test_service", state="open"
        )
        service = CircuitBreakerService(config=config, repository=mock_repo)

        mock_tracker = InMemoryRateLimitTracker()

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            allowed, backoff = service.should_allow_with_ddos_protection("test_service")

        assert allowed is False
        assert backoff > 0

    def test_ddos_protection_disabled(self):
        """Test behavior when DDoS protection is disabled."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            self_ddos_protection_enabled=False,
        )
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._requests["test_service"] = 1000  # Very high

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            allowed, backoff = service.should_allow_with_ddos_protection("test_service")

        # Should allow without backoff when protection disabled
        assert allowed is True
        assert backoff == 0.0


class TestAdaptiveBackoff:
    """Tests for adaptive backoff calculation."""

    def test_calculate_adaptive_backoff_initial(self):
        """Test adaptive backoff at initial level."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            self_ddos_backoff_multiplier=2.0,
        )
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._backoff["test_service"] = 0

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            backoff = service.calculate_adaptive_backoff("test_service")

        assert backoff >= 0

    def test_calculate_adaptive_backoff_exponential(self):
        """Test adaptive backoff increases exponentially."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            self_ddos_backoff_multiplier=2.0,
        )
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        mock_tracker = InMemoryRateLimitTracker()

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            # Level 0
            mock_tracker._backoff["test_service"] = 0
            backoff0 = service.calculate_adaptive_backoff("test_service")

            # Level 2
            mock_tracker._backoff["test_service"] = 2
            backoff2 = service.calculate_adaptive_backoff("test_service")

            # Level 4
            mock_tracker._backoff["test_service"] = 4
            backoff4 = service.calculate_adaptive_backoff("test_service")

        # Backoff should increase with level
        assert backoff2 >= backoff0
        assert backoff4 >= backoff2

    def test_calculate_adaptive_backoff_includes_jitter(self):
        """Test adaptive backoff includes jitter."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._backoff["test_service"] = 1

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            # Call multiple times
            backoffs = [
                service.calculate_adaptive_backoff("test_service") for _ in range(10)
            ]

        # With jitter, values should vary
        # (might be same if jitter is deterministic in tests)
        assert len(backoffs) == 10


# =============================================================================
# 687 D1: adaptive backoff reuses ExponentialBackoff (settings-backed trio)
# =============================================================================


class TestAdaptiveBackoffBehavior:
    """calculate_adaptive_backoff sources base/max/jitter from the config trio
    and delegates to ExponentialBackoff (687 D1).

    Jitter is made deterministic by patching ExponentialBackoff's
    ``random.uniform`` so the exact settings-driven delay can be asserted.
    """

    def _make_service(self, base=1.0, max_delay=60.0, multiplier=2.0, jitter=0.25):
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            self_ddos_backoff_base_seconds=base,
            self_ddos_backoff_max_seconds=max_delay,
            self_ddos_backoff_multiplier=multiplier,
            self_ddos_backoff_jitter_factor=jitter,
        )
        return CircuitBreakerService(
            config=config, repository=InMemoryCircuitBreakerRepository()
        )

    def test_level_zero_equals_base_delay(self):
        # Given: level 0 → attempt 1 → base * multiplier^0 = base, jitter 0
        service = self._make_service(base=1.0, multiplier=2.0)
        tracker = InMemoryRateLimitTracker()
        tracker._backoff["svc"] = 0

        # When
        with (
            patch(
                "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
                return_value=tracker,
            ),
            patch("baldur.core.backoff.random.uniform", return_value=0.0),
        ):
            backoff = service.calculate_adaptive_backoff("svc")

        # Then: 1.0 * 2^0 = 1.0
        assert backoff == 1.0

    def test_level_two_grows_by_multiplier_power_level(self):
        # Given: level 2 → attempt 3 → 1.0 * 2^2 = 4.0
        service = self._make_service(base=1.0, multiplier=2.0)
        tracker = InMemoryRateLimitTracker()
        tracker._backoff["svc"] = 2

        # When
        with (
            patch(
                "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
                return_value=tracker,
            ),
            patch("baldur.core.backoff.random.uniform", return_value=0.0),
        ):
            backoff = service.calculate_adaptive_backoff("svc")

        # Then
        assert backoff == 4.0

    def test_large_level_capped_at_max_delay(self):
        # Given: an extreme level whose exponential value exceeds max_delay
        service = self._make_service(base=1.0, max_delay=60.0, multiplier=2.0)
        tracker = InMemoryRateLimitTracker()
        tracker._backoff["svc"] = 100

        # When
        with (
            patch(
                "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
                return_value=tracker,
            ),
            patch("baldur.core.backoff.random.uniform", return_value=0.0),
        ):
            backoff = service.calculate_adaptive_backoff("svc")

        # Then: capped at the configured max_delay
        assert backoff == 60.0

    def test_defensive_floor_clamps_sub_minimum_delay(self):
        # Given: a base so small the computed delay drops below _MIN_BACKOFF_SECONDS
        from baldur.services.circuit_breaker.protection import _MIN_BACKOFF_SECONDS

        service = self._make_service(base=0.05, multiplier=2.0)
        tracker = InMemoryRateLimitTracker()
        tracker._backoff["svc"] = 0

        # When
        with (
            patch(
                "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
                return_value=tracker,
            ),
            patch("baldur.core.backoff.random.uniform", return_value=0.0),
        ):
            backoff = service.calculate_adaptive_backoff("svc")

        # Then: clamped up to the defensive floor
        assert backoff == _MIN_BACKOFF_SECONDS

    def test_jitter_range_derived_from_delay_and_jitter_factor(self):
        # Given: level 1 → attempt 2 → delay 2.0; jitter_range = 2.0 * 0.25 = 0.5
        service = self._make_service(base=1.0, multiplier=2.0, jitter=0.25)
        tracker = InMemoryRateLimitTracker()
        tracker._backoff["svc"] = 1

        # When: uniform returns a known offset within the jitter range
        with (
            patch(
                "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
                return_value=tracker,
            ),
            patch(
                "baldur.core.backoff.random.uniform", return_value=0.3
            ) as mock_uniform,
        ):
            backoff = service.calculate_adaptive_backoff("svc")

        # Then: uniform is called with the ±(delay * jitter_factor) range and the
        # offset is applied to the base delay
        mock_uniform.assert_called_once_with(-0.5, 0.5)
        assert backoff == pytest.approx(2.3)


# =============================================================================
# 439: Hybrid Cascade Detection — Boundary Analysis (6 scenarios)
# =============================================================================


class TestHybridCascadeBoundaryBehavior:
    """Boundary analysis for the 3-condition hybrid cascade detection.

    Hybrid condition: ALL must be true:
    1. rate_limit_count >= threshold (absolute floor)
    2. total_requests >= minimum_calls (sample size)
    3. rate_limit_count / total_requests >= rate / 100 (rate threshold)
    """

    def _make_service(self, threshold=10, minimum_calls=20, rate=10.0):
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            rate_limit_cascade_threshold=threshold,
            rate_limit_cascade_window_seconds=60,
            rate_limit_cascade_rate=rate,
            rate_limit_cascade_minimum_calls=minimum_calls,
        )
        mock_repo = InMemoryCircuitBreakerRepository()
        return CircuitBreakerService(config=config, repository=mock_repo)

    def test_all_conditions_met_cascade_detected(self):
        """Scenario 1: all 3 conditions met → cascade."""
        service = self._make_service(threshold=10, minimum_calls=20, rate=10.0)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._rate_limits["svc"] = 15  # >= 10 ✓
        mock_tracker._requests["svc"] = 100  # >= 20 ✓, 15/100=15% >= 10% ✓

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            assert service.check_rate_limit_cascade("svc") is True

    def test_threshold_not_met_no_cascade(self):
        """Scenario 2: absolute count below threshold → no cascade."""
        service = self._make_service(threshold=10, minimum_calls=20, rate=10.0)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._rate_limits["svc"] = 9  # < 10 ✗
        mock_tracker._requests["svc"] = 50  # >= 20 ✓, 9/50=18% >= 10% ✓

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            assert service.check_rate_limit_cascade("svc") is False

    def test_minimum_calls_not_met_no_cascade(self):
        """Scenario 3: insufficient sample size → no cascade."""
        service = self._make_service(threshold=10, minimum_calls=20, rate=10.0)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._rate_limits["svc"] = 15  # >= 10 ✓
        mock_tracker._requests["svc"] = 19  # < 20 ✗

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            assert service.check_rate_limit_cascade("svc") is False

    def test_rate_not_met_no_cascade(self):
        """Scenario 4: rate below threshold → no cascade."""
        service = self._make_service(threshold=10, minimum_calls=20, rate=10.0)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._rate_limits["svc"] = 10  # >= 10 ✓
        mock_tracker._requests["svc"] = 200  # >= 20 ✓, 10/200=5% < 10% ✗

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            assert service.check_rate_limit_cascade("svc") is False

    def test_rate_exactly_at_boundary_cascade_detected(self):
        """Scenario 5: rate exactly at threshold → cascade detected (>=)."""
        service = self._make_service(threshold=10, minimum_calls=20, rate=10.0)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._rate_limits["svc"] = 10  # >= 10 ✓
        mock_tracker._requests["svc"] = 100  # >= 20 ✓, 10/100=10% >= 10% ✓

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            assert service.check_rate_limit_cascade("svc") is True

    def test_minimum_calls_exactly_at_boundary_cascade_detected(self):
        """Scenario 6: minimum_calls exactly at boundary → cascade detected (>=)."""
        service = self._make_service(threshold=5, minimum_calls=20, rate=10.0)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._rate_limits["svc"] = 5  # >= 5 ✓
        mock_tracker._requests["svc"] = 20  # >= 20 ✓, 5/20=25% >= 10% ✓

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            assert service.check_rate_limit_cascade("svc") is True


# =============================================================================
# 439: RPS-based Self-DDoS Detection — Boundary Analysis
# =============================================================================


class TestRpsDdosBoundaryBehavior:
    """Boundary analysis for RPS-based self-DDoS detection."""

    def _make_service(self, rps_limit=10, window=10):
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            self_ddos_protection_enabled=True,
            self_ddos_rps_limit=rps_limit,
            self_ddos_window_seconds=window,
        )
        mock_repo = InMemoryCircuitBreakerRepository()
        return CircuitBreakerService(config=config, repository=mock_repo)

    def test_rps_below_limit_not_detected(self):
        """RPS below limit → not detected."""
        service = self._make_service(rps_limit=10, window=10)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._requests["svc"] = 99  # 99/10 = 9.9 RPS < 10

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            assert service.is_self_ddos_detected("svc") is False

    def test_rps_exactly_at_limit_not_detected(self):
        """RPS == limit → not detected (strict >)."""
        service = self._make_service(rps_limit=10, window=10)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._requests["svc"] = 100  # 100/10 = 10.0 RPS == 10, not >

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            assert service.is_self_ddos_detected("svc") is False

    def test_rps_above_limit_detected(self):
        """RPS above limit → detected."""
        service = self._make_service(rps_limit=10, window=10)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._requests["svc"] = 101  # 101/10 = 10.1 RPS > 10

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            assert service.is_self_ddos_detected("svc") is True

    def test_zero_requests_not_detected(self):
        """Zero requests → 0 RPS, not detected."""
        service = self._make_service(rps_limit=10, window=10)

        mock_tracker = InMemoryRateLimitTracker()
        # No requests recorded → 0/10 = 0 RPS

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            assert service.is_self_ddos_detected("svc") is False

    def test_protection_disabled_always_false(self):
        """DDoS protection disabled → always returns False."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            self_ddos_protection_enabled=False,
            self_ddos_rps_limit=1,
            self_ddos_window_seconds=10,
        )
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._requests["svc"] = 99999

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            assert service.is_self_ddos_detected("svc") is False


# =============================================================================
# 439: get_protection_status — New Fields
# =============================================================================


class TestProtectionStatusBehavior:
    """Verify get_protection_status contains 439 new fields."""

    def test_cascade_section_has_hybrid_fields(self):
        """Protection status includes hybrid cascade fields."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            rate_limit_cascade_threshold=10,
            rate_limit_cascade_rate=10.0,
            rate_limit_cascade_minimum_calls=20,
        )
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._rate_limits["svc"] = 5
        mock_tracker._requests["svc"] = 50

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            status = service.get_protection_status("svc")

        cascade = status["rate_limit_cascade"]
        assert "total_requests_in_window" in cascade
        assert cascade["total_requests_in_window"] == 50
        assert "rate_percent" in cascade
        assert cascade["rate_percent"] == 10.0  # 5/50 * 100
        assert "rate_threshold_percent" in cascade
        assert cascade["rate_threshold_percent"] == 10.0
        assert "minimum_calls" in cascade
        assert cascade["minimum_calls"] == 20

    def test_ddos_section_has_rps_fields(self):
        """Protection status includes RPS-based DDoS fields."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            self_ddos_protection_enabled=True,
            self_ddos_rps_limit=200,
            self_ddos_window_seconds=10,
        )
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        mock_tracker = InMemoryRateLimitTracker()
        mock_tracker._requests["svc"] = 50

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            status = service.get_protection_status("svc")

        ddos = status["self_ddos_protection"]
        assert "current_rps" in ddos
        assert ddos["current_rps"] == 5.0  # 50/10
        assert "rps_limit" in ddos
        assert ddos["rps_limit"] == 200

    def test_cascade_rate_percent_zero_when_no_requests(self):
        """Rate percent is 0.0 when no requests recorded."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        mock_tracker = InMemoryRateLimitTracker()

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            status = service.get_protection_status("svc")

        assert status["rate_limit_cascade"]["rate_percent"] == 0.0


class TestProtectionDenominatorBehavior:
    """Who writes the cascade rate's denominator decides what the rate means."""

    def _service(self):
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            rate_limit_cascade_threshold=100,
            rate_limit_cascade_rate=10.0,
            rate_limit_cascade_minimum_calls=1000,
        )
        return CircuitBreakerService(
            config=config, repository=InMemoryCircuitBreakerRepository()
        )

    def test_the_convenience_function_supplies_its_own_denominator(self):
        """A caller with no request counter still produces a usable rate.

        ``record_rate_limit()`` is the hand-wired entry point an application
        calls from its own client code. With the request write moved out of the
        cascade, a convenience that did not write one would leave every such
        caller reading a rate of 0/0.
        """
        from baldur.services.circuit_breaker.convenience import record_rate_limit

        service = self._service()
        mock_tracker = InMemoryRateLimitTracker()

        with (
            patch(
                "baldur.services.circuit_breaker.rate_limit_tracker.get_rate_limit_tracker",
                return_value=mock_tracker,
            ),
            patch(
                "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
                return_value=mock_tracker,
            ),
            patch(
                "baldur.services.circuit_breaker.convenience.get_circuit_breaker_service",
                return_value=service,
            ),
        ):
            for _ in range(10):
                record_rate_limit("test_service")

            status = service.get_protection_status("test_service")

        cascade = status["rate_limit_cascade"]
        assert cascade["count_in_window"] == 10
        assert cascade["total_requests_in_window"] == 10
        assert cascade["rate_percent"] == 100.0

    def test_a_caller_that_counts_its_own_calls_keeps_its_own_rate(self):
        """The observation sites reach the cascade directly, denominator in hand.

        Twenty calls of which ten were throttled must read 50%, not 100% — the
        rate is only meaningful when one writer owns the denominator.
        """
        service = self._service()
        mock_tracker = InMemoryRateLimitTracker()
        for _ in range(20):
            mock_tracker.record_request("test_service")

        with patch(
            "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
            return_value=mock_tracker,
        ):
            for _ in range(10):
                service.record_rate_limit_response("test_service")

            status = service.get_protection_status("test_service")

        cascade = status["rate_limit_cascade"]
        assert cascade["count_in_window"] == 10
        assert cascade["total_requests_in_window"] == 20
        assert cascade["rate_percent"] == 50.0
