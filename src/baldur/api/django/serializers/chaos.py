"""
Chaos Engineering API Serializers

Serializers for the Chaos Engineering Control API.
Provides full API control for all chaos-related settings.
"""

from rest_framework import serializers

# =============================================================================
# Configuration Serializers
# =============================================================================


class SafetyGuardConfigSerializer(serializers.Serializer):
    """Serializer for SafetyGuard configuration."""

    error_budget_min_percent = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=100,
        help_text="Minimum error budget % required to run experiments (default: 20%)",
    )
    error_budget_warning_percent = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=100,
        help_text="Error budget % that triggers warning (default: 50%)",
    )
    experiment_cooldown_minutes = serializers.IntegerField(
        required=False,
        min_value=0,
        max_value=1440,
        help_text="Minimum minutes between experiments (default: 30)",
    )
    require_healthy_system = serializers.BooleanField(
        required=False, help_text="Require system health checks to pass"
    )
    require_no_active_incidents = serializers.BooleanField(
        required=False, help_text="Require no active incidents"
    )
    require_no_deployment_freeze = serializers.BooleanField(
        required=False, help_text="Require no active deployment freeze"
    )
    fail_safe_on_error = serializers.BooleanField(
        required=False, help_text="Block experiments if safety checks fail"
    )


class BlastRadiusPolicySerializer(serializers.Serializer):
    """Serializer for BlastRadius policy configuration."""

    instance_max_concurrent = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=100,
        help_text="Max concurrent INSTANCE level experiments",
    )
    service_max_concurrent = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=20,
        help_text="Max concurrent SERVICE level experiments",
    )
    region_max_concurrent = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=5,
        help_text="Max concurrent REGION level experiments",
    )
    instance_auto_approve = serializers.BooleanField(
        required=False, help_text="Auto-approve INSTANCE level experiments"
    )
    service_auto_approve = serializers.BooleanField(
        required=False, help_text="Auto-approve SERVICE level experiments"
    )
    allowed_hours_start = serializers.IntegerField(
        required=False,
        min_value=0,
        max_value=23,
        help_text="Start hour for allowed experiment window (UTC)",
    )
    allowed_hours_end = serializers.IntegerField(
        required=False,
        min_value=0,
        max_value=23,
        help_text="End hour for allowed experiment window (UTC)",
    )
    allow_outside_window = serializers.BooleanField(
        required=False, help_text="Allow experiments outside maintenance window"
    )
    max_traffic_percent_instance = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=100,
        help_text="Max traffic % for INSTANCE level",
    )
    max_traffic_percent_service = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=100,
        help_text="Max traffic % for SERVICE level",
    )
    max_traffic_percent_region = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=100,
        help_text="Max traffic % for REGION level",
    )
    excluded_services = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="Services excluded from chaos experiments",
    )
    excluded_domains = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="Domains excluded from chaos experiments",
    )


class SchedulerConfigSerializer(serializers.Serializer):
    """Serializer for ChaosScheduler configuration."""

    enabled = serializers.BooleanField(
        required=False, help_text="Enable/disable the scheduler"
    )
    default_schedule_hour_start = serializers.IntegerField(
        required=False,
        min_value=0,
        max_value=23,
        help_text="Default start hour for scheduled experiments",
    )
    default_schedule_hour_end = serializers.IntegerField(
        required=False,
        min_value=0,
        max_value=23,
        help_text="Default end hour for scheduled experiments",
    )
    auto_approve_instance_level = serializers.BooleanField(
        required=False, help_text="Auto-approve INSTANCE level experiments"
    )
    auto_approve_service_level = serializers.BooleanField(
        required=False, help_text="Auto-approve SERVICE level experiments"
    )
    max_concurrent_experiments = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=10,
        help_text="Max concurrent experiments",
    )
    max_experiments_per_day = serializers.IntegerField(
        required=False, min_value=1, max_value=100, help_text="Max experiments per day"
    )
    min_interval_between_experiments_minutes = serializers.IntegerField(
        required=False,
        min_value=0,
        max_value=1440,
        help_text="Min minutes between experiments",
    )


