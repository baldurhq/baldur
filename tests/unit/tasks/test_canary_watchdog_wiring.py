"""Canary watchdog settings wiring and governance-blocked metrics.

Two defects that made the activated lane broken-if-wired, plus the settings
contract behind them:

    A. ``CanaryWatchdogConfig.from_settings()`` had no production caller. The
       watchdog singleton built plain dataclass defaults, so every
       ``BALDUR_CANARY_WATCHDOG_*`` variable was inert on both the Celery lane
       and the meta-watchdog probe that shares the same singleton.
    B. ``baldur_canary_governance_blocked_total`` was emitted with one of its
       three declared labels. ``prometheus_client`` raised, the exception was
       swallowed at debug level, and it took the pending-promotion gauge write
       below it down as well — so both metrics stayed permanently zero.
    C. The settings surface itself: bounds, the opt-in defaults, and the
       removal of ``slack_channel`` (Slack *targets* belong to the unified
       notification manager's per-category routing, not to this class).

No PRO import: none of these paths needs one.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from prometheus_client import REGISTRY
from pydantic import ValidationError

from baldur.interfaces.canary import CanaryRolloutService
from baldur.settings.canary_watchdog import (
    CanaryWatchdogSettings,
    reset_canary_watchdog_settings,
)
from baldur.tasks.canary_watchdog import (
    CanaryWatchdogConfig,
    RolloutWatchdog,
    get_rollout_watchdog,
    reset_watchdog,
)

_BLOCKED_COUNTER = "baldur_canary_governance_blocked_total"
_PENDING_GAUGE = "baldur_canary_pending_promotion"


@pytest.fixture(autouse=True)
def _isolate_watchdog_settings():
    """Reset both singletons around every case.

    The watchdog config is now derived from the settings singleton, so a stale
    settings instance would decide what the wiring cases below observe.
    """
    reset_canary_watchdog_settings()
    reset_watchdog()
    yield
    reset_watchdog()
    reset_canary_watchdog_settings()


def _governance(block_reason: str | None):
    """Governance verdict shaped like the blocked result the watchdog records."""
    return SimpleNamespace(
        allowed=False,
        block_reason=(
            None if block_reason is None else SimpleNamespace(value=block_reason)
        ),
    )


def _watchdog_with_active(active_rollouts):
    """Watchdog bound to a canary service reporting ``active_rollouts``.

    Specced against the OSS Protocol so a call the Protocol does not declare
    raises instead of being silently recorded.
    """
    watchdog = RolloutWatchdog(config=CanaryWatchdogConfig())
    service = Mock(spec=CanaryRolloutService)
    if isinstance(active_rollouts, Exception):
        service.get_active_rollouts.side_effect = active_rollouts
    else:
        service.get_active_rollouts.return_value = active_rollouts
    watchdog._service = service
    return watchdog


def _counter_value(block_reason: str) -> float:
    value = REGISTRY.get_sample_value(
        _BLOCKED_COUNTER,
        {"block_reason": block_reason, "region": "", "tier": ""},
    )
    return 0.0 if value is None else value


# =============================================================================
# A. Settings reach the running watchdog
# =============================================================================


class TestRolloutWatchdogSettingsWiringBehavior:
    """A watchdog built without an explicit config resolves one from settings."""

    def test_env_override_reaches_a_directly_constructed_watchdog(self, monkeypatch):
        """The env round-trip that ``from_settings()`` having no caller broke."""
        monkeypatch.setenv("BALDUR_CANARY_WATCHDOG_ZOMBIE_THRESHOLD_MINUTES", "45")
        monkeypatch.setenv("BALDUR_CANARY_WATCHDOG_MAX_STAGE_DURATION_MINUTES", "7")
        reset_canary_watchdog_settings()

        watchdog = RolloutWatchdog()

        assert watchdog.config.zombie_threshold_minutes == 45
        assert watchdog.config.max_stage_duration_minutes == 7

    def test_env_override_reaches_the_shared_singleton(self, monkeypatch):
        """The Celery lane and the meta-watchdog probe read this same instance,
        so the knob has to arrive through the singleton, not just the class."""
        monkeypatch.setenv("BALDUR_CANARY_WATCHDOG_ENABLE_AUTO_PROMOTE", "true")
        reset_canary_watchdog_settings()

        assert get_rollout_watchdog().config.enable_auto_promote is True

    def test_explicit_config_is_not_overwritten_by_settings(self, monkeypatch):
        """Callers that pass a config keep it — the resolve is a fallback only."""
        monkeypatch.setenv("BALDUR_CANARY_WATCHDOG_ZOMBIE_THRESHOLD_MINUTES", "45")
        reset_canary_watchdog_settings()

        watchdog = RolloutWatchdog(
            config=CanaryWatchdogConfig(zombie_threshold_minutes=12)
        )

        assert watchdog.config.zombie_threshold_minutes == 12

    def test_settings_defaults_land_on_the_config_when_env_is_clean(self, monkeypatch):
        """With nothing set, the resolved config matches the settings defaults.

        Pins the mapping itself: a field silently dropped from
        ``from_settings()`` would leave the dataclass default in place and read
        as "correct" against a defaults-only assertion of either half alone.
        """
        for field in CanaryWatchdogSettings.model_fields:
            monkeypatch.delenv(f"BALDUR_CANARY_WATCHDOG_{field.upper()}", raising=False)
        reset_canary_watchdog_settings()

        config = RolloutWatchdog().config
        settings = CanaryWatchdogSettings()

        for field in CanaryWatchdogSettings.model_fields:
            assert getattr(config, field) == getattr(settings, field)


# =============================================================================
# C. Settings and config surface
# =============================================================================


class TestCanaryWatchdogSettingsContract:
    """Bounds, opt-in defaults, and the fields that are deliberately absent."""

    @pytest.mark.parametrize(
        ("field", "low", "high"),
        [
            ("zombie_threshold_minutes", 5, 240),
            ("auto_rollback_after_minutes", 10, 480),
            ("max_stage_duration_minutes", 1, 120),
        ],
    )
    def test_bounds_accept_their_endpoints(self, field, low, high):
        """Just inside: both endpoints are valid values."""
        # auto_rollback must stay above zombie_threshold, so each case pins its
        # own field while giving the pair a combination the validator accepts.
        base = {"zombie_threshold_minutes": 5, "auto_rollback_after_minutes": 480}

        for value in (low, high):
            settings = CanaryWatchdogSettings(**{**base, field: value})
            assert getattr(settings, field) == value

    @pytest.mark.parametrize(
        ("field", "below", "above"),
        [
            ("zombie_threshold_minutes", 4, 241),
            ("auto_rollback_after_minutes", 9, 481),
            ("max_stage_duration_minutes", 0, 121),
        ],
    )
    def test_bounds_reject_just_outside(self, field, below, above):
        """Just outside: one below the floor and one above the ceiling fail."""
        base = {"zombie_threshold_minutes": 5, "auto_rollback_after_minutes": 480}

        for value in (below, above):
            with pytest.raises(ValidationError):
                CanaryWatchdogSettings(**{**base, field: value})

    def test_mutating_actions_default_off_and_observation_stays_on(self):
        """Activating the lane must not start promoting or rolling back.

        The two mutating actions are opt-ins; notification is not, because the
        lane is useless if a stall is detected and nobody hears about it.
        """
        settings = CanaryWatchdogSettings()

        assert settings.enable_auto_promote is False
        assert settings.enable_auto_rollback is False
        assert settings.notification_enabled is True

    def test_slack_channel_is_not_a_watchdog_setting(self):
        """The field never reached a delivery target and was removed.

        A channel *name* here was passed into the payload's channel *type*
        filter, so the resolved channel set was always empty. Slack targets are
        owned by the unified manager's per-category routing.
        """
        assert "slack_channel" not in CanaryWatchdogSettings.model_fields
        assert not hasattr(CanaryWatchdogConfig(), "slack_channel")


# =============================================================================
# B. Governance-blocked metrics
# =============================================================================


class TestCanaryGovernanceMetricsBehavior:
    """Both metrics land; neither is taken down by the other."""

    def test_blocked_counter_increments_with_the_global_label_pair(self):
        """The counter declares three labels and the watchdog has one.

        The region/tier pair is emitted as the empty string (global /
        unspecified). Passing ``block_reason`` alone raised inside
        prometheus_client, and the exception was swallowed at debug level.
        """
        watchdog = _watchdog_with_active([])
        before = _counter_value("error_budget")

        watchdog._record_governance_blocked_metrics(_governance("error_budget"))

        assert _counter_value("error_budget") - before == 1.0

    def test_pending_gauge_records_the_active_rollout_count(self):
        """The gauge write sits after the counter, so it is the real proof the
        counter no longer raises — it was collateral damage of that bug."""
        watchdog = _watchdog_with_active([object(), object(), object()])

        watchdog._record_governance_blocked_metrics(_governance("kill_switch"))

        assert (
            REGISTRY.get_sample_value(_PENDING_GAUGE, {"reason": "kill_switch"}) == 3.0
        )

    def test_missing_block_reason_is_recorded_as_unknown(self):
        """A verdict with no reason still produces a sample, not a silent drop."""
        watchdog = _watchdog_with_active([])
        before = _counter_value("unknown")

        watchdog._record_governance_blocked_metrics(_governance(None))

        assert _counter_value("unknown") - before == 1.0

    def test_recording_failure_does_not_escape(self):
        """Fail-open: metrics recording must not break the promotion pass.

        The caller is mid-``auto_promote_eligible``, which has already decided
        to block; a metrics fault there must not turn a clean block into an
        errored task run.
        """
        watchdog = _watchdog_with_active(RuntimeError("store down"))

        watchdog._record_governance_blocked_metrics(_governance("emergency_mode"))

    def test_recording_failure_is_reported_at_debug(self):
        """Debug, not warning: a missing sample is not an operator action."""
        import baldur.tasks.canary_watchdog as watchdog_module

        watchdog = _watchdog_with_active(RuntimeError("store down"))

        with patch.object(watchdog_module, "logger") as mock_logger:
            watchdog._record_governance_blocked_metrics(_governance("emergency_mode"))

        assert "watchdog.metrics_recording_failed" in [
            call.args[0] for call in mock_logger.debug.call_args_list
        ]
