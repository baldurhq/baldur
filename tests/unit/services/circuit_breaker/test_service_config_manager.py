"""
Circuit Breaker ServiceConfigManager tests.

Test Coverage:
- ServiceConfigManager: service registration, criticality lookup, Load Shedding target selection
- ServiceConfig immutability: frozen dataclass (top-level + nested recovery config)
"""

from dataclasses import FrozenInstanceError, replace

import pytest

from baldur.services.circuit_breaker.models import (
    CanaryRecoveryStageConfig,
    RecoveryStrategy,
    ServiceConfig,
)

# =============================================================================
# 3.1 ServiceConfigManager Tests
# =============================================================================


class TestServiceConfigManager:
    """ServiceConfigManager tests."""

    def setup_method(self):
        """Reset the singleton before each test."""
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()

    def teardown_method(self):
        """Clean up after each test."""
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()

    def test_singleton_pattern(self):
        """Singleton pattern behavior."""
        from baldur.services.circuit_breaker.service_config import (
            ServiceConfigManager,
            get_service_config_manager,
        )

        manager1 = ServiceConfigManager()
        manager2 = get_service_config_manager()

        assert manager1 is manager2

    def test_register_service_success(self):
        """Service registration succeeds."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        config = ServiceConfig(
            service_id="payment-api",
            criticality="critical",
            shed_priority=0,
        )

        result = manager.register_service(config)

        assert result is True
        assert manager.get_service_count() == 1
        assert manager.get_service_config("payment-api") is not None

    def test_register_services_bulk(self):
        """Bulk registration of multiple services."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        configs = [
            ServiceConfig(
                service_id="payment-api", criticality="critical", shed_priority=0
            ),
            ServiceConfig(service_id="order-api", criticality="high", shed_priority=1),
            ServiceConfig(service_id="review-api", criticality="low", shed_priority=10),
        ]

        count = manager.register_services(configs)

        assert count == 3
        assert manager.get_service_count() == 3

    def test_unregister_service(self):
        """Service unregistration."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_service(
            ServiceConfig(
                service_id="test-api",
                criticality="low",
            )
        )

        result = manager.unregister_service("test-api")

        assert result is True
        assert manager.get_service_config("test-api") is None

    def test_unregister_nonexistent_service(self):
        """Unregistering a nonexistent service."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        result = manager.unregister_service("nonexistent")

        assert result is False

    def test_get_services_by_criticality(self):
        """Look up services by criticality."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_services(
            [
                ServiceConfig(service_id="payment-api", criticality="critical"),
                ServiceConfig(service_id="auth-api", criticality="critical"),
                ServiceConfig(service_id="order-api", criticality="high"),
                ServiceConfig(service_id="review-api", criticality="low"),
            ]
        )

        critical_services = manager.get_services_by_criticality("critical")

        assert len(critical_services) == 2
        assert all(s.criticality == "critical" for s in critical_services)

    def test_get_critical_services(self):
        """Convenience lookup for critical services."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_services(
            [
                ServiceConfig(service_id="payment-api", criticality="critical"),
                ServiceConfig(service_id="review-api", criticality="low"),
            ]
        )

        critical = manager.get_critical_services()

        assert len(critical) == 1
        assert critical[0].service_id == "payment-api"

    def test_get_non_critical_services(self):
        """Look up non-critical services."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_services(
            [
                ServiceConfig(service_id="payment-api", criticality="critical"),
                ServiceConfig(service_id="order-api", criticality="high"),
                ServiceConfig(service_id="review-api", criticality="low"),
            ]
        )

        non_critical = manager.get_non_critical_services()

        assert len(non_critical) == 2
        assert all(s.criticality != "critical" for s in non_critical)


# =============================================================================
# 3.1.1 ServiceConfig Input Validation Tests (P1 - external input defense)
# =============================================================================


class TestServiceConfigInputValidation:
    """ServiceConfig input validation - defense against invalid input."""

    def test_invalid_criticality_raises_error(self):
        """An invalid criticality value raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            ServiceConfig(service_id="test-api", criticality="invalid")

        assert "Invalid criticality" in str(exc_info.value)
        assert "invalid" in str(exc_info.value)

    def test_criticality_typo_raises_error(self):
        """A criticality typo raises ValueError (Critical vs critical)."""
        with pytest.raises(ValueError):
            ServiceConfig(service_id="test-api", criticality="Critical")  # capitalized

        with pytest.raises(ValueError):
            ServiceConfig(service_id="test-api", criticality="HIGH")  # all caps

    def test_negative_shed_priority_raises_error(self):
        """A negative shed_priority raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            ServiceConfig(
                service_id="test-api",
                criticality="low",
                shed_priority=-1,
            )

        assert "shed_priority" in str(exc_info.value)
        assert "non-negative" in str(exc_info.value) or "-1" in str(exc_info.value)

    def test_min_traffic_percentage_below_zero_raises_error(self):
        """min_traffic_percentage below 0 raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            ServiceConfig(
                service_id="test-api",
                criticality="low",
                min_traffic_percentage=-10.0,
            )

        assert "min_traffic_percentage" in str(exc_info.value)

    def test_min_traffic_percentage_above_100_raises_error(self):
        """min_traffic_percentage above 100 raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            ServiceConfig(
                service_id="test-api",
                criticality="low",
                min_traffic_percentage=150.0,
            )

        assert "min_traffic_percentage" in str(exc_info.value)

    def test_valid_criticality_values_accepted(self):
        """Valid criticality values construct normally."""
        valid_levels = ["critical", "high", "medium", "low"]

        for level in valid_levels:
            config = ServiceConfig(service_id=f"test-{level}", criticality=level)
            assert config.criticality == level

    def test_boundary_min_traffic_percentage_accepted(self):
        """Boundary min_traffic_percentage values (0, 100) construct normally."""
        config_zero = ServiceConfig(
            service_id="test-zero",
            criticality="low",
            min_traffic_percentage=0.0,
        )
        assert config_zero.min_traffic_percentage == 0.0

        config_hundred = ServiceConfig(
            service_id="test-hundred",
            criticality="low",
            min_traffic_percentage=100.0,
        )
        assert config_hundred.min_traffic_percentage == 100.0


# =============================================================================
# 3.1.2 ServiceConfig Immutability (frozen dataclass)
# =============================================================================


class TestServiceConfigImmutabilityBehavior:
    """Frozen ServiceConfig closes the unaudited mutation side-door:
    every aliasing path (manager-returned, registrant-retained) fails
    loud on assignment, so shedding/recovery behavior can only change
    through the validated, logged registration path."""

    def setup_method(self):
        """Reset the singleton before each test."""
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()

    def teardown_method(self):
        """Clean up after each test."""
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()

    def test_top_level_field_assignment_raises_frozen_instance_error(self):
        """Assigning a ServiceConfig field raises FrozenInstanceError."""
        config = ServiceConfig(service_id="payment-api", criticality="critical")

        with pytest.raises(FrozenInstanceError):
            config.criticality = "low"

    def test_nested_recovery_strategy_field_assignment_raises(self):
        """The nested recovery strategy is frozen with it (deep freeze)."""
        config = ServiceConfig(
            service_id="payment-api",
            criticality="critical",
            recovery_strategy=RecoveryStrategy(type="canary", strict_mode=True),
        )

        with pytest.raises(FrozenInstanceError):
            config.recovery_strategy.strict_mode = False

    def test_canary_stage_field_assignment_raises(self):
        """Individual canary stages are frozen too."""
        strategy = RecoveryStrategy()

        with pytest.raises(FrozenInstanceError):
            strategy.canary_stages[0].traffic_percent = 100.0

    def test_caller_supplied_stage_list_is_coerced_to_tuple(self):
        """A caller-supplied stage list is accepted and stored as a tuple."""
        stage = CanaryRecoveryStageConfig(
            traffic_percent=50.0,
            duration_seconds=1,
            required_success_rate=90.0,
        )

        strategy = RecoveryStrategy(canary_stages=[stage])

        assert isinstance(strategy.canary_stages, tuple)
        assert strategy.canary_stages == (stage,)

    def test_manager_returned_config_cannot_alter_shedding_behavior(self):
        """A mutation attempt on a manager-returned config raises, and
        shedding selection keeps reflecting the registered values."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_service(
            ServiceConfig(service_id="review-api", criticality="low", shed_priority=10)
        )
        returned = manager.get_service_config("review-api")

        with pytest.raises(FrozenInstanceError):
            returned.shed_priority = 0

        targets = manager.get_shedding_targets(["low"])
        assert [t.service_id for t in targets] == ["review-api"]

    def test_update_routes_through_replace_and_reregistration(self):
        """dataclasses.replace + register_service is the update path."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        original = ServiceConfig(
            service_id="order-api", criticality="high", shed_priority=1
        )
        manager.register_service(original)

        manager.register_service(replace(original, criticality="medium"))

        assert manager.get_service_config("order-api").criticality == "medium"


class TestServiceConfigLoadShedding:
    """ServiceConfigManager Load Shedding tests."""

    def setup_method(self):
        """Reset the singleton before each test."""
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()

    def teardown_method(self):
        """Clean up after each test."""
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()

    def test_get_shedding_targets(self):
        """Look up Load Shedding targets."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_services(
            [
                ServiceConfig(
                    service_id="payment-api", criticality="critical", shed_priority=0
                ),
                ServiceConfig(
                    service_id="review-api", criticality="low", shed_priority=10
                ),
                ServiceConfig(
                    service_id="recommend-api", criticality="low", shed_priority=5
                ),
                ServiceConfig(
                    service_id="analytics-api", criticality="medium", shed_priority=3
                ),
            ]
        )

        targets = manager.get_shedding_targets(["low"])

        assert len(targets) == 2
        # Sorted by shed_priority descending
        assert targets[0].service_id == "review-api"
        assert targets[1].service_id == "recommend-api"

    def test_get_shedding_targets_multiple_criticality(self):
        """Look up shedding targets across multiple criticality levels."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_services(
            [
                ServiceConfig(
                    service_id="payment-api", criticality="critical", shed_priority=0
                ),
                ServiceConfig(
                    service_id="review-api", criticality="low", shed_priority=10
                ),
                ServiceConfig(
                    service_id="analytics-api", criticality="medium", shed_priority=5
                ),
            ]
        )

        targets = manager.get_shedding_targets(["low", "medium"])

        assert len(targets) == 2
        assert targets[0].service_id == "review-api"  # priority 10
        assert targets[1].service_id == "analytics-api"  # priority 5

    def test_shed_priority_zero_excluded(self):
        """Services with shed_priority=0 are excluded from shedding."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_services(
            [
                ServiceConfig(
                    service_id="payment-api", criticality="critical", shed_priority=0
                ),
                ServiceConfig(
                    service_id="review-api", criticality="low", shed_priority=0
                ),  # excluded
            ]
        )

        targets = manager.get_shedding_targets(["low", "critical"])

        assert len(targets) == 0

    def test_get_shedding_order(self):
        """Look up the full shedding order."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_services(
            [
                ServiceConfig(
                    service_id="review-api", criticality="low", shed_priority=10
                ),
                ServiceConfig(
                    service_id="analytics-api", criticality="medium", shed_priority=5
                ),
                ServiceConfig(
                    service_id="payment-api", criticality="critical", shed_priority=0
                ),
            ]
        )

        order = manager.get_shedding_order()

        # Only shed_priority > 0, sorted descending
        assert len(order) == 2
        assert order[0].service_id == "review-api"
        assert order[1].service_id == "analytics-api"

    def test_is_sheddable(self):
        """Check whether a service is a shedding target."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_services(
            [
                ServiceConfig(
                    service_id="payment-api", criticality="critical", shed_priority=0
                ),
                ServiceConfig(
                    service_id="review-api", criticality="low", shed_priority=10
                ),
            ]
        )

        assert manager.is_sheddable("payment-api") is False
        assert manager.is_sheddable("review-api") is True
        assert manager.is_sheddable("nonexistent") is False


