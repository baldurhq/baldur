"""
Settings SSOT Migration — groups.py and root.py unit tests.

Verifies:
- 14 group classes with correct cached_property accessors
- Lazy initialization (not created until accessed)
- Independent reset (delete one doesn't affect others)
- Root group accessors return correct types and are cached
- to_full_dict() serialization behavior
- _cached_property_names() contract
"""

from __future__ import annotations

from functools import cached_property

from baldur.settings.groups import (
    AdaptersGroup,
    AuditGroup,
    CoordinationGroup,
    CoreGroup,
    MetaGroup,
    MetricsGroup,
    MultiRegionGroup,
    ObservabilityGroup,
    ResilienceGroup,
    ScalingGroup,
    SecurityGroup,
    ServicesGroup,
    SLOGroup,
    TestingGroup,
)
from baldur.settings.root import BaldurSettings

# =============================================================================
# Contract Tests — Group class structure
# =============================================================================


class TestGroupClassCountContract:
    """14 group classes exist with the expected cached_property count."""

    @staticmethod
    def _count_cached_properties(cls: type) -> int:
        return sum(
            1 for name, val in vars(cls).items() if isinstance(val, cached_property)
        )

    def test_total_group_classes_is_14(self):
        """Exactly 14 group classes defined in groups.py."""
        group_classes = [
            CoreGroup,
            ServicesGroup,
            AuditGroup,
            CoordinationGroup,
            MultiRegionGroup,
            MetricsGroup,
            ScalingGroup,
            ResilienceGroup,
            ObservabilityGroup,
            AdaptersGroup,
            SecurityGroup,
            SLOGroup,
            MetaGroup,
            TestingGroup,
        ]
        assert len(group_classes) == 14

    def test_core_group_has_9_properties(self):
        """CoreGroup: admission_control, backoff, circuit_breaker, circuit_breaker_advanced, health_check, pool_monitor, retry, system_control, thread_management."""
        assert self._count_cached_properties(CoreGroup) == 9

    def test_services_group_has_47_properties(self):
        """ServicesGroup has 47 cached_property accessors (incl. dlq_outbox per #486,
        canary_interlock per the canary-safeguard wiring)."""
        assert self._count_cached_properties(ServicesGroup) == 47

    def test_audit_group_has_8_properties(self):
        """AuditGroup has 8 cached_property accessors (668 removed the reconciler group)."""
        assert self._count_cached_properties(AuditGroup) == 8

    def test_coordination_group_has_3_properties(self):
        """CoordinationGroup: distributed_lock, leader_election, redis_key_guard."""
        assert self._count_cached_properties(CoordinationGroup) == 3

    def test_multi_region_group_has_6_properties(self):
        """MultiRegionGroup: cell_topology, namespace, regional_emergency, propagation, regional_recovery_policy, tiered_redis.

        The former ``config`` property moved to the private distribution
        with the multiregion package relocation (599 D5).
        """
        assert self._count_cached_properties(MultiRegionGroup) == 6

    def test_metrics_group_has_6_properties(self):
        """MetricsGroup: detection, drift_detection, drift_threshold, metrics, safe_gauge, system_metrics_cache."""
        assert self._count_cached_properties(MetricsGroup) == 6

    def test_scaling_group_has_10_properties(self):
        """ScalingGroup has 10 cached_property accessors (incl. rate_limit_backoff)."""
        assert self._count_cached_properties(ScalingGroup) == 10

    def test_resilience_group_has_5_properties(self):
        """ResilienceGroup: bulkhead, hedging, resilient_recorder, resilient_storage, resource_monitor."""
        assert self._count_cached_properties(ResilienceGroup) == 5

    def test_observability_group_has_5_properties(self):
        """ObservabilityGroup: correlation, correlation_engine, logging_settings, otel, profile."""
        assert self._count_cached_properties(ObservabilityGroup) == 5

    def test_adapters_group_has_10_properties(self):
        """AdaptersGroup: arq, celery_task, config_shadow, http_client, kafka_audit, kafka_producer, middleware, notification_channel, redis, secrets."""
        assert self._count_cached_properties(AdaptersGroup) == 10

    def test_security_group_has_3_properties(self):
        """SecurityGroup: corruption_shield, domain_sensitivity, security."""
        assert self._count_cached_properties(SecurityGroup) == 3

    def test_slo_group_has_6_properties(self):
        """SLOGroup: dashboard, error_budget, postmortem, sla, slo, steady_state."""
        assert self._count_cached_properties(SLOGroup) == 6

    def test_meta_group_has_8_properties(self):
        """MetaGroup has 8 cached_property accessors (impl 489 added daemon_worker)."""
        assert self._count_cached_properties(MetaGroup) == 8

    def test_testing_group_has_5_properties(self):
        """TestingGroup has 5 cached_property accessors."""
        assert self._count_cached_properties(TestingGroup) == 5


