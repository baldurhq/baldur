"""
Tests for infrastructure and API settings.

Dashboard and batching:
- DashboardSettings: dashboard cache TTL settings
- BatchSettings: batch size and flush interval settings

Audit and monitoring:
- AuditSettings: audit logging settings
- AuditIntegritySettings: audit integrity verification settings

API and tasks:
- ApiViewSettings: API pagination settings
- CeleryTaskSettings: Celery task retry settings

Domain and notification:
- DomainSensitivitySettings: per-domain sensitivity settings
- SlackChannelSettings: Slack channel and message limit settings

Regional recovery:
- RegionalRecoveryPolicySettings: per-region recovery policy settings
"""

import pytest
from pydantic import ValidationError


class TestDashboardSettings:
    """Tests for DashboardSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.dashboard import reset_dashboard_settings

        reset_dashboard_settings()
        yield
        reset_dashboard_settings()

    def test_default_values(self):
        """Default values."""
        from baldur.settings.dashboard import DashboardSettings

        settings = DashboardSettings()

        assert settings.cache_ttl_seconds == 30
        assert settings.cache_ttl_status == 15
        assert settings.cache_ttl_activity == 60
        assert settings.tracker_cache_ttl == 30.0

    def test_env_override(self, monkeypatch):
        """Values can be overridden via environment variables."""
        from baldur.settings.dashboard import DashboardSettings

        monkeypatch.setenv("BALDUR_DASHBOARD_CACHE_TTL_SECONDS", "60")

        settings = DashboardSettings()

        assert settings.cache_ttl_seconds == 60

    def test_validation_ttl_range(self):
        """TTL range validation."""
        from baldur.settings.dashboard import DashboardSettings

        with pytest.raises(ValidationError):
            DashboardSettings(cache_ttl_seconds=2)  # < 5

        with pytest.raises(ValidationError):
            DashboardSettings(cache_ttl_activity=1000)  # > 600

    def test_singleton_pattern(self):
        """Singleton pattern returns the same instance."""
        from baldur.settings.dashboard import get_dashboard_settings

        settings1 = get_dashboard_settings()
        settings2 = get_dashboard_settings()

        assert settings1 is settings2


class TestBatchSettings:
    """Tests for BatchSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.batch import reset_batch_settings

        reset_batch_settings()
        yield
        reset_batch_settings()

    def test_default_values(self):
        """Default values."""
        from baldur.settings.batch import BatchSettings

        settings = BatchSettings()

        assert settings.default_batch_size == 100
        assert settings.logger_batch_size == 100  # changed: 10 -> 100
        assert settings.flush_interval == 5.0

    def test_env_override(self, monkeypatch):
        """Values can be overridden via environment variables."""
        from baldur.settings.batch import BatchSettings

        monkeypatch.setenv("BALDUR_BATCH_LOGGER_BATCH_SIZE", "20")

        settings = BatchSettings()

        assert settings.logger_batch_size == 20

    def test_validation_batch_size_range(self):
        """batch_size range validation."""
        from baldur.settings.batch import BatchSettings

        with pytest.raises(ValidationError):
            BatchSettings(default_batch_size=5)  # < 10

        with pytest.raises(ValidationError):
            BatchSettings(default_batch_size=2000)  # > 1000

    def test_singleton_pattern(self):
        """Singleton pattern returns the same instance."""
        from baldur.settings.batch import get_batch_settings

        settings1 = get_batch_settings()
        settings2 = get_batch_settings()

        assert settings1 is settings2


class TestAuditSettings:
    """Tests for AuditSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.audit import reset_audit_settings

        reset_audit_settings()
        yield
        reset_audit_settings()

    def test_default_values(self):
        """Default values."""
        from baldur.settings.audit import AuditSettings

        settings = AuditSettings()

        assert settings.max_history == 100
        assert settings.config_history_entries == 50
        assert settings.retention_days == 90

    def test_env_override(self, monkeypatch):
        """Values can be overridden via environment variables."""
        from baldur.settings.audit import AuditSettings

        monkeypatch.setenv("BALDUR_AUDIT_MAX_HISTORY", "200")

        settings = AuditSettings()

        assert settings.max_history == 200

    def test_validation_history_range(self):
        """history range validation."""
        from baldur.settings.audit import AuditSettings

        with pytest.raises(ValidationError):
            AuditSettings(max_history=5)  # < 10

        with pytest.raises(ValidationError):
            AuditSettings(config_history_entries=1000)  # > 500

    def test_singleton_pattern(self):
        """Singleton pattern returns the same instance."""
        from baldur.settings.audit import get_audit_settings

        settings1 = get_audit_settings()
        settings2 = get_audit_settings()

        assert settings1 is settings2


class TestCeleryTaskSettings:
    """Tests for CeleryTaskSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.celery_task import reset_celery_task_settings

        reset_celery_task_settings()
        yield
        reset_celery_task_settings()

    def test_default_values(self):
        """Default values."""
        from baldur.settings.celery_task import CeleryTaskSettings

        settings = CeleryTaskSettings()

        assert settings.max_retries == 3
        assert settings.default_retry_delay == 60
        assert settings.time_limit == 300
        assert settings.backoff_multiplier == 2.0

    def test_env_override(self, monkeypatch):
        """Values can be overridden via environment variables."""
        from baldur.settings.celery_task import CeleryTaskSettings

        monkeypatch.setenv("BALDUR_CELERY_TASK_MAX_RETRIES", "5")

        settings = CeleryTaskSettings()

        assert settings.max_retries == 5

    def test_validation_retries_range(self):
        """max_retries range (0-10) validation."""
        from baldur.settings.celery_task import CeleryTaskSettings

        # 0 is valid (no retries)
        settings = CeleryTaskSettings(max_retries=0)
        assert settings.max_retries == 0

        with pytest.raises(ValidationError):
            CeleryTaskSettings(max_retries=15)  # > 10

    def test_singleton_pattern(self):
        """Singleton pattern returns the same instance."""
        from baldur.settings.celery_task import get_celery_task_settings

        settings1 = get_celery_task_settings()
        settings2 = get_celery_task_settings()

        assert settings1 is settings2


