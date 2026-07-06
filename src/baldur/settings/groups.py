"""
Settings Domain Groups — Lazy-initialized cached_property containers.

14 logical groups that organize the non-Root settings by domain.
Each group uses @cached_property with lazy import to avoid circular imports
and minimize startup cost.

Usage:
    from baldur.settings.root import get_config
    config = get_config()
    config.core.backoff          # CoreGroup
    config.scaling.backpressure  # ScalingGroup
"""

from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from baldur.settings.admission_control import AdmissionControlSettings
    from baldur.settings.airgap import AirGapSettings
    from baldur.settings.anti_flapping import AntiFlappingSettings
    from baldur.settings.api_rate_limit import ApiRateLimitSettings
    from baldur.settings.api_view import ApiViewSettings
    from baldur.settings.apply_strategy import ApplyStrategySettings
    from baldur.settings.arq_task import ArqTaskSettings
    from baldur.settings.audit import AuditSettings
    from baldur.settings.audit_integrity import AuditIntegritySettings
    from baldur.settings.audit_sync import AuditSyncSettings
    from baldur.settings.audit_watchdog import AuditWatchdogSettings
    from baldur.settings.auto_rollback import AutoRollbackSettings
    from baldur.settings.auto_tuning import AutoTuningSettings
    from baldur.settings.backoff import BackoffSettings
    from baldur.settings.backpressure import BackpressureSettings
    from baldur.settings.batch import BatchSettings
    from baldur.settings.bulkhead import BulkheadSettings
    from baldur.settings.canary import CanarySettings
    from baldur.settings.canary_governance import CanaryGovernanceSettings
    from baldur.settings.canary_interlock import CanaryInterlockSettings
    from baldur.settings.canary_watchdog import CanaryWatchdogSettings
    from baldur.settings.capacity_reservation import CapacityReservationSettings
    from baldur.settings.cascade import CascadeSettings
    from baldur.settings.cascade_retention import CascadeRetentionSettings
    from baldur.settings.celery_task import CeleryTaskSettings
    from baldur.settings.cell_topology import CellTopologySettings
    from baldur.settings.chaos import ChaosSettings
    from baldur.settings.chaos_blast_radius import ChaosBlastRadiusSettings
    from baldur.settings.chaos_experiment import ChaosExperimentSettings
    from baldur.settings.circuit_breaker import CircuitBreakerSettings
    from baldur.settings.circuit_breaker_advanced import (
        CircuitBreakerAdvancedSettings,
    )
    from baldur.settings.circuit_mesh import CircuitMeshSettings
    from baldur.settings.cleanup import CleanupSettings
    from baldur.settings.compliance import ComplianceSettings
    from baldur.settings.config_shadow import ConfigShadowSettings
    from baldur.settings.correlation import CorrelationSettings
    from baldur.settings.correlation_engine import CorrelationEngineSettings
    from baldur.settings.corruption_shield import CorruptionShieldSettings
    from baldur.settings.critical_worker import CriticalWorkerSettings
    from baldur.settings.daemon_worker import DaemonWorkerSettings
    from baldur.settings.daily_report import DailyReportSettings
    from baldur.settings.dashboard import DashboardSettings
    from baldur.settings.decision_engine import DecisionEngineSettings
    from baldur.settings.detection import DetectionSettings
    from baldur.settings.distributed_lock import DistributedLockSettings
    from baldur.settings.dlq import DLQSettings
    from baldur.settings.dlq_outbox import DLQOutboxSettings
    from baldur.settings.domain_sensitivity import DomainSensitivitySettings
    from baldur.settings.drift_detection import DriftDetectionSettings
    from baldur.settings.drift_threshold import DriftThresholdSettings
    from baldur.settings.emergency_mode import EmergencyModeSettings
    from baldur.settings.error_budget import ErrorBudgetSettings
    from baldur.settings.error_budget_gate import ErrorBudgetGateSettings
    from baldur.settings.error_budget_propagation import (
        ErrorBudgetPropagationSettings,
    )
    from baldur.settings.event_buffer import EventBufferSettings
    from baldur.settings.event_bus import EventBusSettings
    from baldur.settings.event_journal import EventJournalSettings
    from baldur.settings.finops import FinOpsSettings
    from baldur.settings.forensic import ForensicSettings
    from baldur.settings.gate_fault import GateFaultSettings
    from baldur.settings.governance import GovernanceSettings
    from baldur.settings.graceful_degradation import GracefulDegradationSettings
    from baldur.settings.hash_chain import HashChainSettings
    from baldur.settings.health_check import HealthCheckSettings
    from baldur.settings.hedging import HedgingSettings
    from baldur.settings.http_client import HttpClientSettings
    from baldur.settings.idempotency import IdempotencySettings
    from baldur.settings.intelligence_task import IntelligenceTaskSettings
    from baldur.settings.jitter import JitterSettings
    from baldur.settings.kafka_audit import KafkaAuditSettings
    from baldur.settings.kafka_producer import KafkaProducerSettings
    from baldur.settings.l2_storage import L2StorageSettings
    from baldur.settings.leader_election import LeaderElectionSettings
    from baldur.settings.learning import LearningSettings
    from baldur.settings.logging_settings import LoggingSettings
    from baldur.settings.meta_watchdog import MetaWatchdogSettings
    from baldur.settings.metrics import MetricsSettings
    from baldur.settings.middleware import BaldurMiddlewareSettings
    from baldur.settings.ml_models import MLModelsSettings
    from baldur.settings.namespace import NamespaceSettings
    from baldur.settings.notification import NotificationSettings
    from baldur.settings.notification_channel import NotificationChannelSettings
    from baldur.settings.observability import ObservabilitySettings
    from baldur.settings.otel import OpenTelemetrySettings
    from baldur.settings.pipeline import PipelineSettings
    from baldur.settings.pool_monitor import PoolMonitorSettings
    from baldur.settings.postmortem import PostmortemSettings
    from baldur.settings.precomputed_cache import PrecomputedCacheSettings
    from baldur.settings.predictive_forecaster import PredictiveForecasterSettings
    from baldur.settings.propagation import PropagationSettings
    from baldur.settings.rate_limit import RateLimitSettings
    from baldur.settings.rate_limit_throttle_integration import (
        RateLimitThrottleIntegrationSettings,
    )
    from baldur.settings.recovery_circuit_breaker import (
        RecoveryCircuitBreakerSettings,
    )
    from baldur.settings.recovery_coordinator import RecoveryCoordinatorSettings
    from baldur.settings.recovery_shutdown import RecoveryShutdownSettings
    from baldur.settings.recovery_tasks import RecoveryTasksSettings
    from baldur.settings.redis import RedisSettings
    from baldur.settings.redis_key_guard import RedisKeyGuardSettings
    from baldur.settings.regional_emergency import RegionalEmergencySettings
    from baldur.settings.regional_recovery_policy import (
        RegionalRecoveryPolicySettings,
    )
    from baldur.settings.replay_automation import ReplayAutomationSettings
    from baldur.settings.resilient_recorder import ResilientRecorderSettings
    from baldur.settings.resilient_storage import ResilientStorageSettings
    from baldur.settings.resource_guard import ResourceGuardSettings
    from baldur.settings.resource_monitor import ResourceMonitorSettings
    from baldur.settings.retry import RetrySettings
    from baldur.settings.ring_buffer import RingBufferSettings
    from baldur.settings.runbook import RunbookSettings
    from baldur.settings.runtime_feedback import RuntimeFeedbackSettings
    from baldur.settings.safe_gauge import SafeGaugeSettings
    from baldur.settings.safety_bounds import SafetyBoundsSettings
    from baldur.settings.saga import SagaSettings
    from baldur.settings.sampling import SamplingSettings
    from baldur.settings.scale import ScaleSettings
    from baldur.settings.secrets import SecretsSettings
    from baldur.settings.security import SecuritySettings
    from baldur.settings.settings_dependency import SettingsDependencySettings
    from baldur.settings.settings_recommendation import (
        SettingsRecommendationSettings,
    )
    from baldur.settings.sla import SLASettings
    from baldur.settings.slack_channel import SlackChannelSettings
    from baldur.settings.slo import SLOSettings
    from baldur.settings.state_cache import StateCacheSettings
    from baldur.settings.steady_state import SteadyStateSettings
    from baldur.settings.stress_test import StressTestSettings
    from baldur.settings.system_control import SystemControlSettings
    from baldur.settings.system_metrics_cache import SystemMetricsCacheSettings
    from baldur.settings.thread_management import ThreadManagementSettings
    from baldur.settings.throttle import ThrottleSettings
    from baldur.settings.tiered_redis import TieredRedisSettings
    from baldur.settings.xtest_cleanup import XTestCleanupSettings