class TestGroupPropertyTypeContract:
    """Each cached_property returns the correct Settings type."""

    def test_core_backoff_returns_backoff_settings(self):
        """CoreGroup.backoff returns BackoffSettings."""
        from baldur.settings.backoff import BackoffSettings

        group = CoreGroup()
        assert isinstance(group.backoff, BackoffSettings)

    def test_core_admission_control_returns_correct_type(self):
        """CoreGroup.admission_control returns AdmissionControlSettings."""
        from baldur.settings.admission_control import AdmissionControlSettings

        group = CoreGroup()
        assert isinstance(group.admission_control, AdmissionControlSettings)

    def test_scaling_throttle_returns_throttle_settings(self):
        """ScalingGroup.throttle returns ThrottleSettings."""
        from baldur.settings.throttle import ThrottleSettings

        group = ScalingGroup()
        assert isinstance(group.throttle, ThrottleSettings)

    def test_scaling_backpressure_returns_correct_type(self):
        """ScalingGroup.backpressure returns BackpressureSettings."""
        from baldur.settings.backpressure import BackpressureSettings

        group = ScalingGroup()
        assert isinstance(group.backpressure, BackpressureSettings)

    def test_audit_group_audit_returns_audit_settings(self):
        """AuditGroup.audit returns AuditSettings."""
        from baldur.settings.audit import AuditSettings

        group = AuditGroup()
        assert isinstance(group.audit, AuditSettings)

    def test_audit_group_hash_chain_returns_correct_type(self):
        """AuditGroup.hash_chain returns HashChainSettings."""
        from baldur.settings.hash_chain import HashChainSettings

        group = AuditGroup()
        assert isinstance(group.hash_chain, HashChainSettings)

    def test_coordination_distributed_lock_returns_correct_type(self):
        """CoordinationGroup.distributed_lock returns DistributedLockSettings."""
        from baldur.settings.distributed_lock import DistributedLockSettings

        group = CoordinationGroup()
        assert isinstance(group.distributed_lock, DistributedLockSettings)

    def test_observability_otel_returns_correct_type(self):
        """ObservabilityGroup.otel returns OpenTelemetrySettings."""
        from baldur.settings.otel import OpenTelemetrySettings

        group = ObservabilityGroup()
        assert isinstance(group.otel, OpenTelemetrySettings)

    def test_resilience_bulkhead_returns_correct_type(self):
        """ResilienceGroup.bulkhead returns BulkheadSettings."""
        from baldur.settings.bulkhead import BulkheadSettings

        group = ResilienceGroup()
        assert isinstance(group.bulkhead, BulkheadSettings)

    def test_security_corruption_shield_returns_correct_type(self):
        """SecurityGroup.corruption_shield returns CorruptionShieldSettings."""
        from baldur.settings.corruption_shield import CorruptionShieldSettings

        group = SecurityGroup()
        assert isinstance(group.corruption_shield, CorruptionShieldSettings)

    def test_slo_group_slo_returns_correct_type(self):
        """SLOGroup.slo returns SLOSettings."""
        from baldur.settings.slo import SLOSettings

        group = SLOGroup()
        assert isinstance(group.slo, SLOSettings)

    def test_meta_safety_bounds_returns_correct_type(self):
        """MetaGroup.safety_bounds returns SafetyBoundsSettings."""
        from baldur.settings.safety_bounds import SafetyBoundsSettings

        group = MetaGroup()
        assert isinstance(group.safety_bounds, SafetyBoundsSettings)

    def test_testing_sampling_returns_correct_type(self):
        """TestingGroup.sampling returns SamplingSettings."""
        from baldur.settings.sampling import SamplingSettings

        group = TestingGroup()
        assert isinstance(group.sampling, SamplingSettings)

    def test_adapters_celery_task_returns_correct_type(self):
        """AdaptersGroup.celery_task returns CeleryTaskSettings."""
        from baldur.settings.celery_task import CeleryTaskSettings

        group = AdaptersGroup()
        assert isinstance(group.celery_task, CeleryTaskSettings)

    def test_multi_region_cell_topology_returns_correct_type(self):
        """MultiRegionGroup.cell_topology returns CellTopologySettings."""
        from baldur.settings.cell_topology import CellTopologySettings

        group = MultiRegionGroup()
        assert isinstance(group.cell_topology, CellTopologySettings)

    def test_services_canary_returns_correct_type(self):
        """ServicesGroup.canary returns CanarySettings."""
        from baldur.settings.canary import CanarySettings

        group = ServicesGroup()
        assert isinstance(group.canary, CanarySettings)

    def test_metrics_drift_detection_returns_correct_type(self):
        """MetricsGroup.drift_detection returns DriftDetectionSettings."""
        from baldur.settings.drift_detection import DriftDetectionSettings

        group = MetricsGroup()
        assert isinstance(group.drift_detection, DriftDetectionSettings)


