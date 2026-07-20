"""Cooperative wall-clock retry-budget tests (704 D2).

Target: ``services/retry_handler/policy.py`` (``_resolve_effective_budget`` +
the two cooperative budget checks in ``execute()``) and ``settings/retry.py``
(``max_elapsed`` field).

The effective budget is a min-of-two over the policy knob (``max_elapsed``) and
any request-scoped deadline (``deadline_context.get_remaining_ms``); the tighter
bound wins the attribution, an exact tie is attributed to ``max_elapsed``.
Attempt 1 always runs; the loop stops before a sleep+attempt that would overrun.

Determinism: the deadline is injected by patching ``get_remaining_ms`` (rather
than ``deadline_scope``, whose value both decays with monotonic time and is
reduced by a network-latency buffer). Where elapsed time must advance, a
deterministic ``_AdvancingClock`` replaces the module's ``time.monotonic`` and is
advanced only by the business function / a recording sleeper — no real waits, no
StopIteration-fragile ``side_effect`` lists.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from baldur.core.backoff import ConstantBackoff
from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import RetryPolicy
from baldur.settings.retry import (
    RetrySettings,
    get_retry_settings,
    reset_retry_settings,
)

_GET_REMAINING_MS = "baldur.scaling.deadline_context.get_remaining_ms"
_POLICY_MONOTONIC = "baldur.services.retry_handler.policy.time.monotonic"


class _AdvancingClock:
    """A deterministic monotonic clock — advances only when told to.

    Not a Mock (so it does not touch the G67 spec-less-mock ratchet); models the
    wall-clock passing exactly as much as the test dictates.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _budget_policy(
    *,
    max_elapsed: float | None = None,
    max_attempts: int = 3,
    backoff_delay: float = 0.0,
    sleeper=None,
    domain: str = "test_time_budget",
) -> RetryPolicy:
    """RetryPolicy with a configurable cooperative budget and no-wait backoff."""
    return RetryPolicy(
        config=RetryPolicyConfig(
            max_attempts=max_attempts,
            max_elapsed=max_elapsed,
            domain=domain,
        ),
        backoff=ConstantBackoff(delay=backoff_delay),
        sleeper=sleeper if sleeper is not None else (lambda _: None),
    )


# =============================================================================
# Behavior — _resolve_effective_budget min-of-two + attribution
# =============================================================================


class TestRetryBudgetResolveBehavior:
    """``_resolve_effective_budget`` picks the tighter of knob and deadline."""

    def test_knob_only_returns_knob_seconds_attributed_to_max_elapsed(self):
        policy = _budget_policy(max_elapsed=10.0)
        with patch(_GET_REMAINING_MS, return_value=None):
            budget, reason = policy._resolve_effective_budget()
        assert budget == 10.0
        assert reason == "max_elapsed"

    def test_deadline_only_returns_deadline_seconds_attributed_to_deadline(self):
        policy = _budget_policy(max_elapsed=None)
        with patch(_GET_REMAINING_MS, return_value=5000.0):
            budget, reason = policy._resolve_effective_budget()
        assert budget == 5.0
        assert reason == "deadline"

    def test_both_set_deadline_tighter_wins_attribution(self):
        policy = _budget_policy(max_elapsed=10.0)
        with patch(_GET_REMAINING_MS, return_value=3000.0):
            assert policy._resolve_effective_budget() == (3.0, "deadline")

    def test_both_set_knob_tighter_wins_attribution(self):
        policy = _budget_policy(max_elapsed=2.0)
        with patch(_GET_REMAINING_MS, return_value=10000.0):
            assert policy._resolve_effective_budget() == (2.0, "max_elapsed")

    def test_exact_tie_is_attributed_to_max_elapsed(self):
        policy = _budget_policy(max_elapsed=5.0)
        with patch(_GET_REMAINING_MS, return_value=5000.0):
            assert policy._resolve_effective_budget() == (5.0, "max_elapsed")

    def test_neither_set_is_unbounded(self):
        policy = _budget_policy(max_elapsed=None)
        with patch(_GET_REMAINING_MS, return_value=None):
            budget, _reason = policy._resolve_effective_budget()
        assert budget is None

    def test_deadline_lookup_fault_degrades_to_knob(self):
        """A raising deadline lookup fails open — only the knob applies."""
        policy = _budget_policy(max_elapsed=7.0)
        with patch(_GET_REMAINING_MS, side_effect=RuntimeError("deadline down")):
            budget, reason = policy._resolve_effective_budget()
        assert budget == 7.0
        assert reason == "max_elapsed"


# =============================================================================
# Behavior — cooperative budget checks in execute()
# =============================================================================


