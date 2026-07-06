"""
#406 Ghost Settings — Miscellaneous module wiring tests.

Tests for:
- RecoveryAdapterError extra_context (U5 exception pattern)
- Governance approval_timeout_hours wiring (Pattern 2)
- Governance require_reason_for_changes validation (Pattern 3)
- Settings removal contract (deleted fields no longer exist)
- MetaWatchdogSettings escalation_api_timeout_seconds (new setting)

The SlackHandler block_text_limit truncation test relocated to
``tests/pro/unit/notification/test_slack_handler_406.py`` with the PRO Slack
transport.
"""

from __future__ import annotations

import pytest

from baldur.core.exceptions import (
    AdapterError,
    BaldurError,
    RecoveryAdapterError,
)

# =============================================================================
# Contract: RecoveryAdapterError
# =============================================================================


class TestRecoveryAdapterErrorContract:
    """RecoveryAdapterError follows project exception hierarchy."""

    def test_inherits_adapter_error(self):
        """RecoveryAdapterError is a subclass of AdapterError."""
        assert issubclass(RecoveryAdapterError, AdapterError)

    def test_inherits_baldur_error(self):
        """RecoveryAdapterError is a subclass of BaldurError."""
        assert issubclass(RecoveryAdapterError, BaldurError)

    def test_extra_context_includes_service_name(self):
        """extra_context() includes service_name when provided."""
        err = RecoveryAdapterError(
            "test error",
            service_name="redis",
            namespace="production",
        )
        ctx = err.extra_context()

        assert ctx["service_name"] == "redis"
        assert ctx["namespace"] == "production"

    def test_extra_context_includes_replicas(self):
        """extra_context() includes replicas when provided."""
        err = RecoveryAdapterError(
            "bad replicas",
            replicas=999,
        )
        ctx = err.extra_context()

        assert ctx["replicas"] == 999

    def test_extra_context_omits_empty_fields(self):
        """extra_context() omits fields with default/empty values."""
        err = RecoveryAdapterError("minimal error")
        ctx = err.extra_context()

        assert "service_name" not in ctx
        assert "replicas" not in ctx
        assert "namespace" not in ctx


# =============================================================================
# Contract: MetaWatchdogSettings new setting
# =============================================================================


class TestEscalationApiTimeoutContract:
    """escalation_api_timeout_seconds exists with correct default."""

    def test_default_value_is_10(self):
        """Default escalation_api_timeout_seconds is 10.0."""
        from baldur.settings.meta_watchdog import MetaWatchdogSettings

        settings = MetaWatchdogSettings()

        assert settings.escalation_api_timeout_seconds == 10.0


# =============================================================================
# Contract: Settings removal — deleted fields no longer exist
# =============================================================================


class TestSettingsRemovalContract:
    """Deleted ghost settings fields no longer exist on their classes."""

    def test_notification_settings_channels_removed(self):
        """NotificationSettings no longer has 'channels' field."""
        from baldur.settings.notification import NotificationSettings

        assert "channels" not in NotificationSettings.model_fields

    def test_notification_settings_slack_block_text_limit_removed(self):
        """NotificationSettings no longer has 'slack_block_text_limit' field."""
        from baldur.settings.notification import NotificationSettings

        assert "slack_block_text_limit" not in NotificationSettings.model_fields

    def test_notification_channel_default_channels_removed(self):
        """NotificationChannelSettings no longer has 'default_channels' field."""
        from baldur.settings.notification_channel import (
            NotificationChannelSettings,
        )

        assert "default_channels" not in NotificationChannelSettings.model_fields

    def test_notification_channel_timeout_seconds_removed(self):
        """NotificationChannelSettings no longer has 'timeout_seconds' field."""
        from baldur.settings.notification_channel import (
            NotificationChannelSettings,
        )

        assert "timeout_seconds" not in NotificationChannelSettings.model_fields

    def test_slack_channel_text_limits_removed(self):
        """SlackChannelSettings no longer has ghost duplicate text limit fields."""
        from baldur.settings.slack_channel import SlackChannelSettings

        for field in [
            "title_max_length",
            "description_max_length",
            "action_taken_max_length",
            "webhook_timeout_seconds",
        ]:
            assert field not in SlackChannelSettings.model_fields, (
                f"{field} should be removed"
            )

    def test_governance_max_approval_retries_removed(self):
        """GovernanceSettings no longer has 'max_approval_retries' field."""
        from baldur.settings.governance import GovernanceSettings

        assert "max_approval_retries" not in GovernanceSettings.model_fields

    def test_governance_audit_log_retention_days_removed(self):
        """GovernanceSettings no longer has 'audit_log_retention_days' field."""
        from baldur.settings.governance import GovernanceSettings

        assert "audit_log_retention_days" not in GovernanceSettings.model_fields

    def test_postmortem_notification_channels_removed(self):
        """PostmortemSettings no longer has 'notification_channels' field."""
        from baldur.settings.postmortem import PostmortemSettings

        assert "notification_channels" not in PostmortemSettings.model_fields

    def test_scaling_settings_module_removed(self):
        """settings.scaling module no longer exists (dead ScalingSettings removed)."""
        with pytest.raises(ImportError):
            from baldur.settings.scaling import ScalingSettings  # noqa: F401

    def test_chaos_safety_caps_module_removed(self):
        """ChaosSafetyCapsSettings module no longer exists."""
        with pytest.raises(ImportError):
            from baldur.settings.chaos_safety_caps import (  # noqa: F401
                ChaosSafetyCapsSettings,
            )

    def test_api_view_incidents_default_limit_removed(self):
        """ApiViewSettings no longer has 'postmortem_incidents_default_limit' field."""
        from baldur.settings.api_view import ApiViewSettings

        assert "postmortem_incidents_default_limit" not in ApiViewSettings.model_fields
