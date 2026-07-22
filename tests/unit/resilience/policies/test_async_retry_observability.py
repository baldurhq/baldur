"""Async retry observability parity — bus emission + metric recording.

Covers ``AsyncRetryPolicy``'s terminal observability, brought to parity with the
synchronous ``RetryPolicy``:

- exhaustion emits the EventBus ``RETRY_EXHAUSTED`` event with the sync payload
  shape (including ``PolicyContext`` identifiers),
- every terminal (loop success, single-attempt success/failure, and each
  exhaustion cause) records to the canonical retry series with the sync-equal
  outcome value and an attempt count equal to ``total_attempts``,
- the global kill switch (``BALDUR_RETRY_ENABLED=false``) runs the function once,
- both channels are fail-open and independent,
- the bus emit is ``asyncio.to_thread``-offloaded so a blocking handler does not
  stall the event loop,
- the shared ``REASON_TO_OUTCOME`` table is contract-checked.

Seams follow the sync retry tests verbatim: the bus via
``baldur.services.event_bus.get_event_bus``, the metric via
``baldur.services.metrics.recorders.record_retry_attempt``, the kill switch via
``baldur.settings.retry.get_retry_settings``. Metric assertions inspect the
facade call args (no Prometheus registry scraping needed).
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur.core.backoff import ConstantBackoff
from baldur.interfaces.resilience_policy import PolicyContext, PolicyOutcome
from baldur.resilience.policies.async_retry import AsyncRetryPolicy
from baldur.services.event_bus import BaldurEventBus
from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.observability import REASON_TO_OUTCOME
from baldur.services.retry_handler.policy import RetryPolicy

_DOMAIN = "async_obs"
_GET_EVENT_BUS = "baldur.services.event_bus.get_event_bus"
_RECORD_RETRY_ATTEMPT = "baldur.services.metrics.recorders.record_retry_attempt"
_GET_RETRY_SETTINGS = "baldur.settings.retry.get_retry_settings"
_GET_REMAINING_MS = "baldur.scaling.deadline_context.get_remaining_ms"
_INTERVENTION_SUPPRESSED = (
    "baldur.resilience.policies.async_retry.intervention_suppressed"
)


# --- test doubles ---------------------------------------------------------


async def _ok():
    return "ok"


async def _always_fail_conn():
    raise ConnectionError("transient")


async def _always_fail_value():
    raise ValueError("bad value")


def _raise_conn_sync():
    """Synchronous raising callable for the sync RetryPolicy reference path."""
    raise ConnectionError("transient")


def _flaky_succeeding_on(attempt: int):
    """Return an async fn that fails until ``attempt``, then returns 'ok'."""
    state = {"n": 0}

    async def fn():
        state["n"] += 1
        if state["n"] < attempt:
            raise ConnectionError("transient")
        return "ok"

    return fn


# --- terminal-matrix drivers (SC2) ----------------------------------------
# Each driver constructs a policy and drives it to exactly one terminal, then
# returns the PolicyResult. Distinct per-terminal setup lives in its own driver
# so the parametrized test body stays a single uniform assertion. Zero-delay /
# oversized-delay backoffs make every exit deterministic with no wall-clock wait
# (a budget break fires *before* the sleep, so the 10s delay is never slept).


async def _drive_exhaustion_max_attempts():
    policy = AsyncRetryPolicy(
        max_retries=1, domain=_DOMAIN, backoff=ConstantBackoff(delay=0.0)
    )
    return await policy.execute(_always_fail_conn)


async def _drive_exhaustion_non_retryable():
    policy = AsyncRetryPolicy(
        max_retries=3, domain=_DOMAIN, non_retryable_exceptions=(ValueError,)
    )
    return await policy.execute(_always_fail_value)


async def _drive_exhaustion_max_elapsed():
    policy = AsyncRetryPolicy(
        max_retries=3,
        domain=_DOMAIN,
        max_elapsed=0.01,
        backoff=ConstantBackoff(delay=10.0),
    )
    return await policy.execute(_always_fail_conn)


async def _drive_exhaustion_deadline():
    policy = AsyncRetryPolicy(
        max_retries=3, domain=_DOMAIN, backoff=ConstantBackoff(delay=10.0)
    )
    # A tiny request-deadline makes the budget exit resolve to "deadline" (the
    # ContextVar side wins over the absent knob). Patching the reader keeps the
    # ContextVar itself untouched, so nothing leaks to a sibling test.
    with patch(_GET_REMAINING_MS, return_value=5.0):
        return await policy.execute(_always_fail_conn)


async def _drive_loop_success():
    policy = AsyncRetryPolicy(
        max_retries=3, domain=_DOMAIN, backoff=ConstantBackoff(delay=0.0)
    )
    return await policy.execute(_flaky_succeeding_on(2))


async def _drive_single_attempt_success():
    with patch(_GET_RETRY_SETTINGS, return_value=SimpleNamespace(enabled=False)):
        policy = AsyncRetryPolicy(max_retries=3, domain=_DOMAIN)
    return await policy.execute(_ok)


async def _drive_single_attempt_failure():
    with patch(_GET_RETRY_SETTINGS, return_value=SimpleNamespace(enabled=False)):
        policy = AsyncRetryPolicy(max_retries=3, domain=_DOMAIN)
    return await policy.execute(_always_fail_conn)


_METRIC_MATRIX = [
    (_drive_exhaustion_max_attempts, "exhausted", 2),
    (_drive_exhaustion_non_retryable, "non_retryable", 1),
    (_drive_exhaustion_max_elapsed, "max_elapsed", 1),
    (_drive_exhaustion_deadline, "deadline", 1),
    (_drive_loop_success, "success", 2),
    (_drive_single_attempt_success, "success", 1),
    (_drive_single_attempt_failure, "failure", 1),
]
_METRIC_IDS = [
    "max_attempts_exhausted",
    "non_retryable",
    "max_elapsed",
    "deadline",
    "loop_success",
    "single_attempt_success",
    "single_attempt_failure",
]


# =============================================================================
# Contract — the shared reason -> outcome vocabulary
# =============================================================================


class TestReasonToOutcomeContract:
    """REASON_TO_OUTCOME maps every retry exit cause to its outcome-label value."""

    def test_reason_to_outcome_holds_exactly_the_six_spec_pairs(self):
        """The shared table is exactly the six design pairs — no more, no less."""
        assert REASON_TO_OUTCOME == {
            "max_attempts": "exhausted",
            "non_retryable": "non_retryable",
            "retry_budget": "retry_budget",
            "max_elapsed": "max_elapsed",
            "deadline": "deadline",
            "rate_limit_deferred": "rate_limit_deferred",
        }


# =============================================================================
# Behavior — exhaustion emits RETRY_EXHAUSTED with the sync payload shape (SC1)
# =============================================================================


class TestAsyncRetryExhaustionEventBehavior:
    """Async exhaustion emits RETRY_EXHAUSTED with a payload identical in shape
    to the sync RetryPolicy's, including PolicyContext identifiers."""

    @pytest.mark.asyncio
    async def test_exhaustion_emits_one_retry_exhausted_event_from_retry_policy(self):
        """Exhaustion emits exactly one RETRY_EXHAUSTED event tagged retry_policy."""
        mock_bus = MagicMock(spec=BaldurEventBus)
        policy = AsyncRetryPolicy(max_retries=0, domain=_DOMAIN)

        with patch(_GET_EVENT_BUS, return_value=mock_bus):
            result = await policy.execute(_always_fail_conn)

        assert result.outcome == PolicyOutcome.FAILURE
        mock_bus.emit.assert_called_once()
        call = mock_bus.emit.call_args
        assert call.kwargs["event_type"] == EventType.RETRY_EXHAUSTED
        assert call.kwargs["source"] == "retry_policy"

    @pytest.mark.asyncio
    async def test_exhaustion_event_includes_context_identifiers(self):
        """order_id / user_id / trace_id from PolicyContext land in the payload."""
        mock_bus = MagicMock(spec=BaldurEventBus)
        policy = AsyncRetryPolicy(max_retries=0, domain=_DOMAIN)
        ctx = PolicyContext(order_id="ORD-1", user_id="USR-2", trace_id="trace-3")

        with patch(_GET_EVENT_BUS, return_value=mock_bus):
            await policy.execute(_always_fail_conn, context=ctx)

        data = mock_bus.emit.call_args.kwargs["data"]
        assert data["order_id"] == "ORD-1"
        assert data["user_id"] == "USR-2"
        assert data["trace_id"] == "trace-3"

    @pytest.mark.asyncio
    async def test_async_event_payload_key_set_matches_sync(self):
        """The async and sync exhaustion events carry the identical key set."""
        # Given — the same context drives both policies to a single-attempt
        # exhaustion with no wall-clock budget (so both omit the same optional
        # keys). Both emit through the shared helper.
        ctx = PolicyContext(order_id="ORD-1", user_id="USR-2", trace_id="trace-3")

        # When — capture the sync reference event
        sync_bus = MagicMock(spec=BaldurEventBus)
        sync_policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=1, domain=_DOMAIN),
            sleeper=lambda _: None,
        )
        with patch(_GET_EVENT_BUS, return_value=sync_bus):
            sync_policy.execute(_raise_conn_sync, context=ctx)
        sync_keys = set(sync_bus.emit.call_args.kwargs["data"].keys())

        # When — capture the async event under test
        async_bus = MagicMock(spec=BaldurEventBus)
        async_policy = AsyncRetryPolicy(max_retries=0, domain=_DOMAIN)
        with patch(_GET_EVENT_BUS, return_value=async_bus):
            await async_policy.execute(_always_fail_conn, context=ctx)
        async_keys = set(async_bus.emit.call_args.kwargs["data"].keys())

        # Then
        assert async_keys == sync_keys

    @pytest.mark.asyncio
    async def test_loop_success_emits_no_event(self):
        """A successful terminal is not an exhaustion — no event is emitted."""
        mock_bus = MagicMock(spec=BaldurEventBus)
        policy = AsyncRetryPolicy(
            max_retries=3, domain=_DOMAIN, backoff=ConstantBackoff(delay=0.0)
        )

        with patch(_GET_EVENT_BUS, return_value=mock_bus):
            result = await policy.execute(_flaky_succeeding_on(2))

        assert result.outcome == PolicyOutcome.SUCCESS
        assert mock_bus.emit.call_count == 0


