"""
Pydantic Settings Module for Baldur Configuration.

Single Source of Truth for all configuration:
- Default values
- Type definitions
- Validation rules
- Environment variable loading

Replaces:
- core/config.py (dataclass definitions)
- core/safe_defaults.py (SAFE_DEFAULTS, VALIDATION_RULES)

Status: Internal
"""

# Lazy barrel — register names in `_LAZY_IMPORTS`; never add an eager
# top-level `from baldur.X import ...` here (defeats the lazy import path
# and is caught by the import-weight gate).

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from baldur.settings.admin_identity import (
        AdminIdentitySettings,
        get_admin_identity_settings,
        reset_admin_identity_settings,
    )
    from baldur.settings.admission_control import (
        AdmissionControlSettings,
        get_admission_control_settings,
        reset_admission_control_settings,
    )
    from baldur.settings.anti_flapping import (
        AntiFlappingSettings,
        get_anti_flapping_settings,
        reset_anti_flapping_settings,
    )
    from baldur.settings.api_rate_limit import (
        ApiRateLimitSettings,
        get_api_rate_limit_settings,
        reset_api_rate_limit_settings,
    )
    from baldur.settings.api_view import (
        ApiViewSettings,
        get_api_view_settings,
        reset_api_view_settings,
    )
    from baldur.settings.apply_strategy import (
        ApplyStrategySettings,
        get_apply_strategy_settings,
        reset_apply_strategy_settings,
    )
    from baldur.settings.arq_task import (
        ArqTaskSettings,
        get_arq_task_settings,
        reset_arq_task_settings,
    )
    from baldur.settings.audit import (
        AuditSettings,
        get_audit_settings,
        reset_audit_settings,
    )
    from baldur.settings.audit_integrity import (
        AuditIntegritySettings,
        get_audit_integrity_settings,
        reset_audit_integrity_settings,
    )
    from baldur.settings.audit_sync import (
        AuditSyncSettings,
        get_audit_sync_settings,
        reset_audit_sync_settings,
    )
    from baldur.settings.audit_watchdog import (
        AuditWatchdogSettings,
        get_audit_watchdog_settings,
        reset_audit_watchdog_settings,
    )
    from baldur.settings.auto_rollback import (
        AutoRollbackSettings,
        get_auto_rollback_settings,
        reset_auto_rollback_settings,
    )
    from baldur.settings.backpressure import (
        LEVEL_RATE_MULTIPLIERS,
        BackpressureLevel,
        BackpressureSettings,
        BackpressureStrategy,
        get_backpressure_settings,
        reset_backpressure_settings,
    )
    from baldur.settings.batch import (
        BatchSettings,
        get_batch_settings,
        reset_batch_settings,
    )
    from baldur.settings.cascade_retention import (
        CascadeRetentionSettings,
        get_cascade_retention_settings,
        reset_cascade_retention_settings,
    )
    from baldur.settings.celery_task import (
        CeleryTaskSettings,
        get_celery_task_settings,
        reset_celery_task_settings,
    )
    from baldur.settings.cell_topology import (
        CellTopologySettings,
        get_cell_topology_settings,
        reset_cell_topology_settings,
    )
    from baldur.settings.chaos import (
        ChaosSettings,
        get_chaos_settings,
        reset_chaos_settings,
    )
    from baldur.settings.chaos_blast_radius import (
        ChaosBlastRadiusSettings,
        get_chaos_blast_radius_settings,
        reset_chaos_blast_radius_settings,
    )
    from baldur.settings.chaos_experiment import (
        ChaosExperimentSettings,
        get_chaos_experiment_settings,
        reset_chaos_experiment_settings,
    )
    from baldur.settings.circuit_breaker import (
        CircuitBreakerSettings,
        get_circuit_breaker_settings,
        reset_circuit_breaker_settings,
    )
    from baldur.settings.circuit_breaker_advanced import (
        CircuitBreakerAdvancedSettings,
        get_circuit_breaker_advanced_settings,
        reset_circuit_breaker_advanced_settings,
    )
    from baldur.settings.cleanup import (
        CleanupSettings,
        get_cleanup_settings,
        reset_cleanup_settings,
    )
    from baldur.settings.corruption_shield import (
        CorruptionShieldSettings,
        get_corruption_shield_settings,
        reset_corruption_shield_settings,
    )
    from baldur.settings.critical_worker import (
        CriticalWorkerSettings,
        DeploymentEnvironment,
        get_critical_worker_settings,
        reset_critical_worker_settings,
    )
    from baldur.settings.daily_report import (
        DailyReportSettings,
        get_daily_report_settings,
        reset_daily_report_settings,
    )
    from baldur.settings.dashboard import (
        DashboardSettings,
        get_dashboard_settings,
        reset_dashboard_settings,
    )
    from baldur.settings.decision_engine import (
        DecisionEngineSettings,
        get_decision_engine_settings,
        reset_decision_engine_settings,
    )
    from baldur.settings.detection import (
        DetectionSettings,
        get_detection_settings,
        reset_detection_settings,
    )
    from baldur.settings.distributed_lock import (
        DistributedLockSettings,
        get_distributed_lock_settings,
        reset_distributed_lock_settings,
    )
    from baldur.settings.dlq import (
        DLQSettings,
        get_dlq_settings,
        reset_dlq_settings,
    )
    from baldur.settings.domain_sensitivity import (
        DomainSensitivitySettings,
        get_domain_sensitivity_settings,
        reset_domain_sensitivity_settings,
    )
    from baldur.settings.drift_monitor import (
        ConfigDriftMonitor,
        get_config_drift_monitor,
        reset_config_drift_monitor,
    )
    from baldur.settings.drift_threshold import (
        DriftThresholdSettings,
        get_drift_threshold_settings,
        reset_drift_threshold_settings,
    )
    from baldur.settings.emergency_mode import (
        EmergencyModeSettings,
        get_emergency_mode_settings,
        reset_emergency_mode_settings,
    )
    from baldur.settings.error_budget import (
        ErrorBudgetSettings,
        get_error_budget_settings,
        reset_error_budget_settings,
    )
    from baldur.settings.error_budget_gate import (
        ErrorBudgetGateSettings,
        get_error_budget_gate_settings,
        reset_error_budget_gate_settings,
    )
    from baldur.settings.error_budget_propagation import (
        ErrorBudgetPropagationSettings,
        get_error_budget_propagation_settings,
        reset_error_budget_propagation_settings,
    )
    from baldur.settings.event_buffer import (
        EventBufferSettings,
        get_event_buffer_settings,
        reset_event_buffer_settings,
    )
    from baldur.settings.event_logging import (
        EventLoggingConfig,
        get_event_logging_config,
        reset_event_logging_config,
    )
    from baldur.settings.field_types import (
        STANDARD_BACKOFF_MULTIPLIER,
        STANDARD_BASE_DELAY,
        STANDARD_BATCH_SIZE,
        STANDARD_CHECK_INTERVAL,
        STANDARD_JITTER_FACTOR,
        STANDARD_MAX_DELAY,
        STANDARD_POOL_SIZE,
        STANDARD_RETRY_COUNT,
        STANDARD_TIMEOUT_SECONDS,
        BackoffMultiplier,
        HugeCount,
        IntervalDuration,
        JitterFactor,
        LargeCount,
        LongDuration,
        MediumCount,
        MediumDuration,
        Percentage,
        Probability,
        ShortDuration,
        ShortInterval,
        SmallCount,
        StrictProbability,
        TinyCount,
        ZeroableSmallCount,
    )
    from baldur.settings.finops import (
        FinOpsSettings,
        get_finops_settings,
        reset_finops_settings,
    )
    from baldur.settings.forensic import (
        ForensicSettings,
        get_forensic_settings,
        reset_forensic_settings,
    )
    from baldur.settings.governance import (
        GovernanceSettings,
        get_governance_settings,
        reset_governance_settings,
    )
    from baldur.settings.hash_chain import (
        HashChainSettings,
        get_hash_chain_settings,
        reset_hash_chain_settings,
    )
    from baldur.settings.health_check import (
        HealthCheckSettings,
        get_health_check_settings,
        reset_health_check_settings,
    )
    from baldur.settings.idempotency import (
        IdempotencySettings,
        get_idempotency_settings,
        reset_idempotency_settings,
    )
    from baldur.settings.kafka_producer import (
        KafkaProducerSettings,
        get_kafka_producer_settings,
        reset_kafka_producer_settings,
    )
    from baldur.settings.l2_storage import (
        L2StorageRuntimeConfig,
        L2StorageSettings,
        get_l2_storage_runtime_config,
        get_l2_storage_settings,
        reset_l2_storage_runtime_config,
        reset_l2_storage_settings,
    )
    from baldur.settings.layered_provider import (
        RequestOverrideContext,
        clear_request_overrides,
        detect_config_source,
        get_all_request_overrides,
        get_circuit_breaker_layered,
        get_config_with_sources,
        get_dlq_layered,
        get_layered_settings,
        get_rate_limit_layered,
        get_request_override,
        get_retry_layered,
        set_request_override,
    )
    from baldur.settings.leader_election import (
        LeaderElectionSettings,
        get_leader_election_settings,
        reset_leader_election_settings,
    )
    from baldur.settings.learning import (
        LearningSettings,
        ThrottleSLARule,
        get_learning_settings,
        reset_learning_settings,
    )
    from baldur.settings.license import (
        EntitlementSettings,
        get_entitlement_settings,
        reset_entitlement_settings,
    )
    from baldur.settings.logging_settings import (
        LoggingSettings,
        get_logging_settings,
        reset_logging_settings,
    )
    from baldur.settings.meta_watchdog import (
        MetaWatchdogSettings,
        get_meta_watchdog_settings,
        reset_meta_watchdog_settings,
    )
    from baldur.settings.metrics import (
        MetricsSettings,
        get_metrics_settings,
        reset_metrics_settings,
    )
    from baldur.settings.notification import (
        NotificationSettings,
        get_notification_settings,
        reset_notification_settings,
    )
    from baldur.settings.notification_channel import (
        NotificationChannelSettings,
        get_notification_channel_settings,
        reset_notification_channel_settings,
    )
    from baldur.settings.observability import (
        ObservabilityProfile,
        ObservabilitySettings,
        get_observability_settings,
        reset_observability_settings,
    )
    from baldur.settings.postgres import (
        PostgresSettings,
        get_postgres_settings,
        reset_postgres_settings,
    )
    from baldur.settings.rate_limit import (
        RateLimitSettings,
        get_rate_limit_settings,
        reset_rate_limit_settings,
    )
    from baldur.settings.rate_limit_backoff import (
        RateLimitBackoffSettings,
        get_rate_limit_backoff_settings,
        reset_rate_limit_backoff_settings,
    )
    from baldur.settings.recovery_circuit_breaker import (
        RecoveryCircuitBreakerSettings,
        get_recovery_circuit_breaker_settings,
        reset_recovery_circuit_breaker_settings,
    )
    from baldur.settings.recovery_coordinator import (
        RecoveryCoordinatorSettings,
        get_recovery_coordinator_settings,
        reset_recovery_coordinator_settings,
    )
    from baldur.settings.recovery_shutdown import (
        RecoveryShutdownSettings,
        get_recovery_shutdown_settings,
        reset_recovery_shutdown_settings,
    )
    from baldur.settings.recovery_tasks import (
        RecoveryTasksSettings,
        get_recovery_tasks_settings,
        reset_recovery_tasks_settings,
    )
    from baldur.settings.redis import (
        RedisSettings,
        get_redis_settings,
        reset_redis_settings,
    )
    from baldur.settings.redis_key_guard import (
        RedisKeyGuardSettings,
        get_redis_key_guard_settings,
        reset_redis_key_guard_settings,
    )
    from baldur.settings.regional_recovery_policy import (
        RegionalRecoveryPolicySettings,
        get_regional_recovery_policy_settings,
        reset_regional_recovery_policy_settings,
    )
    from baldur.settings.replay_automation import (
        ReplayAutomationSettings,
        get_replay_automation_settings,
        reset_replay_automation_settings,
    )
    from baldur.settings.resilient_recorder import (
        ResilientRecorderSettings,
        get_resilient_recorder_settings,
        reset_resilient_recorder_settings,
    )
    from baldur.settings.resource_guard import (
        ResourceGuardSettings,
        get_resource_guard_settings,
        reset_resource_guard_settings,
    )
    from baldur.settings.retry import (
        RetrySettings,
        get_retry_settings,
        reset_retry_settings,
    )
    from baldur.settings.root import (
        BaldurSettings,
        FallbackPolicy,
        configure,
        get_config,
        get_security_thresholds,
        get_sla_thresholds,
        reload_config,
        reset_config,
        set_config,
    )
    from baldur.settings.runbook import (
        RunbookSettings,
        get_runbook_settings,
        reset_runbook_settings,
    )
    from baldur.settings.runtime_feedback import (
        RuntimeFeedbackSettings,
        get_runtime_feedback_settings,
        reset_runtime_feedback_settings,
    )
    from baldur.settings.s3 import (
        S3Settings,
        get_s3_settings,
        reset_s3_settings,
    )
    from baldur.settings.safety_bounds import (
        ParameterBoundConfig,
        SafetyBoundsSettings,
        get_safety_bounds_settings,
        reset_safety_bounds_settings,
    )
    from baldur.settings.saga import (
        SagaSettings,
        get_saga_settings,
        reset_saga_settings,
    )
    from baldur.settings.scale import (
        PROFILE_DEFAULTS,
        ScaleProfile,
        ScaleSettings,
        get_scale_settings,
        reset_scale_settings,
    )
    from baldur.settings.secrets import (
        SecretsSettings,
        get_secrets,
        reset_secrets,
    )
    from baldur.settings.security import (
        SecuritySettings,
        get_security_settings,
        reset_security_settings,
    )
    from baldur.settings.sla import (
        SLASettings,
        get_sla_settings,
        reset_sla_settings,
    )
    from baldur.settings.slack_channel import (
        SlackChannelSettings,
        get_slack_channel_settings,
        reset_slack_channel_settings,
    )
    from baldur.settings.slo import (
        SLOSettings,
        get_slo_settings,
        reset_slo_settings,
    )
    from baldur.settings.sql import (
        SQLDialect,
        SQLSettings,
        get_sql_settings,
        reset_sql_settings,
    )
    from baldur.settings.state_cache import (
        StateCacheSettings,
        get_state_cache_settings,
        reset_state_cache_settings,
    )
    from baldur.settings.system_control import (
        SystemControlSettings,
        get_system_control_settings,
        reset_system_control_settings,
    )
    from baldur.settings.thread_management import (
        ThreadManagementSettings,
        get_thread_management_settings,
        reset_thread_management_settings,
    )
    from baldur.settings.throttle import (
        ThrottleSettings,
        get_throttle_settings,
        reset_throttle_settings,
    )
    from baldur.settings.validators import (
        warn_above,
        warn_below,
    )

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "AdminIdentitySettings": (
        "baldur.settings.admin_identity",
        "AdminIdentitySettings",
    ),
    "get_admin_identity_settings": (
        "baldur.settings.admin_identity",
        "get_admin_identity_settings",
    ),
    "reset_admin_identity_settings": (
        "baldur.settings.admin_identity",
        "reset_admin_identity_settings",
    ),
    "AdmissionControlSettings": (
        "baldur.settings.admission_control",
        "AdmissionControlSettings",
    ),
    "get_admission_control_settings": (
        "baldur.settings.admission_control",
        "get_admission_control_settings",
    ),
    "reset_admission_control_settings": (
        "baldur.settings.admission_control",
        "reset_admission_control_settings",
    ),
    "AntiFlappingSettings": ("baldur.settings.anti_flapping", "AntiFlappingSettings"),
    "get_anti_flapping_settings": (
        "baldur.settings.anti_flapping",
        "get_anti_flapping_settings",
    ),
    "reset_anti_flapping_settings": (
        "baldur.settings.anti_flapping",
        "reset_anti_flapping_settings",
    ),
    "ApiRateLimitSettings": ("baldur.settings.api_rate_limit", "ApiRateLimitSettings"),
    "get_api_rate_limit_settings": (
        "baldur.settings.api_rate_limit",
        "get_api_rate_limit_settings",
    ),
    "reset_api_rate_limit_settings": (
        "baldur.settings.api_rate_limit",
        "reset_api_rate_limit_settings",
    ),
    "ApiViewSettings": ("baldur.settings.api_view", "ApiViewSettings"),
    "get_api_view_settings": ("baldur.settings.api_view", "get_api_view_settings"),
    "reset_api_view_settings": ("baldur.settings.api_view", "reset_api_view_settings"),
    "ApplyStrategySettings": (
        "baldur.settings.apply_strategy",
        "ApplyStrategySettings",
    ),
    "get_apply_strategy_settings": (
        "baldur.settings.apply_strategy",
        "get_apply_strategy_settings",
    ),
    "reset_apply_strategy_settings": (
        "baldur.settings.apply_strategy",
        "reset_apply_strategy_settings",
    ),
    "ArqTaskSettings": ("baldur.settings.arq_task", "ArqTaskSettings"),
    "get_arq_task_settings": ("baldur.settings.arq_task", "get_arq_task_settings"),
    "reset_arq_task_settings": ("baldur.settings.arq_task", "reset_arq_task_settings"),
    "AuditSettings": ("baldur.settings.audit", "AuditSettings"),
    "get_audit_settings": ("baldur.settings.audit", "get_audit_settings"),
    "reset_audit_settings": ("baldur.settings.audit", "reset_audit_settings"),
    "AuditIntegritySettings": (
        "baldur.settings.audit_integrity",
        "AuditIntegritySettings",
    ),
    "get_audit_integrity_settings": (
        "baldur.settings.audit_integrity",
        "get_audit_integrity_settings",
    ),
    "reset_audit_integrity_settings": (
        "baldur.settings.audit_integrity",
        "reset_audit_integrity_settings",
    ),
    "AuditSyncSettings": ("baldur.settings.audit_sync", "AuditSyncSettings"),
    "get_audit_sync_settings": (
        "baldur.settings.audit_sync",
        "get_audit_sync_settings",
    ),
    "reset_audit_sync_settings": (
        "baldur.settings.audit_sync",
        "reset_audit_sync_settings",
    ),
    "AuditWatchdogSettings": (
        "baldur.settings.audit_watchdog",
        "AuditWatchdogSettings",
    ),
    "get_audit_watchdog_settings": (
        "baldur.settings.audit_watchdog",
        "get_audit_watchdog_settings",
    ),
    "reset_audit_watchdog_settings": (
        "baldur.settings.audit_watchdog",
        "reset_audit_watchdog_settings",
    ),
    "AutoRollbackSettings": ("baldur.settings.auto_rollback", "AutoRollbackSettings"),
    "get_auto_rollback_settings": (
        "baldur.settings.auto_rollback",
        "get_auto_rollback_settings",
    ),
    "reset_auto_rollback_settings": (
        "baldur.settings.auto_rollback",
        "reset_auto_rollback_settings",
    ),
    "LEVEL_RATE_MULTIPLIERS": (
        "baldur.settings.backpressure",
        "LEVEL_RATE_MULTIPLIERS",
    ),
    "BackpressureLevel": ("baldur.settings.backpressure", "BackpressureLevel"),
    "BackpressureSettings": ("baldur.settings.backpressure", "BackpressureSettings"),
    "BackpressureStrategy": ("baldur.settings.backpressure", "BackpressureStrategy"),
    "get_backpressure_settings": (
        "baldur.settings.backpressure",
        "get_backpressure_settings",
    ),
    "reset_backpressure_settings": (
        "baldur.settings.backpressure",
        "reset_backpressure_settings",
    ),
    "BatchSettings": ("baldur.settings.batch", "BatchSettings"),
    "get_batch_settings": ("baldur.settings.batch", "get_batch_settings"),
    "reset_batch_settings": ("baldur.settings.batch", "reset_batch_settings"),
    "CascadeRetentionSettings": (
        "baldur.settings.cascade_retention",
        "CascadeRetentionSettings",
    ),
    "get_cascade_retention_settings": (
        "baldur.settings.cascade_retention",
        "get_cascade_retention_settings",
    ),
    "reset_cascade_retention_settings": (
        "baldur.settings.cascade_retention",
        "reset_cascade_retention_settings",
    ),
    "CeleryTaskSettings": ("baldur.settings.celery_task", "CeleryTaskSettings"),
    "get_celery_task_settings": (
        "baldur.settings.celery_task",
        "get_celery_task_settings",
    ),
    "reset_celery_task_settings": (
        "baldur.settings.celery_task",
        "reset_celery_task_settings",
    ),
    "CellTopologySettings": ("baldur.settings.cell_topology", "CellTopologySettings"),
    "get_cell_topology_settings": (
        "baldur.settings.cell_topology",
        "get_cell_topology_settings",
    ),
    "reset_cell_topology_settings": (
        "baldur.settings.cell_topology",
        "reset_cell_topology_settings",
    ),
    "ChaosSettings": ("baldur.settings.chaos", "ChaosSettings"),
    "get_chaos_settings": ("baldur.settings.chaos", "get_chaos_settings"),
    "reset_chaos_settings": ("baldur.settings.chaos", "reset_chaos_settings"),
    "ChaosBlastRadiusSettings": (
        "baldur.settings.chaos_blast_radius",
        "ChaosBlastRadiusSettings",
    ),
    "get_chaos_blast_radius_settings": (
        "baldur.settings.chaos_blast_radius",
        "get_chaos_blast_radius_settings",
    ),
    "reset_chaos_blast_radius_settings": (
        "baldur.settings.chaos_blast_radius",
        "reset_chaos_blast_radius_settings",
    ),
    "ChaosExperimentSettings": (
        "baldur.settings.chaos_experiment",
        "ChaosExperimentSettings",
    ),
    "get_chaos_experiment_settings": (
        "baldur.settings.chaos_experiment",
        "get_chaos_experiment_settings",
    ),
    "reset_chaos_experiment_settings": (
        "baldur.settings.chaos_experiment",
        "reset_chaos_experiment_settings",
    ),
    "CircuitBreakerSettings": (
        "baldur.settings.circuit_breaker",
        "CircuitBreakerSettings",
    ),
    "get_circuit_breaker_settings": (
        "baldur.settings.circuit_breaker",
        "get_circuit_breaker_settings",
    ),
    "reset_circuit_breaker_settings": (
        "baldur.settings.circuit_breaker",
        "reset_circuit_breaker_settings",
    ),
    "CircuitBreakerAdvancedSettings": (
        "baldur.settings.circuit_breaker_advanced",
        "CircuitBreakerAdvancedSettings",
    ),
    "get_circuit_breaker_advanced_settings": (
        "baldur.settings.circuit_breaker_advanced",
        "get_circuit_breaker_advanced_settings",
    ),
    "reset_circuit_breaker_advanced_settings": (
        "baldur.settings.circuit_breaker_advanced",
        "reset_circuit_breaker_advanced_settings",
    ),
    "CleanupSettings": ("baldur.settings.cleanup", "CleanupSettings"),
    "get_cleanup_settings": ("baldur.settings.cleanup", "get_cleanup_settings"),
    "reset_cleanup_settings": ("baldur.settings.cleanup", "reset_cleanup_settings"),
    "CorruptionShieldSettings": (
        "baldur.settings.corruption_shield",
        "CorruptionShieldSettings",
    ),
    "get_corruption_shield_settings": (
        "baldur.settings.corruption_shield",
        "get_corruption_shield_settings",
    ),
    "reset_corruption_shield_settings": (
        "baldur.settings.corruption_shield",
        "reset_corruption_shield_settings",
    ),
    "CriticalWorkerSettings": (
        "baldur.settings.critical_worker",
        "CriticalWorkerSettings",
    ),
    "DeploymentEnvironment": (
        "baldur.settings.critical_worker",
        "DeploymentEnvironment",
    ),
    "get_critical_worker_settings": (
        "baldur.settings.critical_worker",
        "get_critical_worker_settings",
    ),
    "reset_critical_worker_settings": (
        "baldur.settings.critical_worker",
        "reset_critical_worker_settings",
    ),
    "DailyReportSettings": ("baldur.settings.daily_report", "DailyReportSettings"),
    "get_daily_report_settings": (
        "baldur.settings.daily_report",
        "get_daily_report_settings",
    ),
    "reset_daily_report_settings": (
        "baldur.settings.daily_report",
        "reset_daily_report_settings",
    ),
    "DashboardSettings": ("baldur.settings.dashboard", "DashboardSettings"),
    "get_dashboard_settings": ("baldur.settings.dashboard", "get_dashboard_settings"),
    "reset_dashboard_settings": (
        "baldur.settings.dashboard",
        "reset_dashboard_settings",
    ),
    "DecisionEngineSettings": (
        "baldur.settings.decision_engine",
        "DecisionEngineSettings",
    ),
    "get_decision_engine_settings": (
        "baldur.settings.decision_engine",
        "get_decision_engine_settings",
    ),
    "reset_decision_engine_settings": (
        "baldur.settings.decision_engine",
        "reset_decision_engine_settings",
    ),
    "DetectionSettings": ("baldur.settings.detection", "DetectionSettings"),
    "get_detection_settings": ("baldur.settings.detection", "get_detection_settings"),
    "reset_detection_settings": (
        "baldur.settings.detection",
        "reset_detection_settings",
    ),
    "DistributedLockSettings": (
        "baldur.settings.distributed_lock",
        "DistributedLockSettings",
    ),
    "get_distributed_lock_settings": (
        "baldur.settings.distributed_lock",
        "get_distributed_lock_settings",
    ),
    "reset_distributed_lock_settings": (
        "baldur.settings.distributed_lock",
        "reset_distributed_lock_settings",
    ),
    "DLQSettings": ("baldur.settings.dlq", "DLQSettings"),
    "get_dlq_settings": ("baldur.settings.dlq", "get_dlq_settings"),
    "reset_dlq_settings": ("baldur.settings.dlq", "reset_dlq_settings"),
    "DomainSensitivitySettings": (
        "baldur.settings.domain_sensitivity",
        "DomainSensitivitySettings",
    ),
    "get_domain_sensitivity_settings": (
        "baldur.settings.domain_sensitivity",
        "get_domain_sensitivity_settings",
    ),
    "reset_domain_sensitivity_settings": (
        "baldur.settings.domain_sensitivity",
        "reset_domain_sensitivity_settings",
    ),
    "ConfigDriftMonitor": ("baldur.settings.drift_monitor", "ConfigDriftMonitor"),
    "get_config_drift_monitor": (
        "baldur.settings.drift_monitor",
        "get_config_drift_monitor",
    ),
    "reset_config_drift_monitor": (
        "baldur.settings.drift_monitor",
        "reset_config_drift_monitor",
    ),
    "DriftThresholdSettings": (
        "baldur.settings.drift_threshold",
        "DriftThresholdSettings",
    ),
    "get_drift_threshold_settings": (
        "baldur.settings.drift_threshold",
        "get_drift_threshold_settings",
    ),
    "reset_drift_threshold_settings": (
        "baldur.settings.drift_threshold",
        "reset_drift_threshold_settings",
    ),
    "EmergencyModeSettings": (
        "baldur.settings.emergency_mode",
        "EmergencyModeSettings",
    ),
    "get_emergency_mode_settings": (
        "baldur.settings.emergency_mode",
        "get_emergency_mode_settings",
    ),
    "reset_emergency_mode_settings": (
        "baldur.settings.emergency_mode",
        "reset_emergency_mode_settings",
    ),
    "ErrorBudgetSettings": ("baldur.settings.error_budget", "ErrorBudgetSettings"),
    "get_error_budget_settings": (
        "baldur.settings.error_budget",
        "get_error_budget_settings",
    ),
    "reset_error_budget_settings": (
        "baldur.settings.error_budget",
        "reset_error_budget_settings",
    ),
    "ErrorBudgetGateSettings": (
        "baldur.settings.error_budget_gate",
        "ErrorBudgetGateSettings",
    ),
    "get_error_budget_gate_settings": (
        "baldur.settings.error_budget_gate",
        "get_error_budget_gate_settings",
    ),
    "reset_error_budget_gate_settings": (
        "baldur.settings.error_budget_gate",
        "reset_error_budget_gate_settings",
    ),
    "ErrorBudgetPropagationSettings": (
        "baldur.settings.error_budget_propagation",
        "ErrorBudgetPropagationSettings",
    ),
    "get_error_budget_propagation_settings": (
        "baldur.settings.error_budget_propagation",
        "get_error_budget_propagation_settings",
    ),
    "reset_error_budget_propagation_settings": (
        "baldur.settings.error_budget_propagation",
        "reset_error_budget_propagation_settings",
    ),
    "EventBufferSettings": ("baldur.settings.event_buffer", "EventBufferSettings"),
    "get_event_buffer_settings": (
        "baldur.settings.event_buffer",
        "get_event_buffer_settings",
    ),
    "reset_event_buffer_settings": (
        "baldur.settings.event_buffer",
        "reset_event_buffer_settings",
    ),
    "EventLoggingConfig": ("baldur.settings.event_logging", "EventLoggingConfig"),
    "get_event_logging_config": (
        "baldur.settings.event_logging",
        "get_event_logging_config",
    ),
    "reset_event_logging_config": (
        "baldur.settings.event_logging",
        "reset_event_logging_config",
    ),
    "STANDARD_BACKOFF_MULTIPLIER": (
        "baldur.settings.field_types",
        "STANDARD_BACKOFF_MULTIPLIER",
    ),
    "STANDARD_BASE_DELAY": ("baldur.settings.field_types", "STANDARD_BASE_DELAY"),
    "STANDARD_BATCH_SIZE": ("baldur.settings.field_types", "STANDARD_BATCH_SIZE"),
    "STANDARD_CHECK_INTERVAL": (
        "baldur.settings.field_types",
        "STANDARD_CHECK_INTERVAL",
    ),
    "STANDARD_JITTER_FACTOR": ("baldur.settings.field_types", "STANDARD_JITTER_FACTOR"),
    "STANDARD_MAX_DELAY": ("baldur.settings.field_types", "STANDARD_MAX_DELAY"),
    "STANDARD_POOL_SIZE": ("baldur.settings.field_types", "STANDARD_POOL_SIZE"),
    "STANDARD_RETRY_COUNT": ("baldur.settings.field_types", "STANDARD_RETRY_COUNT"),
    "STANDARD_TIMEOUT_SECONDS": (
        "baldur.settings.field_types",
        "STANDARD_TIMEOUT_SECONDS",
    ),
    "BackoffMultiplier": ("baldur.settings.field_types", "BackoffMultiplier"),
    "HugeCount": ("baldur.settings.field_types", "HugeCount"),
    "IntervalDuration": ("baldur.settings.field_types", "IntervalDuration"),
    "JitterFactor": ("baldur.settings.field_types", "JitterFactor"),
    "LargeCount": ("baldur.settings.field_types", "LargeCount"),
    "LongDuration": ("baldur.settings.field_types", "LongDuration"),
    "MediumCount": ("baldur.settings.field_types", "MediumCount"),
    "MediumDuration": ("baldur.settings.field_types", "MediumDuration"),
    "Percentage": ("baldur.settings.field_types", "Percentage"),
    "Probability": ("baldur.settings.field_types", "Probability"),
    "ShortDuration": ("baldur.settings.field_types", "ShortDuration"),
    "ShortInterval": ("baldur.settings.field_types", "ShortInterval"),
    "SmallCount": ("baldur.settings.field_types", "SmallCount"),
    "StrictProbability": ("baldur.settings.field_types", "StrictProbability"),
    "TinyCount": ("baldur.settings.field_types", "TinyCount"),
    "ZeroableSmallCount": ("baldur.settings.field_types", "ZeroableSmallCount"),
    "FinOpsSettings": ("baldur.settings.finops", "FinOpsSettings"),
    "get_finops_settings": ("baldur.settings.finops", "get_finops_settings"),
    "reset_finops_settings": ("baldur.settings.finops", "reset_finops_settings"),
    "ForensicSettings": ("baldur.settings.forensic", "ForensicSettings"),
    "get_forensic_settings": ("baldur.settings.forensic", "get_forensic_settings"),
    "reset_forensic_settings": ("baldur.settings.forensic", "reset_forensic_settings"),
    "GovernanceSettings": ("baldur.settings.governance", "GovernanceSettings"),
    "get_governance_settings": (
        "baldur.settings.governance",
        "get_governance_settings",
    ),
    "reset_governance_settings": (
        "baldur.settings.governance",
        "reset_governance_settings",
    ),
    "HashChainSettings": ("baldur.settings.hash_chain", "HashChainSettings"),
    "get_hash_chain_settings": (
        "baldur.settings.hash_chain",
        "get_hash_chain_settings",
    ),
    "reset_hash_chain_settings": (
        "baldur.settings.hash_chain",
        "reset_hash_chain_settings",
    ),
    "HealthCheckSettings": ("baldur.settings.health_check", "HealthCheckSettings"),
    "get_health_check_settings": (
        "baldur.settings.health_check",
        "get_health_check_settings",
    ),
    "reset_health_check_settings": (
        "baldur.settings.health_check",
        "reset_health_check_settings",
    ),
    "IdempotencySettings": ("baldur.settings.idempotency", "IdempotencySettings"),
    "get_idempotency_settings": (
        "baldur.settings.idempotency",
        "get_idempotency_settings",
    ),
    "reset_idempotency_settings": (
        "baldur.settings.idempotency",
        "reset_idempotency_settings",
    ),
    "KafkaProducerSettings": (
        "baldur.settings.kafka_producer",
        "KafkaProducerSettings",
    ),
    "get_kafka_producer_settings": (
        "baldur.settings.kafka_producer",
        "get_kafka_producer_settings",
    ),
    "reset_kafka_producer_settings": (
        "baldur.settings.kafka_producer",
        "reset_kafka_producer_settings",
    ),
    "L2StorageRuntimeConfig": ("baldur.settings.l2_storage", "L2StorageRuntimeConfig"),
    "L2StorageSettings": ("baldur.settings.l2_storage", "L2StorageSettings"),
    "get_l2_storage_runtime_config": (
        "baldur.settings.l2_storage",
        "get_l2_storage_runtime_config",
    ),
    "get_l2_storage_settings": (
        "baldur.settings.l2_storage",
        "get_l2_storage_settings",
    ),
    "reset_l2_storage_runtime_config": (
        "baldur.settings.l2_storage",
        "reset_l2_storage_runtime_config",
    ),
    "reset_l2_storage_settings": (
        "baldur.settings.l2_storage",
        "reset_l2_storage_settings",
    ),
    "RequestOverrideContext": (
        "baldur.settings.layered_provider",
        "RequestOverrideContext",
    ),
    "clear_request_overrides": (
        "baldur.settings.layered_provider",
        "clear_request_overrides",
    ),
    "detect_config_source": (
        "baldur.settings.layered_provider",
        "detect_config_source",
    ),
    "get_all_request_overrides": (
        "baldur.settings.layered_provider",
        "get_all_request_overrides",
    ),
    "get_circuit_breaker_layered": (
        "baldur.settings.layered_provider",
        "get_circuit_breaker_layered",
    ),
    "get_config_with_sources": (
        "baldur.settings.layered_provider",
        "get_config_with_sources",
    ),
    "get_dlq_layered": ("baldur.settings.layered_provider", "get_dlq_layered"),
    "get_layered_settings": (
        "baldur.settings.layered_provider",
        "get_layered_settings",
    ),
    "get_rate_limit_layered": (
        "baldur.settings.layered_provider",
        "get_rate_limit_layered",
    ),
    "get_request_override": (
        "baldur.settings.layered_provider",
        "get_request_override",
    ),
    "get_retry_layered": ("baldur.settings.layered_provider", "get_retry_layered"),
    "set_request_override": (
        "baldur.settings.layered_provider",
        "set_request_override",
    ),
    "LeaderElectionSettings": (
        "baldur.settings.leader_election",
        "LeaderElectionSettings",
    ),
    "get_leader_election_settings": (
        "baldur.settings.leader_election",
        "get_leader_election_settings",
    ),
    "reset_leader_election_settings": (
        "baldur.settings.leader_election",
        "reset_leader_election_settings",
    ),
    "LearningSettings": ("baldur.settings.learning", "LearningSettings"),
    "ThrottleSLARule": ("baldur.settings.learning", "ThrottleSLARule"),
    "get_learning_settings": ("baldur.settings.learning", "get_learning_settings"),
    "reset_learning_settings": ("baldur.settings.learning", "reset_learning_settings"),
    "EntitlementSettings": ("baldur.settings.license", "EntitlementSettings"),
    "get_entitlement_settings": ("baldur.settings.license", "get_entitlement_settings"),
    "reset_entitlement_settings": (
        "baldur.settings.license",
        "reset_entitlement_settings",
    ),
    "LoggingSettings": ("baldur.settings.logging_settings", "LoggingSettings"),
    "get_logging_settings": (
        "baldur.settings.logging_settings",
        "get_logging_settings",
    ),
    "reset_logging_settings": (
        "baldur.settings.logging_settings",
        "reset_logging_settings",
    ),
    "MetaWatchdogSettings": ("baldur.settings.meta_watchdog", "MetaWatchdogSettings"),
    "get_meta_watchdog_settings": (
        "baldur.settings.meta_watchdog",
        "get_meta_watchdog_settings",
    ),
    "reset_meta_watchdog_settings": (
        "baldur.settings.meta_watchdog",
        "reset_meta_watchdog_settings",
    ),
    "MetricsSettings": ("baldur.settings.metrics", "MetricsSettings"),
    "get_metrics_settings": ("baldur.settings.metrics", "get_metrics_settings"),
    "reset_metrics_settings": ("baldur.settings.metrics", "reset_metrics_settings"),
    "NotificationSettings": ("baldur.settings.notification", "NotificationSettings"),
    "get_notification_settings": (
        "baldur.settings.notification",
        "get_notification_settings",
    ),
    "reset_notification_settings": (
        "baldur.settings.notification",
        "reset_notification_settings",
    ),
    "NotificationChannelSettings": (
        "baldur.settings.notification_channel",
        "NotificationChannelSettings",
    ),
    "get_notification_channel_settings": (
        "baldur.settings.notification_channel",
        "get_notification_channel_settings",
    ),
    "reset_notification_channel_settings": (
        "baldur.settings.notification_channel",
        "reset_notification_channel_settings",
    ),
    "ObservabilityProfile": ("baldur.settings.observability", "ObservabilityProfile"),
    "ObservabilitySettings": ("baldur.settings.observability", "ObservabilitySettings"),
    "get_observability_settings": (
        "baldur.settings.observability",
        "get_observability_settings",
    ),
    "reset_observability_settings": (
        "baldur.settings.observability",
        "reset_observability_settings",
    ),
    "PostgresSettings": ("baldur.settings.postgres", "PostgresSettings"),
    "get_postgres_settings": ("baldur.settings.postgres", "get_postgres_settings"),
    "reset_postgres_settings": ("baldur.settings.postgres", "reset_postgres_settings"),
    "RateLimitSettings": ("baldur.settings.rate_limit", "RateLimitSettings"),
    "get_rate_limit_settings": (
        "baldur.settings.rate_limit",
        "get_rate_limit_settings",
    ),
    "reset_rate_limit_settings": (
        "baldur.settings.rate_limit",
        "reset_rate_limit_settings",
    ),
    "RateLimitBackoffSettings": (
        "baldur.settings.rate_limit_backoff",
        "RateLimitBackoffSettings",
    ),
    "get_rate_limit_backoff_settings": (
        "baldur.settings.rate_limit_backoff",
        "get_rate_limit_backoff_settings",
    ),
    "reset_rate_limit_backoff_settings": (
        "baldur.settings.rate_limit_backoff",
        "reset_rate_limit_backoff_settings",
    ),
    "RecoveryCircuitBreakerSettings": (
        "baldur.settings.recovery_circuit_breaker",
        "RecoveryCircuitBreakerSettings",
    ),
    "get_recovery_circuit_breaker_settings": (
        "baldur.settings.recovery_circuit_breaker",
        "get_recovery_circuit_breaker_settings",
    ),
    "reset_recovery_circuit_breaker_settings": (
        "baldur.settings.recovery_circuit_breaker",
        "reset_recovery_circuit_breaker_settings",
    ),
    "RecoveryCoordinatorSettings": (
        "baldur.settings.recovery_coordinator",
        "RecoveryCoordinatorSettings",
    ),
    "get_recovery_coordinator_settings": (
        "baldur.settings.recovery_coordinator",
        "get_recovery_coordinator_settings",
    ),
    "reset_recovery_coordinator_settings": (
        "baldur.settings.recovery_coordinator",
        "reset_recovery_coordinator_settings",
    ),
    "RecoveryShutdownSettings": (
        "baldur.settings.recovery_shutdown",
        "RecoveryShutdownSettings",
    ),
    "get_recovery_shutdown_settings": (
        "baldur.settings.recovery_shutdown",
        "get_recovery_shutdown_settings",
    ),
    "reset_recovery_shutdown_settings": (
        "baldur.settings.recovery_shutdown",
        "reset_recovery_shutdown_settings",
    ),
    "RecoveryTasksSettings": (
        "baldur.settings.recovery_tasks",
        "RecoveryTasksSettings",
    ),
    "get_recovery_tasks_settings": (
        "baldur.settings.recovery_tasks",
        "get_recovery_tasks_settings",
    ),
    "reset_recovery_tasks_settings": (
        "baldur.settings.recovery_tasks",
        "reset_recovery_tasks_settings",
    ),
    "RedisSettings": ("baldur.settings.redis", "RedisSettings"),
    "get_redis_settings": ("baldur.settings.redis", "get_redis_settings"),
    "reset_redis_settings": ("baldur.settings.redis", "reset_redis_settings"),
    "RedisKeyGuardSettings": (
        "baldur.settings.redis_key_guard",
        "RedisKeyGuardSettings",
    ),
    "get_redis_key_guard_settings": (
        "baldur.settings.redis_key_guard",
        "get_redis_key_guard_settings",
    ),
    "reset_redis_key_guard_settings": (
        "baldur.settings.redis_key_guard",
        "reset_redis_key_guard_settings",
    ),
    "RegionalRecoveryPolicySettings": (
        "baldur.settings.regional_recovery_policy",
        "RegionalRecoveryPolicySettings",
    ),
    "get_regional_recovery_policy_settings": (
        "baldur.settings.regional_recovery_policy",
        "get_regional_recovery_policy_settings",
    ),
    "reset_regional_recovery_policy_settings": (
        "baldur.settings.regional_recovery_policy",
        "reset_regional_recovery_policy_settings",
    ),
    "ReplayAutomationSettings": (
        "baldur.settings.replay_automation",
        "ReplayAutomationSettings",
    ),
    "get_replay_automation_settings": (
        "baldur.settings.replay_automation",
        "get_replay_automation_settings",
    ),
    "reset_replay_automation_settings": (
        "baldur.settings.replay_automation",
        "reset_replay_automation_settings",
    ),
    "ResilientRecorderSettings": (
        "baldur.settings.resilient_recorder",
        "ResilientRecorderSettings",
    ),
    "get_resilient_recorder_settings": (
        "baldur.settings.resilient_recorder",
        "get_resilient_recorder_settings",
    ),
    "reset_resilient_recorder_settings": (
        "baldur.settings.resilient_recorder",
        "reset_resilient_recorder_settings",
    ),
    "ResourceGuardSettings": (
        "baldur.settings.resource_guard",
        "ResourceGuardSettings",
    ),
    "get_resource_guard_settings": (
        "baldur.settings.resource_guard",
        "get_resource_guard_settings",
    ),
    "reset_resource_guard_settings": (
        "baldur.settings.resource_guard",
        "reset_resource_guard_settings",
    ),
    "RetrySettings": ("baldur.settings.retry", "RetrySettings"),
    "get_retry_settings": ("baldur.settings.retry", "get_retry_settings"),
    "reset_retry_settings": ("baldur.settings.retry", "reset_retry_settings"),
    "BaldurSettings": ("baldur.settings.root", "BaldurSettings"),
    "FallbackPolicy": ("baldur.settings.root", "FallbackPolicy"),
    "configure": ("baldur.settings.root", "configure"),
    "get_config": ("baldur.settings.root", "get_config"),
    "get_security_thresholds": ("baldur.settings.root", "get_security_thresholds"),
    "get_sla_thresholds": ("baldur.settings.root", "get_sla_thresholds"),
    "reload_config": ("baldur.settings.root", "reload_config"),
    "reset_config": ("baldur.settings.root", "reset_config"),
    "set_config": ("baldur.settings.root", "set_config"),
    "RunbookSettings": ("baldur.settings.runbook", "RunbookSettings"),
    "get_runbook_settings": ("baldur.settings.runbook", "get_runbook_settings"),
    "reset_runbook_settings": ("baldur.settings.runbook", "reset_runbook_settings"),
    "RuntimeFeedbackSettings": (
        "baldur.settings.runtime_feedback",
        "RuntimeFeedbackSettings",
    ),
    "get_runtime_feedback_settings": (
        "baldur.settings.runtime_feedback",
        "get_runtime_feedback_settings",
    ),
    "reset_runtime_feedback_settings": (
        "baldur.settings.runtime_feedback",
        "reset_runtime_feedback_settings",
    ),
    "S3Settings": ("baldur.settings.s3", "S3Settings"),
    "get_s3_settings": ("baldur.settings.s3", "get_s3_settings"),
    "reset_s3_settings": ("baldur.settings.s3", "reset_s3_settings"),
    "ParameterBoundConfig": ("baldur.settings.safety_bounds", "ParameterBoundConfig"),
    "SafetyBoundsSettings": ("baldur.settings.safety_bounds", "SafetyBoundsSettings"),
    "get_safety_bounds_settings": (
        "baldur.settings.safety_bounds",
        "get_safety_bounds_settings",
    ),
    "reset_safety_bounds_settings": (
        "baldur.settings.safety_bounds",
        "reset_safety_bounds_settings",
    ),
    "SagaSettings": ("baldur.settings.saga", "SagaSettings"),
    "get_saga_settings": ("baldur.settings.saga", "get_saga_settings"),
    "reset_saga_settings": ("baldur.settings.saga", "reset_saga_settings"),
    "PROFILE_DEFAULTS": ("baldur.settings.scale", "PROFILE_DEFAULTS"),
    "ScaleProfile": ("baldur.settings.scale", "ScaleProfile"),
    "ScaleSettings": ("baldur.settings.scale", "ScaleSettings"),
    "get_scale_settings": ("baldur.settings.scale", "get_scale_settings"),
    "reset_scale_settings": ("baldur.settings.scale", "reset_scale_settings"),
    "SecretsSettings": ("baldur.settings.secrets", "SecretsSettings"),
    "get_secrets": ("baldur.settings.secrets", "get_secrets"),
    "reset_secrets": ("baldur.settings.secrets", "reset_secrets"),
    "SecuritySettings": ("baldur.settings.security", "SecuritySettings"),
    "get_security_settings": ("baldur.settings.security", "get_security_settings"),
    "reset_security_settings": ("baldur.settings.security", "reset_security_settings"),
    "SLASettings": ("baldur.settings.sla", "SLASettings"),
    "get_sla_settings": ("baldur.settings.sla", "get_sla_settings"),
    "reset_sla_settings": ("baldur.settings.sla", "reset_sla_settings"),
    "SlackChannelSettings": ("baldur.settings.slack_channel", "SlackChannelSettings"),
    "get_slack_channel_settings": (
        "baldur.settings.slack_channel",
        "get_slack_channel_settings",
    ),
    "reset_slack_channel_settings": (
        "baldur.settings.slack_channel",
        "reset_slack_channel_settings",
    ),
    "SLOSettings": ("baldur.settings.slo", "SLOSettings"),
    "get_slo_settings": ("baldur.settings.slo", "get_slo_settings"),
    "reset_slo_settings": ("baldur.settings.slo", "reset_slo_settings"),
    "SQLDialect": ("baldur.settings.sql", "SQLDialect"),
    "SQLSettings": ("baldur.settings.sql", "SQLSettings"),
    "get_sql_settings": ("baldur.settings.sql", "get_sql_settings"),
    "reset_sql_settings": ("baldur.settings.sql", "reset_sql_settings"),
    "StateCacheSettings": ("baldur.settings.state_cache", "StateCacheSettings"),
    "get_state_cache_settings": (
        "baldur.settings.state_cache",
        "get_state_cache_settings",
    ),
    "reset_state_cache_settings": (
        "baldur.settings.state_cache",
        "reset_state_cache_settings",
    ),
    "SystemControlSettings": (
        "baldur.settings.system_control",
        "SystemControlSettings",
    ),
    "get_system_control_settings": (
        "baldur.settings.system_control",
        "get_system_control_settings",
    ),
    "reset_system_control_settings": (
        "baldur.settings.system_control",
        "reset_system_control_settings",
    ),
    "ThreadManagementSettings": (
        "baldur.settings.thread_management",
        "ThreadManagementSettings",
    ),
    "get_thread_management_settings": (
        "baldur.settings.thread_management",
        "get_thread_management_settings",
    ),
    "reset_thread_management_settings": (
        "baldur.settings.thread_management",
        "reset_thread_management_settings",
    ),
    "ThrottleSettings": ("baldur.settings.throttle", "ThrottleSettings"),
    "get_throttle_settings": ("baldur.settings.throttle", "get_throttle_settings"),
    "reset_throttle_settings": ("baldur.settings.throttle", "reset_throttle_settings"),
    "warn_above": ("baldur.settings.validators", "warn_above"),
    "warn_below": ("baldur.settings.validators", "warn_below"),
}


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        # Resolve live on each access (no globals() memoization) so the barrel
        # transparently reflects the current submodule attribute — a test that
        # patches `<this package>.<submodule>.<name>` must not be shadowed by a
        # value cached from an earlier patch. importlib already caches the module
        # import, so the cost is a dict lookup.
        return getattr(importlib.import_module(module_path), attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return list(__all__)


__all__ = [
    # Root Settings
    "BaldurSettings",
    "get_config",
    "set_config",
    "reset_config",
    "reload_config",
    "configure",
    # 핵심 설정 (5)
    # Circuit Breaker
    "CircuitBreakerSettings",
    "get_circuit_breaker_settings",
    "reset_circuit_breaker_settings",
    # Circuit Breaker Advanced
    "CircuitBreakerAdvancedSettings",
    "get_circuit_breaker_advanced_settings",
    "reset_circuit_breaker_advanced_settings",
    # DLQ
    "DLQSettings",
    "get_dlq_settings",
    "reset_dlq_settings",
    # Retry
    "RetrySettings",
    "get_retry_settings",
    "reset_retry_settings",
    # Rate Limit (inbound quota)
    "RateLimitSettings",
    "get_rate_limit_settings",
    "reset_rate_limit_settings",
    # Rate Limit Backoff (outbound 429 coordination)
    "RateLimitBackoffSettings",
    "get_rate_limit_backoff_settings",
    "reset_rate_limit_backoff_settings",
    # Runbook Executor (272_RUNBOOK_ARCHITECTURE_OVERVIEW.md)
    "RunbookSettings",
    "get_runbook_settings",
    "reset_runbook_settings",
    # Security
    "SecuritySettings",
    "get_security_settings",
    "reset_security_settings",
    # 확장 설정 (12)
    # SLA
    "SLASettings",
    "get_sla_settings",
    "reset_sla_settings",
    # SLO
    "SLOSettings",
    "get_slo_settings",
    "reset_slo_settings",
    # Idempotency
    "IdempotencySettings",
    "get_idempotency_settings",
    "reset_idempotency_settings",
    # Forensic
    "ForensicSettings",
    "get_forensic_settings",
    "reset_forensic_settings",
    # Logging
    "LoggingSettings",
    "get_logging_settings",
    "reset_logging_settings",
    # Metrics
    "MetricsSettings",
    "get_metrics_settings",
    "reset_metrics_settings",
    # Notification
    "NotificationSettings",
    "get_notification_settings",
    "reset_notification_settings",
    # Error Budget
    "ErrorBudgetSettings",
    "get_error_budget_settings",
    "reset_error_budget_settings",
    # Governance
    "GovernanceSettings",
    "get_governance_settings",
    "reset_governance_settings",
    # Chaos
    "ChaosSettings",
    "get_chaos_settings",
    "reset_chaos_settings",
    # Drift Threshold
    "DriftThresholdSettings",
    "get_drift_threshold_settings",
    "reset_drift_threshold_settings",
    # L2 Storage
    "L2StorageSettings",
    "L2StorageRuntimeConfig",
    "get_l2_storage_settings",
    "reset_l2_storage_settings",
    "get_l2_storage_runtime_config",
    "reset_l2_storage_runtime_config",
    # Replay Automation
    "ReplayAutomationSettings",
    "get_replay_automation_settings",
    "reset_replay_automation_settings",
    # 계층형 Provider
    "get_layered_settings",
    "set_request_override",
    "get_request_override",
    "clear_request_overrides",
    "get_all_request_overrides",
    "detect_config_source",
    "get_config_with_sources",
    "RequestOverrideContext",
    "get_circuit_breaker_layered",
    "get_retry_layered",
    "get_dlq_layered",
    "get_rate_limit_layered",
    # 보안 설정
    "SecretsSettings",
    "get_secrets",
    "reset_secrets",
    # Week 1 CRITICAL Settings (92_CONFIG_IMPLEMENTATION_GUIDE.md)
    # Recovery Circuit Breaker
    "RecoveryCircuitBreakerSettings",
    "get_recovery_circuit_breaker_settings",
    "reset_recovery_circuit_breaker_settings",
    # Redis Connection (328_REDIS_CONNECTION_FACTORY.md)
    "RedisSettings",
    "get_redis_settings",
    "reset_redis_settings",
    # Redis Key Guard
    "RedisKeyGuardSettings",
    "get_redis_key_guard_settings",
    "reset_redis_key_guard_settings",
    # Recovery Shutdown
    "RecoveryShutdownSettings",
    "get_recovery_shutdown_settings",
    "reset_recovery_shutdown_settings",
    # Resilient Recorder
    "ResilientRecorderSettings",
    "get_resilient_recorder_settings",
    "reset_resilient_recorder_settings",
    # Week 2 HIGH Settings (92_CONFIG_IMPLEMENTATION_GUIDE.md)
    # Error Budget Propagation
    "ErrorBudgetPropagationSettings",
    "get_error_budget_propagation_settings",
    "reset_error_budget_propagation_settings",
    # Anti-Flapping
    "AntiFlappingSettings",
    "get_anti_flapping_settings",
    "reset_anti_flapping_settings",
    # Throttle
    "ThrottleSettings",
    "get_throttle_settings",
    "reset_throttle_settings",
    # Critical Worker
    "CriticalWorkerSettings",
    "get_critical_worker_settings",
    "reset_critical_worker_settings",
    # Week 3 MEDIUM Settings (92_CONFIG_IMPLEMENTATION_GUIDE.md)
    # Chaos Experiment
    "ChaosExperimentSettings",
    "get_chaos_experiment_settings",
    "reset_chaos_experiment_settings",
    # Chaos Blast Radius
    "ChaosBlastRadiusSettings",
    "get_chaos_blast_radius_settings",
    "reset_chaos_blast_radius_settings",
    # Corruption Shield
    "CorruptionShieldSettings",
    "get_corruption_shield_settings",
    "reset_corruption_shield_settings",
    # Notification Channel
    "NotificationChannelSettings",
    "get_notification_channel_settings",
    "reset_notification_channel_settings",
    # Observability Profile (524 — single backend selector)
    "ObservabilityProfile",
    "ObservabilitySettings",
    "get_observability_settings",
    "reset_observability_settings",
    # Cascade Retention
    "CascadeRetentionSettings",
    "get_cascade_retention_settings",
    "reset_cascade_retention_settings",
    # Distributed Lock
    "DistributedLockSettings",
    "get_distributed_lock_settings",
    "reset_distributed_lock_settings",
    # Week 4 LOW Settings (92_CONFIG_IMPLEMENTATION_GUIDE.md)
    # Dashboard
    "DashboardSettings",
    "get_dashboard_settings",
    "reset_dashboard_settings",
    # Batch
    "BatchSettings",
    "get_batch_settings",
    "reset_batch_settings",
    # Audit
    "AuditSettings",
    "get_audit_settings",
    "reset_audit_settings",
    # Cell Topology
    "CellTopologySettings",
    "get_cell_topology_settings",
    "reset_cell_topology_settings",
    # Celery Task
    "CeleryTaskSettings",
    "get_celery_task_settings",
    "reset_celery_task_settings",
    # API View
    "ApiViewSettings",
    "get_api_view_settings",
    "reset_api_view_settings",
    # Domain Sensitivity
    "DomainSensitivitySettings",
    "get_domain_sensitivity_settings",
    "reset_domain_sensitivity_settings",
    # Slack Channel
    "SlackChannelSettings",
    "get_slack_channel_settings",
    "reset_slack_channel_settings",
    # Audit Integrity
    "AuditIntegritySettings",
    "get_audit_integrity_settings",
    "reset_audit_integrity_settings",
    # Audit Sync
    "AuditSyncSettings",
    "get_audit_sync_settings",
    "reset_audit_sync_settings",
    # Audit Watchdog
    "AuditWatchdogSettings",
    "get_audit_watchdog_settings",
    "reset_audit_watchdog_settings",
    # Regional Recovery Policy
    "RegionalRecoveryPolicySettings",
    "get_regional_recovery_policy_settings",
    "reset_regional_recovery_policy_settings",
    # Coordination Settings (104_HARDCODED_CONFIG_COORDINATION_REFACTORING.md)
    # Recovery Tasks
    "RecoveryTasksSettings",
    "get_recovery_tasks_settings",
    "reset_recovery_tasks_settings",
    # Recovery Coordinator
    "RecoveryCoordinatorSettings",
    "get_recovery_coordinator_settings",
    "reset_recovery_coordinator_settings",
    # Deployment Environment (Critical Worker)
    "DeploymentEnvironment",
    # Core Module Settings (Runtime Feedback, Auto Rollback, Safety Bounds, etc.)
    # Runtime Feedback
    "RuntimeFeedbackSettings",
    "get_runtime_feedback_settings",
    "reset_runtime_feedback_settings",
    # Auto Rollback
    "AutoRollbackSettings",
    "get_auto_rollback_settings",
    "reset_auto_rollback_settings",
    # Safety Bounds
    "SafetyBoundsSettings",
    "ParameterBoundConfig",
    "get_safety_bounds_settings",
    "reset_safety_bounds_settings",
    # State Cache
    "StateCacheSettings",
    "get_state_cache_settings",
    "reset_state_cache_settings",
    # Apply Strategy
    "ApplyStrategySettings",
    "get_apply_strategy_settings",
    "reset_apply_strategy_settings",
    # Decision Engine
    "DecisionEngineSettings",
    "get_decision_engine_settings",
    "reset_decision_engine_settings",
    # Hash Chain (105_HARDCODED_CONFIG_AUDIT_REFACTORING.md Step 2)
    "HashChainSettings",
    "get_hash_chain_settings",
    "reset_hash_chain_settings",
    # API Rate Limit (106_HARDCODED_CONFIG_API_REFACTORING.md Step 1)
    "ApiRateLimitSettings",
    "get_api_rate_limit_settings",
    "reset_api_rate_limit_settings",
    # Daily Report Task Settings (108_HARDCODED_CONFIG_REFACTORING_PART1_CELERY_TASKS.md)
    "DailyReportSettings",
    "get_daily_report_settings",
    "reset_daily_report_settings",
    # Entitlement Settings (427_DISTRIBUTION_ENTITLEMENT.md)
    "EntitlementSettings",
    "get_entitlement_settings",
    "reset_entitlement_settings",
    # Cleanup Task Settings (108_HARDCODED_CONFIG_REFACTORING_PART1_CELERY_TASKS.md)
    "CleanupSettings",
    "get_cleanup_settings",
    "reset_cleanup_settings",
    # X-Test Resource Guard Settings (143_XTEST_RESOURCE_AWARE_INTERLOCK.md)
    "ResourceGuardSettings",
    "get_resource_guard_settings",
    "reset_resource_guard_settings",
    # Event Buffer Settings (169_SETTINGS_SCALE_LIMITS.md)
    "EventBufferSettings",
    "get_event_buffer_settings",
    "reset_event_buffer_settings",
    # Enterprise Scale Settings (169_SETTINGS_SCALE_LIMITS.md)
    "ScaleProfile",
    "ScaleSettings",
    "PROFILE_DEFAULTS",
    "get_scale_settings",
    "reset_scale_settings",
    # 207 위치통일: Backpressure (scaling/config.py → settings/backpressure.py)
    "BackpressureLevel",
    "BackpressureStrategy",
    "LEVEL_RATE_MULTIPLIERS",
    "BackpressureSettings",
    "get_backpressure_settings",
    "reset_backpressure_settings",
    # Admission Control (HTTP 유입 제어)
    "AdmissionControlSettings",
    "get_admission_control_settings",
    "reset_admission_control_settings",
    # 537: Admin Identity (forwarded-header name for PRO resolver)
    "AdminIdentitySettings",
    "get_admin_identity_settings",
    "reset_admin_identity_settings",
    # 207 위치통일: Leader Election (coordination/config.py → settings/leader_election.py)
    "LeaderElectionSettings",
    "get_leader_election_settings",
    "reset_leader_election_settings",
    # 207 위치통일: Meta Watchdog (meta/config.py → settings/meta_watchdog.py)
    "MetaWatchdogSettings",
    "get_meta_watchdog_settings",
    "reset_meta_watchdog_settings",
    # 207 위치통일: Error Budget Gate (services/error_budget_gate/config.py → settings/error_budget_gate.py)
    "ErrorBudgetGateSettings",
    "get_error_budget_gate_settings",
    "reset_error_budget_gate_settings",
    # 313: Thread Management
    "ThreadManagementSettings",
    "get_thread_management_settings",
    "reset_thread_management_settings",
    # 313: Detection
    "DetectionSettings",
    "get_detection_settings",
    "reset_detection_settings",
    # 313: Kafka Producer
    "KafkaProducerSettings",
    "get_kafka_producer_settings",
    "reset_kafka_producer_settings",
    # 337: FinOps Settings
    "FinOpsSettings",
    "get_finops_settings",
    "reset_finops_settings",
    # 338: Emergency Mode Settings
    "EmergencyModeSettings",
    "get_emergency_mode_settings",
    "reset_emergency_mode_settings",
    # 338: Saga Settings
    "SagaSettings",
    "get_saga_settings",
    "reset_saga_settings",
    # 338: Learning Settings
    "LearningSettings",
    "ThrottleSLARule",
    "get_learning_settings",
    "reset_learning_settings",
    # 339: Health Check Settings
    "HealthCheckSettings",
    "get_health_check_settings",
    "reset_health_check_settings",
    # 339: System Control Settings
    "SystemControlSettings",
    "get_system_control_settings",
    "reset_system_control_settings",
    # 340: arq Task Settings
    "ArqTaskSettings",
    "get_arq_task_settings",
    "reset_arq_task_settings",
    # 345: PostgreSQL Settings
    "PostgresSettings",
    "get_postgres_settings",
    "reset_postgres_settings",
    # 429: Framework-free SQL Settings (DB-API 2.0)
    "SQLDialect",
    "SQLSettings",
    "get_sql_settings",
    "reset_sql_settings",
    # 345: S3 Settings
    "S3Settings",
    "get_s3_settings",
    "reset_s3_settings",
    # 358: Config Drift Monitor (from config.py)
    "ConfigDriftMonitor",
    "get_config_drift_monitor",
    "reset_config_drift_monitor",
    # 358: Event Logging Config (from config.py)
    "EventLoggingConfig",
    "get_event_logging_config",
    "reset_event_logging_config",
    # 359: Settings Infrastructure (field types + validators)
    "Probability",
    "StrictProbability",
    "Percentage",
    "TinyCount",
    "SmallCount",
    "MediumCount",
    "LargeCount",
    "HugeCount",
    "ZeroableSmallCount",
    "ShortDuration",
    "MediumDuration",
    "LongDuration",
    "IntervalDuration",
    "ShortInterval",
    "BackoffMultiplier",
    "JitterFactor",
    "STANDARD_RETRY_COUNT",
    "STANDARD_BASE_DELAY",
    "STANDARD_MAX_DELAY",
    "STANDARD_BACKOFF_MULTIPLIER",
    "STANDARD_JITTER_FACTOR",
    "STANDARD_TIMEOUT_SECONDS",
    "STANDARD_CHECK_INTERVAL",
    "STANDARD_BATCH_SIZE",
    "STANDARD_POOL_SIZE",
    "warn_above",
    "warn_below",
]
