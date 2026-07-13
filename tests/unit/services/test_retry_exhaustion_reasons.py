"""Exhaustion-cause disambiguation tests (704 D3).

Target: ``services/retry_handler/policy.py`` (``reason`` plumbing on the
``retry.exhausted`` event, the Prometheus outcome value, and the FAILURE
metadata), ``bridges/tenacity/{callbacks,instrument}.py``
(``reason="stop_condition"``), and ``services/retry_handler/sinks.py``
(DLQ metadata ``reason`` passthrough).

The five sync exit causes each carry a distinct ``reason`` across the event
payload, the metric ``outcome`` value, and the FAILURE ``metadata["reason"]``:

    max_attempts (metric "exhausted") / non_retryable / retry_budget /
    max_elapsed / deadline

The polymorphic-break fix is asserted directly: a *retryable* exception that
exhausts ``max_attempts`` is attributed to ``max_attempts`` — NOT
``non_retryable`` (which is reserved for a classification stop).
"""

from __future__ import annotations

from unittest.mock import patch

from baldur.core.backoff import ConstantBackoff
from baldur.interfaces.resilience_policy import PolicyOutcome, PolicyResult
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import RetryPolicy

_GET_REMAINING_MS = "baldur.scaling.deadline_context.get_remaining_ms"
_POLICY_MONOTONIC = "baldur.services.retry_handler.policy.time.monotonic"
_GET_EVENT_BUS = "baldur.services.event_bus.get_event_bus"
_RECORD_RETRY = "baldur.services.metrics.recorders.record_retry_attempt"


