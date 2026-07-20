"""
Security and Notification Configuration Serializers.

Security and Notification config serializers.

Adds Fail-Safe Default hardening.
"""

from rest_framework import serializers

from .base import ApplyStrategyMixin


class SecurityConfigSerializer(ApplyStrategyMixin):
    """
    Serializer for Security configuration.

    Applies the Safe Default fallback.
    """

    _config_type = "security"

    temporary_ban_hours = serializers.IntegerField(
        required=False, min_value=1, max_value=168
    )
    permanent_ban_threshold = serializers.IntegerField(
        required=False, min_value=1, max_value=100
    )
    suspicious_ip_cache_timeout = serializers.IntegerField(
        required=False, min_value=60, max_value=604800
    )
    injection_ban_hours = serializers.IntegerField(
        required=False, min_value=1, max_value=720
    )

    def validate(self, attrs):
        """Validate + Safe Default fallback."""
        validated = super().validate(attrs)
        return self.validate_with_safe_fallback(validated)


class NotificationConfigSerializer(ApplyStrategyMixin):
    """
    Serializer for Notification configuration.

    Applies the Safe Default fallback.
    """

    _config_type = "notification"

    enabled = serializers.BooleanField(required=False)
    channels = serializers.ListField(
        required=False,
        child=serializers.ChoiceField(choices=["slack", "pagerduty", "webhook"]),
    )
    critical_threshold = serializers.IntegerField(
        required=False, min_value=1, max_value=100
    )
    warning_threshold = serializers.IntegerField(
        required=False, min_value=1, max_value=100
    )
    slack_block_text_limit = serializers.IntegerField(
        required=False, min_value=100, max_value=10000
    )
    description_max_length = serializers.IntegerField(
        required=False, min_value=50, max_value=5000
    )
    action_taken_max_length = serializers.IntegerField(
        required=False, min_value=50, max_value=1000
    )
    title_max_length = serializers.IntegerField(
        required=False, min_value=20, max_value=500
    )
    notification_timeout_seconds = serializers.IntegerField(
        required=False, min_value=1, max_value=60
    )

    def validate(self, attrs):
        """Validate + Safe Default fallback."""
        validated = super().validate(attrs)
        return self.validate_with_safe_fallback(validated)
