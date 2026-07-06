"""
L2 Storage and Replay Automation Configuration Serializers.

L2StorageConfig, L2StorageStatus, ShadowLog, ReplayAutomation serializers.
"""

from rest_framework import serializers

from .base import ApplyStrategyMixin


class L2StorageConfigSerializer(ApplyStrategyMixin):
    """
    Serializer for L2 Storage resilience configuration.
    """

    # Timeout settings (ms)
    redis_timeout_ms = serializers.IntegerField(
        required=False,
        min_value=10,
        max_value=1000,
        help_text="Redis adapter timeout (ms). Default: 50ms",
    )
    database_timeout_ms = serializers.IntegerField(
        required=False,
        min_value=50,
        max_value=5000,
        help_text="Database adapter timeout (ms). Default: 200ms",
    )
    fallback_timeout_ms = serializers.IntegerField(
        required=False,
        min_value=10,
        max_value=1000,
        help_text="Fallback timeout for unknown adapters (ms). Default: 100ms",
    )

    # Shadow Logging settings
    shadow_log_max_entries = serializers.IntegerField(
        required=False,
        min_value=100,
        max_value=10000,
        help_text="Maximum number of Shadow Log entries to retain. Default: 1000",
    )

    # Drift Reconciliation settings
    reconciliation_jitter_min_seconds = serializers.FloatField(
        required=False,
        min_value=0.0,
        max_value=60.0,
        help_text="Minimum Reconciliation Jitter time (seconds). Default: 0.0",
    )
    reconciliation_jitter_max_seconds = serializers.FloatField(
        required=False,
        min_value=0.0,
        max_value=60.0,
        help_text="Maximum Reconciliation Jitter time (seconds). Default: 5.0",
    )

    # Health check settings
    health_check_interval_seconds = serializers.FloatField(
        required=False,
        min_value=5.0,
        max_value=300.0,
        help_text="L2 health check interval (seconds). Default: 30.0",
    )
    health_check_timeout_ms = serializers.IntegerField(
        required=False,
        min_value=10,
        max_value=1000,
        help_text="L2 health check timeout (ms). Default: 100",
    )

    def validate(self, data):
        """Cross-field validation."""
        # Jitter min <= max validation
        jitter_min = data.get("reconciliation_jitter_min_seconds")
        jitter_max = data.get("reconciliation_jitter_max_seconds")

        if (
            jitter_min is not None
            and jitter_max is not None
            and jitter_min > jitter_max
        ):
            raise serializers.ValidationError(
                "reconciliation_jitter_min_seconds must be less than or equal to "
                "reconciliation_jitter_max_seconds."
            )

        return data


class L2StorageStatusSerializer(serializers.Serializer):
    """Serializer for L2 Storage status response."""

    l1_type = serializers.CharField(read_only=True)
    l1_count = serializers.IntegerField(read_only=True)
    l2_enabled = serializers.BooleanField(read_only=True)
    l2_type = serializers.CharField(read_only=True, allow_null=True)
    l2_adapter_type = serializers.CharField(read_only=True)
    l2_healthy = serializers.BooleanField(read_only=True)
    l2_consecutive_failures = serializers.IntegerField(read_only=True)
    l2_last_error_time = serializers.CharField(read_only=True, allow_null=True)
    sync_interval_seconds = serializers.FloatField(read_only=True)
    last_sync_time = serializers.CharField(read_only=True, allow_null=True)
    timeout_ms = serializers.FloatField(read_only=True)


class ShadowLogEntrySerializer(serializers.Serializer):
    """Serializer for Shadow Log entry."""

    service_name = serializers.CharField(read_only=True)
    intended_state = serializers.CharField(read_only=True)
    failure_time = serializers.DateTimeField(read_only=True)
    error_message = serializers.CharField(read_only=True)
    l1_state_at_failure = serializers.CharField(read_only=True)
    adapter_type = serializers.CharField(read_only=True)
    operation = serializers.CharField(read_only=True)
    synced_after_recovery = serializers.BooleanField(read_only=True)
    recovery_time = serializers.DateTimeField(read_only=True, allow_null=True)


