"""
BaseCircuitBreakerStateAdmin unit tests.

Covers the Django admin base class shipped with baldur: its display
methods and its configuration.
"""

import os
from unittest.mock import MagicMock

import pytest

# Django settings (required in the test environment)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")

import django

django.setup()

from django.db import models

from baldur.adapters.django.admin import BaseCircuitBreakerStateAdmin

# =============================================================================
# Mock model for these tests (defined once, at module level)
# =============================================================================


class MockCircuitBreakerState(models.Model):
    """Stand-in CircuitBreakerState model."""

    STATE_CHOICES = [
        ("closed", "Closed"),
        ("open", "Open"),
        ("half_open", "Half Open"),
    ]

    service_name = models.CharField(max_length=255)
    state = models.CharField(max_length=20, choices=STATE_CHOICES, default="closed")
    failure_count = models.IntegerField(default=0)
    success_count = models.IntegerField(default=0)
    manually_controlled = models.BooleanField(default=False)
    controlled_by_id = models.IntegerField(null=True, blank=True)
    control_reason = models.TextField(blank=True, default="")
    last_failure_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "baldur_test"
        managed = False  # No database table is created


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def admin_class():
    """The admin class itself."""
    return BaseCircuitBreakerStateAdmin


@pytest.fixture
def admin_instance():
    """An admin instance bound to the stand-in model."""
    from django.contrib.admin.sites import AdminSite

    site = AdminSite()
    return BaseCircuitBreakerStateAdmin(model=MockCircuitBreakerState, admin_site=site)


# =============================================================================
# Display Methods Tests
# =============================================================================


class TestBaseCircuitBreakerAdminStateDisplay:
    """state_display()."""

    def test_state_display_closed(self, admin_instance):
        """closed renders green."""
        obj = MagicMock()
        obj.state = "closed"
        obj.get_state_display.return_value = "Closed"

        result = admin_instance.state_display(obj)
        assert "green" in result
        assert "Closed" in result

    def test_state_display_open(self, admin_instance):
        """open renders red."""
        obj = MagicMock()
        obj.state = "open"
        obj.get_state_display.return_value = "Open"

        result = admin_instance.state_display(obj)
        assert "red" in result
        assert "Open" in result

    def test_state_display_half_open(self, admin_instance):
        """half_open renders orange."""
        obj = MagicMock()
        obj.state = "half_open"
        obj.get_state_display.return_value = "Half Open"

        result = admin_instance.state_display(obj)
        assert "orange" in result
        assert "Half Open" in result


class TestBaseCircuitBreakerAdminManualControlDisplay:
    """manually_controlled_display()."""

    def test_manually_controlled_true(self, admin_instance):
        """A pinned breaker renders as Manual."""
        obj = MagicMock()
        obj.manually_controlled = True

        result = admin_instance.manually_controlled_display(obj)
        assert "Manual" in result
        assert "blue" in result

    def test_manually_controlled_false(self, admin_instance):
        """An unpinned breaker renders as Auto."""
        obj = MagicMock()
        obj.manually_controlled = False

        result = admin_instance.manually_controlled_display(obj)
        assert "Auto" in result
        assert "gray" in result


# =============================================================================
# Configuration Tests
# =============================================================================


class TestBaseCircuitBreakerAdminConfiguration:
    """Admin configuration."""

    def test_list_display_fields(self, admin_class):
        """The list_display columns."""
        expected_fields = [
            "service_name",
            "state_display",
            "failure_count",
            "success_count",
            "manually_controlled_display",
            "controlled_by_id",
            "opened_at",
            "updated_at",
        ]
        assert admin_class.list_display == expected_fields

    def test_list_filter_fields(self, admin_class):
        """The list_filter entries."""
        expected_filters = [
            "state",
            "manually_controlled",
            "created_at",
        ]
        assert admin_class.list_filter == expected_filters

    def test_search_fields(self, admin_class):
        """The search_fields entries."""
        assert "service_name" in admin_class.search_fields
        assert "control_reason" in admin_class.search_fields

    def test_ordering_descending(self, admin_class):
        """Ordered newest first."""
        assert admin_class.ordering == ["-updated_at"]

    def test_actions_available(self, admin_class):
        """The registered admin actions."""
        expected_actions = [
            "force_open_selected",
            "force_close_selected",
            "force_close_with_replay",
            "reset_selected",
        ]
        assert admin_class.actions == expected_actions

    def test_readonly_fields(self, admin_class):
        """The readonly_fields entries."""
        expected_readonly = [
            "failure_count",
            "success_count",
            "last_failure_at",
            "opened_at",
            "created_at",
            "updated_at",
            # Editable on the change form until 741: ticking it pinned a
            # breaker with no lifetime, which nothing lifts automatically.
            "manually_controlled",
        ]
        assert admin_class.readonly_fields == expected_readonly

    def test_fieldsets_structure(self, admin_class):
        """The fieldsets structure."""
        fieldsets = admin_class.fieldsets

        section_names = [fs[0] for fs in fieldsets]
        assert "Service Information" in section_names
        assert "Counters" in section_names
        assert "Manual Control" in section_names
        assert "Timestamps" in section_names

    def test_manual_control_flag_is_readonly_yet_still_displayed(self, admin_class):
        """741 D13 — visible but not settable from the change form.

        Ticking the box by hand pinned a breaker with no lifetime, and nothing
        lifts such a pin on its own. Hiding the field instead would have taken
        away the only place an admin can see that a breaker is pinned, so it
        stays in the fieldset and becomes read-only.
        """
        manual_control_fields = next(
            options["fields"]
            for name, options in admin_class.fieldsets
            if name == "Manual Control"
        )

        assert "manually_controlled" in admin_class.readonly_fields
        assert "manually_controlled" in manual_control_fields

    def test_service_routed_release_actions_replace_the_checkbox(self, admin_class):
        """The release paths the read-only flag hands the lifecycle to.

        Each routes through the circuit breaker service, so every pin they
        create or clear carries a lifetime.
        """
        for action in (
            "force_open_selected",
            "force_close_selected",
            "force_close_with_replay",
            "reset_selected",
        ):
            assert action in admin_class.actions
            assert callable(getattr(admin_class, action))