class _AdvancingClock:
    """A deterministic monotonic clock advanced only by the business function."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _DenyingRetryBudget:
    """An AdaptiveRetryBudget stand-in that denies the first retry.

    A plain class (not a Mock) so it is invisible to the G67 ratchet while giving
    the ``retry_budget`` exit cause a deterministic trigger.
    """

    def record_request(self, is_retry: bool) -> None:  # noqa: D102
        pass

    def should_allow_retry(self) -> bool:  # noqa: D102
        return False

    def get_stats(self) -> dict:  # noqa: D102
        return {"denied": True}


def _always_connection_error():
    """A retryable failure that never resolves (drives attempt exhaustion)."""
    raise ConnectionError("permanent")


def _always_value_error():
    """A non-retryable failure (classification stop on attempt 1)."""
    raise ValueError("bad-value")


def _reason_policy(
    *,
    max_attempts: int = 3,
    max_elapsed: float | None = None,
    non_retryable: tuple[type[Exception], ...] | None = None,
    retry_budget=None,
    domain: str = "test_reason",
) -> RetryPolicy:
    """RetryPolicy on the real loop with zero-delay, no-wait backoff."""
    config_kwargs: dict = {
        "max_attempts": max_attempts,
        "max_elapsed": max_elapsed,
        "domain": domain,
    }
    if non_retryable is not None:
        config_kwargs["non_retryable_exceptions"] = non_retryable
    return RetryPolicy(
        config=RetryPolicyConfig(**config_kwargs),
        backoff=ConstantBackoff(delay=0.0),
        sleeper=lambda _: None,
        retry_budget=retry_budget,
    )


def _execute_capturing(policy: RetryPolicy, fn):
    """Run ``execute`` capturing the exhausted-event payload and metric outcome.

    Returns ``(result, event_data, metric_outcome)``. Both the EventBus and the
    metric recorder are stubbed via with-form patches (auto-created mocks — no
    spec-less-mock creation), so the run is hermetic.
    """
    with patch(_GET_EVENT_BUS) as get_bus, patch(_RECORD_RETRY) as record:
        result = policy.execute(fn)
    event_data = get_bus.return_value.emit.call_args.kwargs["data"]
    metric_outcome = record.call_args.args[2]
    return result, event_data, metric_outcome


# =============================================================================
# Behavior — the five exit causes carry distinct reason / outcome / metadata
# =============================================================================


class TestRetryExhaustionReasonsBehavior:
    """Each exit cause plumbs its ``reason`` to event, metric, and metadata."""

    def test_retryable_exhaustion_is_max_attempts_not_non_retryable(self):
        """A retryable exception out of attempts → ``max_attempts`` /
        ``exhausted`` — NEVER ``non_retryable`` (polymorphic-break fix)."""
        policy = _reason_policy(max_attempts=2)
        result, event_data, outcome = _execute_capturing(
            policy, _always_connection_error
        )
        assert result.outcome == PolicyOutcome.FAILURE
        assert event_data["reason"] == "max_attempts"
        assert event_data["reason"] != "non_retryable"
        assert outcome == "exhausted"
        assert result.metadata["reason"] == "max_attempts"

    def test_non_retryable_abort_is_non_retryable(self):
        """A non-retryable classification stop on attempt 1 → ``non_retryable``,
        and moves OUT of the ``exhausted`` metric value."""
        policy = _reason_policy(max_attempts=3, non_retryable=(ValueError,))
        result, event_data, outcome = _execute_capturing(policy, _always_value_error)
        assert result.total_attempts == 1
        assert event_data["reason"] == "non_retryable"
        assert outcome == "non_retryable"
        assert result.metadata["reason"] == "non_retryable"

    def test_retry_budget_break_is_retry_budget(self):
        """An adaptive-budget denial → ``retry_budget`` across the triple."""
        policy = _reason_policy(max_attempts=5, retry_budget=_DenyingRetryBudget())
        result, event_data, outcome = _execute_capturing(
            policy, _always_connection_error
        )
        assert event_data["reason"] == "retry_budget"
        assert outcome == "retry_budget"
        assert result.metadata["reason"] == "retry_budget"

    def test_max_elapsed_budget_break_is_max_elapsed(self):
        """The policy knob firing → ``max_elapsed``, with elapsed on the event."""
        clock = _AdvancingClock()

        def failing():
            clock.advance(0.2)  # blows the 0.1s budget in one attempt
            raise ConnectionError("fail")

        policy = _reason_policy(max_attempts=5, max_elapsed=0.1)
        with patch(_POLICY_MONOTONIC, clock):
            result, event_data, outcome = _execute_capturing(policy, failing)

        assert event_data["reason"] == "max_elapsed"
        assert "elapsed" in event_data
        assert outcome == "max_elapsed"
        assert result.metadata["reason"] == "max_elapsed"

    def test_deadline_budget_break_is_deadline(self):
        """A request-scoped deadline firing → ``deadline`` (knob unset)."""
        clock = _AdvancingClock()

        def failing():
            clock.advance(0.2)  # blows the 100ms deadline budget
            raise ConnectionError("fail")

        policy = _reason_policy(max_attempts=5, max_elapsed=None)
        with (
            patch(_GET_REMAINING_MS, return_value=100.0),
            patch(_POLICY_MONOTONIC, clock),
        ):
            result, event_data, outcome = _execute_capturing(policy, failing)

        assert event_data["reason"] == "deadline"
        assert outcome == "deadline"
        assert result.metadata["reason"] == "deadline"


# =============================================================================
# Behavior — tenacity bridge emits the honest bridge-only reason
# =============================================================================


class TestTenacityBridgeReason:
    """Both bridge exhausted-event emitters carry ``reason="stop_condition"``."""

    def test_callbacks_exhausted_event_carries_stop_condition(self):
        from baldur.bridges.tenacity.callbacks import _emit_retry_exhausted_event

        with patch(_GET_EVENT_BUS) as get_bus:
            _emit_retry_exhausted_event(
                domain="payment", attempts=3, last_error=RuntimeError("boom")
            )
        data = get_bus.return_value.emit.call_args.kwargs["data"]
        assert data["reason"] == "stop_condition"

    def test_instrument_exhausted_event_carries_stop_condition(self):
        from baldur.bridges.tenacity.instrument import _emit_retry_exhausted

        with patch(_GET_EVENT_BUS) as get_bus:
            _emit_retry_exhausted(attempts=2, last_error=ValueError("nope"))
        data = get_bus.return_value.emit.call_args.kwargs["data"]
        assert data["reason"] == "stop_condition"


# =============================================================================
# Behavior — DLQSink passes the exhaustion reason through to DLQ metadata
# =============================================================================


class TestDLQSinkReasonPassthrough:
    """``_build_dlq_metadata`` forwards ``reason`` so DLQ triage can read it."""

    def test_build_dlq_metadata_forwards_reason(self):
        from baldur.services.retry_handler.sinks import DLQSink

        policy_result = PolicyResult(
            outcome=PolicyOutcome.FAILURE,
            error=ConnectionError("x"),
            total_attempts=3,
            executed_policies=["retry"],
            metadata={
                "reason": "max_elapsed",
                "should_dlq": True,
                "domain": "payment",
                "retry_history": [],
                "max_attempts": 3,
            },
        )
        metadata, domain = DLQSink._build_dlq_metadata(policy_result)
        assert metadata["reason"] == "max_elapsed"
        assert domain == "payment"

    def test_build_dlq_metadata_reason_defaults_to_none_when_absent(self):
        from baldur.services.retry_handler.sinks import DLQSink

        policy_result = PolicyResult(
            outcome=PolicyOutcome.FAILURE,
            error=ConnectionError("x"),
            total_attempts=1,
            executed_policies=["retry"],
            metadata={"should_dlq": True, "domain": "default"},
        )
        metadata, _domain = DLQSink._build_dlq_metadata(policy_result)
        assert metadata["reason"] is None
