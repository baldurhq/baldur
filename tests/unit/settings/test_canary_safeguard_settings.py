"""
Canary safeguard settings tests (618 D6 / D13).

Covers the two settings surfaces the canary-safeguard wiring added:
- CanaryInterlockSettings (BALDUR_CANARY_INTERLOCK_) — EmergencyStateRefresher
  daemon configuration, with bounded poll/jitter/failure fields and a Literal
  failure action, plus the get/reset singleton pair.
- CanarySettings.tier_map / default_tier (BALDUR_CANARY_) — service-tier
  resolution inputs, validated against the {critical, standard, non_essential}
  vocabulary.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from baldur.settings.canary import CanarySettings
from baldur.settings.canary_interlock import (
    CanaryInterlockSettings,
    get_canary_interlock_settings,
    reset_canary_interlock_settings,
)

# =============================================================================
# Contract: CanaryInterlockSettings defaults, bounds, singleton
# =============================================================================


class TestCanaryInterlockSettingsContract:
    """CanaryInterlockSettings default values, field bounds, and singleton pair."""

    def test_defaults(self):
        """Defaults match the 618 D6 specification."""
        settings = CanaryInterlockSettings()

        assert settings.refresher_enabled is True
        assert settings.refresh_interval_seconds == 30
        assert settings.jitter_max_seconds == 5
        assert settings.max_consecutive_failures == 3
        assert settings.on_refresh_failure_action == "log_and_continue"

    @pytest.mark.parametrize(
        ("value", "should_pass"),
        [
            (4, False),  # below ge=5
            (5, True),  # at ge=5
            (300, True),  # at le=300
            (301, False),  # above le=300
        ],
        ids=["below_min", "at_min", "at_max", "above_max"],
    )
    def test_refresh_interval_seconds_boundary(self, value, should_pass):
        """refresh_interval_seconds is bounded ge=5, le=300."""
        if should_pass:
            assert (
                CanaryInterlockSettings(
                    refresh_interval_seconds=value
                ).refresh_interval_seconds
                == value
            )
        else:
            with pytest.raises(ValidationError):
                CanaryInterlockSettings(refresh_interval_seconds=value)

    @pytest.mark.parametrize(
        ("value", "should_pass"),
        [
            (-1, False),  # below ge=0
            (0, True),  # at ge=0
            (30, True),  # at le=30
            (31, False),  # above le=30
        ],
        ids=["below_min", "at_min", "at_max", "above_max"],
    )
    def test_jitter_max_seconds_boundary(self, value, should_pass):
        """jitter_max_seconds is bounded ge=0, le=30."""
        if should_pass:
            assert (
                CanaryInterlockSettings(jitter_max_seconds=value).jitter_max_seconds
                == value
            )
        else:
            with pytest.raises(ValidationError):
                CanaryInterlockSettings(jitter_max_seconds=value)

    @pytest.mark.parametrize(
        ("value", "should_pass"),
        [
            (0, False),  # below ge=1
            (1, True),  # at ge=1
            (10, True),  # at le=10
            (11, False),  # above le=10
        ],
        ids=["below_min", "at_min", "at_max", "above_max"],
    )
    def test_max_consecutive_failures_boundary(self, value, should_pass):
        """max_consecutive_failures is bounded ge=1, le=10."""
        if should_pass:
            assert (
                CanaryInterlockSettings(
                    max_consecutive_failures=value
                ).max_consecutive_failures
                == value
            )
        else:
            with pytest.raises(ValidationError):
                CanaryInterlockSettings(max_consecutive_failures=value)

    @pytest.mark.parametrize("action", ["log_and_continue", "fail_closed"])
    def test_on_refresh_failure_action_accepts_literals(self, action):
        """Both documented failure actions are accepted."""
        assert (
            CanaryInterlockSettings(
                on_refresh_failure_action=action
            ).on_refresh_failure_action
            == action
        )

    def test_on_refresh_failure_action_rejects_unknown(self):
        """An out-of-vocabulary failure action is rejected by the Literal."""
        with pytest.raises(ValidationError):
            CanaryInterlockSettings(on_refresh_failure_action="explode")

    def test_get_is_cached_singleton(self):
        """get_canary_interlock_settings() returns a cached instance."""
        reset_canary_interlock_settings()
        s1 = get_canary_interlock_settings()
        s2 = get_canary_interlock_settings()

        assert isinstance(s1, CanaryInterlockSettings)
        assert s1 is s2

    def test_reset_clears_cache(self):
        """reset_canary_interlock_settings() forces a fresh instance."""
        s1 = get_canary_interlock_settings()
        reset_canary_interlock_settings()
        s2 = get_canary_interlock_settings()

        assert s2 is not s1


# =============================================================================
# Contract: CanarySettings tier_map / default_tier validators
# =============================================================================


class TestCanaryTierSettingsContract:
    """CanarySettings.tier_map / default_tier defaults and validation."""

    def test_tier_defaults(self):
        """default_tier defaults to behavior-preserving 'standard'; tier_map empty."""
        settings = CanarySettings()

        assert settings.default_tier == "standard"
        assert settings.tier_map == {}

    @pytest.mark.parametrize("tier", ["critical", "standard", "non_essential"])
    def test_default_tier_accepts_known_tiers(self, tier):
        """Every known service tier is accepted for default_tier."""
        assert CanarySettings(default_tier=tier).default_tier == tier

    def test_default_tier_rejects_unknown(self):
        """An out-of-vocabulary default_tier is rejected."""
        with pytest.raises(ValidationError):
            CanarySettings(default_tier="platinum")

    def test_tier_map_accepts_known_tiers(self):
        """A tier_map mapping config_type -> known tier is accepted."""
        settings = CanarySettings(
            tier_map={"circuit_breaker": "critical", "cache": "non_essential"}
        )
        assert settings.tier_map["circuit_breaker"] == "critical"
        assert settings.tier_map["cache"] == "non_essential"

    def test_tier_map_rejects_unknown_tier(self):
        """A tier_map value outside the known vocabulary is rejected."""
        with pytest.raises(ValidationError):
            CanarySettings(tier_map={"circuit_breaker": "platinum"})


# =============================================================================
# Contract: CanarySettings.lock_timeout_minutes renewal-cadence warning (623 D11)
# =============================================================================


class TestLockTimeoutWarnBounds:
    """_warn_lock_timeout warns outside the [15, 60] band but still accepts any
    value within the ge=5 / le=120 field bounds (the warning is advisory)."""

    def test_below_renewal_cadence_margin_logs_warning(self):
        # 14 < warn_below(15): too short relative to the 5-min renewal cadence.
        with patch("baldur.settings.validators.logger") as mock_logger:
            settings = CanarySettings(lock_timeout_minutes=14)

        assert settings.lock_timeout_minutes == 14
        mock_logger.warning.assert_called_once_with(
            "canary_settings.low_below_renewal_cadence_margin",
            setting_value=14,
        )

    def test_at_renewal_cadence_margin_is_silent(self):
        # 15 is the boundary — warn_below uses strict `<`, so no warning.
        with patch("baldur.settings.validators.logger") as mock_logger:
            settings = CanarySettings(lock_timeout_minutes=15)

        assert settings.lock_timeout_minutes == 15
        mock_logger.warning.assert_not_called()

    def test_above_responsiveness_threshold_logs_warning(self):
        # 61 > warn_above(60): slow zombie-lock cleanup.
        with patch("baldur.settings.validators.logger") as mock_logger:
            settings = CanarySettings(lock_timeout_minutes=61)

        assert settings.lock_timeout_minutes == 61
        mock_logger.warning.assert_called_once_with(
            "canary_settings.high_consider_using_responsiveness",
            setting_value=61,
        )

    def test_in_band_default_is_silent(self):
        # The 30-min default sits inside [15, 60] — no warning either way.
        with patch("baldur.settings.validators.logger") as mock_logger:
            settings = CanarySettings(lock_timeout_minutes=30)

        assert settings.lock_timeout_minutes == 30
        mock_logger.warning.assert_not_called()
