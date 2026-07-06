"""
Tests for Pydantic Settings consistency with legacy dataclass configs.
"""


class TestPydanticConsistencyWithLegacy:
    """
    Pydantic 설정과 기존 dataclass 설정의 일관성 검증.
    """

    def test_sla_consistency(self):
        """SLAConfig와 SLASettings 기본값 일치."""
        from baldur.core.config import SLAConfig
        from baldur.settings.sla import SLASettings

        legacy = SLAConfig()
        pydantic = SLASettings()

        assert pydantic.default_hours == legacy.default_hours

    def test_idempotency_consistency(self):
        """IdempotencyConfig와 IdempotencySettings 기본값 일치."""
        from baldur.core.config import IdempotencyConfig
        from baldur.settings.idempotency import IdempotencySettings

        legacy = IdempotencyConfig()
        pydantic = IdempotencySettings()

        assert pydantic.default_cache_ttl == legacy.default_cache_ttl
        assert pydantic.extended_cache_ttl == legacy.extended_cache_ttl
        assert (
            pydantic.clock_skew_tolerance_seconds == legacy.clock_skew_tolerance_seconds
        )

    def test_forensic_consistency(self):
        """ForensicConfig와 ForensicSettings 기본값 일치."""
        from baldur.core.config import ForensicConfig
        from baldur.settings.forensic import ForensicSettings

        legacy = ForensicConfig()
        pydantic = ForensicSettings()

        assert pydantic.error_message_max_length == legacy.error_message_max_length
        assert pydantic.response_body_max_length == legacy.response_body_max_length
        assert pydantic.max_stack_frames == legacy.max_stack_frames
        assert pydantic.max_context_size_bytes == legacy.max_context_size_bytes

    def test_logging_consistency(self):
        """LoggingConfig와 LoggingSettings 기본값 일치."""
        from baldur.core.config import LoggingConfig
        from baldur.settings.logging_settings import LoggingSettings

        legacy = LoggingConfig()
        pydantic = LoggingSettings()

        assert pydantic.dlq_log_level == legacy.dlq_log_level
        assert pydantic.circuit_breaker_log_level == legacy.circuit_breaker_log_level
        assert pydantic.structured_json == legacy.structured_json

    def test_metrics_consistency(self):
        """MetricsConfig와 MetricsSettings 기본값 일치."""
        from baldur.core.config import MetricsConfig
        from baldur.settings.metrics import MetricsSettings

        legacy = MetricsConfig()
        pydantic = MetricsSettings()

        assert pydantic.enabled == legacy.enabled
        assert pydantic.prefix == legacy.prefix

    def test_notification_consistency(self):
        """NotificationConfig와 NotificationSettings 기본값 일치."""
        from baldur.core.config import NotificationConfig
        from baldur.settings.notification import NotificationSettings

        legacy = NotificationConfig()
        pydantic = NotificationSettings()

        assert pydantic.enabled == legacy.enabled
        assert pydantic.critical_threshold == legacy.critical_threshold
        assert pydantic.warning_threshold == legacy.warning_threshold

    def test_error_budget_consistency(self):
        """ErrorBudgetConfig와 ErrorBudgetSettings 기본값 일치."""
        from baldur.core.config import ErrorBudgetConfig
        from baldur.settings.error_budget import ErrorBudgetSettings

        legacy = ErrorBudgetConfig()
        pydantic = ErrorBudgetSettings()

        assert pydantic.threshold_healthy == legacy.threshold_healthy
        assert pydantic.threshold_caution == legacy.threshold_caution
        assert pydantic.burn_rate_fast_critical == legacy.burn_rate_fast_critical
        assert pydantic.heartbeat_enabled == legacy.heartbeat_enabled

    def test_governance_consistency(self):
        """GovernanceConfig와 GovernanceSettings 기본값 일치."""
        from baldur.core.config import GovernanceConfig
        from baldur.settings.governance import GovernanceSettings

        legacy = GovernanceConfig()
        pydantic = GovernanceSettings()

        assert pydantic.threshold_operator == legacy.threshold_operator
        assert pydantic.threshold_admin == legacy.threshold_admin
        assert pydantic.emergency_expiry_hours == legacy.emergency_expiry_hours
        assert pydantic.default_mode == legacy.default_mode

    def test_chaos_consistency(self):
        """ChaosConfig와 ChaosSettings 기본값 일치."""
        from baldur.core.config import ChaosConfig
        from baldur.settings.chaos import ChaosSettings

        legacy = ChaosConfig()
        pydantic = ChaosSettings()

        assert pydantic.max_blast_radius == legacy.max_blast_radius
        assert pydantic.max_failure_rate == legacy.max_failure_rate
        assert pydantic.auto_rollback_enabled == legacy.auto_rollback_enabled

    def test_drift_threshold_consistency(self):
        """DriftThresholdConfig와 DriftThresholdSettings 기본값 일치."""
        from baldur.core.config import DriftThresholdConfig
        from baldur.settings.drift_threshold import DriftThresholdSettings

        legacy = DriftThresholdConfig()
        pydantic = DriftThresholdSettings()

        assert pydantic.warning_threshold == legacy.warning_threshold
        assert pydantic.critical_threshold == legacy.critical_threshold
        assert pydantic.alert_enabled == legacy.alert_enabled