# =============================================================================
# Behavior — every terminal records the canonical retry series (SC2)
# =============================================================================


class TestAsyncRetryMetricsParityBehavior:
    """Every async terminal records to the retry series with the sync-equal
    outcome value and an attempt count equal to PolicyResult.total_attempts."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("driver", "expected_outcome", "expected_attempts"),
        _METRIC_MATRIX,
        ids=_METRIC_IDS,
    )
    async def test_terminal_records_outcome_with_total_attempts(
        self, driver, expected_outcome, expected_attempts
    ):
        """Each terminal calls record_retry_attempt(domain, total_attempts, outcome)."""
        with patch(_RECORD_RETRY_ATTEMPT, autospec=True) as mock_record:
            result = await driver()

        mock_record.assert_called_once_with(
            _DOMAIN, expected_attempts, expected_outcome
        )
        # Boundary: the recorded attempt count is exactly total_attempts.
        assert result.total_attempts == expected_attempts


# =============================================================================
# Behavior — single-attempt paths record but never emit (SC3)
# =============================================================================


class TestAsyncSingleAttemptObservabilityBehavior:
    """The single-attempt paths (globally disabled / observe-only) record the
    terminal metric but emit no bus event — sync parity, a single attempt is
    not an exhaustion."""

    @pytest.mark.asyncio
    async def test_disabled_path_records_failure_metric_without_event(self):
        """Disabled + failing call records 'failure' and emits no event."""
        mock_bus = MagicMock(spec=BaldurEventBus)
        with patch(_GET_RETRY_SETTINGS, return_value=SimpleNamespace(enabled=False)):
            policy = AsyncRetryPolicy(max_retries=3, domain=_DOMAIN)

        with patch(_GET_EVENT_BUS, return_value=mock_bus):
            with patch(_RECORD_RETRY_ATTEMPT, autospec=True) as mock_record:
                result = await policy.execute(_always_fail_conn)

        assert result.outcome == PolicyOutcome.FAILURE
        assert mock_bus.emit.call_count == 0
        mock_record.assert_called_once_with(_DOMAIN, 1, "failure")

    @pytest.mark.asyncio
    async def test_observe_only_path_records_success_metric_without_event(self):
        """Observe-only + successful call records 'success' and emits no event."""
        mock_bus = MagicMock(spec=BaldurEventBus)
        policy = AsyncRetryPolicy(max_retries=3, domain=_DOMAIN)

        with patch(_INTERVENTION_SUPPRESSED, return_value=True):
            with patch(_GET_EVENT_BUS, return_value=mock_bus):
                with patch(_RECORD_RETRY_ATTEMPT, autospec=True) as mock_record:
                    result = await policy.execute(_ok)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert mock_bus.emit.call_count == 0
        mock_record.assert_called_once_with(_DOMAIN, 1, "success")


# =============================================================================
# Behavior — global kill switch (SC4)
# =============================================================================


class TestAsyncKillSwitchBehavior:
    """BALDUR_RETRY_ENABLED=false makes execute run the function once, no retry."""

    @pytest.mark.asyncio
    async def test_disabled_runs_failing_function_exactly_once(self):
        """A disabled policy calls a failing function once, not to exhaustion."""
        calls = {"n": 0}

        async def failing():
            calls["n"] += 1
            raise ConnectionError("transient")

        with patch(_GET_RETRY_SETTINGS, return_value=SimpleNamespace(enabled=False)):
            policy = AsyncRetryPolicy(max_retries=3, domain=_DOMAIN)
        result = await policy.execute(failing)

        # Single attempt — the old behavior (retry to exhaustion = 4 calls) is gone.
        assert calls["n"] == 1
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.total_attempts == 1

    @pytest.mark.asyncio
    async def test_disabled_preserves_successful_result(self):
        """A disabled policy still returns the function's successful value."""
        with patch(_GET_RETRY_SETTINGS, return_value=SimpleNamespace(enabled=False)):
            policy = AsyncRetryPolicy(max_retries=3, domain=_DOMAIN)
        result = await policy.execute(_ok)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"


