"""
Metric Sync API Serializers.

Serializers for the poll-removal + manual API.

POST /api/baldur/metrics/sync/ - manual metric sync
GET /api/baldur/metrics/drift-report/ - query drift status
"""

from rest_framework import serializers

# =============================================================================
# Request Serializers
# =============================================================================


class MetricSyncRequestSerializer(serializers.Serializer):
    """
    Request serializer for POST /api/baldur/metrics/sync/.

    Used when an operator manually triggers a metric sync.
    """

    domains = serializers.ListField(
        child=serializers.CharField(max_length=50),
        required=False,
        allow_empty=True,
        help_text="List of domains to sync. All domains if not specified.",
    )

    dry_run = serializers.BooleanField(
        required=False,
        default=False,
        help_text="If True, only generates a report without performing actual sync.",
    )

    reason = serializers.CharField(
        max_length=500,
        required=False,
        allow_blank=True,
        help_text="Reason for sync (for audit logging).",
    )


# =============================================================================
# Response Serializers
# =============================================================================


class DriftDetailSerializer(serializers.Serializer):
    """Drift details for a single metric."""

    before = serializers.FloatField(help_text="In-memory value before sync")
    after = serializers.FloatField(help_text="Actual value after sync")
    drift = serializers.FloatField(help_text="Difference (after - before)")


class DomainSyncResultSerializer(serializers.Serializer):
    """Per-domain sync result."""

    dlq_pending = DriftDetailSerializer(required=False)
    circuit_breaker_state = serializers.DictField(required=False)
    retry_success_rate = DriftDetailSerializer(required=False)


class SyncSummarySerializer(serializers.Serializer):
    """Sync summary information."""

    total_drifts_detected = serializers.IntegerField(
        help_text="Total number of drifts detected"
    )
    total_drifts_corrected = serializers.IntegerField(
        help_text="Total number of drifts corrected"
    )
    max_drift_percent = serializers.FloatField(
        required=False, help_text="Maximum drift percentage"
    )


class MetricSyncResponseSerializer(serializers.Serializer):
    """
    Response serializer for POST /api/baldur/metrics/sync/.
    """

    status = serializers.ChoiceField(
        choices=["completed", "dry_run", "partial", "failed"],
        help_text="Sync status",
    )
    synced_at = serializers.DateTimeField(help_text="Sync timestamp (ISO 8601)")
    actor = serializers.CharField(help_text="Sync performer (username)")
    dry_run = serializers.BooleanField(help_text="Whether dry run mode is enabled")
    results = serializers.DictField(
        child=DomainSyncResultSerializer(),
        help_text="Per-domain sync results",
    )
    summary = SyncSummarySerializer(help_text="Sync summary")


# =============================================================================
# Drift Report Serializers
# =============================================================================


class MetricDriftItemSerializer(serializers.Serializer):
    """Drift information for an individual metric."""

    in_memory = serializers.FloatField(help_text="Current in-memory (Gauge) value")
    actual = serializers.FloatField(help_text="Actual value retrieved from DB")
    drift = serializers.FloatField(help_text="Difference (actual - in_memory)")
    drift_percent = serializers.FloatField(required=False, help_text="Drift percentage")
    is_critical = serializers.BooleanField(
        help_text="Whether the threshold is exceeded"
    )


class DriftReportMetricsSerializer(serializers.Serializer):
    """Drift information per metric type."""

    dlq_pending_count = serializers.DictField(
        child=MetricDriftItemSerializer(),
        required=False,
        help_text="DLQ pending count drift per domain",
    )
    circuit_breaker_state = serializers.DictField(
        child=MetricDriftItemSerializer(),
        required=False,
        help_text="Circuit Breaker state drift per service",
    )
    retry_success_rate = serializers.DictField(
        child=MetricDriftItemSerializer(),
        required=False,
        help_text="Retry success rate drift per domain",
    )


class DriftReportResponseSerializer(serializers.Serializer):
    """
    Response serializer for GET /api/baldur/metrics/drift-report/.

    Queries the current drift status (read-only).
    It reads from the DB but does not change any Gauge values.
    """

    generated_at = serializers.DateTimeField(
        help_text="Report generation timestamp (ISO 8601)"
    )
    metrics = DriftReportMetricsSerializer(
        help_text="Drift information per metric type"
    )
    overall_health = serializers.ChoiceField(
        choices=["healthy", "warning", "critical", "incident"],
        help_text="Overall health status",
    )
    max_drift_percent = serializers.FloatField(help_text="Maximum drift percentage")
    recommendation = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Recommended action",
    )


__all__ = [
    # Request
    "MetricSyncRequestSerializer",
    # Response
    "MetricSyncResponseSerializer",
    "DriftReportResponseSerializer",
    # Nested
    "DriftDetailSerializer",
    "DomainSyncResultSerializer",
    "SyncSummarySerializer",
    "MetricDriftItemSerializer",
    "DriftReportMetricsSerializer",
]
