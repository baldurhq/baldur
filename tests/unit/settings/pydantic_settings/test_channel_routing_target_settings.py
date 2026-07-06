"""
ChannelRoutingSettings and ChannelTargetSettings unit tests.

Tests for #410 settings: default values (contract), validators, singleton lifecycle.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.channel_routing import (
    ChannelRoutingSettings,
    get_channel_routing_settings,
    reset_channel_routing_settings,
)
from baldur.settings.channel_target import (
    ChannelTargetSettings,
    get_channel_target_settings,
    reset_channel_target_settings,
)

# =============================================================================
# ChannelRoutingSettings — Contract
# =============================================================================


class TestChannelRoutingSettingsContract:
    """Design contract values from 410 DC-2."""

    def test_priority_channels_critical_routes_slack_pagerduty(self):
        """CRITICAL maps to slack + pagerduty (email/sms removed in 657)."""
        s = ChannelRoutingSettings()
        assert s.priority_channels["critical"] == ["slack", "pagerduty"]

    def test_priority_channels_high_includes_slack_only(self):
        """HIGH maps to slack only (email removed in 657)."""
        s = ChannelRoutingSettings()
        assert s.priority_channels["high"] == ["slack"]

    def test_priority_channels_medium_includes_slack_only(self):
        """MEDIUM maps to slack only."""
        s = ChannelRoutingSettings()
        assert s.priority_channels["medium"] == ["slack"]

    def test_priority_channels_info_is_empty(self):
        """INFO maps to empty list (log only)."""
        s = ChannelRoutingSettings()
        assert s.priority_channels["info"] == []

    def test_category_channels_security_contract(self):
        """SECURITY category defaults to slack only (email removed in 657)."""
        s = ChannelRoutingSettings()
        assert s.category_channels["security"] == ["slack"]

    def test_category_channels_governance_contract(self):
        """GOVERNANCE category defaults to slack only (email removed in 657)."""
        s = ChannelRoutingSettings()
        assert s.category_channels["governance"] == ["slack"]

    def test_category_cooldown_security_60s(self):
        """SECURITY cooldown is 60 seconds."""
        s = ChannelRoutingSettings()
        assert s.category_cooldown_seconds["security"] == 60

    def test_category_cooldown_approval_zero(self):
        """APPROVAL cooldown is 0 (no suppression)."""
        s = ChannelRoutingSettings()
        assert s.category_cooldown_seconds["approval"] == 0

    def test_category_cooldown_report_zero(self):
        """REPORT cooldown is 0 (no suppression for daily reports)."""
        s = ChannelRoutingSettings()
        assert s.category_cooldown_seconds["report"] == 0

    def test_category_cooldown_sla_1800s(self):
        """SLA cooldown is 1800 seconds (30 min)."""
        s = ChannelRoutingSettings()
        assert s.category_cooldown_seconds["sla"] == 1800

    def test_category_slack_targets_empty_by_default(self):
        """No category-specific Slack targets by default."""
        s = ChannelRoutingSettings()
        assert s.category_slack_targets == {}

    def test_env_prefix(self):
        """Env prefix is BALDUR_CHANNEL_ROUTING_."""
        assert (
            ChannelRoutingSettings.model_config["env_prefix"]
            == "BALDUR_CHANNEL_ROUTING_"
        )

    def test_all_priority_keys_present(self):
        """All 5 priority levels are present in default."""
        s = ChannelRoutingSettings()
        assert set(s.priority_channels.keys()) == {
            "critical",
            "high",
            "medium",
            "low",
            "info",
        }

    def test_all_cooldown_categories_present(self):
        """All 9 category cooldowns are present in default."""
        s = ChannelRoutingSettings()
        assert len(s.category_cooldown_seconds) == 9


# =============================================================================
# ChannelRoutingSettings — Behavior
# =============================================================================


class TestChannelRoutingSettingsBehavior:
    """Validator and singleton behavior."""

    def test_invalid_priority_key_raises_validation_error(self):
        """Unknown priority key is rejected by validator."""
        with pytest.raises(ValidationError, match="Unknown priority keys"):
            ChannelRoutingSettings(priority_channels={"unknown_priority": ["slack"]})

    def test_invalid_category_channels_key_raises_validation_error(self):
        """Unknown category_channels key is rejected by validator."""
        with pytest.raises(ValidationError, match="Unknown category keys"):
            ChannelRoutingSettings(
                category_channels={"nonexistent_category": ["slack"]}
            )

    def test_invalid_cooldown_category_key_raises_validation_error(self):
        """Unknown category_cooldown_seconds key is rejected by validator."""
        with pytest.raises(ValidationError, match="Unknown category keys"):
            ChannelRoutingSettings(category_cooldown_seconds={"bad_key": 100})

    @pytest.mark.parametrize(
        ("field", "valid_key"),
        [
            ("priority_channels", "critical"),
            ("category_channels", "security"),
        ],
        ids=["priority_channels", "category_channels"],
    )
    @pytest.mark.parametrize(
        "token",
        ["email", "sms", "telegram"],
        ids=["email", "sms", "telegram"],
    )
    def test_unknown_channel_value_raises_validation_error(
        self, field, valid_key, token
    ):
        """A channel type not in MessageChannel is rejected at config load (657 D4).

        email/sms (removed in 657) and any unknown token (telegram) must fail loudly
        at settings load — not silently skip at incident time. A valid priority/category
        key is used so the rejection comes from the channel-value validator, not the
        key validator (asserted via the distinct ``Unknown channel types`` message).
        """
        with pytest.raises(ValidationError, match="Unknown channel types"):
            ChannelRoutingSettings(**{field: {valid_key: [token]}})

    @pytest.mark.parametrize(
        "token",
        ["webhook", "teams", "stdout"],
        ids=["webhook", "teams", "stdout"],
    )
    def test_valid_nondefault_channel_value_accepted(self, token):
        """A valid MessageChannel value outside the defaults still passes (657 D4).

        Proves the channel-value validator allowlists the whole enum, not just the
        slack/pagerduty defaults — the just-after-boundary half of the reject test.
        """
        s = ChannelRoutingSettings(priority_channels={"critical": [token]})
        assert s.priority_channels["critical"] == [token]

    def test_singleton_returns_same_instance(self):
        """get_channel_routing_settings returns cached instance."""
        reset_channel_routing_settings()
        try:
            a = get_channel_routing_settings()
            b = get_channel_routing_settings()
            assert a is b
        finally:
            reset_channel_routing_settings()

    def test_reset_clears_singleton(self):
        """reset clears cached instance, next call creates new one."""
        reset_channel_routing_settings()
        try:
            a = get_channel_routing_settings()
            reset_channel_routing_settings()
            b = get_channel_routing_settings()
            assert a is not b
        finally:
            reset_channel_routing_settings()


# =============================================================================
# ChannelTargetSettings — Contract
# =============================================================================


class TestChannelTargetSettingsContract:
    """Design contract values from 410 DC-6."""

    def test_slack_webhook_url_default_empty(self):
        """Slack webhook URL defaults to empty string."""
        s = ChannelTargetSettings()
        assert s.slack_webhook_url == ""

    def test_pagerduty_service_key_default_empty(self):
        """PagerDuty service key defaults to empty string."""
        s = ChannelTargetSettings()
        assert s.pagerduty_service_key == ""

    def test_pagerduty_enabled_default_false(self):
        """PagerDuty disabled by default (fail-safe)."""
        s = ChannelTargetSettings()
        assert s.pagerduty_enabled is False

    def test_dry_run_default_false(self):
        """dry_run disabled by default."""
        s = ChannelTargetSettings()
        assert s.dry_run is False

    def test_env_prefix(self):
        """Env prefix is BALDUR_CHANNEL_TARGET_."""
        assert (
            ChannelTargetSettings.model_config["env_prefix"] == "BALDUR_CHANNEL_TARGET_"
        )


# =============================================================================
# ChannelTargetSettings — Behavior
# =============================================================================


class TestChannelTargetSettingsBehavior:
    """Singleton lifecycle."""

    def test_singleton_returns_same_instance(self):
        """get_channel_target_settings returns cached instance."""
        reset_channel_target_settings()
        try:
            a = get_channel_target_settings()
            b = get_channel_target_settings()
            assert a is b
        finally:
            reset_channel_target_settings()

    def test_reset_clears_singleton(self):
        """reset clears cached instance."""
        reset_channel_target_settings()
        try:
            a = get_channel_target_settings()
            reset_channel_target_settings()
            b = get_channel_target_settings()
            assert a is not b
        finally:
            reset_channel_target_settings()
