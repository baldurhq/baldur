"""
Circuit Breaker Advanced Protection Tests

Data models and settings of the Circuit Breaker advanced protection surface.
"""

import pytest

from baldur.core.config import (
    CircuitBreakerAdvancedConfig as CoreCBAdvancedConfig,
)
from baldur.core.config import (
    get_circuit_breaker_advanced_settings,
)
from baldur.services.circuit_breaker.models import (
    FreezeModeState,
    LoadSheddingPolicy,
    PanicThresholdConfig,
    ServiceConfig,
    SheddingLevel,
)

# =============================================================================
# ServiceConfig Tests
# =============================================================================


class TestServiceConfig:
    """ServiceConfig data model."""

    def test_valid_service_config(self):
        """A well-formed service config is accepted."""
        config = ServiceConfig(
            service_id="payment-api",
            criticality="critical",
            shed_priority=0,
            min_traffic_percentage=100.0,
        )
        assert config.service_id == "payment-api"
        assert config.criticality == "critical"
        assert config.shed_priority == 0
        assert config.min_traffic_percentage == 100.0

    def test_all_criticality_levels(self):
        """Every criticality level is accepted."""
        for level in ["critical", "high", "medium", "low"]:
            config = ServiceConfig(service_id=f"test-{level}", criticality=level)
            assert config.criticality == level

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"service_id": "test", "criticality": "invalid"}, "Invalid criticality"),
            (
                {
                    "service_id": "test",
                    "criticality": "low",
                    "min_traffic_percentage": -1.0,
                },
                "min_traffic_percentage must be between",
            ),
            (
                {
                    "service_id": "test",
                    "criticality": "low",
                    "min_traffic_percentage": 101.0,
                },
                "min_traffic_percentage must be between",
            ),
            (
                {"service_id": "test", "criticality": "low", "shed_priority": -1},
                "shed_priority must be non-negative",
            ),
        ],
        ids=[
            "invalid_criticality",
            "negative_min_traffic",
            "over100_min_traffic",
            "negative_shed_priority",
        ],
    )
    def test_invalid_service_config_raises_error(self, kwargs, match):
        """Invalid values raise."""
        with pytest.raises(ValueError, match=match):
            ServiceConfig(**kwargs)

    def test_service_config_with_threshold_overrides(self):
        """Per-service CB threshold overrides."""
        config = ServiceConfig(
            service_id="sensitive-api",
            criticality="high",
            failure_threshold=10,
            window_seconds=120,
        )
        assert config.failure_threshold == 10
        assert config.window_seconds == 120


# =============================================================================
# SheddingLevel Tests
# =============================================================================


class TestSheddingLevel:
    """SheddingLevel data model."""

    def test_valid_shedding_level(self):
        """A well-formed shedding level is accepted as given."""
        level = SheddingLevel(
            error_rate=30.0,
            shed_criticality=["low"],
            traffic_limit=50.0,
            description="Level 1",
        )
        assert level.error_rate == 30.0
        assert level.shed_criticality == ["low"]
        assert level.traffic_limit == 50.0

    def test_critical_in_shed_criticality_raises_error(self):
        """critical can never be a shedding target."""
        with pytest.raises(ValueError, match="'critical' cannot be included"):
            SheddingLevel(
                error_rate=70.0,
                shed_criticality=["low", "critical"],
                traffic_limit=0.0,
            )

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            (
                {
                    "error_rate": -10.0,
                    "shed_criticality": ["low"],
                    "traffic_limit": 50.0,
                },
                "error_rate must be between",
            ),
            (
                {
                    "error_rate": 150.0,
                    "shed_criticality": ["low"],
                    "traffic_limit": 50.0,
                },
                "error_rate must be between",
            ),
            (
                {
                    "error_rate": 30.0,
                    "shed_criticality": ["low"],
                    "traffic_limit": -1.0,
                },
                "traffic_limit must be between",
            ),
        ],
        ids=["negative_error_rate", "over100_error_rate", "negative_traffic_limit"],
    )
    def test_invalid_shedding_level_raises_error(self, kwargs, match):
        """Invalid values raise."""
        with pytest.raises(ValueError, match=match):
            SheddingLevel(**kwargs)


# =============================================================================
# LoadSheddingPolicy Tests
# =============================================================================


class TestLoadSheddingPolicy:
    """LoadSheddingPolicy data model."""

    def test_default_policy(self):
        """Default Load Shedding policy."""
        policy = LoadSheddingPolicy()
        assert policy.enabled is True
        assert policy.trigger_threshold == 30.0
        assert len(policy.levels) == 3

    def test_default_levels_progressive(self):
        """Default levels tighten progressively."""
        policy = LoadSheddingPolicy()

        # Level 1: 30% error rate, low restricted to 50%
        assert policy.levels[0].error_rate == 30.0
        assert policy.levels[0].shed_criticality == ["low"]
        assert policy.levels[0].traffic_limit == 50.0

        # Level 2: 50% error rate, low+medium restricted by 80%
        assert policy.levels[1].error_rate == 50.0
        assert "medium" in policy.levels[1].shed_criticality
        assert policy.levels[1].traffic_limit == 20.0

        # Level 3: 70% error rate, low+medium fully blocked
        assert policy.levels[2].error_rate == 70.0
        assert policy.levels[2].traffic_limit == 0.0

    def test_custom_policy(self):
        """A custom policy keeps the levels it was built with."""
        custom_levels = [
            SheddingLevel(
                error_rate=40.0, shed_criticality=["low"], traffic_limit=60.0
            ),
            SheddingLevel(
                error_rate=80.0, shed_criticality=["low", "medium"], traffic_limit=0.0
            ),
        ]
        policy = LoadSheddingPolicy(
            enabled=True, trigger_threshold=40.0, levels=custom_levels
        )
        assert policy.trigger_threshold == 40.0
        assert len(policy.levels) == 2


