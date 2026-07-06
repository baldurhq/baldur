"""
Unit tests for SagaSettings.

Covers:
- Design contract values (defaults, field count, env_prefix)
- Environment variable overrides
- Model validator (heartbeat vs stale threshold ratio)
- Boundary analysis (ge constraints, Literal backoff strategy)
- Singleton caching / reset

Target: baldur.settings.saga
Reference: 338_SETTINGS_GAP_EMERGENCY_SAGA_LEARNING.md §6
"""

import os
from unittest import mock

import pytest
from pydantic import ValidationError


class TestSagaSettingsContract:
    """SagaSettings design contract verification.

    Validates default values and structural contracts specified in
    338_SETTINGS_GAP_EMERGENCY_SAGA_LEARNING.md §6.1.
    """

    def test_field_count(self):
        """SagaSettings has exactly 12 fields (backpressure_rejection_level added, 409 UU-E4)."""
        from baldur.settings.saga import SagaSettings

        assert len(SagaSettings.model_fields) == 12

    def test_env_prefix_is_baldur_saga(self):
        """env_prefix contract: BALDUR_SAGA_."""
        from baldur.settings.saga import SagaSettings

        assert SagaSettings.model_config["env_prefix"] == "BALDUR_SAGA_"

    def test_lock_heartbeat_interval_seconds_default(self):
        """Default lock_heartbeat_interval_seconds is 60."""
        from baldur.settings.saga import SagaSettings, reset_saga_settings

        reset_saga_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = SagaSettings()
            assert settings.lock_heartbeat_interval_seconds == 60

    def test_lock_extend_seconds_default(self):
        """Default lock_extend_seconds is 300."""
        from baldur.settings.saga import SagaSettings, reset_saga_settings

        reset_saga_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = SagaSettings()
            assert settings.lock_extend_seconds == 300

    def test_max_resume_count_default(self):
        """Default max_resume_count is 10."""
        from baldur.settings.saga import SagaSettings, reset_saga_settings

        reset_saga_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = SagaSettings()
            assert settings.max_resume_count == 10

    def test_stale_threshold_seconds_default(self):
        """Default stale_threshold_seconds is 300."""
        from baldur.settings.saga import SagaSettings, reset_saga_settings

        reset_saga_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = SagaSettings()
            assert settings.stale_threshold_seconds == 300

    def test_default_timeout_seconds_default(self):
        """Default default_timeout_seconds is 600."""
        from baldur.settings.saga import SagaSettings, reset_saga_settings

        reset_saga_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = SagaSettings()
            assert settings.default_timeout_seconds == 600

    def test_default_max_retries_per_step_default(self):
        """Default default_max_retries_per_step is 2."""
        from baldur.settings.saga import SagaSettings, reset_saga_settings

        reset_saga_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = SagaSettings()
            assert settings.default_max_retries_per_step == 2

    def test_default_retry_backoff_strategy_default(self):
        """Default default_retry_backoff_strategy is 'exponential'."""
        from baldur.settings.saga import SagaSettings, reset_saga_settings

        reset_saga_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = SagaSettings()
            assert settings.default_retry_backoff_strategy == "exponential"

    def test_orphan_scan_interval_seconds_default(self):
        """Default orphan_scan_interval_seconds is 120.0."""
        from baldur.settings.saga import SagaSettings, reset_saga_settings

        reset_saga_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = SagaSettings()
            assert settings.orphan_scan_interval_seconds == 120.0

    def test_resume_task_max_retries_default(self):
        """Default resume_task_max_retries is 3."""
        from baldur.settings.saga import SagaSettings, reset_saga_settings

        reset_saga_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = SagaSettings()
            assert settings.resume_task_max_retries == 3

    def test_resume_task_retry_delay_seconds_default(self):
        """Default resume_task_retry_delay_seconds is 60."""
        from baldur.settings.saga import SagaSettings, reset_saga_settings

        reset_saga_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = SagaSettings()
            assert settings.resume_task_retry_delay_seconds == 60


