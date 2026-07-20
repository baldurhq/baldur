"""
SLA/SLO Configuration Serializers.

SLA, SLO Definition, SLO Config, ErrorBudget serializers.

Adds Fail-Safe Default hardening.
"""

from rest_framework import serializers

from .base import ApplyStrategyMixin


class SLAConfigSerializer(ApplyStrategyMixin):
    """
    Serializer for SLA configuration.

    Applies the Safe Default fallback.
    """

    _config_type = "sla"

    default_hours = serializers.IntegerField(required=False, min_value=1, max_value=720)
    thresholds_by_domain = serializers.DictField(
        required=False,
        child=serializers.IntegerField(min_value=1, max_value=720),
    )

    def validate(self, attrs):
        """Validate + Safe Default fallback."""
        validated = super().validate(attrs)
        return self.validate_with_safe_fallback(validated)


class SLODefinitionSerializer(serializers.Serializer):
    """
    Serializer for a single SLO definition.

    Used when creating/updating an SLO through the API.
    """

    name = serializers.CharField(
        max_length=100,
        help_text="SLO name (e.g., api_availability, checkout_latency)",
    )
    sli_type = serializers.ChoiceField(
        required=False,
        choices=[
            "availability",
            "latency_p99",
            "latency_p95",
            "latency_p50",
            "error_rate",
            "throughput",
        ],
        help_text="SLI type",
    )
    target = serializers.FloatField(
        required=False,
        min_value=0.0,
        max_value=1.0,
        help_text="Target value (0.0-1.0, e.g., 0.999 = 99.9%)",
    )
    window_days = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=365,
        help_text="Measurement window (days)",
    )
    description = serializers.CharField(
        required=False,
        max_length=500,
        allow_blank=True,
        help_text="SLO description",
    )
    service_name = serializers.CharField(
        required=False,
        max_length=100,
        allow_blank=True,
        help_text="Service name",
    )
    domain = serializers.CharField(
        required=False,
        max_length=100,
        allow_blank=True,
        help_text="Domain (e.g., payment, order)",
    )
    warning_threshold = serializers.FloatField(
        required=False,
        min_value=0.0,
        max_value=1.0,
        allow_null=True,
        help_text="Warning threshold (0.0-1.0)",
    )
    critical_threshold = serializers.FloatField(
        required=False,
        min_value=0.0,
        max_value=1.0,
        allow_null=True,
        help_text="Critical threshold (0.0-1.0)",
    )
    fast_burn_rate = serializers.FloatField(
        required=False,
        min_value=1.0,
        max_value=100.0,
        help_text="Fast burn rate threshold (Default: 14.4x)",
    )
    slow_burn_rate = serializers.FloatField(
        required=False,
        min_value=0.5,
        max_value=50.0,
        help_text="Slow burn rate threshold (Default: 3.0x)",
    )

    def validate_name(self, value):
        """Validate the SLO name: letters, digits, and underscores only."""
        import re

        if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", value):
            raise serializers.ValidationError(
                "SLO name must start with a letter and contain only letters, digits, and underscores."
            )
        return value

    def validate(self, data):
        """Validate the whole SLO definition."""
        # Validate warning_threshold / critical_threshold ordering
        warning = data.get("warning_threshold")
        critical = data.get("critical_threshold")
        target = data.get("target", 0.999)

        if warning is not None and critical is not None and warning <= critical:
            raise serializers.ValidationError(
                "warning_threshold must be greater than critical_threshold."
            )

        if warning is not None and warning <= target:
            raise serializers.ValidationError(
                "warning_threshold must be greater than target."
            )

        if critical is not None and critical < target:
            raise serializers.ValidationError(
                "critical_threshold must be greater than or equal to target."
            )

        # Validate burn_rate ordering
        fast = data.get("fast_burn_rate", 14.4)
        slow = data.get("slow_burn_rate", 3.0)
        if fast <= slow:
            raise serializers.ValidationError(
                "fast_burn_rate must be greater than slow_burn_rate."
            )

        return data


class SLOConfigSerializer(ApplyStrategyMixin):
    """
    Serializer for SLO configuration.

    Lets SLO definitions be managed dynamically through the API.
    - GET: list all currently registered SLOs
    - PUT: update SLO defaults and add/update SLOs
    - DELETE: delete a specific SLO (separate endpoint)

    Applies the Safe Default fallback.
    """

    _config_type = "slo"

    # Default settings
    default_window_days = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=365,
        help_text="Default window (days) when creating a new SLO",
    )
    default_target = serializers.FloatField(
        required=False,
        min_value=0.9,
        max_value=1.0,
        help_text="Default target (0.9-1.0) when creating a new SLO",
    )
    default_fast_burn_rate = serializers.FloatField(
        required=False,
        min_value=1.0,
        max_value=100.0,
        help_text="Default fast burn rate when creating a new SLO",
    )
    default_slow_burn_rate = serializers.FloatField(
        required=False,
        min_value=0.5,
        max_value=50.0,
        help_text="Default slow burn rate when creating a new SLO",
    )

    # For adding/updating SLOs (a single SLO or a list)
    slo = SLODefinitionSerializer(
        required=False, help_text="SLO definition to add/update (single)"
    )
    slos = serializers.ListField(
        required=False,
        child=SLODefinitionSerializer(),
        help_text="SLO definitions to add/update (multiple)",
    )

    def validate(self, data):
        """Validate default burn_rate ordering + Safe Default fallback."""
        fast = data.get("default_fast_burn_rate")
        slow = data.get("default_slow_burn_rate")
        if fast is not None and slow is not None and fast <= slow:
            raise serializers.ValidationError(
                "default_fast_burn_rate must be greater than default_slow_burn_rate."
            )
        return self.validate_with_safe_fallback(data)


