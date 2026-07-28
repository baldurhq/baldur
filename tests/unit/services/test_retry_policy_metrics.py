"""Inline retry-metric recording tests (631 D1/D2).

``RetryPolicy.execute()`` / ``_single_attempt`` record terminal retry outcomes to
the Prometheus retry series (``baldur_retry_outcomes_total`` /
``baldur_retry_attempts_distribution``) via the ``record_retry_resolution`` facade,
so the OSS synchronous ``@baldur.protected(retry=True)`` path is observable
instead of metric-silent — the exact gap 630 ``/verify`` found.

Verification approach (terminal-all, D2): the recorder writes to the global
in-process ``prometheus_client`` REGISTRY shared across the whole xdist worker,
so every assertion is a before/after sample *delta* (never an absolute total) to
stay order-independent under parallel execution. ``execute()`` is driven with an
injected no-op ``sleeper`` (``lambda _: None``) — no wall-clock waits; failures
come from a function that raises. A *registered* domain is used throughout so
``resolve_domain_label`` preserves the label verbatim (unregistered domains
collapse to ``OTHER_DOMAIN``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from structlog.testing import capture_logs

from baldur.core.backoff import ConstantBackoff
from baldur.core.test_mode_context import TestModeContext
from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.services.backoff_calculator.budget import AdaptiveRetryBudget
from baldur.services.rate_limit_coordinator import RateLimitCoordinator
from baldur.services.rate_limit_coordinator.models import RateLimitResult
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import RetryPolicy

_OUTCOMES = "baldur_retry_outcomes_total"
_ATTEMPTS = "baldur_retry_attempts_distribution"
_RECORD_ATTEMPT_STARTED = (
    "baldur.services.metrics.recorders.record_retry_attempt_started"
)
_RECORD_RESOLUTION = "baldur.services.metrics.recorders.record_retry_resolution"


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    """Current Prometheus sample value, treating a missing series as 0.0."""
    from prometheus_client import REGISTRY

    value = REGISTRY.get_sample_value(name, labels)
    return 0.0 if value is None else value


def _outcome_labels(domain: str, outcome: str, *, synthetic: str = "false") -> dict:
    """Full label set for one ``baldur_retry_outcomes_total`` series."""
    return {"domain": domain, "outcome": outcome, "is_synthetic": synthetic}


def _make_policy(domain: str, *, max_attempts: int = 3) -> RetryPolicy:
    """RetryPolicy on the real retry loop with zero-delay, no-wait backoff."""
    return RetryPolicy(
        config=RetryPolicyConfig(max_attempts=max_attempts, domain=domain),
        backoff=ConstantBackoff(delay=0.0),
        sleeper=lambda _: None,
    )


def _flaky_until(succeed_on_attempt: int):
    """Return a fn that raises ConnectionError until ``succeed_on_attempt``, then 'ok'."""
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        if attempts["n"] < succeed_on_attempt:
            raise ConnectionError("temporary")
        return "ok"

    return fn


def _always_fail():
    """A retryable failure that never resolves (drives exhaustion)."""
    raise ConnectionError("permanent")


def _run_via_single_attempt_route(route: str, domain: str, fn):
    """Drive ``execute()`` down the ``_single_attempt`` path via the named route.

    Both routes — retry globally disabled, and observe-only (intervention
    suppressed) — bypass the retry loop and run the business call exactly once.
    """
    if route == "globally_disabled":
        with patch(
            "baldur.settings.retry.get_retry_settings",
            return_value=SimpleNamespace(enabled=False),
        ):
            policy = RetryPolicy(
                config=RetryPolicyConfig(max_attempts=3, domain=domain)
            )
        return policy.execute(fn)

    # observe_only: globally enabled, but the retry intervention is suppressed
    policy = RetryPolicy(config=RetryPolicyConfig(max_attempts=3, domain=domain))
    with patch(
        "baldur.services.retry_handler.policy.intervention_suppressed",
        return_value=True,
    ):
        return policy.execute(fn)


# =============================================================================
# Terminal-all recording — RetryPolicy populates the retry series (G1/D2)
# =============================================================================


class TestInlineRetryMetricRecording:
    """RetryPolicy records terminal retry outcomes to the Prometheus retry series."""

    def test_retry_loop_success_terminal_records_success_with_attempt_count(self):
        """A flaky call resolving on attempt 2 records one `success` outcome and
        observes one sample in the attempts histogram."""
        # Given
        domain = "external_service"
        policy = _make_policy(domain)
        succ_labels = _outcome_labels(domain, "success")
        count_labels = {"domain": domain, "is_synthetic": "false"}
        before_succ = _sample(_OUTCOMES, succ_labels)
        before_count = _sample(_ATTEMPTS + "_count", count_labels)

        # When
        result = policy.execute(_flaky_until(2))

        # Then — the retry loop resolved on the 2nd attempt and recorded it
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.total_attempts == 2
        assert _sample(_OUTCOMES, succ_labels) - before_succ == 1.0
        assert _sample(_ATTEMPTS + "_count", count_labels) - before_count == 1.0

    def test_retry_loop_exhaustion_terminal_records_exhausted_with_final_attempt(self):
        """An always-failing call records one `exhausted` outcome at the final attempt."""
        # Given
        domain = "external_service"
        policy = _make_policy(domain, max_attempts=3)
        exh_labels = _outcome_labels(domain, "exhausted")
        count_labels = {"domain": domain, "is_synthetic": "false"}
        before_exh = _sample(_OUTCOMES, exh_labels)
        before_count = _sample(_ATTEMPTS + "_count", count_labels)

        # When
        result = policy.execute(_always_fail)

        # Then
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.total_attempts == 3
        assert _sample(_OUTCOMES, exh_labels) - before_exh == 1.0
        assert _sample(_ATTEMPTS + "_count", count_labels) - before_count == 1.0

    @pytest.mark.parametrize(
        ("route", "raises", "expected_outcome", "expected_policy_outcome"),
        [
            ("globally_disabled", False, "success", PolicyOutcome.SUCCESS),
            ("globally_disabled", True, "failure", PolicyOutcome.FAILURE),
            ("observe_only", False, "success", PolicyOutcome.SUCCESS),
            ("observe_only", True, "failure", PolicyOutcome.FAILURE),
        ],
        ids=[
            "globally_disabled_success",
            "globally_disabled_failure",
            "observe_only_success",
            "observe_only_failure",
        ],
    )
    def test_single_attempt_terminal_records_outcome_with_attempt_one(
        self, route, raises, expected_outcome, expected_policy_outcome
    ):
        """Both single-attempt entry routes (globally-disabled and observe-only)
        record their terminal outcome with attempt-count 1 — the two routes are an
        equivalence partition over `_single_attempt`."""
        # Given
        domain = "internal_process"
        labels = _outcome_labels(domain, expected_outcome)
        before = _sample(_OUTCOMES, labels)

        def fn():
            if raises:
                raise ValueError("boom")
            return "ok"

        # When
        result = _run_via_single_attempt_route(route, domain, fn)

        # Then — no retry occurred and the single attempt was recorded
        assert result.outcome == expected_policy_outcome
        assert result.total_attempts == 1
        assert _sample(_OUTCOMES, labels) - before == 1.0

    def test_exhausted_and_success_distinguishable_by_outcome_label(self):
        """A success-after-retry and an exhausted run on the same domain land on
        distinct `outcome` label values, so the two are independently countable."""
        # Given
        domain = "async_task"
        succ_labels = _outcome_labels(domain, "success")
        exh_labels = _outcome_labels(domain, "exhausted")
        before_succ = _sample(_OUTCOMES, succ_labels)
        before_exh = _sample(_OUTCOMES, exh_labels)

        # When — one resolves after a retry, one exhausts
        _make_policy(domain).execute(_flaky_until(2))
        _make_policy(domain, max_attempts=2).execute(_always_fail)

        # Then — each outcome label incremented exactly once, independently
        assert _sample(_OUTCOMES, succ_labels) - before_succ == 1.0
        assert _sample(_OUTCOMES, exh_labels) - before_exh == 1.0

    def test_no_retry_observes_bucket_one_while_retried_separates_to_higher_bucket(
        self,
    ):
        """attempt-1 (no retry) lands in histogram bucket le=1.0; a 2-attempt
        resolution does NOT increment le=1.0 but does increment le=2.0 — the
        bucket-1 vs bucket>=2 self-separation the terminal-all design relies on."""
        # Given
        domain = "notification"
        bucket = _ATTEMPTS + "_bucket"
        le1 = {"domain": domain, "is_synthetic": "false", "le": "1.0"}
        le2 = {"domain": domain, "is_synthetic": "false", "le": "2.0"}
        before_le1 = _sample(bucket, le1)
        before_le2 = _sample(bucket, le2)

        # When — a single-attempt resolution observes attempt-count 1
        _run_via_single_attempt_route("observe_only", domain, lambda: "ok")
        after1_le1 = _sample(bucket, le1)
        after1_le2 = _sample(bucket, le2)

        # Then — bucket le=1.0 captured it (1 <= 1), and cumulative le=2.0 too
        assert after1_le1 - before_le1 == 1.0
        assert after1_le2 - before_le2 == 1.0

        # When — a 2-attempt resolution observes attempt-count 2
        _make_policy(domain).execute(_flaky_until(2))
        after2_le1 = _sample(bucket, le1)
        after2_le2 = _sample(bucket, le2)

        # Then — le=1.0 unchanged (2 > 1); le=2.0 incremented (2 <= 2)
        assert after2_le1 - after1_le1 == 0.0
        assert after2_le2 - after1_le2 == 1.0


# =============================================================================
# Fail-open — a recorder fault never changes the business result (D1/SC#3)
# =============================================================================


class TestRetryMetricFailOpen:
    """_record_outcome is fail-open: an injected raising recorder is swallowed."""

    def test_fail_open_success_preserves_return_value(self):
        """A raising record_retry_resolution leaves a successful result intact."""
        policy = _make_policy("external_service")

        with patch(
            "baldur.services.metrics.recorders.record_retry_resolution",
            side_effect=RuntimeError("recorder down"),
        ):
            result = policy.execute(_flaky_until(2))

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"
        assert result.total_attempts == 2

    def test_fail_open_exhaustion_preserves_propagated_error(self):
        """A raising record_retry_resolution leaves the exhaustion error intact."""
        policy = _make_policy("external_service", max_attempts=2)
        sentinel = ConnectionError("permanent")

        def fn():
            raise sentinel

        with patch(
            "baldur.services.metrics.recorders.record_retry_resolution",
            side_effect=RuntimeError("recorder down"),
        ):
            result = policy.execute(fn)

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.error is sentinel

    def test_fail_open_logs_metric_recording_failed_warning(self):
        """A recorder fault is swallowed and logged once as retry.metric_recording_failed."""
        policy = _make_policy("external_service", max_attempts=1)

        with patch(
            "baldur.services.metrics.recorders.record_retry_resolution",
            side_effect=RuntimeError("recorder down"),
        ):
            with capture_logs() as logs:
                policy.execute(lambda: "ok")

        events = [e for e in logs if e["event"] == "retry.metric_recording_failed"]
        assert len(events) == 1


# =============================================================================
# Synthetic label — is_synthetic tracks TestModeContext on the inline path (D2/D3)
# =============================================================================


class TestInlineRetryMetricSyntheticLabel:
    """The is_synthetic label on the inline retry path follows TestModeContext."""

    @pytest.mark.parametrize(
        ("use_synthetic_context", "expected_label"),
        [(False, "false"), (True, "true")],
        ids=["real_traffic", "synthetic_traffic"],
    )
    def test_synthetic_context_sets_is_synthetic_label(
        self, use_synthetic_context, expected_label
    ):
        """A retry-loop success records under the is_synthetic value of the active context."""
        # Given
        domain = "data_sync"
        labels = _outcome_labels(domain, "success", synthetic=expected_label)
        before = _sample(_OUTCOMES, labels)
        policy = _make_policy(domain)

        # When
        if use_synthetic_context:
            with TestModeContext.start():
                policy.execute(_flaky_until(2))
        else:
            policy.execute(_flaky_until(2))

        # Then — the recording landed on the matching is_synthetic series
        assert _sample(_OUTCOMES, labels) - before == 1.0


# =============================================================================
# Timely pressure — the sync loop records an attempt start at admission (729 D6)
# =============================================================================


class TestSyncLoopAttemptsStartedBehavior:
    """The sync retry loop records one attempt start per admitted attempt.

    Assertions read the facade's call args rather than the registry: what is at
    stake here is *when* and *how often* the loop records, and the call list
    carries the ordering the registry's accumulated totals cannot.

    The pin that matters is placement. The record sits after the loop's refusal
    checks and before the rate-limit cooldown wait — an honored ``Retry-After``
    can hold that wait for up to an hour, and a record on the far side of it
    would leave the retry share flat for the whole duration of exactly the
    storm the alert exists to catch.
    """

    def test_sync_loop_records_three_attempts_started_with_no_retry_budget_injected(
        self,
    ):
        """A 3-attempt exhaustion is one first attempt and two retries.

        This is the canonical ``protect(retry=True)`` shape: nothing in the
        tree constructs an ``AdaptiveRetryBudget``, so the recording must not
        depend on one — asserted as a precondition rather than assumed.
        """
        # Given
        domain = "external_service"
        policy = _make_policy(domain, max_attempts=3)
        assert policy._retry_budget is None

        # When
        with patch(_RECORD_ATTEMPT_STARTED, autospec=True) as mock_started:
            result = policy.execute(_always_fail)

        # Then — one denominator-only attempt, two that are retry pressure
        assert result.total_attempts == 3
        assert mock_started.call_args_list == [
            call(domain, is_retry=False),
            call(domain, is_retry=True),
            call(domain, is_retry=True),
        ]

    def test_attempts_started_is_recorded_before_the_cooldown_wait_begins(self):
        """The storm is counted at sleep start, not at resolution.

        The coordinator double reports how many starts had been recorded at the
        moment the wait began. One means the attempt was already counted before
        it went to sleep; moving the record past the wait would make it zero
        and the series would stall for the length of the cooldown.
        """
        # Given
        domain = "external_service"
        coordinator = MagicMock(spec=RateLimitCoordinator)
        starts_at_wait_entry: list[int] = []

        # When
        with patch(_RECORD_ATTEMPT_STARTED, autospec=True) as mock_started:

            def _wait(_key, max_wait=None):
                starts_at_wait_entry.append(mock_started.call_count)
                return RateLimitResult(waited=False)

            coordinator.wait_if_needed.side_effect = _wait
            policy = RetryPolicy(
                config=RetryPolicyConfig(max_attempts=2, domain=domain),
                backoff=ConstantBackoff(delay=0.0),
                rate_limit_coordinator=coordinator,
                sleeper=lambda _: None,
            )
            result = policy.execute(lambda: "ok")

        # Then
        assert result.outcome == PolicyOutcome.SUCCESS
        assert starts_at_wait_entry == [1]

    def test_deferred_attempt_records_attempts_started_though_func_never_runs(self):
        """A cooldown deferral is refused demand, and demand is what the ratio counts.

        The attempt was admitted and then turned away by the downstream's own
        cooldown; the call never ran, but the pressure was real. Suppressing it
        would hide the retry storm that caused the cooldown.
        """
        # Given
        domain = "external_service"
        coordinator = MagicMock(spec=RateLimitCoordinator)
        coordinator.wait_if_needed.return_value = RateLimitResult(
            deferred=True, not_before=1.0
        )
        invocations: list[int] = []
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=3, domain=domain),
            backoff=ConstantBackoff(delay=0.0),
            rate_limit_coordinator=coordinator,
            sleeper=lambda _: None,
        )

        # When
        with patch(_RECORD_ATTEMPT_STARTED, autospec=True) as mock_started:
            result = policy.execute(lambda: invocations.append(1))

        # Then — recorded once, and the business call demonstrably never ran
        assert invocations == []
        assert result.metadata["reason"] == "rate_limit_deferred"
        assert mock_started.call_args_list == [call(domain, is_retry=False)]

    def test_budget_refused_iteration_records_no_attempts_started(self):
        """An iteration refused before the record never ran, so it is not demand.

        The refusal happens above the record on purpose: the budget breaks the
        loop, no call is made, and counting it would inflate the retry share
        with attempts that were prevented rather than attempted.
        """
        # Given
        domain = "external_service"
        budget = MagicMock(spec=AdaptiveRetryBudget)
        budget.should_allow_retry.return_value = False
        budget.get_stats.return_value = {}
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=3, domain=domain),
            backoff=ConstantBackoff(delay=0.0),
            retry_budget=budget,
            sleeper=lambda _: None,
        )

        # When
        with patch(_RECORD_ATTEMPT_STARTED, autospec=True) as mock_started:
            result = policy.execute(_always_fail)

        # Then — the refusal fired, and only the admitted first attempt recorded
        budget.should_allow_retry.assert_called_once()
        assert result.metadata["reason"] == "retry_budget"
        assert mock_started.call_args_list == [call(domain, is_retry=False)]

    def test_allowing_budget_records_the_same_attempts_started_shape_as_no_budget(
        self,
    ):
        """The recording path consults no admission predicate of its own.

        A budget that refuses nothing changes what the loop *does* in no way,
        so it must change what the loop *records* in no way either. If the
        record were gated on budget state instead of on admission, these two
        runs would diverge.
        """
        # Given
        domain = "external_service"
        budget = MagicMock(spec=AdaptiveRetryBudget)
        budget.should_allow_retry.return_value = True

        # When — the same 3-attempt exhaustion, with and without a budget
        with patch(_RECORD_ATTEMPT_STARTED, autospec=True) as without_budget:
            _make_policy(domain, max_attempts=3).execute(_always_fail)

        with patch(_RECORD_ATTEMPT_STARTED, autospec=True) as with_budget:
            RetryPolicy(
                config=RetryPolicyConfig(max_attempts=3, domain=domain),
                backoff=ConstantBackoff(delay=0.0),
                retry_budget=budget,
                sleeper=lambda _: None,
            ).execute(_always_fail)

        # Then
        assert with_budget.call_args_list == without_budget.call_args_list

    @pytest.mark.parametrize(
        "route",
        ["globally_disabled", "observe_only"],
        ids=["globally_disabled", "observe_only"],
    )
    def test_single_attempt_route_records_one_attempts_started_and_one_terminal(
        self, route
    ):
        """The no-retry routes owe the ratio's denominator, not just its terminal.

        Both routes already record a terminal. Recording the terminal without
        the matching start would shrink the denominator alone, inflating the
        retry share on every deployment that runs with retries turned off.
        """
        # Given
        domain = "internal_process"

        # When
        with (
            patch(_RECORD_ATTEMPT_STARTED, autospec=True) as mock_started,
            patch(_RECORD_RESOLUTION, autospec=True) as mock_resolution,
        ):
            result = _run_via_single_attempt_route(route, domain, lambda: "ok")

        # Then
        assert result.total_attempts == 1
        mock_started.assert_called_once_with(domain, is_retry=False)
        mock_resolution.assert_called_once_with(domain, 1, "success")