class CoreGroup:
    """Core module settings: backoff, circuit breaker, pool monitor, admission control, retry, thread management."""

    @cached_property
    def admission_control(self) -> AdmissionControlSettings:
        from baldur.settings.admission_control import AdmissionControlSettings

        return AdmissionControlSettings()

    @cached_property
    def backoff(self) -> BackoffSettings:
        from baldur.settings.backoff import BackoffSettings

        return BackoffSettings()

    @cached_property
    def circuit_breaker(self) -> CircuitBreakerSettings:
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        return CircuitBreakerSettings()

    @cached_property
    def circuit_breaker_advanced(self) -> CircuitBreakerAdvancedSettings:
        from baldur.settings.circuit_breaker_advanced import (
            CircuitBreakerAdvancedSettings,
        )

        return CircuitBreakerAdvancedSettings()

    @cached_property
    def health_check(self) -> HealthCheckSettings:
        from baldur.settings.health_check import HealthCheckSettings

        return HealthCheckSettings()

    @cached_property
    def pool_monitor(self) -> PoolMonitorSettings:
        from baldur.settings.pool_monitor import PoolMonitorSettings

        return PoolMonitorSettings()

    @cached_property
    def retry(self) -> RetrySettings:
        from baldur.settings.retry import RetrySettings

        return RetrySettings()

    @cached_property
    def system_control(self) -> SystemControlSettings:
        from baldur.settings.system_control import SystemControlSettings

        return SystemControlSettings()

    @cached_property
    def thread_management(self) -> ThreadManagementSettings:
        from baldur.settings.thread_management import ThreadManagementSettings

        return ThreadManagementSettings()