# =============================================================================
# Behavior Tests — Lazy initialization and caching
# =============================================================================


class TestGroupLazyInitBehavior:
    """Cached properties are not created until accessed."""

    def test_new_group_has_empty_dict(self):
        """Freshly created group has no cached_property values in __dict__."""
        group = CoreGroup()
        cached_names = {
            name
            for name, val in vars(CoreGroup).items()
            if isinstance(val, cached_property)
        }
        initialized = cached_names & set(group.__dict__)
        assert initialized == set()

    def test_accessing_property_populates_dict(self):
        """Accessing a property adds it to __dict__."""
        group = CoreGroup()
        _ = group.backoff
        assert "backoff" in group.__dict__

    def test_unaccessed_properties_remain_absent(self):
        """Other properties remain absent after accessing one."""
        group = CoreGroup()
        _ = group.backoff
        assert "admission_control" not in group.__dict__
        assert "pool_monitor" not in group.__dict__


class TestGroupCachingBehavior:
    """Second access returns the same instance (cached_property contract)."""

    def test_same_instance_on_repeated_access(self):
        """Repeated access to a property returns the same object."""
        group = ScalingGroup()
        first = group.throttle
        second = group.throttle
        assert first is second


class TestGroupIndependentResetBehavior:
    """Deleting one cached_property does not affect others."""

    def test_delete_one_property_preserves_others(self):
        """Deleting 'backoff' does not affect 'pool_monitor'."""
        group = CoreGroup()
        _ = group.backoff
        _ = group.pool_monitor
        assert "backoff" in group.__dict__
        assert "pool_monitor" in group.__dict__

        # When
        del group.__dict__["backoff"]

        # Then
        assert "backoff" not in group.__dict__
        assert "pool_monitor" in group.__dict__

    def test_deleted_property_recreates_fresh_instance(self):
        """After delete, next access returns a new instance."""
        group = CoreGroup()
        first = group.backoff
        del group.__dict__["backoff"]
        second = group.backoff
        assert first is not second


# =============================================================================
# Contract Tests — Root group accessors
# =============================================================================


class TestRootCachedPropertyNamesContract:
    """_cached_property_names() returns expected 14 group accessor names."""

    EXPECTED_GROUP_NAMES = {
        "core",
        "scaling",
        "audit_group",
        "coordination",
        "multi_region",
        "metrics_group",
        "resilience",
        "obs",
        "adapters",
        "security_group",
        "slo_group",
        "meta",
        "testing",
        "services_group",
    }

    def test_cached_property_names_count(self):
        """Root has exactly 14 cached_property group accessors."""
        names = BaldurSettings._cached_property_names()
        assert len(names) == 14

    def test_cached_property_names_match_expected(self):
        """Root cached_property names match expected group names."""
        names = set(BaldurSettings._cached_property_names())
        assert names == self.EXPECTED_GROUP_NAMES


class TestRootGroupAccessorTypeContract:
    """Root group accessors return the correct group class."""

    def test_core_returns_core_group(self):
        """config.core returns CoreGroup."""
        config = BaldurSettings()
        assert isinstance(config.core, CoreGroup)

    def test_scaling_returns_scaling_group(self):
        """config.scaling returns ScalingGroup."""
        config = BaldurSettings()
        assert isinstance(config.scaling, ScalingGroup)

    def test_audit_group_returns_audit_group(self):
        """config.audit_group returns AuditGroup."""
        config = BaldurSettings()
        assert isinstance(config.audit_group, AuditGroup)

    def test_coordination_returns_coordination_group(self):
        """config.coordination returns CoordinationGroup."""
        config = BaldurSettings()
        assert isinstance(config.coordination, CoordinationGroup)

    def test_multi_region_returns_multi_region_group(self):
        """config.multi_region returns MultiRegionGroup."""
        config = BaldurSettings()
        assert isinstance(config.multi_region, MultiRegionGroup)

    def test_resilience_returns_resilience_group(self):
        """config.resilience returns ResilienceGroup."""
        config = BaldurSettings()
        assert isinstance(config.resilience, ResilienceGroup)

    def test_obs_returns_observability_group(self):
        """config.obs returns ObservabilityGroup."""
        config = BaldurSettings()
        assert isinstance(config.obs, ObservabilityGroup)

    def test_adapters_returns_adapters_group(self):
        """config.adapters returns AdaptersGroup."""
        config = BaldurSettings()
        assert isinstance(config.adapters, AdaptersGroup)

    def test_security_group_returns_security_group(self):
        """config.security_group returns SecurityGroup."""
        config = BaldurSettings()
        assert isinstance(config.security_group, SecurityGroup)

    def test_slo_group_returns_slo_group(self):
        """config.slo_group returns SLOGroup."""
        config = BaldurSettings()
        assert isinstance(config.slo_group, SLOGroup)

    def test_meta_returns_meta_group(self):
        """config.meta returns MetaGroup."""
        config = BaldurSettings()
        assert isinstance(config.meta, MetaGroup)

    def test_testing_returns_testing_group(self):
        """config.testing returns TestingGroup."""
        config = BaldurSettings()
        assert isinstance(config.testing, TestingGroup)

    def test_services_group_returns_services_group(self):
        """config.services_group returns ServicesGroup."""
        config = BaldurSettings()
        assert isinstance(config.services_group, ServicesGroup)

    def test_metrics_group_returns_metrics_group(self):
        """config.metrics_group returns MetricsGroup."""
        config = BaldurSettings()
        assert isinstance(config.metrics_group, MetricsGroup)