class TestApiViewSettings:
    """Tests for ApiViewSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.api_view import reset_api_view_settings

        reset_api_view_settings()
        yield
        reset_api_view_settings()

    def test_default_values(self):
        """Default values."""
        from baldur.settings.api_view import ApiViewSettings

        settings = ApiViewSettings()

        assert settings.default_limit == 100
        assert settings.default_offset == 0
        assert settings.max_limit == 1000
        assert settings.default_order == "-created_at"

    def test_env_override(self, monkeypatch):
        """Values can be overridden via environment variables."""
        from baldur.settings.api_view import ApiViewSettings

        monkeypatch.setenv("BALDUR_API_VIEW_DEFAULT_LIMIT", "50")

        settings = ApiViewSettings()

        assert settings.default_limit == 50

    def test_validation_limit_range(self):
        """limit range validation."""
        from baldur.settings.api_view import ApiViewSettings

        with pytest.raises(ValidationError):
            ApiViewSettings(default_limit=5)  # < 10

        with pytest.raises(ValidationError):
            ApiViewSettings(max_limit=20000)  # > 10000

    def test_singleton_pattern(self):
        """Singleton pattern returns the same instance."""
        from baldur.settings.api_view import get_api_view_settings

        settings1 = get_api_view_settings()
        settings2 = get_api_view_settings()

        assert settings1 is settings2


class TestDomainSensitivitySettings:
    """Tests for DomainSensitivitySettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.domain_sensitivity import (
            reset_domain_sensitivity_settings,
        )

        reset_domain_sensitivity_settings()
        yield
        reset_domain_sensitivity_settings()

    def test_default_values(self):
        """Default values."""
        from baldur.settings.domain_sensitivity import DomainSensitivitySettings

        settings = DomainSensitivitySettings()

        assert settings.domains["payment"] == 10.0
        assert settings.domains["order"] == 5.0
        assert settings.domains["inventory"] == 3.0
        assert settings.domains["notification"] == 1.5
        assert settings.domains["analytics"] == 1.0
        assert settings.default_sensitivity == 1.0

    def test_env_override(self, monkeypatch):
        """Values can be overridden via environment variables."""
        from baldur.settings.domain_sensitivity import DomainSensitivitySettings

        monkeypatch.setenv(
            "BALDUR_DOMAIN_SENSITIVITY_DOMAINS",
            '{"payment": 15.0, "order": 5.0}',
        )

        settings = DomainSensitivitySettings()

        assert settings.domains["payment"] == 15.0

    def test_validation_sensitivity_range(self):
        """sensitivity range validation."""
        from baldur.settings.domain_sensitivity import DomainSensitivitySettings

        with pytest.raises(ValidationError):
            DomainSensitivitySettings(domains={"bad": 0.05})  # < 0.1

        with pytest.raises(ValidationError):
            DomainSensitivitySettings(domains={"bad": 150.0})  # > 100.0

    def test_singleton_pattern(self):
        """Singleton pattern returns the same instance."""
        from baldur.settings.domain_sensitivity import (
            get_domain_sensitivity_settings,
        )

        settings1 = get_domain_sensitivity_settings()
        settings2 = get_domain_sensitivity_settings()

        assert settings1 is settings2