# =============================================================================
# Behavior — fail-open on both channels, independently (SC7)
# =============================================================================


class TestAsyncObservabilityFailOpenBehavior:
    """A raising bus or a raising recorder leaves the PolicyResult intact; the
    two channels are independent, so the healthy one still fires."""

    @pytest.mark.asyncio
    async def test_raising_event_bus_preserves_result_and_still_records_metric(self):
        """A bus fault is swallowed; the metric channel still records."""
        policy = AsyncRetryPolicy(max_retries=0, domain=_DOMAIN)
        sentinel = ConnectionError("permanent")

        async def failing():
            raise sentinel

        with patch(_GET_EVENT_BUS, side_effect=RuntimeError("bus down")):
            with patch(_RECORD_RETRY_ATTEMPT, autospec=True) as mock_record:
                result = await policy.execute(failing)

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.error is sentinel
        assert result.total_attempts == 1
        mock_record.assert_called_once_with(_DOMAIN, 1, "exhausted")

    @pytest.mark.asyncio
    async def test_raising_metric_recorder_preserves_result_and_still_emits_event(self):
        """A recorder fault is swallowed; the bus channel still emits."""
        mock_bus = MagicMock(spec=BaldurEventBus)
        policy = AsyncRetryPolicy(max_retries=0, domain=_DOMAIN)
        sentinel = ConnectionError("permanent")

        async def failing():
            raise sentinel

        with patch(_RECORD_RETRY_ATTEMPT, side_effect=RuntimeError("recorder down")):
            with patch(_GET_EVENT_BUS, return_value=mock_bus):
                result = await policy.execute(failing)

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.error is sentinel
        mock_bus.emit.assert_called_once()