class ServicesGroup:
    """Services module settings: chaos, DLQ, recovery, canary, governance, forensic, etc."""

    @cached_property
    def anti_flapping(self) -> AntiFlappingSettings:
        from baldur.settings.anti_flapping import AntiFlappingSettings

        return AntiFlappingSettings()

    @cached_property
    def api_rate_limit(self) -> ApiRateLimitSettings:
        from baldur.settings.api_rate_limit import ApiRateLimitSettings

        return ApiRateLimitSettings()

    @cached_property
    def api_view(self) -> ApiViewSettings:
        from baldur.settings.api_view import ApiViewSettings

        return ApiViewSettings()

    @cached_property
    def apply_strategy(self) -> ApplyStrategySettings:
        from baldur.settings.apply_strategy import ApplyStrategySettings

        return ApplyStrategySettings()

    @cached_property
    def auto_rollback(self) -> AutoRollbackSettings:
        from baldur.settings.auto_rollback import AutoRollbackSettings

        return AutoRollbackSettings()

    @cached_property
    def auto_tuning(self) -> AutoTuningSettings:
        from baldur.settings.auto_tuning import AutoTuningSettings

        return AutoTuningSettings()

    @cached_property
    def batch(self) -> BatchSettings:
        from baldur.settings.batch import BatchSettings

        return BatchSettings()

    @cached_property
    def canary(self) -> CanarySettings:
        from baldur.settings.canary import CanarySettings

        return CanarySettings()

    @cached_property
    def canary_governance(self) -> CanaryGovernanceSettings:
        from baldur.settings.canary_governance import CanaryGovernanceSettings

        return CanaryGovernanceSettings()

    @cached_property
    def canary_interlock(self) -> CanaryInterlockSettings:
        from baldur.settings.canary_interlock import CanaryInterlockSettings

        return CanaryInterlockSettings()

    @cached_property
    def canary_watchdog(self) -> CanaryWatchdogSettings:
        from baldur.settings.canary_watchdog import CanaryWatchdogSettings

        return CanaryWatchdogSettings()

    @cached_property
    def capacity_reservation(self) -> CapacityReservationSettings:
        from baldur.settings.capacity_reservation import (
            CapacityReservationSettings,
        )

        return CapacityReservationSettings()

    @cached_property
    def compliance(self) -> ComplianceSettings:
        from baldur.settings.compliance import ComplianceSettings

        return ComplianceSettings()

    @cached_property
    def chaos(self) -> ChaosSettings:
        from baldur.settings.chaos import ChaosSettings

        return ChaosSettings()

    @cached_property
    def chaos_blast_radius(self) -> ChaosBlastRadiusSettings:
        from baldur.settings.chaos_blast_radius import ChaosBlastRadiusSettings

        return ChaosBlastRadiusSettings()

    @cached_property
    def chaos_experiment(self) -> ChaosExperimentSettings:
        from baldur.settings.chaos_experiment import ChaosExperimentSettings

        return ChaosExperimentSettings()

    @cached_property
    def circuit_mesh(self) -> CircuitMeshSettings:
        from baldur.settings.circuit_mesh import CircuitMeshSettings

        return CircuitMeshSettings()

    @cached_property
    def cleanup(self) -> CleanupSettings:
        from baldur.settings.cleanup import CleanupSettings

        return CleanupSettings()

    @cached_property
    def critical_worker(self) -> CriticalWorkerSettings:
        from baldur.settings.critical_worker import CriticalWorkerSettings

        return CriticalWorkerSettings()

    @cached_property
    def daily_report(self) -> DailyReportSettings:
        from baldur.settings.daily_report import DailyReportSettings

        return DailyReportSettings()

    @cached_property
    def decision_engine(self) -> DecisionEngineSettings:
        from baldur.settings.decision_engine import DecisionEngineSettings

        return DecisionEngineSettings()

    @cached_property
    def dlq(self) -> DLQSettings:
        from baldur.settings.dlq import DLQSettings

        return DLQSettings()

    @cached_property
    def dlq_outbox(self) -> DLQOutboxSettings:
        from baldur.settings.dlq_outbox import DLQOutboxSettings

        return DLQOutboxSettings()

    @cached_property
    def emergency_mode(self) -> EmergencyModeSettings:
        from baldur.settings.emergency_mode import EmergencyModeSettings

        return EmergencyModeSettings()

    @cached_property
    def error_budget_gate(self) -> ErrorBudgetGateSettings:
        from baldur.settings.error_budget_gate import ErrorBudgetGateSettings

        return ErrorBudgetGateSettings()

    @cached_property
    def error_budget_propagation(self) -> ErrorBudgetPropagationSettings:
        from baldur.settings.error_budget_propagation import (
            ErrorBudgetPropagationSettings,
        )

        return ErrorBudgetPropagationSettings()

    @cached_property
    def event_bus(self) -> EventBusSettings:
        from baldur.settings.event_bus import EventBusSettings

        return EventBusSettings()

    @cached_property
    def finops(self) -> FinOpsSettings:
        from baldur.settings.finops import FinOpsSettings

        return FinOpsSettings()

    @cached_property
    def forensic(self) -> ForensicSettings:
        from baldur.settings.forensic import ForensicSettings

        return ForensicSettings()

    @cached_property
    def governance(self) -> GovernanceSettings:
        from baldur.settings.governance import GovernanceSettings

        return GovernanceSettings()

    @cached_property
    def idempotency(self) -> IdempotencySettings:
        from baldur.settings.idempotency import IdempotencySettings

        return IdempotencySettings()

    @cached_property
    def intelligence_task(self) -> IntelligenceTaskSettings:
        from baldur.settings.intelligence_task import IntelligenceTaskSettings

        return IntelligenceTaskSettings()

    @cached_property
    def l2_storage(self) -> L2StorageSettings:
        from baldur.settings.l2_storage import L2StorageSettings

        return L2StorageSettings()

    @cached_property
    def learning(self) -> LearningSettings:
        from baldur.settings.learning import LearningSettings

        return LearningSettings()

    @cached_property
    def notification(self) -> NotificationSettings:
        from baldur.settings.notification import NotificationSettings

        return NotificationSettings()

    @cached_property
    def precomputed_cache(self) -> PrecomputedCacheSettings:
        from baldur.settings.precomputed_cache import PrecomputedCacheSettings

        return PrecomputedCacheSettings()

    @cached_property
    def recovery_circuit_breaker(self) -> RecoveryCircuitBreakerSettings:
        from baldur.settings.recovery_circuit_breaker import (
            RecoveryCircuitBreakerSettings,
        )

        return RecoveryCircuitBreakerSettings()

    @cached_property
    def recovery_coordinator(self) -> RecoveryCoordinatorSettings:
        from baldur.settings.recovery_coordinator import (
            RecoveryCoordinatorSettings,
        )

        return RecoveryCoordinatorSettings()

    @cached_property
    def recovery_shutdown(self) -> RecoveryShutdownSettings:
        from baldur.settings.recovery_shutdown import RecoveryShutdownSettings

        return RecoveryShutdownSettings()

    @cached_property
    def recovery_tasks(self) -> RecoveryTasksSettings:
        from baldur.settings.recovery_tasks import RecoveryTasksSettings

        return RecoveryTasksSettings()

    @cached_property
    def replay_automation(self) -> ReplayAutomationSettings:
        from baldur.settings.replay_automation import ReplayAutomationSettings

        return ReplayAutomationSettings()

    @cached_property
    def runbook(self) -> RunbookSettings:
        from baldur.settings.runbook import RunbookSettings

        return RunbookSettings()

    @cached_property
    def saga(self) -> SagaSettings:
        from baldur.settings.saga import SagaSettings

        return SagaSettings()

    @cached_property
    def slack_channel(self) -> SlackChannelSettings:
        from baldur.settings.slack_channel import SlackChannelSettings

        return SlackChannelSettings()

    @cached_property
    def ml_models(self) -> MLModelsSettings:
        from baldur.settings.ml_models import MLModelsSettings

        return MLModelsSettings()

    @cached_property
    def predictive_forecaster(self) -> PredictiveForecasterSettings:
        from baldur.settings.predictive_forecaster import (
            PredictiveForecasterSettings,
        )

        return PredictiveForecasterSettings()

    @cached_property
    def settings_recommendation(self) -> SettingsRecommendationSettings:
        from baldur.settings.settings_recommendation import (
            SettingsRecommendationSettings,
        )

        return SettingsRecommendationSettings()


