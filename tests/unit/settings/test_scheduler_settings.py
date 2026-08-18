"""Unit tests for SchedulerSettings — the default scheduler's operator knobs (759 D4).

Two knobs govern the in-process scheduler ``baldur.init()`` starts:

- ``autostart`` — all-or-nothing. It absorbs a raw-env read that used to live in
  ``bootstrap._start_default_scheduler``, so its parse table is a compatibility
  surface: no value that disabled the scheduler before may start it now.
- ``disabled_jobs`` — the targeted form, a comma-separated job-name list.

The two fields degrade independently. That is the point of the ``autostart``
validator coercing rather than raising: a raise makes the whole model
unconstructable, taking the operator's ``disabled_jobs`` list down with the typo.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from baldur.settings.scheduler import (
    SchedulerSettings,
    get_scheduler_settings,
    reset_scheduler_settings,
)

AUTOSTART_ENV = "BALDUR_SCHEDULER_AUTOSTART"
DISABLED_JOBS_ENV = "BALDUR_SCHEDULER_DISABLED_JOBS"

# The suite-wide conftest sets AUTOSTART=0 for the whole test process, so every
# default-value assertion has to construct against a cleared environment.
_CLEARED_ENV: dict[str, str] = {}


class TestSchedulerSettingsContract:
    """SchedulerSettings design contract — defaults, env names, singleton pair."""

    def test_autostart_defaults_to_true(self):
        """Out of the box init() registers the default jobs and starts them."""
        with mock.patch.dict(os.environ, _CLEARED_ENV, clear=True):
            assert SchedulerSettings().autostart is True

    def test_disabled_jobs_defaults_to_empty_string(self):
        """No job is disabled unless an operator names one."""
        with mock.patch.dict(os.environ, _CLEARED_ENV, clear=True):
            assert SchedulerSettings().disabled_jobs == ""

    def test_default_disabled_jobs_parses_to_no_names(self):
        """The default parses to an empty tuple, not to one empty name.

        A naive ``"".split(",")`` yields ``[""]``, which the caller's
        unknown-name check would report on every boot of every deployment that
        never set the variable.
        """
        with mock.patch.dict(os.environ, _CLEARED_ENV, clear=True):
            assert SchedulerSettings().get_disabled_job_names() == ()

    def test_both_fields_are_configured_by_their_env_vars(self):
        """BALDUR_SCHEDULER_AUTOSTART / _DISABLED_JOBS reach their fields.

        The env-var names are the operator's whole interface to both knobs — a
        renamed prefix would leave two documented variables configuring nothing.
        """
        env = {AUTOSTART_ENV: "0", DISABLED_JOBS_ENV: "config_apply"}
        with mock.patch.dict(os.environ, env, clear=True):
            settings = SchedulerSettings()

        assert settings.autostart is False
        assert settings.disabled_jobs == "config_apply"

    def test_get_scheduler_settings_returns_the_cached_instance(self):
        """Repeated gets share one instance — env is read once per runtime."""
        assert get_scheduler_settings() is get_scheduler_settings()

    def test_reset_scheduler_settings_clears_the_cached_instance(self):
        """reset_*() drops the cache so the next get re-reads the environment."""
        first = get_scheduler_settings()

        reset_scheduler_settings()

        assert get_scheduler_settings() is not first


class TestSchedulerAutostartCoercionBehavior:
    """The autostart validator parses an operator-typed value without raising."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0", False),
            ("0 ", False),
            (" 0", False),
            ("false", False),
            ("no", False),
            ("off", False),
            ("f", False),
            ("n", False),
            ("1", True),
            ("TRUE", True),
            ("true", True),
            ("yes", True),
            ("on", True),
            ("t", True),
            ("y", True),
            (" ", True),
            ("", True),
        ],
        ids=[
            "zero",
            "zero_trailing_space",
            "zero_leading_space",
            "false",
            "no",
            "off",
            "f",
            "n",
            "one",
            "true_uppercase",
            "true",
            "yes",
            "on",
            "t",
            "y",
            "whitespace_only",
            "empty",
        ],
    )
    def test_autostart_env_value_resolves_to_the_operator_intent(self, raw, expected):
        """Every accepted spelling lands on the side the operator wrote.

        The surrounding-whitespace rows are why the validator exists: pydantic's
        own bool coercion rejects ``"0 "``, and a rejected value falls back to
        the enabled default — starting the scheduler despite an explicit
        off-switch. Blank and whitespace-only read as "not set".
        """
        with mock.patch.dict(os.environ, {AUTOSTART_ENV: raw}, clear=True):
            assert SchedulerSettings().autostart is expected

    def test_autostart_unparseable_value_coerces_to_true_with_a_warning(self):
        """A typo enables the scheduler and names itself in a WARNING.

        Asserted as a coercion, never as a raise: an unconstructable model would
        discard the sibling disabled_jobs list along with the typo.
        """
        import baldur.settings.scheduler as scheduler_settings_module

        with (
            mock.patch.dict(os.environ, {AUTOSTART_ENV: "yess"}, clear=True),
            mock.patch.object(scheduler_settings_module, "logger") as mock_logger,
        ):
            settings = SchedulerSettings()

        assert settings.autostart is True
        warnings = [
            call
            for call in mock_logger.warning.call_args_list
            if call.args and call.args[0] == "scheduler.autostart_value_unparseable"
        ]
        assert len(warnings) == 1
        assert warnings[0].kwargs["env_var"] == AUTOSTART_ENV
        assert warnings[0].kwargs["value"] == "yess"

    def test_autostart_bool_default_survives_the_before_validator(self):
        """With no env var at all the validator sees the bool default, not a str.

        ``validate_default=True`` is on for every settings class, so the
        ``mode="before"`` validator runs over the ``True`` default — the
        isinstance guard is what keeps ``.strip()`` off a bool.
        """
        with mock.patch.dict(os.environ, _CLEARED_ENV, clear=True):
            settings = SchedulerSettings()

        assert settings.autostart is True


