"""``ReplayAutomationConfigSerializer`` bounds for the on-recovery dials.

The console writes these two numbers into runtime config, and the sweep reads
them at the moment a circuit closes. Two things make the bounds worth pinning
rather than trusting:

- ``on_recovery_max_items`` no longer means "how much one recovery drains" —
  it bounds one *pass* of a chain, and the per-recovery axis moved to the new
  ``on_recovery_max_continuations``. An operator who reads the old meaning
  into the old field is now sizing the wrong thing;
- the serializer bound and the settings bound are two separate declarations of
  the same constraint, so they can drift: a console value the serializer
  accepts but the settings model rejects fails at load, after the write.
"""

import annotated_types
import django
import pytest
from django.conf import settings

if not settings.configured:
    settings.configure(
        DEBUG=True,
        DATABASES={},
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "rest_framework",
        ],
        REST_FRAMEWORK={},
        SECRET_KEY="test-secret-key",
    )
    django.setup()

from baldur.api.django.serializers.config.storage_configs import (
    ReplayAutomationConfigSerializer,
)
from baldur.settings.replay_automation import ReplayAutomationSettings


def _settings_bounds(field_name: str) -> tuple[int | None, int | None]:
    metadata = ReplayAutomationSettings.model_fields[field_name].metadata
    minimum = next((m.ge for m in metadata if isinstance(m, annotated_types.Ge)), None)
    maximum = next((m.le for m in metadata if isinstance(m, annotated_types.Le)), None)
    return minimum, maximum


class TestReplayAutomationConfigSerializerContract:
    """The on-recovery dials the console may write."""

    def test_the_continuation_bound_is_console_editable(self):
        """An operator whose chain is stopping short has no other dial."""
        assert (
            "on_recovery_max_continuations"
            in ReplayAutomationConfigSerializer().get_fields()
        )

    def test_continuation_bound_declares_one_to_one_thousand(self):
        field = ReplayAutomationConfigSerializer().get_fields()[
            "on_recovery_max_continuations"
        ]

        assert field.min_value == 1
        assert field.max_value == 1000
        assert field.required is False

    def test_continuation_bound_matches_the_settings_declaration(self):
        """Two declarations of one constraint; a drift is only visible at
        load time, after the console has already written the value."""
        field = ReplayAutomationConfigSerializer().get_fields()[
            "on_recovery_max_continuations"
        ]

        assert (field.min_value, field.max_value) == _settings_bounds(
            "on_recovery_max_continuations"
        )

    @pytest.mark.parametrize("value", [0, 1001])
    def test_out_of_range_continuation_counts_are_rejected(self, value):
        serializer = ReplayAutomationConfigSerializer(
            data={"on_recovery_max_continuations": value}
        )

        assert serializer.is_valid() is False
        assert "on_recovery_max_continuations" in serializer.errors

    @pytest.mark.parametrize("value", [1, 1000])
    def test_bounds_are_inclusive(self, value):
        serializer = ReplayAutomationConfigSerializer(
            data={"on_recovery_max_continuations": value}
        )

        assert serializer.is_valid() is True

    def test_the_per_pass_budget_keeps_its_own_bound(self):
        """It is a per-pass number now, so its ceiling is unrelated to the
        number of passes a recovery may run."""
        field = ReplayAutomationConfigSerializer().get_fields()["on_recovery_max_items"]

        assert field.min_value == 1
        assert field.max_value == 500

    def test_the_two_dials_are_independent(self):
        serializer = ReplayAutomationConfigSerializer(
            data={
                "on_recovery_max_items": 25,
                "on_recovery_max_continuations": 4,
            }
        )

        assert serializer.is_valid() is True
        assert serializer.validated_data["on_recovery_max_items"] == 25
        assert serializer.validated_data["on_recovery_max_continuations"] == 4
