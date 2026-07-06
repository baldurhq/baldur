"""Observe-only (dry-run) behaviour for the retry + DLQ sites (doc 603).

Two ``standard_pipeline`` intervention sites gated on the shared
``intervention_suppressed`` predicate (D5):

- ``RetryPolicy.execute`` — under observe-only takes the single-attempt path
  (``_single_attempt``), so the business call runs exactly once and the FAILURE
  result carries no ``should_dlq`` flag (the downstream DLQ sink stays
  observe-only too).
- ``DLQSink.handle_failure`` — under observe-only suppresses the DLQ write and
  returns None as if nothing stored, logging the would-store decision.

Behaviors are computed from source (PolicyOutcome.*, single-attempt path), so
these are Behavior-class tests.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import structlog

from baldur.core.backoff import ConstantBackoff
from baldur.interfaces.resilience_policy import PolicyOutcome, PolicyResult
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import RetryPolicy
from baldur.services.retry_handler.sinks import DLQSink
from tests.factories import dry_run_active


def _counting(returns=None, raises=None):
    """Build a callable that counts invocations."""
    state = {"n": 0}

    def fn():
        state["n"] += 1
        if raises is not None:
            raise raises
        return returns

    return fn, state


class TestRetryPolicyDryRun:
    """RetryPolicy.execute observe-only branch (single-attempt, no re-execution)."""

    @staticmethod
    def _policy() -> RetryPolicy:
        return RetryPolicy(
            config=RetryPolicyConfig(max_attempts=3, domain="payment"),
            backoff=ConstantBackoff(delay=0.0),
            sleeper=lambda _: None,
        )

    def test_failing_call_runs_exactly_once_under_dry_run(self):
        # Given a call that always fails and max_attempts=3
        fn, state = _counting(raises=ConnectionError("boom"))
        policy = self._policy()
        # When executed under observe-only
        with dry_run_active():
            result = policy.execute(fn)
        # Then the retry intervention is suppressed — exactly one attempt
        assert state["n"] == 1
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.total_attempts == 1

    def test_failure_metadata_has_no_should_dlq_under_dry_run(self):
        # The single-attempt path sets no should_dlq, so the DLQ sink also stays
        # observe-only downstream.
        fn, _ = _counting(raises=ConnectionError("boom"))
        policy = self._policy()
        with dry_run_active():
            result = policy.execute(fn)
        assert "should_dlq" not in result.metadata

    def test_success_returns_immediately_under_dry_run(self):
        fn, state = _counting(returns="ok")
        policy = self._policy()
        with dry_run_active():
            result = policy.execute(fn)
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"
        assert state["n"] == 1

    def test_failing_call_retries_when_not_dry_run(self):
        # Control: without dry-run the same failing call IS retried max_attempts.
        fn, state = _counting(raises=ConnectionError("boom"))
        policy = self._policy()
        result = policy.execute(fn)
        assert state["n"] == 3
        assert result.outcome == PolicyOutcome.FAILURE

    def test_logs_would_have_decision_under_dry_run(self):
        fn, _ = _counting(raises=ConnectionError("boom"))
        policy = self._policy()
        with dry_run_active(), structlog.testing.capture_logs() as logs:
            policy.execute(fn)
        would_have = [
            e
            for e in logs
            if e.get("event") == "execution_mode.intervention_suppressed"
        ]
        assert len(would_have) == 1
        assert would_have[0]["action"] == "retry"


class TestDLQSinkDryRun:
    """DLQSink.handle_failure observe-only branch (suppress the DLQ write)."""

    @staticmethod
    def _result(should_dlq: bool) -> PolicyResult:
        return PolicyResult(
            outcome=PolicyOutcome.FAILURE,
            error=RuntimeError("final failure"),
            total_attempts=3,
            executed_policies=["retry"],
            metadata={"should_dlq": should_dlq, "domain": "payment"},
        )

    def test_store_skipped_and_returns_none_under_dry_run(self):
        sink = DLQSink()
        error = RuntimeError("final failure")
        with (
            patch(
                "baldur.services.retry_handler.sinks.store_to_dlq", autospec=True
            ) as mock_store,
            dry_run_active(),
        ):
            out = sink.handle_failure(error, None, self._result(should_dlq=True))
        assert out is None
        mock_store.assert_not_called()

    def test_logs_would_store_decision_under_dry_run(self):
        sink = DLQSink()
        error = RuntimeError("final failure")
        with (
            patch("baldur.services.retry_handler.sinks.store_to_dlq", autospec=True),
            dry_run_active(),
            structlog.testing.capture_logs() as logs,
        ):
            sink.handle_failure(error, None, self._result(should_dlq=True))
        would_have = [
            e
            for e in logs
            if e.get("event") == "execution_mode.intervention_suppressed"
        ]
        assert len(would_have) == 1
        assert would_have[0]["action"] == "dlq_store"

    def test_store_invoked_when_not_dry_run(self):
        # Control: should_dlq=True without dry-run DOES write to the DLQ.
        sink = DLQSink()
        error = RuntimeError("final failure")
        with patch(
            "baldur.services.retry_handler.sinks.store_to_dlq",
            return_value=SimpleNamespace(success=True, dlq_id="dlq-1", error=None),
        ) as mock_store:
            out = sink.handle_failure(error, None, self._result(should_dlq=True))
        mock_store.assert_called_once()
        assert out == "dlq-1"

    def test_should_dlq_false_returns_none_before_gate(self):
        # When should_dlq is False the sink returns None regardless of dry-run
        # (the gate is reached only on the would-store path).
        sink = DLQSink()
        error = RuntimeError("final failure")
        with patch(
            "baldur.services.retry_handler.sinks.store_to_dlq", autospec=True
        ) as mock_store:
            out = sink.handle_failure(error, None, self._result(should_dlq=False))
        assert out is None
        mock_store.assert_not_called()