# =============================================================================
# Behavior — the bus emit is to_thread-offloaded (SC8)
# =============================================================================


class TestAsyncEmitOffloadBehavior:
    """The bus emit runs via asyncio.to_thread, so a blocking bus handler does
    not stall the event loop — a bare (non-offloaded) emit would."""

    @pytest.mark.asyncio
    async def test_slow_bus_emit_does_not_block_the_event_loop(self):
        """While the emit blocks in its worker thread, another coroutine runs.

        Deterministic (no wall-clock assertion): the emit blocks on a
        threading.Event that the observer releases only after proving the loop
        stayed free. A bare emit would run the block inside the coroutine's own
        call stack, so the observer could not run until the emit finished.
        """
        emit_entered = threading.Event()
        release = threading.Event()
        emit_completed: list[bool] = []

        def blocking_emit(**kwargs):
            emit_entered.set()
            release.wait(timeout=5.0)
            emit_completed.append(True)

        mock_bus = MagicMock(spec=BaldurEventBus)
        mock_bus.emit.side_effect = blocking_emit
        policy = AsyncRetryPolicy(max_retries=0, domain=_DOMAIN)

        async def exhaust():
            await policy.execute(_always_fail_conn)

        async def observer() -> list[bool]:
            while not emit_entered.is_set():
                await asyncio.sleep(0.005)
            snapshot = list(emit_completed)  # empty => loop free while emit blocked
            release.set()
            return snapshot

        with patch(_GET_EVENT_BUS, return_value=mock_bus):
            _, observed = await asyncio.gather(exhaust(), observer())

        # The observer ran while the emit was still blocked (loop was free).
        assert observed == []
        # The emit did complete once released (offloaded call is still awaited).
        assert emit_completed == [True]
