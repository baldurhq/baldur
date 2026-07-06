"""
342 Settings Singleton Migration — Verification Tests.

Verifies:
- Migrated get_*/reset_* functions return/clear settings via RootConfig groups
- Root convenience getters access correct group paths
- reset_config() cross-cutting reset invalidates all group-cached settings
- warn_default_cluster_id validator works with multi_region.namespace path
"""

from __future__ import annotations

import pytest

from baldur.settings.root import (
    BaldurSettings,
    get_config,
    reset_config,
)


@pytest.fixture(autouse=True)
def _reset_root():
    """Reset root config before/after each test for isolation."""
    reset_config()
    yield
    reset_config()


# =============================================================================
# Contract Tests — Convenience getter paths return correct types
# =============================================================================


class TestConvenienceGetterPathContract:
    """Root convenience getters access correct group paths and return expected types.

    The per-domain env accessors that collided with RuntimeConfigManager methods
    (get_circuit_breaker_config / get_dlq_config / get_retry_config /
    get_rate_limit_config / get_notification_config / get_forensic_config) were
    removed (686 D7); the surviving root convenience getters are the two whose
    names never collided (_thresholds, not _config). Per-module get_*_settings()
    type contracts are covered by TestMigratedSingletonLifecycleBehavior below.
    """

    def test_get_sla_thresholds_returns_sla_settings(self):
        """get_sla_thresholds() returns SLASettings via slo_group."""
        from baldur.settings.root import get_sla_thresholds
        from baldur.settings.sla import SLASettings

        result = get_sla_thresholds()
        assert isinstance(result, SLASettings)

    def test_get_security_thresholds_returns_security_settings(self):
        """get_security_thresholds() returns SecuritySettings via security_group."""
        from baldur.settings.root import get_security_thresholds
        from baldur.settings.security import SecuritySettings

        result = get_security_thresholds()
        assert isinstance(result, SecuritySettings)


class TestPerModuleGetterContract:
    """The per-module get_*_settings() getters (canonical env accessors after the
    686 D7 collision cleanup) return their expected settings types."""

    def test_get_dlq_settings_returns_dlq_settings(self):
        """get_dlq_settings() returns DLQSettings."""
        from baldur.settings.dlq import DLQSettings, get_dlq_settings

        assert isinstance(get_dlq_settings(), DLQSettings)

    def test_get_retry_settings_returns_retry_settings(self):
        """get_retry_settings() returns RetrySettings."""
        from baldur.settings.retry import RetrySettings, get_retry_settings

        assert isinstance(get_retry_settings(), RetrySettings)

    def test_get_rate_limit_settings_returns_rate_limit_settings(self):
        """get_rate_limit_settings() returns RateLimitSettings."""
        from baldur.settings.rate_limit import (
            RateLimitSettings,
            get_rate_limit_settings,
        )

        assert isinstance(get_rate_limit_settings(), RateLimitSettings)


# =============================================================================
# Behavior Tests — Migrated singleton lifecycle (representative per group)
# =============================================================================


class TestMigratedSingletonLifecycleBehavior:
    """get_*/reset_* for migrated settings use RootConfig group path correctly."""

    def test_core_circuit_breaker_get_returns_cached_instance(self):
        """get_circuit_breaker_settings() returns same instance on repeated calls."""
        from baldur.settings.circuit_breaker import get_circuit_breaker_settings

        first = get_circuit_breaker_settings()
        second = get_circuit_breaker_settings()
        assert first is second

    def test_core_circuit_breaker_reset_clears_cached_instance(self):
        """reset_circuit_breaker_settings() clears cached instance, next get creates new."""
        from baldur.settings.circuit_breaker import (
            get_circuit_breaker_settings,
            reset_circuit_breaker_settings,
        )

        first = get_circuit_breaker_settings()
        reset_circuit_breaker_settings()
        second = get_circuit_breaker_settings()
        assert first is not second

    def test_services_dlq_get_returns_cached_instance(self):
        """get_dlq_settings() returns same instance on repeated calls."""
        from baldur.settings.dlq import get_dlq_settings

        first = get_dlq_settings()
        second = get_dlq_settings()
        assert first is second

    def test_services_dlq_reset_clears_cached_instance(self):
        """reset_dlq_settings() clears cached instance."""
        from baldur.settings.dlq import get_dlq_settings, reset_dlq_settings

        first = get_dlq_settings()
        reset_dlq_settings()
        second = get_dlq_settings()
        assert first is not second

    def test_slo_sla_get_returns_cached_instance(self):
        """get_sla_settings() returns same instance on repeated calls."""
        from baldur.settings.sla import get_sla_settings

        first = get_sla_settings()
        second = get_sla_settings()
        assert first is second

    def test_slo_error_budget_reset_clears_cached_instance(self):
        """reset_error_budget_settings() clears cached instance."""
        from baldur.settings.error_budget import (
            get_error_budget_settings,
            reset_error_budget_settings,
        )

        first = get_error_budget_settings()
        reset_error_budget_settings()
        second = get_error_budget_settings()
        assert first is not second

    def test_scaling_rate_limit_get_returns_cached_instance(self):
        """get_rate_limit_settings() returns same instance on repeated calls."""
        from baldur.settings.rate_limit import get_rate_limit_settings

        first = get_rate_limit_settings()
        second = get_rate_limit_settings()
        assert first is second

    def test_metrics_detection_get_returns_cached_instance(self):
        """get_detection_settings() returns same instance on repeated calls."""
        from baldur.settings.detection import get_detection_settings

        first = get_detection_settings()
        second = get_detection_settings()
        assert first is second

    def test_multi_region_namespace_get_returns_cached_instance(self):
        """get_namespace_settings() returns same instance on repeated calls."""
        from baldur.settings.namespace import get_namespace_settings

        first = get_namespace_settings()
        second = get_namespace_settings()
        assert first is second

    def test_security_security_get_returns_cached_instance(self):
        """get_security_settings() returns same instance on repeated calls."""
        from baldur.settings.security import get_security_settings

        first = get_security_settings()
        second = get_security_settings()
        assert first is second

    def test_obs_logging_get_returns_cached_instance(self):
        """get_logging_settings() returns same instance on repeated calls."""
        from baldur.settings.logging_settings import get_logging_settings

        first = get_logging_settings()
        second = get_logging_settings()
        assert first is second

    def test_adapters_kafka_producer_get_returns_cached_instance(self):
        """get_kafka_producer_settings() returns same instance on repeated calls."""
        from baldur.settings.kafka_producer import get_kafka_producer_settings

        first = get_kafka_producer_settings()
        second = get_kafka_producer_settings()
        assert first is second

    def test_adapters_redis_lifecycle(self):
        """AdaptersGroup redis: get/reset lifecycle works correctly."""
        from baldur.settings.redis import get_redis_settings, reset_redis_settings

        first = get_redis_settings()
        second = get_redis_settings()
        assert first is second

        reset_redis_settings()
        third = get_redis_settings()
        assert first is not third


