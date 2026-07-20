"""
Baldur Control API Serializers.

Provides request/response serializers for the Baldur Control API.
These are Django REST Framework serializers for the REST API endpoints.
"""

from rest_framework import serializers

# =============================================================================
# Constants - single source is core/constants.py (Item 31-33 dedup)
# =============================================================================
from baldur.core.constants import (  # noqa: E402
    ControlAPIActions,
    ControlAPIEnvironments,
)

# =============================================================================
# Request Serializers
# =============================================================================


class ControlRequestSerializer(serializers.Serializer):
    """
    Control API Request Serializer.

    Validates incoming control requests.
    """

    service_name = serializers.CharField(
        max_length=100,
        help_text="Target service or module (e.g., 'payment', 'inventory')",
    )
    action = serializers.ChoiceField(
        choices=ControlAPIActions.CHOICES,
        help_text="Action to execute: allow, block, override, reset, inject_failure, inject_success",
    )
    reason = serializers.CharField(
        max_length=500,
        help_text="Business or technical justification (required for all actions)",
    )
    environment = serializers.ChoiceField(
        choices=ControlAPIEnvironments.CHOICES,
        help_text="Execution environment: test, chaos, ops",
    )
    ttl_minutes = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=1440,  # 24 hours max
        help_text="Time-to-live in minutes. Required for override in ops (max 60)",
    )
    request_id = serializers.UUIDField(
        required=False,
        help_text="Correlates retries and auditing. Auto-generated if not provided",
    )
    metadata = serializers.DictField(
        required=False,
        help_text="Chaos/test parameters (e.g., simulate_latency_ms)",
    )

    def validate(self, data):
        """
        Cross-field validation for control requests.

        Rules:
        - inject_failure is FORBIDDEN in ops environment
        - override in ops requires TTL (max 60 minutes)
        """
        action = data.get("action")
        environment = data.get("environment")
        ttl_minutes = data.get("ttl_minutes")

        # Rule 1: inject_failure forbidden in ops
        if (
            action == ControlAPIActions.INJECT_FAILURE
            and environment == ControlAPIEnvironments.OPS
        ):
            raise serializers.ValidationError(
                {
                    "action": "inject_failure is FORBIDDEN in ops environment",
                    "error_code": "ACTION_FORBIDDEN_IN_ENVIRONMENT",
                }
            )

        # Rule 2: override in ops requires TTL (max 60 minutes)
        if (
            action == ControlAPIActions.OVERRIDE
            and environment == ControlAPIEnvironments.OPS
        ):
            if not ttl_minutes:
                raise serializers.ValidationError(
                    {
                        "ttl_minutes": "TTL is required for override action in ops environment",
                        "error_code": "TTL_REQUIRED_FOR_OPS_OVERRIDE",
                    }
                )
            if ttl_minutes > 60:
                raise serializers.ValidationError(
                    {
                        "ttl_minutes": f"TTL cannot exceed 60 minutes in ops environment (got: {ttl_minutes})",
                        "error_code": "TTL_EXCEEDS_OPS_LIMIT",
                    }
                )

        return data


class ComplianceRunSerializer(serializers.Serializer):
    """Request serializer for compliance run operations."""

    domain = serializers.CharField(
        required=False,
        max_length=50,
        help_text="Domain partition for exemption evaluation (e.g., 'internal_admin')",
    )


class DLQReplayRequestSerializer(serializers.Serializer):
    """Request serializer for DLQ replay operations."""

    domain = serializers.CharField(
        required=False,
        max_length=50,
        help_text="Domain to replay (payment, point, inventory)",
    )
    service_name = serializers.CharField(
        required=False,
        max_length=100,
        help_text="Service name filter",
    )
    batch_size = serializers.IntegerField(
        required=False,
        default=50,
        min_value=1,
        max_value=200,
        help_text="Maximum items to replay (default: 50, max: 200)",
    )
    status = serializers.CharField(
        required=False,
        default="pending",
        help_text="DLQ item status to replay (default: pending)",
    )


# =============================================================================
# Response Serializers
# =============================================================================


class EvidenceSerializer(serializers.Serializer):
    """Evidence metadata in response."""

    recent_latency_avg_ms = serializers.FloatField(required=False)
    error_rate = serializers.FloatField(required=False)
    failure_count = serializers.IntegerField(required=False)
    success_count = serializers.IntegerField(required=False)
    last_failure_at = serializers.DateTimeField(required=False, allow_null=True)


class ControlResponseSerializer(serializers.Serializer):
    """Control API Response Serializer."""

    status = serializers.ChoiceField(
        choices=[
            ("success", "Action completed successfully"),
            ("rejected", "Action rejected due to policy violation"),
            ("error", "Internal error occurred"),
        ]
    )
    action_applied = serializers.CharField(help_text="The action that was applied")
    system_state = serializers.ChoiceField(
        choices=[
            ("allow", "Operations allowed (CB CLOSED)"),
            ("block", "Operations blocked (CB OPEN)"),
            ("half_open", "Limited operations allowed"),
        ],
        required=False,
    )
    effective_until = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="When the override/block expires",
    )
    reason_classification = serializers.CharField(
        required=False,
        help_text="AI/system assigned classification",
    )
    evidence = EvidenceSerializer(required=False)
    correlation_id = serializers.UUIDField(
        required=False,
        help_text="AI-assisted correlation for retry/replay",
    )
    error_code = serializers.CharField(required=False)
    error_message = serializers.CharField(required=False)