class AuditGroup:
    """Audit module settings: audit logging, hash chain, WAL, etc."""

    @cached_property
    def audit(self) -> AuditSettings:
        from baldur.settings.audit import AuditSettings

        return AuditSettings()

    @cached_property
    def audit_integrity(self) -> AuditIntegritySettings:
        from baldur.settings.audit_integrity import AuditIntegritySettings

        return AuditIntegritySettings()

    @cached_property
    def audit_sync(self) -> AuditSyncSettings:
        from baldur.settings.audit_sync import AuditSyncSettings

        return AuditSyncSettings()

    @cached_property
    def audit_watchdog(self) -> AuditWatchdogSettings:
        from baldur.settings.audit_watchdog import AuditWatchdogSettings

        return AuditWatchdogSettings()

    @cached_property
    def cascade(self) -> CascadeSettings:
        from baldur.settings.cascade import CascadeSettings

        return CascadeSettings()

    @cached_property
    def cascade_retention(self) -> CascadeRetentionSettings:
        from baldur.settings.cascade_retention import CascadeRetentionSettings

        return CascadeRetentionSettings()

    @cached_property
    def event_journal(self) -> EventJournalSettings:
        from baldur.settings.event_journal import EventJournalSettings

        return EventJournalSettings()

    @cached_property
    def hash_chain(self) -> HashChainSettings:
        from baldur.settings.hash_chain import HashChainSettings

        return HashChainSettings()