class TestDisabledJobsParseBehavior:
    """get_disabled_job_names() turns the operator's string into job names."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", ()),
            (",", ()),
            (" , ", ()),
            ("config_apply", ("config_apply",)),
            (" config_apply ", ("config_apply",)),
            ("config_apply,sla_drift", ("config_apply", "sla_drift")),
            (" config_apply , sla_drift ", ("config_apply", "sla_drift")),
            ("not_a_job", ("not_a_job",)),
        ],
        ids=[
            "empty",
            "bare_separator",
            "separator_with_whitespace",
            "single",
            "single_padded",
            "multi",
            "multi_padded",
            "unknown_name_passes_through",
        ],
    )
    def test_disabled_jobs_parses_to_the_named_jobs(self, raw, expected):
        """Empty entries are dropped and surrounding whitespace stripped.

        Unknown names pass through unchanged — validating them here would need
        the bootstrap job table, and the caller that owns that table is the one
        that warns.
        """
        with mock.patch.dict(os.environ, {DISABLED_JOBS_ENV: raw}, clear=True):
            assert SchedulerSettings().get_disabled_job_names() == expected


class TestSchedulerSettingsDegradeIndependentlyBehavior:
    """One unparseable field must not take the other one down with it."""

    def test_unparseable_autostart_does_not_discard_the_disabled_job_list(self):
        """A typo'd autostart leaves the operator's disable list intact.

        The whole reason the validator coerces instead of raising: a
        ValidationError makes the model unconstructable, so the caller's
        fallback runs everything — including the job the operator disabled.
        """
        env = {AUTOSTART_ENV: "ture", DISABLED_JOBS_ENV: "config_apply"}
        with mock.patch.dict(os.environ, env, clear=True):
            settings = SchedulerSettings()

        assert settings.autostart is True
        assert settings.get_disabled_job_names() == ("config_apply",)