class ReportConfigSerializer(serializers.Serializer):
    """Serializer for ReportGenerator configuration."""

    grade_a_min_pass_rate = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=100,
        help_text="Min pass rate for grade A",
    )
    grade_b_min_pass_rate = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=100,
        help_text="Min pass rate for grade B",
    )
    grade_c_min_pass_rate = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=100,
        help_text="Min pass rate for grade C",
    )
    grade_d_min_pass_rate = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=100,
        help_text="Min pass rate for grade D",
    )
    acceptable_recovery_time_seconds = serializers.FloatField(
        required=False, min_value=0, help_text="Acceptable recovery time in seconds"
    )
    warning_recovery_time_seconds = serializers.FloatField(
        required=False, min_value=0, help_text="Warning recovery time in seconds"
    )
    notify_on_grade_drop = serializers.BooleanField(
        required=False, help_text="Notify when grade drops"
    )
    notify_on_critical_grade = serializers.BooleanField(
        required=False, help_text="Notify on critical grade (F)"
    )


# =============================================================================
# Experiment Serializers
# =============================================================================


class ExperimentConfigSerializer(serializers.Serializer):
    """Serializer for experiment configuration."""

    target_service = serializers.CharField(
        required=True, help_text="Target service name"
    )
    target_domain = serializers.CharField(
        required=False, default="", help_text="Target domain"
    )
    target_instances = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
        help_text="Specific instances to target",
    )
    injection_rate = serializers.FloatField(
        required=False,
        min_value=0.0001,
        max_value=1.0,
        default=0.001,
        help_text="Injection rate (0.001 = 0.1%)",
    )
    duration_seconds = serializers.IntegerField(
        required=False,
        min_value=10,
        max_value=3600,
        default=300,
        help_text="Experiment duration in seconds",
    )
    traffic_type = serializers.ChoiceField(
        choices=["synthetic", "shadow", "canary", "production"],
        required=False,
        default="synthetic",
        help_text="Type of traffic to target",
    )
    auto_rollback_on_sla_breach = serializers.BooleanField(
        required=False, default=True, help_text="Auto-rollback on SLA breach"
    )
    parameters = serializers.DictField(
        required=False, default=dict, help_text="Experiment-specific parameters"
    )


class ScheduledExperimentSerializer(serializers.Serializer):
    """Serializer for creating scheduled experiments."""

    experiment_type = serializers.ChoiceField(
        choices=[
            "latency_injection",
            "error_5xx",
            "packet_loss",
            "timeout",
            "resource_exhaustion",
        ],
        required=True,
        help_text="Type of experiment",
    )
    target_service = serializers.CharField(required=True, help_text="Target service")
    target_domain = serializers.CharField(
        required=False, default="", help_text="Target domain"
    )
    blast_radius = serializers.ChoiceField(
        choices=["instance", "service", "region"],
        required=False,
        default="instance",
        help_text="Blast radius level",
    )
    schedule_type = serializers.ChoiceField(
        choices=["once", "daily", "weekly", "cron"],
        required=False,
        default="daily",
        help_text="Schedule type",
    )
    schedule_time = serializers.RegexField(
        regex=r"^\d{2}:\d{2}$",
        required=False,
        default="02:00",
        help_text="Time in HH:MM format (UTC)",
    )
    schedule_day = serializers.IntegerField(
        required=False,
        min_value=0,
        max_value=6,
        default=0,
        help_text="Day for weekly schedule (0=Monday)",
    )
    schedule_cron = serializers.CharField(
        required=False, default="", help_text="Cron expression for custom schedule"
    )
    experiment_config = ExperimentConfigSerializer(
        required=False, help_text="Experiment configuration"
    )
    description = serializers.CharField(
        required=False,
        default="",
        max_length=500,
        help_text="Human-readable description",
    )
    tags = serializers.ListField(
        child=serializers.CharField(max_length=50),
        required=False,
        default=list,
        help_text="Tags for filtering",
    )