class CoordinationGroup:
    """Coordination module settings: distributed lock, leader election, Redis key guard."""

    @cached_property
    def distributed_lock(self) -> DistributedLockSettings:
        from baldur.settings.distributed_lock import DistributedLockSettings

        return DistributedLockSettings()

    @cached_property
    def leader_election(self) -> LeaderElectionSettings:
        from baldur.settings.leader_election import LeaderElectionSettings

        return LeaderElectionSettings()

    @cached_property
    def redis_key_guard(self) -> RedisKeyGuardSettings:
        from baldur.settings.redis_key_guard import RedisKeyGuardSettings

        return RedisKeyGuardSettings()


class MultiRegionGroup:
    """Multi-region module settings: namespace, propagation, cell topology, regional recovery.

    Holds only the OSS-resident settings; the multiregion package's own
    config is a self-contained singleton in the private distribution.
    """

    # 599 D5 — the former ``config`` cached_property moved with the package
    # to baldur_dormant.multiregion.config (module-level singleton there).
    @cached_property
    def cell_topology(self) -> CellTopologySettings:
        from baldur.settings.cell_topology import CellTopologySettings

        return CellTopologySettings()

    @cached_property
    def namespace(self) -> NamespaceSettings:
        from baldur.settings.namespace import NamespaceSettings

        return NamespaceSettings()

    @cached_property
    def regional_emergency(self) -> RegionalEmergencySettings:
        from baldur.settings.regional_emergency import RegionalEmergencySettings

        return RegionalEmergencySettings()

    @cached_property
    def propagation(self) -> PropagationSettings:
        from baldur.settings.propagation import PropagationSettings

        return PropagationSettings()

    @cached_property
    def regional_recovery_policy(self) -> RegionalRecoveryPolicySettings:
        from baldur.settings.regional_recovery_policy import (
            RegionalRecoveryPolicySettings,
        )

        return RegionalRecoveryPolicySettings()

    @cached_property
    def tiered_redis(self) -> TieredRedisSettings:
        from baldur.settings.tiered_redis import TieredRedisSettings

        return TieredRedisSettings()