class TestServiceConfigRecoveryStrategy:
    """ServiceConfigManager Recovery strategy tests."""

    def setup_method(self):
        """Reset the singleton before each test."""
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()

    def teardown_method(self):
        """Clean up after each test."""
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()

    def test_get_recovery_strategy_default(self):
        """Returns the default Recovery strategy."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_service(
            ServiceConfig(
                service_id="test-api",
                criticality="medium",
            )
        )

        strategy = manager.get_recovery_strategy("test-api")

        assert strategy.type == "canary"  # default

    def test_get_recovery_strategy_service_override(self):
        """Per-service Recovery strategy override."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_service(
            ServiceConfig(
                service_id="payment-api",
                criticality="critical",
                recovery_strategy=RecoveryStrategy(type="canary", strict_mode=True),
            )
        )

        strategy = manager.get_recovery_strategy("payment-api")

        assert strategy.type == "canary"
        assert strategy.strict_mode is True

    def test_set_default_recovery_strategy(self):
        """Set the default Recovery strategy."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.set_default_recovery_strategy(RecoveryStrategy(type="immediate"))
        manager.register_service(
            ServiceConfig(
                service_id="test-api",
                criticality="medium",
            )
        )

        strategy = manager.get_recovery_strategy("test-api")

        assert strategy.type == "immediate"


class TestServiceConfigThresholdOverride:
    """ServiceConfigManager threshold override tests."""

    def setup_method(self):
        """Reset the singleton before each test."""
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()

    def teardown_method(self):
        """Clean up after each test."""
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()

    def test_get_failure_threshold_default(self):
        """Returns the default failure threshold."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_service(
            ServiceConfig(
                service_id="test-api",
                criticality="medium",
            )
        )

        threshold = manager.get_failure_threshold("test-api", default=5)

        assert threshold == 5

    def test_get_failure_threshold_service_override(self):
        """Per-service failure threshold override."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_service(
            ServiceConfig(
                service_id="payment-api",
                criticality="critical",
                failure_threshold=10,  # override
            )
        )

        threshold = manager.get_failure_threshold("payment-api", default=5)

        assert threshold == 10

    def test_get_window_seconds_service_override(self):
        """Per-service window override."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_service(
            ServiceConfig(
                service_id="payment-api",
                criticality="critical",
                window_seconds=120,  # override
            )
        )

        window = manager.get_window_seconds("payment-api", default=60)

        assert window == 120