class ScheduledExperimentResponseSerializer(serializers.Serializer):
    """Response serializer for scheduled experiments."""

    id = serializers.CharField()
    experiment_type = serializers.CharField()
    experiment_config = serializers.DictField()
    target_service = serializers.CharField()
    target_domain = serializers.CharField()
    blast_radius = serializers.CharField()
    schedule_type = serializers.CharField()
    schedule_time = serializers.CharField()
    schedule_day = serializers.IntegerField()
    schedule_cron = serializers.CharField()
    approval_status = serializers.CharField()
    approved_by = serializers.CharField()
    approved_at = serializers.CharField()
    enabled = serializers.BooleanField()
    last_run_at = serializers.CharField()
    last_run_result = serializers.CharField()
    next_run_at = serializers.CharField()
    run_count = serializers.IntegerField()
    created_by = serializers.CharField()
    created_at = serializers.CharField()
    description = serializers.CharField()
    tags = serializers.ListField(child=serializers.CharField())


# =============================================================================
# Approval Serializers
# =============================================================================


class ApprovalRequestSerializer(serializers.Serializer):
    """Serializer for approval requests."""

    experiment_id = serializers.CharField()
    blast_radius = serializers.CharField()
    target_service = serializers.CharField()
    target_domain = serializers.CharField()
    requested_by = serializers.CharField()
    requested_at = serializers.CharField()
    reason = serializers.CharField()
    status = serializers.CharField()
    approved_by = serializers.CharField()
    approved_at = serializers.CharField()
    denial_reason = serializers.CharField()
    expires_at = serializers.CharField()


class ApprovalActionSerializer(serializers.Serializer):
    """Serializer for approval actions."""

    action = serializers.ChoiceField(
        choices=["approve", "deny"], required=True, help_text="Approval action"
    )
    reason = serializers.CharField(
        required=False, default="", max_length=500, help_text="Reason for denial"
    )


# =============================================================================
# Report Serializers
# =============================================================================


class ExperimentSummarySerializer(serializers.Serializer):
    """Serializer for experiment summary in reports."""

    experiment_id = serializers.CharField()
    experiment_type = serializers.CharField()
    target_service = serializers.CharField()
    outcome = serializers.CharField()
    status = serializers.CharField()
    started_at = serializers.CharField()
    duration_seconds = serializers.FloatField()
    recovery_time_seconds = serializers.FloatField()
    errors_injected = serializers.IntegerField()
    sla_breaches = serializers.IntegerField()
    steady_state_passed = serializers.BooleanField()
    forensic_recommendations = serializers.ListField(child=serializers.CharField())


class DailyResilienceReportSerializer(serializers.Serializer):
    """Serializer for daily resilience reports."""

    report_id = serializers.CharField()
    report_date = serializers.CharField()
    generated_at = serializers.CharField()
    grade = serializers.CharField()
    grade_explanation = serializers.CharField()
    total_experiments = serializers.IntegerField()
    passed_experiments = serializers.IntegerField()
    failed_experiments = serializers.IntegerField()
    skipped_experiments = serializers.IntegerField()
    total_sla_breaches = serializers.IntegerField()
    average_recovery_time_seconds = serializers.FloatField()
    max_recovery_time_seconds = serializers.FloatField()
    error_budget_consumed_percent = serializers.FloatField()
    error_budget_remaining_percent = serializers.FloatField()
    experiments = ExperimentSummarySerializer(many=True)
    grade_trend = serializers.CharField()
    week_over_week_change = serializers.FloatField()
    recommendations = serializers.ListField(child=serializers.CharField())
    action_items = serializers.ListField(child=serializers.DictField())
    forensic_summary = serializers.DictField()


