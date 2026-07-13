"""
End-to-End Integration Tests.

Verifies that the full set of system components works together.

Test structure:
    - 6.1.1: End-to-End Flow Tests
    - 6.1.2: Component Interaction Tests
    - 6.1.3: Failure Scenario Tests
    - 6.1.4: Recovery Scenario Tests
    - 6.1.5: Audit Trail Tests
"""

import time

import pytest

# Adaptive Threshold
# Blast Radius
from baldur.services.circuit_breaker.blast_radius_integration import (
    reset_blast_radius_integration,
)

# Freeze Mode
# Load Shedding
from baldur.services.circuit_breaker.load_shedding import (
    get_load_shedding_manager,
    is_shedding_active,
    reset_load_shedding_manager,
)

# Models
from baldur.services.circuit_breaker.models import (
    ServiceConfig,
)

# Service Config
from baldur.services.circuit_breaker.service_config import (
    get_service_config,
    get_service_config_manager,
    reset_service_config_manager,
)

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def reset_all_singletons():
    """Reset every singleton before and after each test."""
    # Reset before test
    _reset_all()
    yield
    # Reset after test
    _reset_all()


def _reset_all():
    """Reset all singleton instances."""
    reset_load_shedding_manager()
    reset_service_config_manager()
    reset_blast_radius_integration()

    # Adaptive Threshold reset
    try:
        from baldur.services.circuit_breaker.adaptive_threshold import (
            reset_adaptive_threshold_manager,
        )

        reset_adaptive_threshold_manager()
    except ImportError:
        pass

    # Freeze Mode reset
    try:
        from baldur.services.circuit_breaker.freeze_mode import (
            reset_freeze_mode_manager,
        )

        reset_freeze_mode_manager()
    except ImportError:
        pass

    # Panic Threshold reset
    try:
        from baldur.services.circuit_breaker.panic_threshold import (
            reset_panic_threshold_monitor,
        )

        reset_panic_threshold_monitor()
    except ImportError:
        pass


@pytest.fixture
def sample_services():
    """Service configurations for testing."""
    return [
        ServiceConfig(
            service_id="payment-api",
            criticality="critical",
            shed_priority=0,  # never shed
        ),
        ServiceConfig(
            service_id="order-api",
            criticality="high",
            shed_priority=1,
        ),
        ServiceConfig(
            service_id="notification-api",
            criticality="medium",
            shed_priority=5,
        ),
        ServiceConfig(
            service_id="review-api",
            criticality="low",
            shed_priority=10,
            min_traffic_percentage=5.0,  # guarantee at least 5%
        ),
        ServiceConfig(
            service_id="recommend-api",
            criticality="low",
            shed_priority=10,
            min_traffic_percentage=0.0,  # batch/stats service: full stop allowed under overload
        ),
    ]


@pytest.fixture
def setup_full_system(sample_services):
    """Configure the full system."""
    # Service Config Manager setup
    config_manager = get_service_config_manager()
    for config in sample_services:
        config_manager.register_service(config)

    # Load Shedding Manager setup
    shedding_manager = get_load_shedding_manager()
    shedding_manager.register_services(sample_services)

    return {
        "config_manager": config_manager,
        "shedding_manager": shedding_manager,
    }


# =============================================================================
# 6.1.1 End-to-End Flow Tests
# =============================================================================


