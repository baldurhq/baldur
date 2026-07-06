"""
Unit tests for baldur.settings.validators.

Tests warn_above and warn_below validator factory functions:
boundary behavior, side effects (logging), value passthrough, extra_fields.
"""

from unittest.mock import patch

from baldur.settings.validators import warn_above, warn_below

# =========================================================================
# Behavior: warn_above
# =========================================================================


class TestWarnAboveBehavior:
    """warn_above() validator factory behavior verification."""

    def test_above_threshold_returns_value_unchanged(self):
        """Value above threshold is returned unchanged."""
        check = warn_above(50, "test.event")
        result = check(100)
        assert result == 100

    def test_at_threshold_returns_value_without_warning(self):
        """Value exactly at threshold returns value without triggering warning."""
        with patch(
            # structlog BoundLogger is a dynamic proxy — autospec incompatible
            "baldur.settings.validators.logger"
        ) as mock_logger:
            check = warn_above(50, "test.event")
            result = check(50)

        assert result == 50
        mock_logger.warning.assert_not_called()

    def test_below_threshold_returns_value_without_warning(self):
        """Value below threshold returns value without triggering warning."""
        with patch(
            # structlog BoundLogger is a dynamic proxy — autospec incompatible
            "baldur.settings.validators.logger"
        ) as mock_logger:
            check = warn_above(50, "test.event")
            result = check(10)

        assert result == 10
        mock_logger.warning.assert_not_called()

    def test_above_threshold_logs_warning_with_event(self):
        """Value above threshold logs warning with correct event name."""
        with patch(
            # structlog BoundLogger is a dynamic proxy — autospec incompatible
            "baldur.settings.validators.logger"
        ) as mock_logger:
            check = warn_above(50, "safe_default.high_consider_using_safety")
            check(51)

        mock_logger.warning.assert_called_once_with(
            "safe_default.high_consider_using_safety",
            setting_value=51,
        )

    def test_above_threshold_includes_extra_fields_in_log(self):
        """Extra fields are included in the warning log."""
        with patch(
            # structlog BoundLogger is a dynamic proxy — autospec incompatible
            "baldur.settings.validators.logger"
        ) as mock_logger:
            check = warn_above(
                10,
                "test.event",
                extra_fields={"component": "retry", "tier": "critical"},
            )
            check(20)

        mock_logger.warning.assert_called_once_with(
            "test.event",
            setting_value=20,
            component="retry",
            tier="critical",
        )

    def test_above_threshold_without_extra_fields_omits_them(self):
        """When extra_fields is None, only setting_value is logged."""
        with patch(
            # structlog BoundLogger is a dynamic proxy — autospec incompatible
            "baldur.settings.validators.logger"
        ) as mock_logger:
            check = warn_above(5, "test.event")
            check(10)

        mock_logger.warning.assert_called_once_with(
            "test.event",
            setting_value=10,
        )

    def test_float_threshold_boundary_at_threshold_no_warning(self):
        """Float value exactly at threshold does not trigger warning."""
        with patch(
            # structlog BoundLogger is a dynamic proxy — autospec incompatible
            "baldur.settings.validators.logger"
        ) as mock_logger:
            check = warn_above(0.5, "test.event")
            result = check(0.5)

        assert result == 0.5
        mock_logger.warning.assert_not_called()

    def test_float_threshold_boundary_just_above_warns(self):
        """Float value just above threshold triggers warning."""
        with patch(
            # structlog BoundLogger is a dynamic proxy — autospec incompatible
            "baldur.settings.validators.logger"
        ) as mock_logger:
            check = warn_above(0.5, "test.event")
            check(0.50001)

        mock_logger.warning.assert_called_once()

    def test_returns_original_value_type_int(self):
        """Integer input returns integer output."""
        check = warn_above(10, "test.event")
        result = check(5)
        assert result == 5
        assert isinstance(result, int)

    def test_returns_original_value_type_float(self):
        """Float input returns float output."""
        check = warn_above(1.0, "test.event")
        result = check(0.5)
        assert result == 0.5
        assert isinstance(result, float)