# =============================================================================
# Kill Switch Serializers
# =============================================================================


class KillSwitchSerializer(serializers.Serializer):
    """Serializer for kill switch actions."""

    action = serializers.ChoiceField(
        choices=["kill_one", "kill_all", "block_global", "unblock_global"],
        required=True,
        help_text="Kill switch action",
    )
    experiment_id = serializers.CharField(
        required=False, help_text="Experiment ID for kill_one action"
    )
    reason = serializers.CharField(
        required=False,
        default="Manual kill switch activation",
        max_length=500,
        help_text="Reason for kill switch activation",
    )

    def validate(self, data):
        if data["action"] == "kill_one" and not data.get("experiment_id"):
            raise serializers.ValidationError(
                "experiment_id is required for kill_one action"
            )
        return data


# =============================================================================
# Safety Check Serializers
# =============================================================================


class SafetyCheckResultSerializer(serializers.Serializer):
    """Serializer for safety check results."""

    status = serializers.CharField()
    allowed = serializers.BooleanField()
    checks_performed = serializers.ListField(child=serializers.CharField())
    checks_passed = serializers.ListField(child=serializers.CharField())
    checks_failed = serializers.ListField(child=serializers.CharField())
    warnings = serializers.ListField(child=serializers.CharField())
    block_reason = serializers.CharField()
    block_message = serializers.CharField()
    error_budget_remaining_percent = serializers.FloatField()
    error_budget_threshold = serializers.FloatField()
    system_healthy = serializers.BooleanField()
    active_incidents = serializers.IntegerField()
    deployment_freeze_active = serializers.BooleanField()
    kill_switch_active = serializers.BooleanField()
    last_experiment_at = serializers.CharField()
    cooldown_remaining_minutes = serializers.IntegerField()
    checked_at = serializers.CharField()


# =============================================================================
# Blast Radius Check Serializers
# =============================================================================


class BlastRadiusCheckRequestSerializer(serializers.Serializer):
    """Request serializer for blast radius check."""

    blast_radius = serializers.ChoiceField(
        choices=["instance", "service", "region"],
        required=True,
        help_text="Blast radius level",
    )
    target_service = serializers.CharField(required=True, help_text="Target service")
    target_domain = serializers.CharField(
        required=False, default="", help_text="Target domain"
    )
    experiment_id = serializers.CharField(
        required=False, default="", help_text="Experiment ID for approval lookup"
    )
    traffic_percent = serializers.FloatField(
        required=False,
        default=100.0,
        min_value=0,
        max_value=100,
        help_text="Percentage of traffic to affect",
    )


class BlastRadiusCheckResultSerializer(serializers.Serializer):
    """Response serializer for blast radius check."""

    allowed = serializers.BooleanField()
    blast_radius = serializers.CharField()
    requires_approval = serializers.BooleanField()
    approval_status = serializers.CharField()
    violations = serializers.ListField(child=serializers.CharField())
    max_traffic_percent = serializers.FloatField()
    max_concurrent = serializers.IntegerField()
    current_concurrent = serializers.IntegerField()
    within_allowed_window = serializers.BooleanField()


# =============================================================================
# Safety Mechanism Serializers (TTL, Stop Conditions, Dry Run)
# =============================================================================


class TTLConfigSerializer(serializers.Serializer):
    """Serializer for TTL (Self-Expiration) configuration."""

    default_ttl_seconds = serializers.IntegerField(
        required=False,
        min_value=60,
        max_value=3600,
        default=600,
        help_text="Default TTL (seconds, Default: 600=10min). Time after which experiment auto-expires.",
    )
    min_ttl_seconds = serializers.IntegerField(
        required=False,
        min_value=30,
        max_value=600,
        default=60,
        help_text="Minimum TTL (seconds). TTL shorter than this value is not allowed.",
    )
    max_ttl_seconds = serializers.IntegerField(
        required=False,
        min_value=600,
        max_value=7200,
        default=3600,
        help_text="Maximum TTL (seconds, Default: 3600=1hour). TTL longer than this value is not allowed.",
    )
    auto_expiration_enabled = serializers.BooleanField(
        required=False,
        default=True,
        help_text="Whether to enable auto-expiration. If True, chaos config is automatically removed after TTL.",
    )