class ErrorBudgetConfigSerializer(ApplyStrategyMixin):
    """
    Serializer for Error Budget configuration.

    Error Budget and Burn Rate threshold settings.
    Applies the Safe Default fallback.
    """

    _config_type = "error_budget"

    # Error Budget thresholds (%)
    threshold_healthy = serializers.FloatField(
        required=False,
        min_value=50.0,
        max_value=100.0,
        help_text="Healthy state threshold (Default: 75%)",
    )
    threshold_caution = serializers.FloatField(
        required=False,
        min_value=20.0,
        max_value=80.0,
        help_text="Caution state threshold (Default: 50%)",
    )
    threshold_warning = serializers.FloatField(
        required=False,
        min_value=5.0,
        max_value=50.0,
        help_text="Warning state threshold (Default: 20%)",
    )
    threshold_critical = serializers.FloatField(
        required=False,
        min_value=0.0,
        max_value=20.0,
        help_text="Critical state threshold (Default: 0%)",
    )

    # Burn Rate thresholds
    burn_rate_fast_critical = serializers.FloatField(
        required=False,
        min_value=10.0,
        max_value=50.0,
        help_text="Fast burn critical threshold (Default: 14.4x)",
    )
    burn_rate_fast_warning = serializers.FloatField(
        required=False,
        min_value=3.0,
        max_value=15.0,
        help_text="Fast burn warning threshold (Default: 6.0x)",
    )
    burn_rate_slow_warning = serializers.FloatField(
        required=False,
        min_value=1.0,
        max_value=10.0,
        help_text="Slow burn warning threshold (Default: 3.0x)",
    )
    burn_rate_slow_info = serializers.FloatField(
        required=False,
        min_value=0.5,
        max_value=3.0,
        help_text="Normal burn rate threshold (Default: 1.0x)",
    )

    # Fail-Safe settings
    failsafe_alert_enabled = serializers.BooleanField(
        required=False, help_text="Whether to send alerts on Fail-Safe activation"
    )
    failsafe_cooldown_seconds = serializers.IntegerField(
        required=False,
        min_value=60,
        max_value=3600,
        help_text="Cooldown to prevent consecutive alerts (seconds)",
    )

    # Heartbeat (Dead Man's Snitch) settings
    heartbeat_enabled = serializers.BooleanField(
        required=False,
        help_text="Whether to enable Heartbeat (Dead Man's Snitch). Default: True",
    )
    heartbeat_interval_seconds = serializers.IntegerField(
        required=False,
        min_value=10,
        max_value=300,
        help_text="Heartbeat send interval (seconds, Default: 60s)",
    )
    heartbeat_timeout_seconds = serializers.IntegerField(
        required=False,
        min_value=30,
        max_value=600,
        help_text="Heartbeat timeout (seconds, Default: 120s, considered Dead if no response within this time)",
    )

    # Recovery Notification settings
    recovery_alert_enabled = serializers.BooleanField(
        required=False,
        help_text="Whether to send recovery completion alerts. Default: True",
    )
    recovery_alert_include_downtime = serializers.BooleanField(
        required=False,
        help_text="Whether to include downtime duration in recovery alerts. Default: True",
    )

    # Override escalation settings
    escalation_enabled = serializers.BooleanField(
        required=False, help_text="Whether to enable override escalation. Default: True"
    )
    escalation_channel = serializers.CharField(
        required=False,
        max_length=100,
        help_text="Escalation notification channel (Default: #governance)",
    )
    escalation_mention = serializers.CharField(
        required=False,
        max_length=200,
        help_text="Escalation mention targets (Default: @cto @security)",
    )

    def validate(self, data):
        """Validate threshold ordering and heartbeat settings + Safe Default."""
        # Validate threshold ordering: healthy > caution > warning > critical
        thresholds = [
            ("threshold_healthy", data.get("threshold_healthy", 75.0)),
            ("threshold_caution", data.get("threshold_caution", 50.0)),
            ("threshold_warning", data.get("threshold_warning", 20.0)),
            ("threshold_critical", data.get("threshold_critical", 0.0)),
        ]
        for i in range(len(thresholds) - 1):
            if thresholds[i][1] <= thresholds[i + 1][1]:
                raise serializers.ValidationError(
                    f"{thresholds[i][0]} must be greater than {thresholds[i + 1][0]}."
                )

        # The heartbeat timeout must be greater than the interval
        interval = data.get("heartbeat_interval_seconds", 60)
        timeout = data.get("heartbeat_timeout_seconds", 120)
        if timeout <= interval:
            raise serializers.ValidationError(
                "heartbeat_timeout_seconds must be greater than heartbeat_interval_seconds."
            )

        return self.validate_with_safe_fallback(data)