class MetricsGroup:
    """Metrics module settings: drift detection, detection, metrics, drift threshold, etc."""

    @cached_property
    def detection(self) -> DetectionSettings:
        from baldur.settings.detection import DetectionSettings

        return DetectionSettings()

    @cached_property
    def drift_detection(self) -> DriftDetectionSettings:
        from baldur.settings.drift_detection import DriftDetectionSettings

        return DriftDetectionSettings()

    @cached_property
    def drift_threshold(self) -> DriftThresholdSettings:
        from baldur.settings.drift_threshold import DriftThresholdSettings

        return DriftThresholdSettings()

    @cached_property
    def metrics(self) -> MetricsSettings:
        from baldur.settings.metrics import MetricsSettings

        return MetricsSettings()

    @cached_property
    def safe_gauge(self) -> SafeGaugeSettings:
        from baldur.settings.safe_gauge import SafeGaugeSettings

        return SafeGaugeSettings()

    @cached_property
    def system_metrics_cache(self) -> SystemMetricsCacheSettings:
        from baldur.settings.system_metrics_cache import SystemMetricsCacheSettings

        return SystemMetricsCacheSettings()


class ScalingGroup:
    """Scaling module settings: backpressure, event buffer, load shedding, rate limit, throttle, etc."""

    @cached_property
    def backpressure(self) -> BackpressureSettings:
        from baldur.settings.backpressure import BackpressureSettings

        return BackpressureSettings()

    @cached_property
    def event_buffer(self) -> EventBufferSettings:
        from baldur.settings.event_buffer import EventBufferSettings

        return EventBufferSettings()

    @cached_property
    def graceful_degradation(self) -> GracefulDegradationSettings:
        from baldur.settings.graceful_degradation import (
            GracefulDegradationSettings,
        )

        return GracefulDegradationSettings()

    @cached_property
    def rate_limit(self) -> RateLimitSettings:
        from baldur.settings.rate_limit import RateLimitSettings

        return RateLimitSettings()

    @cached_property
    def rate_limit_throttle_integration(self) -> RateLimitThrottleIntegrationSettings:
        from baldur.settings.rate_limit_throttle_integration import (
            RateLimitThrottleIntegrationSettings,
        )

        return RateLimitThrottleIntegrationSettings()

    @cached_property
    def ring_buffer(self) -> RingBufferSettings:
        from baldur.settings.ring_buffer import RingBufferSettings

        return RingBufferSettings()

    @cached_property
    def scale(self) -> ScaleSettings:
        from baldur.settings.scale import ScaleSettings

        return ScaleSettings()

    @cached_property
    def state_cache(self) -> StateCacheSettings:
        from baldur.settings.state_cache import StateCacheSettings

        return StateCacheSettings()

    @cached_property
    def throttle(self) -> ThrottleSettings:
        from baldur.settings.throttle import ThrottleSettings

        return ThrottleSettings()