class TestSlackChannelSettings:
    """Tests for SlackChannelSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.slack_channel import reset_slack_channel_settings

        reset_slack_channel_settings()
        yield
        reset_slack_channel_settings()

    def test_default_values(self):
        """Default values."""
        from baldur.settings.slack_channel import SlackChannelSettings

        settings = SlackChannelSettings()

        assert settings.default_channel == "#baldur-alerts"
        assert settings.critical_channel == "#baldur-critical"
        assert settings.emergency_channel == "#baldur-emergency"
        assert settings.block_text_limit == 3000
        assert settings.max_attachments == 10

    def test_env_override(self, monkeypatch):
        """Values can be overridden via environment variables."""
        from baldur.settings.slack_channel import SlackChannelSettings

        monkeypatch.setenv("BALDUR_SLACK_CHANNEL_DEFAULT_CHANNEL", "#custom-alerts")

        settings = SlackChannelSettings()

        assert settings.default_channel == "#custom-alerts"

    def test_validation_text_limit_range(self):
        """text_limit range validation."""
        from baldur.settings.slack_channel import SlackChannelSettings

        with pytest.raises(ValidationError):
            SlackChannelSettings(block_text_limit=500)  # < 1000

        with pytest.raises(ValidationError):
            SlackChannelSettings(block_text_limit=20000)  # > 10000

    def test_singleton_pattern(self):
        """Singleton pattern returns the same instance."""
        from baldur.settings.slack_channel import get_slack_channel_settings

        settings1 = get_slack_channel_settings()
        settings2 = get_slack_channel_settings()

        assert settings1 is settings2


class TestAuditIntegritySettings:
    """Tests for AuditIntegritySettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.audit_integrity import reset_audit_integrity_settings

        reset_audit_integrity_settings()
        yield
        reset_audit_integrity_settings()

    def test_default_values(self):
        """Default values."""
        from baldur.settings.audit_integrity import AuditIntegritySettings

        settings = AuditIntegritySettings()

        assert settings.pending_ttl_seconds == 30
        assert settings.orphan_ttl_seconds == 86400
        assert settings.archive_threshold_days == 7
        assert settings.cold_retention_years == 7

    def test_env_override(self, monkeypatch):
        """Values can be overridden via environment variables."""
        from baldur.settings.audit_integrity import AuditIntegritySettings

        monkeypatch.setenv("BALDUR_AUDIT_INTEGRITY_ARCHIVE_THRESHOLD_DAYS", "14")

        settings = AuditIntegritySettings()

        assert settings.archive_threshold_days == 14

    def test_validation_ttl_range(self):
        """TTL range validation."""
        from baldur.settings.audit_integrity import AuditIntegritySettings

        with pytest.raises(ValidationError):
            AuditIntegritySettings(pending_ttl_seconds=5)  # < 10

        with pytest.raises(ValidationError):
            AuditIntegritySettings(orphan_ttl_seconds=1000000)  # > 604800

    def test_singleton_pattern(self):
        """Singleton pattern returns the same instance."""
        from baldur.settings.audit_integrity import get_audit_integrity_settings

        settings1 = get_audit_integrity_settings()
        settings2 = get_audit_integrity_settings()

        assert settings1 is settings2


class TestRegionalRecoveryPolicySettings:
    """Tests for RegionalRecoveryPolicySettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.regional_recovery_policy import (
            reset_regional_recovery_policy_settings,
        )

        reset_regional_recovery_policy_settings()
        yield
        reset_regional_recovery_policy_settings()

    def test_default_values(self):
        """Default values."""
        from baldur.settings.regional_recovery_policy import (
            RegionalRecoveryPolicySettings,
        )

        settings = RegionalRecoveryPolicySettings()

        assert settings.error_rate_threshold == 0.10
        assert settings.success_rate_threshold == 0.95
        assert settings.stability_check_duration_minutes == 10
        assert settings.max_recovery_duration_minutes == 60
        assert settings.cooldown_minutes == 15

    def test_env_override(self, monkeypatch):
        """Values can be overridden via environment variables."""
        from baldur.settings.regional_recovery_policy import (
            RegionalRecoveryPolicySettings,
        )

        monkeypatch.setenv(
            "BALDUR_REGIONAL_RECOVERY_POLICY_ERROR_RATE_THRESHOLD", "0.15"
        )

        settings = RegionalRecoveryPolicySettings()

        assert settings.error_rate_threshold == 0.15

    def test_validation_rate_range(self):
        """rate range validation."""
        from baldur.settings.regional_recovery_policy import (
            RegionalRecoveryPolicySettings,
        )

        with pytest.raises(ValidationError):
            RegionalRecoveryPolicySettings(error_rate_threshold=0.0)  # < 0.01

        with pytest.raises(ValidationError):
            RegionalRecoveryPolicySettings(success_rate_threshold=1.1)  # > 1.0

    def test_singleton_pattern(self):
        """Singleton pattern returns the same instance."""
        from baldur.settings.regional_recovery_policy import (
            get_regional_recovery_policy_settings,
        )

        settings1 = get_regional_recovery_policy_settings()
        settings2 = get_regional_recovery_policy_settings()

        assert settings1 is settings2
