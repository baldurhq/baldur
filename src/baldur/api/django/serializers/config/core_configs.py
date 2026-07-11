"""
Core Baldur Configuration Serializers.

CircuitBreaker, DLQ, Retry, RateLimit, Idempotency config serializers.

Fail-Safe Default 강화 추가.
"""

from rest_framework import serializers

from .base import ApplyStrategyMixin


class CircuitBreakerConfigSerializer(ApplyStrategyMixin):
    """
    Serializer for Circuit Breaker configuration.

    Safe Default 폴백 적용.
    """

    _config_type = "circuit_breaker"

    enabled = serializers.BooleanField(required=False, default=True)
    failure_threshold = serializers.IntegerField(
        required=False, min_value=1, max_value=100
    )
    recovery_timeout = serializers.IntegerField(
        required=False, min_value=1, max_value=3600
    )
    success_threshold = serializers.IntegerField(
        required=False, min_value=1, max_value=100
    )
    half_open_max_calls = serializers.IntegerField(
        required=False, min_value=1, max_value=100
    )
    rate_limit_cascade_threshold = serializers.IntegerField(
        required=False, min_value=1, max_value=1000
    )
    rate_limit_cascade_window_seconds = serializers.IntegerField(
        required=False, min_value=1, max_value=3600
    )
    rate_limit_cascade_rate = serializers.FloatField(
        required=False, min_value=0.0, max_value=100.0
    )
    rate_limit_cascade_minimum_calls = serializers.IntegerField(
        required=False, min_value=1, max_value=100
    )
    self_ddos_protection_enabled = serializers.BooleanField(required=False)
    self_ddos_rps_limit = serializers.IntegerField(
        required=False, min_value=1, max_value=10000
    )
    self_ddos_window_seconds = serializers.IntegerField(
        required=False, min_value=1, max_value=300
    )
    self_ddos_backoff_multiplier = serializers.FloatField(
        required=False, min_value=1.0, max_value=10.0
    )

    def validate(self, attrs):
        """검증 + Safe Default 폴백."""
        validated = super().validate(attrs)
        return self.validate_with_safe_fallback(validated)


class DLQConfigSerializer(ApplyStrategyMixin):
    """
    Serializer for DLQ configuration.

    Safe Default 폴백 적용.
    """

    _config_type = "dlq"

    enabled = serializers.BooleanField(required=False, default=True)
    max_retries = serializers.IntegerField(required=False, min_value=1, max_value=20)
    retry_delay = serializers.IntegerField(required=False, min_value=1, max_value=3600)
    expiry_hours = serializers.IntegerField(required=False, min_value=1, max_value=720)
    retention_days = serializers.IntegerField(
        required=False, min_value=1, max_value=365
    )
    batch_size = serializers.IntegerField(required=False, min_value=1, max_value=1000)
    max_replay_attempts = serializers.IntegerField(
        required=False, min_value=1, max_value=10
    )

    def validate(self, attrs):
        """검증 + Safe Default 폴백."""
        validated = super().validate(attrs)
        return self.validate_with_safe_fallback(validated)


class RetryConfigSerializer(ApplyStrategyMixin):
    """
    Serializer for Retry configuration.

    Safe Default 폴백 적용.
    """

    _config_type = "retry"

    max_attempts = serializers.IntegerField(required=False, min_value=1, max_value=20)
    backoff_strategy = serializers.ChoiceField(
        required=False,
        choices=["exponential", "linear", "constant", "decorrelated_jitter"],
    )
    backoff_base = serializers.IntegerField(required=False, min_value=1, max_value=10)
    base_delay = serializers.FloatField(required=False, min_value=0.1, max_value=60.0)
    max_delay = serializers.FloatField(required=False, min_value=1.0, max_value=3600.0)
    min_delay = serializers.IntegerField(required=False, min_value=1, max_value=60)
    jitter = serializers.BooleanField(required=False)
    jitter_percent = serializers.IntegerField(
        required=False, min_value=0, max_value=100
    )

    def validate(self, attrs):
        """검증 + Safe Default 폴백."""
        validated = super().validate(attrs)
        return self.validate_with_safe_fallback(validated)


class RateLimitConfigSerializer(ApplyStrategyMixin):
    """
    Serializer for Rate Limit (inbound quota) configuration.

    Validates the inbound quota family only; the outbound 429-backoff dials
    are not runtime-editable. Applies Safe Default fallback.
    """

    _config_type = "rate_limit"

    control_api_rate_limit = serializers.IntegerField(
        required=False, min_value=1, max_value=10000
    )
    control_api_window_seconds = serializers.IntegerField(
        required=False, min_value=1, max_value=3600
    )
    emergency_rate_limit = serializers.IntegerField(
        required=False, min_value=1, max_value=100
    )
    emergency_window_seconds = serializers.IntegerField(
        required=False, min_value=1, max_value=3600
    )
    middleware_rate_limit = serializers.IntegerField(
        required=False, min_value=0, max_value=10000
    )
    middleware_window_seconds = serializers.IntegerField(
        required=False, min_value=1, max_value=3600
    )
    decorator_enabled = serializers.BooleanField(required=False)
    redis_ttl = serializers.IntegerField(required=False, min_value=60, max_value=86400)

    def validate(self, attrs):
        """Validate, then apply Safe Default fallback."""
        validated = super().validate(attrs)
        return self.validate_with_safe_fallback(validated)


class IdempotencyConfigSerializer(ApplyStrategyMixin):
    """
    Serializer for Idempotency configuration.

    Safe Default 폴백 적용.
    """

    _config_type = "idempotency"

    default_cache_ttl = serializers.IntegerField(
        required=False, min_value=1, max_value=3600
    )
    extended_cache_ttl = serializers.IntegerField(
        required=False, min_value=1, max_value=86400
    )
    clock_skew_tolerance_seconds = serializers.FloatField(
        required=False, min_value=0.0, max_value=60.0
    )

    def validate(self, attrs):
        """검증 + Safe Default 폴백."""
        validated = super().validate(attrs)
        return self.validate_with_safe_fallback(validated)