class ResilienceGroup:
    """Resilience module settings: bulkhead, hedging, resilient recorder, resource monitor."""

    @cached_property
    def bulkhead(self) -> BulkheadSettings:
        from baldur.settings.bulkhead import BulkheadSettings

        return BulkheadSettings()

    @cached_property
    def hedging(self) -> HedgingSettings:
        from baldur.settings.hedging import HedgingSettings

        return HedgingSettings()

    @cached_property
    def resilient_recorder(self) -> ResilientRecorderSettings:
        from baldur.settings.resilient_recorder import ResilientRecorderSettings

        return ResilientRecorderSettings()

    @cached_property
    def resilient_storage(self) -> ResilientStorageSettings:
        from baldur.settings.resilient_storage import ResilientStorageSettings

        return ResilientStorageSettings()

    @cached_property
    def resource_monitor(self) -> ResourceMonitorSettings:
        from baldur.settings.resource_monitor import ResourceMonitorSettings

        return ResourceMonitorSettings()


class ObservabilityGroup:
    """Observability module settings: correlation, OTEL, logging."""

    @cached_property
    def correlation(self) -> CorrelationSettings:
        from baldur.settings.correlation import CorrelationSettings

        return CorrelationSettings()

    @cached_property
    def correlation_engine(self) -> CorrelationEngineSettings:
        from baldur.settings.correlation_engine import CorrelationEngineSettings

        return CorrelationEngineSettings()

    @cached_property
    def logging_settings(self) -> LoggingSettings:
        from baldur.settings.logging_settings import LoggingSettings

        return LoggingSettings()

    @cached_property
    def otel(self) -> OpenTelemetrySettings:
        from baldur.settings.otel import OpenTelemetrySettings

        return OpenTelemetrySettings()

    @cached_property
    def profile(self) -> ObservabilitySettings:
        from baldur.settings.observability import ObservabilitySettings

        return ObservabilitySettings()


class AdaptersGroup:
    """Adapters module settings: celery task, arq task, config shadow, HTTP client, kafka producer, etc."""

    @cached_property
    def arq(self) -> ArqTaskSettings:
        from baldur.settings.arq_task import ArqTaskSettings

        return ArqTaskSettings()

    @cached_property
    def celery_task(self) -> CeleryTaskSettings:
        from baldur.settings.celery_task import CeleryTaskSettings

        return CeleryTaskSettings()

    @cached_property
    def config_shadow(self) -> ConfigShadowSettings:
        from baldur.settings.config_shadow import ConfigShadowSettings

        return ConfigShadowSettings()

    @cached_property
    def http_client(self) -> HttpClientSettings:
        from baldur.settings.http_client import HttpClientSettings

        return HttpClientSettings()

    @cached_property
    def middleware(self) -> BaldurMiddlewareSettings:
        from baldur.settings.middleware import BaldurMiddlewareSettings

        return BaldurMiddlewareSettings()

    @cached_property
    def kafka_audit(self) -> KafkaAuditSettings:
        from baldur.settings.kafka_audit import KafkaAuditSettings

        return KafkaAuditSettings()

    @cached_property
    def kafka_producer(self) -> KafkaProducerSettings:
        from baldur.settings.kafka_producer import KafkaProducerSettings

        return KafkaProducerSettings()

    @cached_property
    def notification_channel(self) -> NotificationChannelSettings:
        from baldur.settings.notification_channel import (
            NotificationChannelSettings,
        )

        return NotificationChannelSettings()

    @cached_property
    def redis(self) -> RedisSettings:
        from baldur.settings.redis import RedisSettings

        return RedisSettings()

    @cached_property
    def secrets(self) -> SecretsSettings:
        from baldur.settings.secrets import SecretsSettings

        return SecretsSettings()