# =============================================================================
# PanicThresholdConfig Tests
# =============================================================================


class TestPanicThresholdConfig:
    """PanicThresholdConfig data model."""

    def test_default_config(self):
        """Default Panic Threshold configuration."""
        config = PanicThresholdConfig()
        assert config.threshold_percent == 70.0
        assert config.action == "freeze"
        assert config.consecutive_triggers_required == 2
        assert config.min_registered_services == 3

    def test_alert_only_action(self):
        """The alert_only action is accepted by the validator."""
        config = PanicThresholdConfig(action="alert_only")
        assert config.action == "alert_only"

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"threshold_percent": 150.0}, "threshold_percent must be between"),
            ({"action": "shutdown"}, "Invalid action"),
            (
                {"consecutive_triggers_required": 0},
                "consecutive_triggers_required must be",
            ),
            ({"min_registered_services": 0}, "min_registered_services must be"),
        ],
        ids=[
            "invalid_threshold",
            "invalid_action",
            "invalid_consecutive",
            "invalid_min_services",
        ],
    )
    def test_invalid_panic_config_raises_error(self, kwargs, match):
        """Invalid configuration raises."""
        with pytest.raises(ValueError, match=match):
            PanicThresholdConfig(**kwargs)


# =============================================================================
# FreezeModeState Tests
# =============================================================================


class TestFreezeModeState:
    """FreezeModeState data model."""

    def test_default_inactive(self):
        """The default state is inactive."""
        state = FreezeModeState()
        assert state.active is False
        assert state.activated_at is None
        assert state.reason == ""
        assert state.activated_by == ""

    def test_active_state(self):
        """An active state."""
        state = FreezeModeState(
            active=True,
            activated_at="2026-01-05T14:30:00Z",
            reason="Freeze Mode activated due to LOCKDOWN entry",
            activated_by="system",
        )
        assert state.active is True
        assert state.activated_at == "2026-01-05T14:30:00Z"
        assert "LOCKDOWN" in state.reason

    def test_operator_activation(self):
        """An operator-attributed activation."""
        state = FreezeModeState(
            active=True,
            activated_at="2026-01-05T14:30:00Z",
            reason="emergency maintenance",
            activated_by="operator:admin",
        )
        assert state.activated_by == "operator:admin"


# =============================================================================
# Core Config Integration Tests
# =============================================================================


class TestCoreConfigIntegration:
    """core/config.py integration.

    NOTE: the API changed with the Pydantic v2 migration.
    - circuit_breaker_advanced is a settings class of its own
    - reached through get_circuit_breaker_advanced_settings()
    - serialized with model_dump() / model_validate()
    """

    def test_circuit_breaker_advanced_config_available(self):
        """CircuitBreakerAdvancedSettings is reachable on its own."""
        settings = get_circuit_breaker_advanced_settings()
        assert settings is not None
        assert isinstance(settings, CoreCBAdvancedConfig)

    def test_default_values(self):
        """Defaults (deferred surface: every enable flag defaults False)."""
        cb_advanced = get_circuit_breaker_advanced_settings()

        assert cb_advanced.enabled is False
        assert cb_advanced.load_shedding_enabled is False
        assert cb_advanced.panic_threshold_percent == 70.0
        assert cb_advanced.panic_threshold_action == "freeze"

    def test_model_validate(self):
        """Settings load through Pydantic v2 model_validate."""
        config_dict = {
            "enabled": False,
            "panic_threshold_percent": 80.0,
        }
        config = CoreCBAdvancedConfig.model_validate(config_dict)

        assert config.enabled is False
        assert config.panic_threshold_percent == 80.0

    def test_model_dump(self):
        """Settings serialize through Pydantic v2 model_dump."""
        config = CoreCBAdvancedConfig()
        config_dict = config.model_dump()

        assert "enabled" in config_dict
        assert config_dict["enabled"] is False
        assert config_dict["panic_threshold_percent"] == 70.0

    def test_get_circuit_breaker_advanced_settings(self):
        """The convenience getter returns the defaults."""
        # Confirms the defaults come back
        settings = get_circuit_breaker_advanced_settings()
        assert settings.enabled is False
        assert settings.panic_threshold_percent == 70.0


# =============================================================================
# Design Decision Tests
# =============================================================================


class TestDesignDecisions:
    """Design decisions the documentation states."""

    def test_critical_cannot_be_shed(self):
        """A critical service can never be a Load Shedding target."""
        with pytest.raises(ValueError):
            SheddingLevel(
                error_rate=70.0,
                shed_criticality=["low", "critical"],  # including critical raises
                traffic_limit=0.0,
            )