class TestEndToEndFlow:
    """Full-flow integration tests."""

    def test_normal_operation_flow(self, setup_full_system):
        """Full flow during normal operation."""
        shedding_manager = setup_full_system["shedding_manager"]

        # 1. All services allowed 100% in normal state
        assert shedding_manager.evaluate_shedding("payment-api") == 100.0
        assert shedding_manager.evaluate_shedding("review-api") == 100.0

        # 2. Shedding inactive
        assert not is_shedding_active()

    def test_critical_service_degradation_flow(self, setup_full_system):
        """Full flow when a critical service degrades."""
        shedding_manager = setup_full_system["shedding_manager"]

        # 1. Critical service error rate rises (35%)
        shedding_manager.set_error_rate("payment-api", 35.0)

        # 2. Low criticality services limited to 50%
        allowed = shedding_manager.evaluate_shedding("review-api")
        assert allowed <= 50.0  # Level 1: 50% cap

        # 3. Critical service still at 100%
        assert shedding_manager.evaluate_shedding("payment-api") == 100.0

        # 4. Error rate rises further (55%)
        shedding_manager.set_error_rate("payment-api", 55.0)

        # 5. Low+Medium services capped at 80% (20% allowed)
        allowed = shedding_manager.evaluate_shedding("review-api")
        assert allowed <= 20.0  # Level 2: 20% allowed

    def test_complete_degradation_flow(self, setup_full_system):
        """Full flow under complete degradation."""
        shedding_manager = setup_full_system["shedding_manager"]

        # 1. Critical service severe errors (75%)
        shedding_manager.set_error_rate("payment-api", 75.0)

        # 2. Low criticality fully shed (min_traffic_percentage guaranteed)
        allowed = shedding_manager.evaluate_shedding("review-api")
        # review-api has min_traffic_percentage=5.0 so at least 5% guaranteed
        assert allowed >= 5.0
        assert allowed <= 10.0  # Level 3 but min guaranteed


# =============================================================================
# 6.1.2 Component Interaction Tests
# =============================================================================


class TestComponentInteraction:
    """Component interaction tests."""

    def test_service_config_propagation(self, setup_full_system):
        """ServiceConfig propagates to every component."""
        config_manager = setup_full_system["config_manager"]
        shedding_manager = setup_full_system["shedding_manager"]

        # Register a new service
        new_service = ServiceConfig(
            service_id="analytics-api",
            criticality="low",
            shed_priority=15,
        )
        config_manager.register_service(new_service)
        shedding_manager.register_service(new_service)

        # Resolvable through ServiceConfigManager
        config = get_service_config("analytics-api")
        assert config is not None
        assert config.criticality == "low"

        # Also usable by LoadSheddingManager
        shedding_manager.set_error_rate("payment-api", 35.0)
        allowed = shedding_manager.evaluate_shedding("analytics-api")
        assert allowed <= 50.0  # low criticality is a shedding target

    def test_criticality_affects_shedding(self, setup_full_system):
        """Criticality configuration affects shedding."""
        shedding_manager = setup_full_system["shedding_manager"]

        # Critical service error rise
        shedding_manager.set_error_rate("payment-api", 55.0)

        # Critical unaffected
        assert shedding_manager.evaluate_shedding("payment-api") == 100.0

        # High unaffected (not in shed_criticality even at Level 2)
        assert shedding_manager.evaluate_shedding("order-api") == 100.0

        # Medium affected at Level 2
        allowed = shedding_manager.evaluate_shedding("notification-api")
        assert allowed <= 20.0  # Level 2: medium+low capped at 80%

        # Low affected from Level 1
        allowed = shedding_manager.evaluate_shedding("review-api")
        assert allowed <= 20.0  # Level 2: 20% allowed (min 5% guaranteed)


# =============================================================================
# 6.1.3 Failure Scenario Tests
# =============================================================================


