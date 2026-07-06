"""
Unit tests for new settings fields added by 443 (D3, D4).

Covers:
- CleanupSettings.approval_record_retention_days default and boundaries
- DLQSettings.stale_replaying_timeout_minutes default and boundaries
- GovernanceSettings: four_eyes_expiry_hours removed
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


class TestApprovalRecordRetentionDaysContract:
    """Contract: approval_record_retention_days design values."""

    def test_default_is_7(self):
        """Default approval_record_retention_days is 7 days."""
        from baldur.settings.cleanup import CleanupSettings

        settings = CleanupSettings()
        assert settings.approval_record_retention_days == 7

    @pytest.mark.parametrize(
        ("value", "should_pass"),
        [
            (0, False),
            (1, True),
            (90, True),
            (91, False),
        ],
        ids=["below_min", "at_min", "at_max", "above_max"],
    )
    def test_boundary_values(self, value, should_pass):
        """approval_record_retention_days boundary: ge=1, le=90."""
        from baldur.settings.cleanup import CleanupSettings

        if should_pass:
            settings = CleanupSettings(approval_record_retention_days=value)
            assert settings.approval_record_retention_days == value
        else:
            with pytest.raises(ValidationError):
                CleanupSettings(approval_record_retention_days=value)


class TestStaleReplayingTimeoutMinutesContract:
    """Contract: stale_replaying_timeout_minutes design values."""

    def test_default_is_30(self):
        """Default stale_replaying_timeout_minutes is 30."""
        from baldur.settings.dlq import DLQSettings

        settings = DLQSettings()
        assert settings.stale_replaying_timeout_minutes == 30

    @pytest.mark.parametrize(
        ("value", "should_pass"),
        [
            (4, False),
            (5, True),
            (1440, True),
            (1441, False),
        ],
        ids=["below_min", "at_min", "at_max", "above_max"],
    )
    def test_boundary_values(self, value, should_pass):
        """stale_replaying_timeout_minutes boundary: ge=5, le=1440."""
        from baldur.settings.dlq import DLQSettings

        if should_pass:
            settings = DLQSettings(stale_replaying_timeout_minutes=value)
            assert settings.stale_replaying_timeout_minutes == value
        else:
            with pytest.raises(ValidationError):
                DLQSettings(stale_replaying_timeout_minutes=value)


class TestFourEyesExpiryHoursRemovedContract:
    """Contract: four_eyes_expiry_hours field has been removed (443 D2)."""

    def test_field_not_in_governance_settings(self):
        """GovernanceSettings no longer has four_eyes_expiry_hours field."""
        from baldur.settings.governance import GovernanceSettings

        assert "four_eyes_expiry_hours" not in GovernanceSettings.model_fields


# =============================================================================
# 484 D5: cb_stale_key_* settings (CB stale-key cleanup task tunables)
# =============================================================================


class TestCBStaleKeyRetentionDaysContract:
    """484 D5: ``cb_stale_key_retention_days`` default + boundary."""

    def test_default_is_30(self):
        """Default retention is 30 days (matches DLQ archive cadence)."""
        from baldur.settings.cleanup import CleanupSettings

        assert CleanupSettings().cb_stale_key_retention_days == 30

    @pytest.mark.parametrize(
        ("value", "should_pass"),
        [
            (0, False),
            (1, True),
            (30, True),
            (365, True),
            (366, False),
        ],
        ids=["below_min", "at_min", "default", "at_max", "above_max"],
    )
    def test_boundary_values(self, value, should_pass):
        """``cb_stale_key_retention_days`` boundary: ge=1, le=365."""
        from baldur.settings.cleanup import CleanupSettings

        if should_pass:
            settings = CleanupSettings(cb_stale_key_retention_days=value)
            assert settings.cb_stale_key_retention_days == value
        else:
            with pytest.raises(ValidationError):
                CleanupSettings(cb_stale_key_retention_days=value)


class TestCBStaleKeyMaxRetriesContract:
    """484 D5: ``cb_stale_key_max_retries`` default + boundary."""

    def test_default_is_2(self):
        """Default Celery retry count is 2 (idempotent cleanup → cheap retry)."""
        from baldur.settings.cleanup import CleanupSettings

        assert CleanupSettings().cb_stale_key_max_retries == 2

    @pytest.mark.parametrize(
        ("value", "should_pass"),
        [
            (-1, False),
            (0, True),
            (2, True),
            (10, True),
            (11, False),
        ],
        ids=["below_min", "at_min", "default", "at_max", "above_max"],
    )
    def test_boundary_values(self, value, should_pass):
        """``cb_stale_key_max_retries`` boundary: ge=0, le=10."""
        from baldur.settings.cleanup import CleanupSettings

        if should_pass:
            settings = CleanupSettings(cb_stale_key_max_retries=value)
            assert settings.cb_stale_key_max_retries == value
        else:
            with pytest.raises(ValidationError):
                CleanupSettings(cb_stale_key_max_retries=value)


class TestCBStaleKeyRetryDelayContract:
    """484 D5: ``cb_stale_key_retry_delay`` default + boundary."""

    def test_default_is_300(self):
        """Default retry delay is 300s (5 minutes — backs off across CB outages)."""
        from baldur.settings.cleanup import CleanupSettings

        assert CleanupSettings().cb_stale_key_retry_delay == 300

    @pytest.mark.parametrize(
        ("value", "should_pass"),
        [
            (9, False),
            (10, True),
            (300, True),
            (1800, True),
            (1801, False),
        ],
        ids=["below_min", "at_min", "default", "at_max", "above_max"],
    )
    def test_boundary_values(self, value, should_pass):
        """``cb_stale_key_retry_delay`` boundary: ge=10, le=1800."""
        from baldur.settings.cleanup import CleanupSettings

        if should_pass:
            settings = CleanupSettings(cb_stale_key_retry_delay=value)
            assert settings.cb_stale_key_retry_delay == value
        else:
            with pytest.raises(ValidationError):
                CleanupSettings(cb_stale_key_retry_delay=value)