class StopConditionsConfigSerializer(serializers.Serializer):
    """Serializer for Stop Conditions (automatic abort) configuration."""

    max_error_rate_percent = serializers.FloatField(
        required=False,
        min_value=0.1,
        max_value=100,
        default=5.0,
        help_text="Maximum allowed error rate (%). Experiment auto-stops when exceeded.",
    )
    max_latency_p99_ms = serializers.IntegerField(
        required=False,
        min_value=100,
        max_value=30000,
        default=2000,
        help_text="Maximum allowed P99 latency (ms). Experiment auto-stops when exceeded.",
    )
    max_latency_p95_ms = serializers.IntegerField(
        required=False,
        min_value=50,
        max_value=20000,
        default=1000,
        help_text="Maximum allowed P95 latency (ms). Experiment auto-stops when exceeded.",
    )
    min_error_budget_percent = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=100,
        default=10.0,
        help_text="Minimum error budget (%). Experiment auto-stops when budget falls below this value.",
    )
    check_interval_seconds = serializers.IntegerField(
        required=False,
        min_value=5,
        max_value=60,
        default=10,
        help_text="Metric check interval (seconds).",
    )
    consecutive_breaches_required = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=10,
        default=2,
        help_text="Consecutive breach count. Stops only after N consecutive breaches to ignore transient spikes.",
    )
    enabled = serializers.BooleanField(
        required=False, default=True, help_text="Whether to enable Stop Conditions."
    )


class DryRunConfigSerializer(serializers.Serializer):
    """Serializer for Dry Run configuration."""

    enabled = serializers.BooleanField(
        required=False,
        default=True,
        help_text="Enable Dry Run mode. If True, only simulation is performed without actual fault injection.",
    )
    reason = serializers.CharField(
        required=False,
        max_length=500,
        default="Initial deployment - simulation mode",
        help_text="Reason for enabling Dry Run mode.",
    )


class KillAllRequestSerializer(serializers.Serializer):
    """Request serializer for kill all operation."""

    reason = serializers.CharField(
        required=True, max_length=500, help_text="Reason for stopping all experiments"
    )
    operator = serializers.CharField(
        required=False, max_length=100, help_text="Operator email or ID"
    )


class KillAllResponseSerializer(serializers.Serializer):
    """Response serializer for kill all operation."""

    status = serializers.CharField()
    experiments_killed = serializers.IntegerField()
    rollbacks_initiated = serializers.IntegerField()
    ttl_configs_cleared = serializers.IntegerField()


class ExperimentConfigWithTTLSerializer(serializers.Serializer):
    """Serializer for experiment configuration with TTL support."""

    target_service = serializers.CharField(
        required=True, help_text="Target service name"
    )
    target_domain = serializers.CharField(
        required=False, default="", help_text="Target domain"
    )
    target_instances = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
        help_text="Specific instances to target",
    )
    injection_rate = serializers.FloatField(
        required=False,
        min_value=0.0001,
        max_value=1.0,
        default=0.001,
        help_text="Injection rate (0.001 = 0.1%)",
    )
    duration_seconds = serializers.IntegerField(
        required=False,
        min_value=10,
        max_value=3600,
        default=300,
        help_text="Experiment duration in seconds",
    )
    traffic_type = serializers.ChoiceField(
        choices=["synthetic", "shadow", "canary", "production"],
        required=False,
        default="synthetic",
        help_text="Type of traffic to target",
    )
    auto_rollback_on_sla_breach = serializers.BooleanField(
        required=False, default=True, help_text="Auto-rollback on SLA breach"
    )
    parameters = serializers.DictField(
        required=False, default=dict, help_text="Experiment-specific parameters"
    )
    # TTL configuration
    ttl_seconds = serializers.IntegerField(
        required=False,
        min_value=60,
        max_value=3600,
        help_text="Experiment auto-expiration time (seconds). Uses default if None.",
    )
    # Dry Run mode
    dry_run = serializers.BooleanField(
        required=False,
        default=False,
        help_text="If True, only simulation is performed without actual fault injection.",
    )