class SecurityGroup:
    """Security module settings: corruption shield, domain sensitivity, security."""

    @cached_property
    def corruption_shield(self) -> CorruptionShieldSettings:
        from baldur.settings.corruption_shield import CorruptionShieldSettings

        return CorruptionShieldSettings()

    @cached_property
    def domain_sensitivity(self) -> DomainSensitivitySettings:
        from baldur.settings.domain_sensitivity import DomainSensitivitySettings

        return DomainSensitivitySettings()

    @cached_property
    def security(self) -> SecuritySettings:
        from baldur.settings.security import SecuritySettings

        return SecuritySettings()


class SLOGroup:
    """SLO module settings: dashboard, error budget, postmortem, SLA, SLO, steady state."""

    @cached_property
    def dashboard(self) -> DashboardSettings:
        from baldur.settings.dashboard import DashboardSettings

        return DashboardSettings()

    @cached_property
    def error_budget(self) -> ErrorBudgetSettings:
        from baldur.settings.error_budget import ErrorBudgetSettings

        return ErrorBudgetSettings()

    @cached_property
    def postmortem(self) -> PostmortemSettings:
        from baldur.settings.postmortem import PostmortemSettings

        return PostmortemSettings()

    @cached_property
    def sla(self) -> SLASettings:
        from baldur.settings.sla import SLASettings

        return SLASettings()

    @cached_property
    def slo(self) -> SLOSettings:
        from baldur.settings.slo import SLOSettings

        return SLOSettings()

    @cached_property
    def steady_state(self) -> SteadyStateSettings:
        from baldur.settings.steady_state import SteadyStateSettings

        return SteadyStateSettings()


class MetaGroup:
    """Meta module settings: gate fault, meta watchdog, pipeline, resource guard, etc."""

    @cached_property
    def daemon_worker(self) -> DaemonWorkerSettings:
        from baldur.settings.daemon_worker import DaemonWorkerSettings

        return DaemonWorkerSettings()

    @cached_property
    def gate_fault(self) -> GateFaultSettings:
        from baldur.settings.gate_fault import GateFaultSettings

        return GateFaultSettings()

    @cached_property
    def meta_watchdog(self) -> MetaWatchdogSettings:
        from baldur.settings.meta_watchdog import MetaWatchdogSettings

        return MetaWatchdogSettings()

    @cached_property
    def pipeline(self) -> PipelineSettings:
        from baldur.settings.pipeline import PipelineSettings

        return PipelineSettings()

    @cached_property
    def resource_guard(self) -> ResourceGuardSettings:
        from baldur.settings.resource_guard import ResourceGuardSettings

        return ResourceGuardSettings()

    @cached_property
    def runtime_feedback(self) -> RuntimeFeedbackSettings:
        from baldur.settings.runtime_feedback import RuntimeFeedbackSettings

        return RuntimeFeedbackSettings()

    @cached_property
    def safety_bounds(self) -> SafetyBoundsSettings:
        from baldur.settings.safety_bounds import SafetyBoundsSettings

        return SafetyBoundsSettings()

    @cached_property
    def settings_dependency(self) -> SettingsDependencySettings:
        from baldur.settings.settings_dependency import SettingsDependencySettings

        return SettingsDependencySettings()


class TestingGroup:
    """Testing module settings: airgap, jitter, sampling, etc."""

    @cached_property
    def airgap(self) -> AirGapSettings:
        from baldur.settings.airgap import AirGapSettings

        return AirGapSettings()

    @cached_property
    def jitter(self) -> JitterSettings:
        from baldur.settings.jitter import JitterSettings

        return JitterSettings()

    @cached_property
    def sampling(self) -> SamplingSettings:
        from baldur.settings.sampling import SamplingSettings

        return SamplingSettings()

    @cached_property
    def stress_test(self) -> StressTestSettings:
        from baldur.settings.stress_test import StressTestSettings

        return StressTestSettings()

    @cached_property
    def xtest_cleanup(self) -> XTestCleanupSettings:
        from baldur.settings.xtest_cleanup import XTestCleanupSettings

        return XTestCleanupSettings()