# =============================================================================
# Behavior Tests — Cross-cutting reset
# =============================================================================


class TestCrossCuttingResetBehavior:
    """reset_config() invalidates all settings across all groups."""

    def test_reset_config_invalidates_core_settings(self):
        """reset_config() causes core group settings to be recreated."""
        from baldur.settings.circuit_breaker import get_circuit_breaker_settings

        first = get_circuit_breaker_settings()
        reset_config()
        second = get_circuit_breaker_settings()
        assert first is not second

    def test_reset_config_invalidates_services_settings(self):
        """reset_config() causes services group settings to be recreated."""
        from baldur.settings.dlq import get_dlq_settings

        first = get_dlq_settings()
        reset_config()
        second = get_dlq_settings()
        assert first is not second

    def test_reset_config_invalidates_slo_settings(self):
        """reset_config() causes SLO group settings to be recreated."""
        from baldur.settings.sla import get_sla_settings

        first = get_sla_settings()
        reset_config()
        second = get_sla_settings()
        assert first is not second

    def test_reset_config_invalidates_scaling_settings(self):
        """reset_config() causes scaling group settings to be recreated."""
        from baldur.settings.rate_limit import get_rate_limit_settings

        first = get_rate_limit_settings()
        reset_config()
        second = get_rate_limit_settings()
        assert first is not second

    def test_reset_config_invalidates_multiple_groups_atomically(self):
        """reset_config() invalidates settings from multiple groups simultaneously."""
        from baldur.settings.circuit_breaker import get_circuit_breaker_settings
        from baldur.settings.dlq import get_dlq_settings
        from baldur.settings.sla import get_sla_settings

        # Given — access settings from 3 different groups
        cb_first = get_circuit_breaker_settings()
        dlq_first = get_dlq_settings()
        sla_first = get_sla_settings()

        # When
        reset_config()

        # Then — all 3 are new instances
        cb_second = get_circuit_breaker_settings()
        dlq_second = get_dlq_settings()
        sla_second = get_sla_settings()
        assert cb_first is not cb_second
        assert dlq_first is not dlq_second
        assert sla_first is not sla_second


# =============================================================================
# Behavior Tests — Validator path migration
# =============================================================================


class TestValidatorPathMigrationBehavior:
    """warn_default_cluster_id validator accesses namespace via multi_region group."""

    def test_validator_does_not_crash_on_default_config(self):
        """Default config construction succeeds (validator accesses multi_region.namespace)."""
        config = BaldurSettings()
        assert config.cluster_id == "default"

    def test_validator_accesses_multi_region_namespace_path(self):
        """Validator accesses self.multi_region.namespace without AttributeError."""
        # The validator runs during __init__ and accesses multi_region.namespace.
        # If the path were wrong, BaldurSettings() would raise AttributeError.
        config = BaldurSettings()
        # multi_region is initialized by the validator
        assert "multi_region" in config.__dict__


# =============================================================================
# Behavior Tests — Batch 2-5 representative lifecycle
# =============================================================================