# =============================================================================
# Behavior Tests — Root group caching and to_full_dict
# =============================================================================


class TestRootGroupCachingBehavior:
    """Root group accessors are cached (same object on second call)."""

    def test_core_accessor_is_cached(self):
        """config.core returns same instance on second access."""
        config = BaldurSettings()
        assert config.core is config.core

    def test_scaling_accessor_is_cached(self):
        """config.scaling returns same instance on second access."""
        config = BaldurSettings()
        assert config.scaling is config.scaling


class TestRootToFullDictBehavior:
    """to_full_dict() returns model_dump() + initialized cached_property groups."""

    def test_includes_model_dump_fields(self):
        """to_full_dict() includes standard Pydantic fields."""
        config = BaldurSettings()
        result = config.to_full_dict()
        assert "cluster_id" in result
        assert "fallback_policy" in result

    def test_excludes_non_initialized_groups(self):
        """to_full_dict() does not include groups that haven't been accessed (except validator-triggered ones)."""
        config = BaldurSettings()
        result = config.to_full_dict()
        # Groups initialized by validators:
        # - multi_region: warn_default_cluster_id
        # - core, scaling, slo_group, services_group: _run_cross_validation (420)
        validator_initialized_groups = {
            "multi_region",
            "core",
            "scaling",
            "slo_group",
            "services_group",
        }
        non_validator_groups = {
            name
            for name in BaldurSettings._cached_property_names()
            if name not in validator_initialized_groups
        }
        for name in non_validator_groups:
            assert name not in result

    def test_includes_initialized_group(self):
        """to_full_dict() includes a group after it has been accessed."""
        config = BaldurSettings()
        _ = config.core
        result = config.to_full_dict()
        assert "core" in result
        assert isinstance(result["core"], dict)

    def test_initialized_group_includes_only_accessed_sub_properties(self):
        """Group dict only includes accessed cached_properties of the group."""
        config = BaldurSettings()
        # Access core group and one sub-property
        _ = config.core.backoff
        result = config.to_full_dict()
        core_dict = result["core"]
        assert "backoff" in core_dict
        assert "pool_monitor" not in core_dict

    def test_initialized_sub_property_is_model_dump(self):
        """Accessed sub-property is serialized via model_dump()."""
        config = BaldurSettings()
        _ = config.core.backoff
        result = config.to_full_dict()
        backoff_data = result["core"]["backoff"]
        assert isinstance(backoff_data, dict)
        assert "exponential_base_delay" in backoff_data

    def test_multiple_groups_included_when_accessed(self):
        """Multiple accessed groups appear in to_full_dict()."""
        config = BaldurSettings()
        _ = config.core
        _ = config.scaling
        result = config.to_full_dict()
        assert "core" in result
        assert "scaling" in result
        assert "meta" not in result


class TestRootGroupToDictStaticMethodBehavior:
    """_group_to_dict() serializes only initialized cached_properties."""

    def test_empty_group_returns_empty_dict(self):
        """Group with no accessed properties returns {}."""
        group = CoreGroup()
        result = BaldurSettings._group_to_dict(group)
        assert result == {}

    def test_accessed_property_appears_in_dict(self):
        """Accessed property is included in serialized dict."""
        group = CoreGroup()
        _ = group.backoff
        result = BaldurSettings._group_to_dict(group)
        assert "backoff" in result
        assert isinstance(result["backoff"], dict)

    def test_unaccessed_properties_absent_from_dict(self):
        """Unaccessed properties are not in serialized dict."""
        group = CoreGroup()
        _ = group.backoff
        result = BaldurSettings._group_to_dict(group)
        assert "pool_monitor" not in result
