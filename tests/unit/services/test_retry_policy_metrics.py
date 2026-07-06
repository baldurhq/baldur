"""Inline retry-metric recording tests (631 D1/D2).

``RetryPolicy.execute()`` / ``_single_attempt`` record terminal retry outcomes to
the Prometheus retry series (``baldur_retry_outcomes_total`` /
``baldur_retry_attempts_distribution``) via the ``record_retry_attempt`` facade,
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
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.core.backoff import ConstantBackoff
from baldur.core.test_mode_context import TestModeContext
from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import RetryPolicy

_OUTCOMES = "baldur_retry_outcomes_total"
_ATTEMPTS = "baldur_retry_attempts_distribution"


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
        """A raising record_retry_attempt leaves a successful result intact."""
        policy = _make_policy("external_service")

        with patch(
            "baldur.services.metrics.recorders.record_retry_attempt",
            side_effect=RuntimeError("recorder down"),
        ):
            result = policy.execute(_flaky_until(2))

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"
        assert result.total_attempts == 2

    def test_fail_open_exhaustion_preserves_propagated_error(self):
        """A raising record_retry_attempt leaves the exhaustion error intact."""
        policy = _make_policy("external_service", max_attempts=2)
        sentinel = ConnectionError("permanent")

        def fn():
            raise sentinel

        with patch(
            "baldur.services.metrics.recorders.record_retry_attempt",
            side_effect=RuntimeError("recorder down"),
        ):
            result = policy.execute(fn)

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.error is sentinel

    def test_fail_open_logs_metric_recording_failed_warning(self):
        """A recorder fault is swallowed and logged once as retry.metric_recording_failed."""
        policy = _make_policy("external_service", max_attempts=1)

        with patch(
            "baldur.services.metrics.recorders.record_retry_attempt",
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