# =========================================================================
# Behavior: warn_below
# =========================================================================


class TestWarnBelowBehavior:
    """warn_below() validator factory behavior verification."""

    def test_below_threshold_returns_value_unchanged(self):
        """Value below threshold is returned unchanged."""
        check = warn_below(10, "test.event")
        result = check(5)
        assert result == 5

    def test_at_threshold_returns_value_without_warning(self):
        """Value exactly at threshold returns value without triggering warning."""
        with patch(
            # structlog BoundLogger is a dynamic proxy — autospec incompatible
            "baldur.settings.validators.logger"
        ) as mock_logger:
            check = warn_below(10, "test.event")
            result = check(10)

        assert result == 10
        mock_logger.warning.assert_not_called()

    def test_above_threshold_returns_value_without_warning(self):
        """Value above threshold returns value without triggering warning."""
        with patch(
            # structlog BoundLogger is a dynamic proxy — autospec incompatible
            "baldur.settings.validators.logger"
        ) as mock_logger:
            check = warn_below(10, "test.event")
            result = check(100)

        assert result == 100
        mock_logger.warning.assert_not_called()

    def test_below_threshold_logs_warning_with_event(self):
        """Value below threshold logs warning with correct event name."""
        with patch(
            # structlog BoundLogger is a dynamic proxy — autospec incompatible
            "baldur.settings.validators.logger"
        ) as mock_logger:
            check = warn_below(10, "audit_sync.sync_interval_too_short")
            check(9)

        mock_logger.warning.assert_called_once_with(
            "audit_sync.sync_interval_too_short",
            setting_value=9,
        )

    def test_below_threshold_includes_extra_fields_in_log(self):
        """Extra fields are included in the warning log."""
        with patch(
            # structlog BoundLogger is a dynamic proxy — autospec incompatible
            "baldur.settings.validators.logger"
        ) as mock_logger:
            check = warn_below(
                5.0,
                "test.event",
                extra_fields={"recommended_min": 5.0},
            )
            check(2.0)

        mock_logger.warning.assert_called_once_with(
            "test.event",
            setting_value=2.0,
            recommended_min=5.0,
        )

    def test_float_threshold_boundary_at_threshold_no_warning(self):
        """Float value exactly at threshold does not trigger warning."""
        with patch(
            # structlog BoundLogger is a dynamic proxy — autospec incompatible
            "baldur.settings.validators.logger"
        ) as mock_logger:
            check = warn_below(0.5, "test.event")
            result = check(0.5)

        assert result == 0.5
        mock_logger.warning.assert_not_called()

    def test_float_threshold_boundary_just_below_warns(self):
        """Float value just below threshold triggers warning."""
        with patch(
            # structlog BoundLogger is a dynamic proxy — autospec incompatible
            "baldur.settings.validators.logger"
        ) as mock_logger:
            check = warn_below(0.5, "test.event")
            check(0.49999)

        mock_logger.warning.assert_called_once()

    def test_returns_original_value_type_int(self):
        """Integer input returns integer output."""
        check = warn_below(10, "test.event")
        result = check(20)
        assert result == 20
        assert isinstance(result, int)

    def test_returns_original_value_type_float(self):
        """Float input returns float output."""
        check = warn_below(1.0, "test.event")
        result = check(2.0)
        assert result == 2.0
        assert isinstance(result, float)


# =========================================================================
# Behavior: __all__ exports
# =========================================================================


class TestValidatorsExportsContract:
    """validators module __all__ verification."""

    def test_all_contains_warn_above_and_warn_below(self):
        """__all__ exports both factory functions."""
        import baldur.settings.validators as v

        assert "warn_above" in v.__all__
        assert "warn_below" in v.__all__

    def test_all_has_exactly_two_entries(self):
        """__all__ has exactly 2 entries."""
        import baldur.settings.validators as v

        assert len(v.__all__) == 2