class ControlErrorResponseSerializer(serializers.Serializer):
    """Error response for Control API."""

    status = serializers.CharField(default="rejected")
    error_code = serializers.CharField()
    error_message = serializers.CharField()
    action_requested = serializers.CharField()
    environment = serializers.CharField()


# =============================================================================
# Status & List Serializers
# =============================================================================


class ServiceStateSerializer(serializers.Serializer):
    """Single service state in status response."""

    service_name = serializers.CharField()
    state = serializers.CharField()
    failure_count = serializers.IntegerField()
    success_count = serializers.IntegerField()
    last_failure_at = serializers.DateTimeField(allow_null=True)
    opened_at = serializers.DateTimeField(allow_null=True)
    manually_controlled = serializers.BooleanField()
    controlled_by = serializers.CharField(allow_null=True)
    control_reason = serializers.CharField(allow_null=True)
    expires_at = serializers.DateTimeField(allow_null=True)


class ControlStatusResponseSerializer(serializers.Serializer):
    """Response for GET /control/status endpoint."""

    services = ServiceStateSerializer(many=True)
    environment = serializers.CharField()
    timestamp = serializers.DateTimeField()


class AuditLogSerializer(serializers.Serializer):
    """Audit log entry serializer."""

    id = serializers.IntegerField()
    action = serializers.CharField()
    service_name = serializers.CharField()
    environment = serializers.CharField()
    reason = serializers.CharField()
    actor = serializers.CharField()
    timestamp = serializers.DateTimeField()
    status = serializers.CharField()
    evidence = serializers.DictField(required=False)
    risk_level = serializers.CharField()


class AuditLogListResponseSerializer(serializers.Serializer):
    """Response for GET /control/audit endpoint."""

    logs = AuditLogSerializer(many=True)
    total_count = serializers.IntegerField()
    page = serializers.IntegerField()
    page_size = serializers.IntegerField()


# =============================================================================
# Metrics Serializers
# =============================================================================


class ServiceMetricsSerializer(serializers.Serializer):
    """Per-service metrics for trends analysis."""

    service_name = serializers.CharField(
        help_text="Service identifier (e.g., 'payment', 'inventory')",
    )
    failure_rate_5m = serializers.FloatField(
        help_text="Failure rate in last 5 minutes (0.0 - 1.0)",
    )
    retry_success_rate = serializers.FloatField(
        help_text="Retry success rate (0.0 - 100.0)",
    )
    dlq_count = serializers.IntegerField(
        help_text="Current DLQ pending count for this service",
    )
    circuit_state = serializers.CharField(
        help_text="Current circuit breaker state (closed, open, half_open)",
    )
    avg_recovery_time_seconds = serializers.FloatField(
        help_text="Average time to recovery in seconds",
        allow_null=True,
    )


class MetricsResponseSerializer(serializers.Serializer):
    """Response for GET /api/baldur/metrics/ endpoint."""

    # Aggregate metrics
    total_services = serializers.IntegerField(
        help_text="Total number of tracked services",
    )
    healthy_services = serializers.IntegerField(
        help_text="Number of services with closed circuit breakers",
    )
    degraded_services = serializers.IntegerField(
        help_text="Number of services with open/half-open circuit breakers",
    )

    # Trend metrics
    last_5m_failure_rate = serializers.FloatField(
        help_text="Overall failure rate in last 5 minutes (0.0 - 1.0)",
    )
    last_5m_request_count = serializers.IntegerField(
        help_text="Total requests processed in last 5 minutes",
    )

    # Recovery metrics
    avg_time_to_recovery = serializers.FloatField(
        help_text="Average time to recovery in seconds (last 24h)",
        allow_null=True,
    )

    # Automation metrics
    auto_allowed_count_24h = serializers.IntegerField(
        help_text="Auto-recovery (allow) count in last 24 hours",
    )
    auto_blocked_count_24h = serializers.IntegerField(
        help_text="Auto-protection (block) count in last 24 hours",
    )

    # DLQ overview
    total_dlq_pending = serializers.IntegerField(
        help_text="Total pending items across all DLQ domains",
    )
    dlq_by_service = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="DLQ pending count by service/domain",
    )

    # Per-service breakdown
    services = ServiceMetricsSerializer(
        many=True,
        help_text="Detailed metrics per service",
    )

    # Metadata
    timestamp = serializers.DateTimeField(
        help_text="When metrics were collected",
    )
    collection_duration_ms = serializers.IntegerField(
        help_text="Time taken to collect metrics in milliseconds",
    )


class HealthCheckResponseSerializer(serializers.Serializer):
    """Health check response."""

    status = serializers.CharField()
    circuit_breaker_enabled = serializers.BooleanField()
    services_count = serializers.IntegerField()
    timestamp = serializers.DateTimeField()


class DLQReplayResponseSerializer(serializers.Serializer):
    """DLQ replay response."""

    status = serializers.CharField()
    total = serializers.IntegerField()
    success_count = serializers.IntegerField()
    failed_count = serializers.IntegerField()
    skipped_count = serializers.IntegerField()