class TestBatch2To5SingletonLifecycleBehavior:
    """Batch 2-5 migrated settings: get/reset lifecycle works correctly."""

    def test_services_group_apply_strategy_lifecycle(self):
        """ServicesGroup apply_strategy: get/reset works."""
        from baldur.settings.apply_strategy import (
            get_apply_strategy_settings,
            reset_apply_strategy_settings,
        )

        first = get_apply_strategy_settings()
        assert first is get_apply_strategy_settings()
        reset_apply_strategy_settings()
        assert first is not get_apply_strategy_settings()

    def test_services_group_runbook_lifecycle(self):
        """ServicesGroup runbook: get/reset works."""
        from baldur.settings.runbook import (
            get_runbook_settings,
            reset_runbook_settings,
        )

        first = get_runbook_settings()
        assert first is get_runbook_settings()
        reset_runbook_settings()
        assert first is not get_runbook_settings()

    def test_audit_group_cascade_retention_lifecycle(self):
        """AuditGroup cascade_retention: get/reset works."""
        from baldur.settings.cascade_retention import (
            get_cascade_retention_settings,
            reset_cascade_retention_settings,
        )

        first = get_cascade_retention_settings()
        assert first is get_cascade_retention_settings()
        reset_cascade_retention_settings()
        assert first is not get_cascade_retention_settings()

    def test_scaling_group_throttle_lifecycle(self):
        """ScalingGroup throttle: get/reset works."""
        from baldur.settings.throttle import (
            get_throttle_settings,
            reset_throttle_settings,
        )

        first = get_throttle_settings()
        assert first is get_throttle_settings()
        reset_throttle_settings()
        assert first is not get_throttle_settings()

    def test_scaling_group_ring_buffer_lifecycle(self):
        """ScalingGroup ring_buffer: get/reset works."""
        from baldur.settings.ring_buffer import (
            get_ring_buffer_settings,
            reset_ring_buffer_settings,
        )

        first = get_ring_buffer_settings()
        assert first is get_ring_buffer_settings()
        reset_ring_buffer_settings()
        assert first is not get_ring_buffer_settings()


# =============================================================================
# Contract Tests — ROOT-LEVEL group assignment (doc §4.2)
# =============================================================================


class TestRootLevelGroupAssignmentContract:
    """21 ROOT-LEVEL settings are assigned to the correct groups per doc §4.2."""

    def test_circuit_breaker_in_core_group(self):
        """circuit_breaker is accessible via config.core.circuit_breaker."""
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        config = get_config()
        assert isinstance(config.core.circuit_breaker, CircuitBreakerSettings)

    def test_retry_in_core_group(self):
        """retry is accessible via config.core.retry."""
        from baldur.settings.retry import RetrySettings

        config = get_config()
        assert isinstance(config.core.retry, RetrySettings)

    def test_thread_management_in_core_group(self):
        """thread_management is accessible via config.core.thread_management."""
        from baldur.settings.thread_management import ThreadManagementSettings

        config = get_config()
        assert isinstance(config.core.thread_management, ThreadManagementSettings)

    def test_dlq_in_services_group(self):
        """dlq is accessible via config.services_group.dlq."""
        from baldur.settings.dlq import DLQSettings

        config = get_config()
        assert isinstance(config.services_group.dlq, DLQSettings)

    def test_chaos_in_services_group(self):
        """chaos is accessible via config.services_group.chaos."""
        from baldur.settings.chaos import ChaosSettings

        config = get_config()
        assert isinstance(config.services_group.chaos, ChaosSettings)

    def test_error_budget_in_slo_group(self):
        """error_budget is accessible via config.slo_group.error_budget."""
        from baldur.settings.error_budget import ErrorBudgetSettings

        config = get_config()
        assert isinstance(config.slo_group.error_budget, ErrorBudgetSettings)

    def test_sla_in_slo_group(self):
        """sla is accessible via config.slo_group.sla."""
        from baldur.settings.sla import SLASettings

        config = get_config()
        assert isinstance(config.slo_group.sla, SLASettings)

    def test_rate_limit_in_scaling_group(self):
        """rate_limit is accessible via config.scaling.rate_limit."""
        from baldur.settings.rate_limit import RateLimitSettings

        config = get_config()
        assert isinstance(config.scaling.rate_limit, RateLimitSettings)

    def test_namespace_in_multi_region_group(self):
        """namespace is accessible via config.multi_region.namespace."""
        from baldur.settings.namespace import NamespaceSettings

        config = get_config()
        assert isinstance(config.multi_region.namespace, NamespaceSettings)

    def test_security_in_security_group(self):
        """security is accessible via config.security_group.security."""
        from baldur.settings.security import SecuritySettings

        config = get_config()
        assert isinstance(config.security_group.security, SecuritySettings)

    def test_logging_settings_in_obs_group(self):
        """logging_settings is accessible via config.obs.logging_settings."""
        from baldur.settings.logging_settings import LoggingSettings

        config = get_config()
        assert isinstance(config.obs.logging_settings, LoggingSettings)

    def test_kafka_producer_in_adapters_group(self):
        """kafka_producer is accessible via config.adapters.kafka_producer."""
        from baldur.settings.kafka_producer import KafkaProducerSettings

        config = get_config()
        assert isinstance(config.adapters.kafka_producer, KafkaProducerSettings)
