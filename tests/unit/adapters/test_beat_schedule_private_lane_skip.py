"""Unit tests for _load_schedule_module's per-cause log split.

The beat composition table lists lanes whose module lives in a private
distribution. On an install without that wheel those lanes are *expected*
absent — the lane mechanism was designed that way — so their ImportError is
normal flow and logs DEBUG. An ImportError on a first-party module path is a
real misconfiguration (broken install, typo in the table) and keeps WARNING,
as does a module that loads but has no getter.

Tests enumerate every documented exit of the function.
"""

from __future__ import annotations

import importlib
from unittest.mock import patch

import pytest

from baldur.adapters.celery import beat_schedule as beat_schedule_module
from baldur.adapters.celery.beat_schedule import (
    _PRIVATE_LANE_PREFIXES,
    _load_schedule_module,
)


class TestPrivateLanePrefixesContract:
    """Contract: the prefixes name the two private distributions."""

    def test_prefixes_are_the_private_distribution_roots(self):
        """Both private distribution roots are covered, dotted to avoid prefix bleed."""
        assert _PRIVATE_LANE_PREFIXES == ("baldur_pro.", "baldur_dormant.")

    @pytest.mark.parametrize("prefix", ["baldur_pro.", "baldur_dormant."])
    def test_prefix_is_dot_terminated(self, prefix):
        """The trailing dot keeps a same-stem first-party package from matching.

        Without it a hypothetical ``baldur_prometheus`` lane would be silently
        demoted to DEBUG and its broken install would stop being reported.
        """
        assert prefix.endswith(".")
        assert prefix in _PRIVATE_LANE_PREFIXES


class TestLoadScheduleModuleBehavior:
    """Behavior: each exit of _load_schedule_module, per the design inventory."""

    def test_successful_load_returns_the_lane_schedule(self):
        """Import + getter succeed → the getter's dict is returned."""
        schedule = _load_schedule_module(
            "baldur.celery_tasks.dlq_tasks",
            "get_dlq_maintenance_beat_schedule",
            "dlq maintenance",
        )

        assert isinstance(schedule, dict)
        assert "release-stale-replaying-entries" in schedule

    @pytest.mark.parametrize(
        "module_path",
        ["baldur_pro.tasks.saga_tasks", "baldur_dormant.tasks.kafka_tasks"],
        ids=["pro", "dormant"],
    )
    def test_absent_private_lane_logs_debug_and_returns_empty(self, module_path):
        """Private-distribution ImportError → DEBUG, empty dict, no WARNING."""
        with (
            patch.object(
                importlib,
                "import_module",
                side_effect=ImportError("No module named x"),
            ),
            patch.object(beat_schedule_module, "logger") as mock_logger,
        ):
            result = _load_schedule_module(
                module_path, "get_schedule", "a private lane"
            )

        assert result == {}
        mock_logger.warning.assert_not_called()
        mock_logger.debug.assert_called_once()
        assert (
            mock_logger.debug.call_args.args[0] == "beat_schedule.private_lane_skipped"
        )
        assert mock_logger.debug.call_args.kwargs["module_path"] == module_path

    def test_absent_first_party_lane_still_logs_warning(self):
        """ImportError on a baldur.* path is a real misconfiguration → WARNING."""
        with (
            patch.object(
                importlib,
                "import_module",
                side_effect=ImportError("No module named x"),
            ),
            patch.object(beat_schedule_module, "logger") as mock_logger,
        ):
            result = _load_schedule_module(
                "baldur.celery_tasks.typo_tasks", "get_schedule", "a first-party lane"
            )

        assert result == {}
        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args.args[0] == "beat_schedule.load_tasks"

    def test_missing_getter_logs_warning_regardless_of_path(self):
        """AttributeError is module/getter drift, never expected-absent → WARNING.

        Asserted on a *private* path so the split cannot be implemented as
        "private prefix → always DEBUG": a private lane whose wheel IS present
        but whose getter went missing is still drift.
        """
        with (
            patch.object(
                importlib,
                "import_module",
                return_value=object(),
            ),
            patch.object(beat_schedule_module, "logger") as mock_logger,
        ):
            result = _load_schedule_module(
                "baldur_pro.tasks.saga_tasks", "get_missing_getter", "a private lane"
            )

        assert result == {}
        mock_logger.debug.assert_not_called()
        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args.args[0] == "beat_schedule.getter_not_found"

    def test_unexpected_exception_propagates(self):
        """A getter bug is not swallowed — only ImportError/AttributeError are.

        Silently returning {} here would drop a lane from the schedule with no
        trace; the composition crash is the intended fail-loud behavior.
        """
        with (
            patch.object(
                importlib,
                "import_module",
                side_effect=ValueError("getter blew up"),
            ),
            pytest.raises(ValueError, match="getter blew up"),
        ):
            _load_schedule_module("baldur_pro.tasks.saga_tasks", "get_schedule", "lane")