class ShadowLogStatsSerializer(serializers.Serializer):
    """Serializer for Shadow Log statistics."""

    total_records = serializers.IntegerField(read_only=True)
    unsynced_count = serializers.IntegerField(read_only=True)
    affected_services = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
    )
    max_entries = serializers.IntegerField(read_only=True)
    oldest_record = serializers.CharField(read_only=True, allow_null=True)
    newest_record = serializers.CharField(read_only=True, allow_null=True)


class ReplayAutomationConfigSerializer(ApplyStrategyMixin):
    """
    Serializer for Replay Automation configuration.

    Manages DLQ Replay automation settings including:
    - On-recovery: event-driven replay on CB recovery
    - Traffic-aware: replay gated on traffic normalization
    - Adaptive mode for dynamic batch sizing
    - Domain priority-based replay
    """

    _config_type = "replay_automation"

    # On-Recovery Replay
    on_recovery_enabled = serializers.BooleanField(
        required=False,
        help_text="Enable event-driven replay on circuit breaker close",
    )
    on_recovery_max_items = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=500,
        help_text="Maximum items to replay on CB recovery",
    )

    # Traffic-Aware Replay
    traffic_aware_enabled = serializers.BooleanField(
        required=False,
        help_text="Enable traffic-aware replay",
    )
    traffic_aware_max_items = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=200,
        help_text="Maximum items for traffic-aware replay",
    )

    # Adaptive Mode
    adaptive_enabled = serializers.BooleanField(
        required=False,
        help_text="Enable adaptive batch sizing based on success rate",
    )
    adaptive_initial_items = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=500,
        help_text="Initial batch size for adaptive mode",
    )
    adaptive_min_items = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=50,
        help_text="Minimum batch size for adaptive mode",
    )
    adaptive_max_items = serializers.IntegerField(
        required=False,
        min_value=10,
        max_value=500,
        help_text="Maximum batch size for adaptive mode",
    )
    adaptive_failure_threshold = serializers.FloatField(
        required=False,
        min_value=0.05,
        max_value=0.5,
        help_text="Failure rate threshold to trigger batch size reduction (0.05-0.5)",
    )

    # Domain Priority Policy
    priority_enabled = serializers.BooleanField(
        required=False,
        help_text="Enable priority-based batch processing by domain",
    )
    domain_priorities = serializers.DictField(
        required=False,
        child=serializers.ChoiceField(choices=["critical", "normal", "low"]),
        help_text='Domain priority mapping. Values: "critical", "normal", "low". Example: {"payment": "critical", "notification": "low"}',
    )
    domain_max_retries = serializers.DictField(
        required=False,
        child=serializers.IntegerField(min_value=1, max_value=20),
        help_text='Domain-specific max_retries override. Example: {"payment": 10, "notification": 3}',
    )
    domain_on_circuit_close = serializers.DictField(
        required=False,
        child=serializers.BooleanField(),
        help_text='Domain-specific on-recovery trigger setting. Example: {"payment": true, "analytics": false}',
    )
    service_failure_type_map = serializers.DictField(
        required=False,
        child=serializers.ListField(child=serializers.CharField()),
        help_text=(
            "Service→failure_types mapping consulted by "
            "replay_on_circuit_close(). Example: "
            '{"payment_api": ["TIMEOUT", "CONNECTION_ERROR"]}. '
            "Empty default — on-recovery replay cannot drain DLQ without this."
        ),
    )

    def validate(self, attrs):
        """Validate + Safe Default fallback."""
        validated = super().validate(attrs)

        # adaptive_min <= adaptive_max validation
        adaptive_min = validated.get("adaptive_min_items")
        adaptive_max = validated.get("adaptive_max_items")

        if (
            adaptive_min is not None
            and adaptive_max is not None
            and adaptive_min > adaptive_max
        ):
            raise serializers.ValidationError(
                "adaptive_min_items must be less than or equal to adaptive_max_items"
            )

        # Validate domain_priorities values (already checked by ChoiceField; extra guard)
        domain_priorities = validated.get("domain_priorities", {})
        valid_priorities = {"critical", "normal", "low"}
        for domain, priority in domain_priorities.items():
            if priority not in valid_priorities:
                raise serializers.ValidationError(
                    f"Invalid priority '{priority}' for domain '{domain}'. "
                    f"Must be one of: {valid_priorities}"
                )

        return self.validate_with_safe_fallback(validated)