class TestSagaSettingsBehavior:
    """SagaSettings behavior verification."""

    # === Environment Variable Override ===

    def test_env_override_lock_heartbeat(self):
        """BALDUR_SAGA_LOCK_HEARTBEAT_INTERVAL_SECONDS=30 overrides to 30."""
        from baldur.settings.saga import SagaSettings, reset_saga_settings

        reset_saga_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_SAGA_LOCK_HEARTBEAT_INTERVAL_SECONDS": "30",
                # stale must be >= 3x heartbeat (30*3=90), default 300 is fine
            },
            clear=True,
        ):
            settings = SagaSettings()
            assert settings.lock_heartbeat_interval_seconds == 30

    def test_env_override_stale_threshold(self):
        """BALDUR_SAGA_STALE_THRESHOLD_SECONDS overrides via env."""
        from baldur.settings.saga import SagaSettings, reset_saga_settings

        reset_saga_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_SAGA_STALE_THRESHOLD_SECONDS": "600",
            },
            clear=True,
        ):
            settings = SagaSettings()
            assert settings.stale_threshold_seconds == 600

    def test_env_override_backoff_strategy(self):
        """BALDUR_SAGA_DEFAULT_RETRY_BACKOFF_STRATEGY='linear' overrides strategy."""
        from baldur.settings.saga import SagaSettings, reset_saga_settings

        reset_saga_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_SAGA_DEFAULT_RETRY_BACKOFF_STRATEGY": "linear",
            },
            clear=True,
        ):
            settings = SagaSettings()
            assert settings.default_retry_backoff_strategy == "linear"

    # === Boundary / Validation: heartbeat vs stale threshold ===

    def test_heartbeat_gte_stale_raises_validation_error(self):
        """heartbeat >= stale_threshold raises ValueError."""
        from baldur.settings.saga import SagaSettings, reset_saga_settings

        reset_saga_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_SAGA_LOCK_HEARTBEAT_INTERVAL_SECONDS": "300",
                "BALDUR_SAGA_STALE_THRESHOLD_SECONDS": "300",
            },
            clear=True,
        ):
            with pytest.raises(
                ValidationError, match="must be less than stale_threshold_seconds"
            ):
                SagaSettings()

    def test_stale_less_than_3x_heartbeat_raises_validation_error(self):
        """stale_threshold < 3x heartbeat raises ValueError."""
        from baldur.settings.saga import SagaSettings, reset_saga_settings

        reset_saga_settings()
        # heartbeat=100, stale=200 → 200 < 100*3=300 → violation
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_SAGA_LOCK_HEARTBEAT_INTERVAL_SECONDS": "100",
                "BALDUR_SAGA_STALE_THRESHOLD_SECONDS": "200",
            },
            clear=True,
        ):
            with pytest.raises(ValidationError, match="should be at least"):
                SagaSettings()

    def test_heartbeat_below_minimum_raises_validation_error(self):
        """lock_heartbeat_interval_seconds below ge=5 raises ValidationError."""
        from baldur.settings.saga import SagaSettings, reset_saga_settings

        reset_saga_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_SAGA_LOCK_HEARTBEAT_INTERVAL_SECONDS": "4",
            },
            clear=True,
        ):
            with pytest.raises(ValidationError):
                SagaSettings()

    # === Boundary / Validation: backoff strategy Literal ===

    def test_invalid_backoff_strategy_raises_validation_error(self):
        """Invalid backoff strategy 'invalid_strategy' raises ValidationError."""
        from baldur.settings.saga import SagaSettings, reset_saga_settings

        reset_saga_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_SAGA_DEFAULT_RETRY_BACKOFF_STRATEGY": "invalid_strategy",
            },
            clear=True,
        ):
            with pytest.raises(ValidationError):
                SagaSettings()

    @pytest.mark.parametrize(
        "strategy",
        ["exponential", "linear", "constant", "decorrelated"],
    )
    def test_valid_backoff_strategies_accepted(self, strategy: str):
        """All 4 valid backoff strategies are accepted."""
        from baldur.settings.saga import SagaSettings, reset_saga_settings

        reset_saga_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_SAGA_DEFAULT_RETRY_BACKOFF_STRATEGY": strategy,
            },
            clear=True,
        ):
            settings = SagaSettings()
            assert settings.default_retry_backoff_strategy == strategy

    # === Singleton ===

    def test_singleton_get_returns_via_root_config(self):
        """get_saga_settings() returns the same cached instance on repeated calls."""
        from baldur.settings.saga import get_saga_settings, reset_saga_settings

        reset_saga_settings()
        first = get_saga_settings()
        second = get_saga_settings()
        assert first is second

    def test_singleton_reset_clears_cached_instance(self):
        """reset_saga_settings() clears the cache so next get returns a new instance."""
        from baldur.settings.saga import get_saga_settings, reset_saga_settings

        reset_saga_settings()
        first = get_saga_settings()
        reset_saga_settings()
        second = get_saga_settings()
        assert first is not second
