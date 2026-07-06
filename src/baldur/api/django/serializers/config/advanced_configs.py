"""
Advanced Configuration Serializers.

Forensic, Metrics, Logging config serializers.

Fail-Safe Default 강화 추가.
"""

from rest_framework import serializers

from .base import ApplyStrategyMixin


class ForensicConfigSerializer(ApplyStrategyMixin):
    """
    Serializer for Forensic configuration.

    Forensic 분석 및 디버깅 관련 설정.
    """

    _config_type = "forensic"

    # 기존 필드
    error_message_max_length = serializers.IntegerField(
        required=False, min_value=50, max_value=5000
    )
    response_body_max_length = serializers.IntegerField(
        required=False, min_value=100, max_value=100000
    )
    user_agent_max_length = serializers.IntegerField(
        required=False, min_value=50, max_value=2000
    )

    # 추가 Forensic 설정 (이전 env만 노출되었던 설정들)
    max_stack_frames = serializers.IntegerField(
        required=False,
        min_value=10,
        max_value=200,
        help_text="Maximum number of stack frames to collect. Default: 50",
    )
    max_context_size_bytes = serializers.IntegerField(
        required=False,
        min_value=1024,
        max_value=1048576,  # 1MB 상한
        help_text="Maximum context data size (bytes). Default: 65536 (64KB)",
    )
    include_local_variables = serializers.BooleanField(
        required=False,
        help_text="Whether to include local variables in stack traces. Default: False (disabled for security)",
    )
    sanitize_sensitive_data = serializers.BooleanField(
        required=False,
        help_text="Whether to mask sensitive data. Default: True",
    )
    sensitive_key_patterns = serializers.ListField(
        required=False,
        child=serializers.CharField(max_length=100),
        help_text="List of key patterns to mask. Default: [password, secret, token, key, auth]",
    )

    def validate(self, attrs):
        """검증 + Safe Default 폴백."""
        validated = super().validate(attrs)
        return self.validate_with_safe_fallback(validated)


class MetricsConfigSerializer(ApplyStrategyMixin):
    """
    Serializer for Metrics configuration.

    Safe Default 폴백 적용.
    """

    _config_type = "metrics"

    enabled = serializers.BooleanField(required=False)
    prefix = serializers.CharField(required=False, max_length=50)
    # Jitter settings (Thundering Herd prevention)
    # Clamping: min=0.0 (음수 방지), max=300.0 (5분 상한)
    jitter_enabled = serializers.BooleanField(
        required=False,
        help_text="Whether to enable Jitter (Default: True). Prevents Thundering Herd in distributed environments",
    )
    jitter_max_delay_seconds = serializers.FloatField(
        required=False,
        min_value=0.0,  # 음수 방지 (Clamping)
        max_value=300.0,  # 5분 상한
        help_text="Maximum Jitter delay time (seconds). Range: 0-300 (Default: 60.0)",
    )

    def validate(self, attrs):
        """검증 + Safe Default 폴백."""
        validated = super().validate(attrs)
        return self.validate_with_safe_fallback(validated)


class LoggingConfigSerializer(ApplyStrategyMixin):
    """
    Serializer for Logging configuration.

    각 Baldur 컴포넌트별 로깅 레벨 설정.
    이전에는 환경변수로만 제어 가능했던 설정들을 API로 노출.
    """

    _config_type = "logging"

    LEVEL_CHOICES = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

    # 컴포넌트별 로그 레벨
    dlq_log_level = serializers.ChoiceField(
        required=False,
        choices=LEVEL_CHOICES,
        help_text="DLQ log level. Default: INFO",
    )
    circuit_breaker_log_level = serializers.ChoiceField(
        required=False,
        choices=LEVEL_CHOICES,
        help_text="Circuit Breaker log level. Default: INFO",
    )
    replay_log_level = serializers.ChoiceField(
        required=False,
        choices=LEVEL_CHOICES,
        help_text="DLQ Replay log level. Default: INFO",
    )
    sla_log_level = serializers.ChoiceField(
        required=False,
        choices=LEVEL_CHOICES,
        help_text="SLA/SLO monitoring log level. Default: INFO",
    )
    forensic_log_level = serializers.ChoiceField(
        required=False,
        choices=LEVEL_CHOICES,
        help_text="Forensic analysis log level. Default: DEBUG",
    )
    emergency_log_level = serializers.ChoiceField(
        required=False,
        choices=LEVEL_CHOICES,
        help_text="Emergency Mode log level. Default: WARNING",
    )
    chaos_log_level = serializers.ChoiceField(
        required=False,
        choices=LEVEL_CHOICES,
        help_text="Chaos Engineering log level. Default: INFO",
    )
    l2_storage_log_level = serializers.ChoiceField(
        required=False,
        choices=LEVEL_CHOICES,
        help_text="L2 Storage Resilience log level. Default: INFO",
    )

    # 로그 포맷 설정
    include_timestamps = serializers.BooleanField(
        required=False,
        help_text="Whether to include timestamps in logs. Default: True",
    )
    include_request_id = serializers.BooleanField(
        required=False,
        help_text="Whether to include Request ID in logs. Default: True",
    )
    include_user_info = serializers.BooleanField(
        required=False,
        help_text="Whether to include user info in logs. Default: False (disabled for security)",
    )

    # 로그 출력 설정
    structured_json = serializers.BooleanField(
        required=False,
        help_text="Use structured JSON log format. Default: True (production)",
    )

    def validate(self, attrs):
        """검증 + Safe Default 폴백."""
        validated = super().validate(attrs)
        return self.validate_with_safe_fallback(validated)