# =============================================================================
# Impact Prediction Serializers
# =============================================================================


class DryRunAnalysisRequestSerializer(serializers.Serializer):
    """Request serializer for Dry Run analysis with prediction."""

    target_service = serializers.CharField(
        required=True, help_text="Target service name"
    )
    experiment_type = serializers.ChoiceField(
        choices=[
            "latency_injection",
            "failure_injection",
            "resource_exhaustion",
            "network_partition",
        ],
        required=True,
        help_text="Type of chaos experiment",
    )
    config = serializers.DictField(
        required=False,
        default=dict,
        help_text="Experiment-specific configuration (e.g., latency_ms, failure_rate)",
    )
    include_blast_radius = serializers.BooleanField(
        required=False, default=True, help_text="Include blast radius analysis"
    )


class ServiceImpactSerializer(serializers.Serializer):
    """Serializer for service impact prediction."""

    service_name = serializers.CharField()
    impact_level = serializers.CharField()
    predicted_latency_increase_ms = serializers.FloatField()
    predicted_error_rate_percent = serializers.FloatField()
    predicted_availability_drop_percent = serializers.FloatField()
    is_direct_target = serializers.BooleanField()
    dependency_chain = serializers.ListField(
        child=serializers.CharField(), required=False
    )


class PredictedOutcomeSerializer(serializers.Serializer):
    """Serializer for predicted outcome."""

    predicted_cb_state = serializers.CharField()
    predicted_recovery_time_seconds = serializers.FloatField()
    predicted_error_rate_increase_percent = serializers.FloatField()
    confidence_score = serializers.FloatField()
    patterns_used = serializers.IntegerField()
    similar_experiment_ids = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    recommendations = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    requires_approval = serializers.BooleanField()
    approval_reason = serializers.CharField(allow_blank=True)


class DependencyNodeSerializer(serializers.Serializer):
    """Serializer for dependency graph node."""

    service_name = serializers.CharField()
    depth = serializers.IntegerField()
    is_critical = serializers.BooleanField()
    dependency_type = serializers.CharField()
    impact_score = serializers.FloatField()


class BlastRadiusAnalysisResultSerializer(serializers.Serializer):
    """Serializer for blast radius analysis result."""

    target_service = serializers.CharField()
    experiment_type = serializers.CharField()
    level = serializers.CharField()
    affected_services = DependencyNodeSerializer(many=True)
    total_affected_count = serializers.IntegerField()
    includes_critical_services = serializers.BooleanField()
    critical_services = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    risk_score = serializers.FloatField()
    recommendations = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    requires_approval = serializers.BooleanField()
    approval_level = serializers.CharField(allow_blank=True)
    experiment_allowed = serializers.BooleanField()
    blocking_reasons = serializers.ListField(
        child=serializers.CharField(), required=False
    )


class DryRunAnalysisResponseSerializer(serializers.Serializer):
    """Response serializer for Dry Run analysis."""

    target_service = serializers.CharField()
    experiment_type = serializers.CharField()
    predicted_outcome = PredictedOutcomeSerializer()
    blast_radius_analysis = BlastRadiusAnalysisResultSerializer(required=False)
    service_impacts = ServiceImpactSerializer(many=True, required=False)
    experiment_allowed = serializers.BooleanField()
    requires_approval = serializers.BooleanField()
    approval_level = serializers.CharField(allow_blank=True)
    overall_risk_level = serializers.CharField()  # "low", "medium", "high", "critical"