class TestRetryBudgetExecuteBehavior:
    """The budget stops the loop cooperatively; attempt 1 is never blocked."""

    def test_attempt_one_always_runs_even_when_budget_already_expired(self):
        """An already-expired deadline does not prevent the first attempt."""
        policy = _budget_policy(max_elapsed=None, max_attempts=3)
        with patch(_GET_REMAINING_MS, return_value=0.0):
            result = policy.execute(lambda: "ok")
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.total_attempts == 1

    def test_budget_stops_before_a_sleep_attempt_that_would_overrun(self):
        """When ``elapsed + next_delay`` exceeds the budget the loop stops without
        sleeping — no sleeper call, exit attributed to ``max_elapsed``."""
        # Given — attempt 1 burns 0.2s; the 0.1s budget cannot fund a 2nd attempt
        clock = _AdvancingClock()
        sleeps: list[float] = []
        calls: list[int] = []

        def failing():
            calls.append(1)
            clock.advance(0.2)
            raise ConnectionError("fail")

        policy = _budget_policy(
            max_elapsed=0.1,
            max_attempts=5,
            backoff_delay=0.0,
            sleeper=sleeps.append,
        )

        # When
        with patch(_POLICY_MONOTONIC, clock):
            result = policy.execute(failing)

        # Then — one attempt, no sleep, budget attribution
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.total_attempts == 1
        assert len(calls) == 1
        assert sleeps == []
        assert result.metadata["reason"] == "max_elapsed"

    def test_budget_burned_by_prior_sleeps_stops_at_loop_top(self):
        """Wall-clock consumed by prior sleeps is caught by the loop-top check
        (the second-attempt-onward guard), not only the pre-sleep check."""
        # Given — each backoff sleep burns 0.1s; the function itself is instant
        clock = _AdvancingClock()

        def _sleeper(delay: float) -> None:
            clock.advance(delay)

        calls: list[int] = []

        def failing():
            calls.append(1)
            raise ConnectionError("fail")

        policy = _budget_policy(
            max_elapsed=0.3,
            max_attempts=10,
            backoff_delay=0.1,
            sleeper=_sleeper,
        )

        # When
        with patch(_POLICY_MONOTONIC, clock):
            result = policy.execute(failing)

        # Then — the budget stopped the loop once the accumulated sleeps hit 0.3s
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.metadata["reason"] == "max_elapsed"
        # 3 sleeps of 0.1s reach the 0.3s budget; the loop-top check then fires
        # before a 4th attempt — fewer than the 10-attempt ceiling.
        assert len(calls) < 10


# =============================================================================
# Contract — RetrySettings.max_elapsed field
# =============================================================================


class TestRetrySettingsContract:
    """``max_elapsed`` design contract: default None, ``gt=0``, high-value warn."""

    def test_max_elapsed_default_is_none(self):
        assert RetrySettings().max_elapsed is None

    @pytest.mark.parametrize(
        "value", [0, -1, -0.5], ids=["zero", "neg_int", "neg_float"]
    )
    def test_max_elapsed_rejects_non_positive(self, value):
        with pytest.raises(ValidationError):
            RetrySettings(max_elapsed=value)

    def test_max_elapsed_accepts_positive(self):
        assert RetrySettings(max_elapsed=30.0).max_elapsed == 30.0

    def test_max_elapsed_env_var_round_trips_to_settings(self, monkeypatch):
        """``BALDUR_RETRY_MAX_ELAPSED`` populates the settings field."""
        monkeypatch.setenv("BALDUR_RETRY_MAX_ELAPSED", "42.5")
        reset_retry_settings()
        try:
            assert get_retry_settings().max_elapsed == 42.5
        finally:
            reset_retry_settings()

    def test_max_elapsed_above_one_hour_logs_responsiveness_warning(self):
        """A budget over 3600s logs the high-value safe-default warning."""
        with capture_logs() as logs:
            settings = RetrySettings(max_elapsed=3601)
        assert settings.max_elapsed == 3601
        events = [
            e
            for e in logs
            if e["event"] == "safe_default.high_consider_using_responsiveness"
        ]
        assert len(events) >= 1


# =============================================================================
# Behavior — max_elapsed sourcing on the config dataclass
# =============================================================================


class TestRetryBudgetConfigMappingBehavior:
    """``max_elapsed`` flows through from_settings onto the policy config."""

    def test_from_settings_maps_max_elapsed_via_static_path(self):
        """The settings-derived config carries ``max_elapsed`` from RetrySettings
        on the PRO-absent static path."""
        fake_config = SimpleNamespace(
            core=SimpleNamespace(
                retry=SimpleNamespace(max_attempts=3, max_delay=180, max_elapsed=45.0),
                backoff=SimpleNamespace(legacy_base=4, legacy_jitter_percent=25),
            ),
            services_group=SimpleNamespace(dlq=SimpleNamespace(enabled=True)),
            domain_configs={},
        )
        with patch(
            "baldur.factory.registry.ProviderRegistry.runtime_config_manager"
        ) as manager_slot:
            manager_slot.safe_get.return_value = None  # force the static path
            with patch(
                "baldur.services.retry_handler.models.get_config",
                return_value=fake_config,
            ):
                cfg = RetryPolicyConfig.from_settings("default")
        assert cfg.max_elapsed == 45.0
