"""RateLimitBackoffSettings — contract and prefix-split regression tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.rate_limit import RateLimitSettings
from baldur.settings.rate_limit_backoff import (
    RateLimitBackoffSettings,
    get_rate_limit_backoff_settings,
    reset_rate_limit_backoff_settings,
)

BACKOFF_FIELDS = {
    "base_delay",
    "max_delay",
    "jitter_percent",
    "default_retry_after",
    "backoff_multiplier",
}


@pytest.fixture
def _reset_backoff_settings():
    """Reset the backoff-settings singleton around each test."""
    reset_rate_limit_backoff_settings()
    yield
    reset_rate_limit_backoff_settings()


class TestRateLimitBackoffSettingsContract:
    """Design-contract values for the outbound 429-backoff family."""

    def test_env_prefix_is_rate_limit_backoff(self):
        assert (
            RateLimitBackoffSettings.model_config.get("env_prefix")
            == "BALDUR_RATE_LIMIT_BACKOFF_"
        )

    def test_defaults_match_previous_rate_limit_family(self):
        """Defaults carry over unchanged from the old BALDUR_RATE_LIMIT_ family."""
        s = RateLimitBackoffSettings()
        assert s.base_delay == 1.0
        assert s.max_delay == 60.0
        assert s.jitter_percent == 30.0
        assert s.default_retry_after == 5.0
        assert s.backoff_multiplier == 2.0

    def test_debounce_window_default_is_5_seconds(self):
        """Absorbed from the coordinator config's former inline default."""
        s = RateLimitBackoffSettings()
        assert s.debounce_window_seconds == 5.0

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("base_delay", 0.05),
            ("base_delay", 61.0),
            ("max_delay", 0.5),
            ("max_delay", 301.0),
            ("jitter_percent", -1.0),
            ("jitter_percent", 101.0),
            ("default_retry_after", 0.05),
            ("default_retry_after", 61.0),
            ("backoff_multiplier", 0.5),
            ("backoff_multiplier", 11.0),
            ("debounce_window_seconds", 0.05),
            ("debounce_window_seconds", 61.0),
        ],
    )
    def test_out_of_range_values_rejected(self, field, value):
        with pytest.raises(ValidationError):
            RateLimitBackoffSettings(**{field: value})

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("BALDUR_RATE_LIMIT_BACKOFF_BASE_DELAY", "2.5")
        s = RateLimitBackoffSettings()
        assert s.base_delay == 2.5


class TestRateLimitPrefixSplitContract:
    """The two families resolve under distinct prefixes with zero field overlap."""

    def test_backoff_fields_removed_from_rate_limit_settings(self):
        assert not (BACKOFF_FIELDS & set(RateLimitSettings.model_fields))

    def test_field_sets_are_disjoint(self):
        overlap = set(RateLimitSettings.model_fields) & set(
            RateLimitBackoffSettings.model_fields
        )
        assert overlap == set()

    def test_old_env_var_is_inert_after_the_split(self, monkeypatch):
        """BALDUR_RATE_LIMIT_BASE_DELAY neither errors nor lands anywhere (clean break)."""
        monkeypatch.setenv("BALDUR_RATE_LIMIT_BASE_DELAY", "9.0")

        quota = RateLimitSettings()
        backoff = RateLimitBackoffSettings()

        assert "base_delay" not in RateLimitSettings.model_fields
        assert quota.control_api_rate_limit == 100
        assert backoff.base_delay == 1.0


class TestRateLimitBackoffSingletonBehavior:
    """get/reset singleton lifecycle via the scaling settings node."""

    def test_get_returns_cached_instance(self, _reset_backoff_settings):
        first = get_rate_limit_backoff_settings()
        second = get_rate_limit_backoff_settings()
        assert first is second

    def test_reset_clears_cached_instance(self, _reset_backoff_settings):
        first = get_rate_limit_backoff_settings()
        reset_rate_limit_backoff_settings()
        second = get_rate_limit_backoff_settings()
        assert first is not second


class TestCoordinatorConfigFromSettingsBehavior:
    """RateLimitCoordinatorConfig.from_settings reads the backoff family."""

    def test_from_settings_reads_backoff_family(
        self, monkeypatch, _reset_backoff_settings
    ):
        from baldur.services.rate_limit_coordinator.models import (
            RateLimitCoordinatorConfig,
        )

        monkeypatch.setenv("BALDUR_RATE_LIMIT_BACKOFF_BASE_DELAY", "3.0")
        monkeypatch.setenv("BALDUR_RATE_LIMIT_BACKOFF_DEBOUNCE_WINDOW_SECONDS", "7.5")
        reset_rate_limit_backoff_settings()

        config = RateLimitCoordinatorConfig.from_settings()

        assert config.base_delay == 3.0
        assert config.debounce_window_seconds == 7.5
        assert config.max_delay == 60.0