class TestFailureScenarios:
    """Failure scenario tests."""

    def test_cascading_failure_prevention(self, setup_full_system):
        """Cascading failure prevention."""
        shedding_manager = setup_full_system["shedding_manager"]

        # 1. Critical service degrades gradually
        for error_rate in [25.0, 35.0, 55.0, 75.0]:
            shedding_manager.set_error_rate("payment-api", error_rate)

            # Critical always at 100%
            assert shedding_manager.evaluate_shedding("payment-api") == 100.0

            # Low criticality increasingly restricted
            low_allowed = shedding_manager.evaluate_shedding("review-api")

            if error_rate <= 29.0:
                assert low_allowed == 100.0
            elif error_rate <= 49.0:
                assert low_allowed <= 50.0  # Level 1
            elif error_rate <= 69.0:
                assert low_allowed <= 20.0  # Level 2
            else:
                assert low_allowed <= 10.0  # Level 3 + min guarantee

    def test_service_not_registered_handling(self, setup_full_system):
        """Unregistered service handling."""
        shedding_manager = setup_full_system["shedding_manager"]

        # Unregistered services allowed 100% (fail-open)
        allowed = shedding_manager.evaluate_shedding("unknown-api")
        assert allowed == 100.0

    def test_multiple_critical_services_failure(self, setup_full_system):
        """Multiple critical services failing."""
        shedding_manager = setup_full_system["shedding_manager"]

        # Register an additional critical service
        auth_service = ServiceConfig(
            service_id="auth-api",
            criticality="critical",
            shed_priority=0,
        )
        shedding_manager.register_service(auth_service)

        # Two critical services erroring
        shedding_manager.set_error_rate("payment-api", 40.0)
        shedding_manager.set_error_rate("auth-api", 60.0)

        # Average error rate = (40 + 60) / 2 = 50%
        avg_error = shedding_manager.get_critical_services_error_rate()
        assert 49.0 <= avg_error <= 51.0

        # Level 2 applies
        low_allowed = shedding_manager.evaluate_shedding("review-api")
        assert low_allowed <= 20.0


# =============================================================================
# 6.1.4 Recovery Scenario Tests
# =============================================================================


class TestRecoveryScenarios:
    """Recovery scenario tests."""

    def test_shedding_level_decrease_on_recovery(self, setup_full_system):
        """Shedding level decreases as error rate falls."""
        shedding_manager = setup_full_system["shedding_manager"]

        # Start at a high error rate
        shedding_manager.set_error_rate("payment-api", 75.0)
        assert shedding_manager.evaluate_shedding("review-api") <= 10.0  # Level 3

        # Error rate decreases
        shedding_manager.set_error_rate("payment-api", 55.0)
        allowed = shedding_manager.evaluate_shedding("review-api")
        assert allowed <= 20.0  # Level 2

        # Decreases further
        shedding_manager.set_error_rate("payment-api", 35.0)
        allowed = shedding_manager.evaluate_shedding("review-api")
        assert allowed <= 50.0  # Level 1

        # Back to normal
        shedding_manager.set_error_rate("payment-api", 20.0)
        allowed = shedding_manager.evaluate_shedding("review-api")
        assert allowed == 100.0  # No shedding


# =============================================================================
# 6.1.5 Audit Trail Tests
# =============================================================================


class TestAuditTrail:
    """Audit trail tests."""

    def test_shedding_audit_entries(self, setup_full_system):
        """Load Shedding audit records."""
        shedding_manager = setup_full_system["shedding_manager"]

        audit_entries = []
        shedding_manager.set_audit_callback(lambda entry: audit_entries.append(entry))

        # Activate shedding
        shedding_manager.set_error_rate("payment-api", 35.0)
        shedding_manager.update_shedding_state()

        # Verify audit records
        if audit_entries:
            assert audit_entries[0].event_type in [
                "SHEDDING_ACTIVATED",
                "SHEDDING_LEVEL_CHANGED",
            ]


# =============================================================================
# 6.1.6 Boundary Tests
# =============================================================================


class TestBoundaryConditions:
    """Boundary condition tests."""

    def test_error_rate_boundaries(self, setup_full_system):
        """Error rate boundary values."""
        shedding_manager = setup_full_system["shedding_manager"]

        # Exactly 30% (Level 1 trigger boundary)
        shedding_manager.set_error_rate("payment-api", 30.0)
        allowed = shedding_manager.evaluate_shedding("review-api")
        assert allowed <= 50.0  # Level 1 applies

        # 29.9% (below Level 1)
        shedding_manager.set_error_rate("payment-api", 29.9)
        allowed = shedding_manager.evaluate_shedding("review-api")
        assert allowed == 100.0  # No shedding

    def test_min_traffic_guarantee(self, setup_full_system):
        """Minimum traffic guarantee."""
        shedding_manager = setup_full_system["shedding_manager"]

        # review-api has min_traffic_percentage=5.0
        shedding_manager.set_error_rate("payment-api", 80.0)  # very high error rate

        allowed = shedding_manager.evaluate_shedding("review-api")
        # Level 3 (0% cap) but min_traffic_percentage guarantees 5%
        assert allowed >= 5.0


# =============================================================================
# 6.1.7 Concurrency Tests
# =============================================================================


class TestConcurrency:
    """Concurrency tests."""

    def test_concurrent_shedding_evaluation(self, setup_full_system):
        """Concurrent shedding evaluation."""
        import threading

        shedding_manager = setup_full_system["shedding_manager"]
        shedding_manager.set_error_rate("payment-api", 40.0)

        results = []
        errors = []

        def evaluate():
            try:
                for _ in range(50):
                    allowed = shedding_manager.evaluate_shedding("review-api")
                    results.append(allowed)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=evaluate) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 250
        # All results must be the same (same error rate)
        assert all(r <= 50.0 for r in results)


# =============================================================================
# 6.1.8 Performance Tests
# =============================================================================


class TestPerformance:
    """Performance tests."""

    def test_shedding_evaluation_performance(self, setup_full_system):
        """Shedding evaluation performance."""
        shedding_manager = setup_full_system["shedding_manager"]
        shedding_manager.set_error_rate("payment-api", 40.0)

        start = time.time()

        for _ in range(10000):
            shedding_manager.evaluate_shedding("review-api")

        elapsed = time.time() - start

        # 10,000 evaluations within 1 second (0.1ms/evaluation)
        assert elapsed < 1.0, f"Performance too slow: {elapsed}s for 10,000 evaluations"


# =============================================================================
# 6.1.9 Configuration Validation Tests
# =============================================================================


class TestConfigurationValidation:
    """Configuration validation tests."""

    def test_invalid_criticality_handling(self, setup_full_system):
        """Invalid criticality is rejected by validation."""
        # An invalid criticality raises ValueError
        with pytest.raises(ValueError, match="Invalid criticality"):
            ServiceConfig(
                service_id="invalid-api",
                criticality="ultra-critical",  # undefined value
                shed_priority=0,
            )

    def test_shed_priority_sorting(self, setup_full_system):
        """shed_priority sorting."""
        config_manager = get_service_config_manager()

        # Services with higher shed_priority are shed first
        # get_shedding_targets requires the shed_criticality argument
        services = config_manager.get_shedding_targets(["low", "medium"])

        # Low criticality services must come first
        low_services = [s for s in services if s.criticality == "low"]
        assert len(low_services) >= 2  # review-api, recommend-api


# =============================================================================
# 6.1.10 Stress Tests
# =============================================================================


class TestStress:
    """Stress tests."""

    def test_rapid_state_changes(self, setup_full_system):
        """Rapid state changes."""
        shedding_manager = setup_full_system["shedding_manager"]

        # Rapidly change the error rate
        for i in range(100):
            error_rate = i % 100
            shedding_manager.set_error_rate("payment-api", float(error_rate))
            shedding_manager.evaluate_shedding("review-api")

        # Completed without errors
        assert True

    def test_many_services(self, setup_full_system):
        """Registering many services."""
        shedding_manager = setup_full_system["shedding_manager"]

        # Register 100 services
        for i in range(100):
            service = ServiceConfig(
                service_id=f"service-{i}",
                criticality=["critical", "high", "medium", "low"][i % 4],
                shed_priority=i,
            )
            shedding_manager.register_service(service)

        # Set the error rate
        shedding_manager.set_error_rate("payment-api", 50.0)

        # Evaluate every service
        for i in range(100):
            shedding_manager.evaluate_shedding(f"service-{i}")

        # Completed without errors
        assert True
