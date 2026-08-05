"""``ReplayExecutionMixin._execute_replay`` is where the operator replay surface
enters the replay metrics.

Every admin replay — single-entry retry, force-redrive, and the PRO batch and
throttle-aware overlays — converges on this one primitive, and it previously
recorded a duration and nothing else. The attempt, outcome and duration families
therefore described only the replay service's own stack, so a console retry
produced no measurable replay at all.

Two claims are covered here, both of which are about what the *families* say
rather than about which method was called:

- **Emission pairing.** Attempts and outcomes move together or not at all. A
  lone outcome pushes an operator's ``outcomes / attempts`` success-rate panel
  above 1; a lone attempt sinks it. The two gate refusals emit neither, so an
  attempt never counts an entry whose handler was not reached.
- **Duration honesty.** The replay-duration histogram observes only when the
  handler actually ran. A gate refusal costs microseconds, and mixing those
  non-events with second-scale replays drags the reported quantile toward zero
  — a hundred blocked entries beside five three-second replays merge to roughly
  0.1 ms, which the console would render as a true-looking ``replay p95``.
  The refusal keeps its own visibility as a ``duration_ms`` field on the
  ``dlq.replay_blocked`` WARNING it already logged.

Assertions read the process-global prometheus registry, so they are **deltas
over a family total** rather than absolute reads of one domain's label child.
That is deliberate: ``resolve_domain_label`` folds an unregistered domain onto
the shared fallback label, so a per-domain read would report 0 even when the
emission fired, and registering a domain per test would burn the registry's
domain cap. Deltas are sound because a family total is monotone and pytest runs
a worker's tests serially.
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from baldur.interfaces.repositories import FailedOperationData, FailedOperationStatus
from baldur.services.dlq_read import ReplayExecutionMixin
from baldur.services.dlq_read.replay_execution import REPLAY_TYPE_SINGLE
from baldur.services.replay_service import ReplayHandler, register_replay_handler
from baldur.services.replay_service.models import ReplayResult

_ATTEMPTS = "baldur_replay_attempts_total"
_OUTCOMES = "baldur_replay_outcomes_total"
_REPLAY_DURATION = "baldur_dlq_replay_duration_seconds"


# =============================================================================
# Registry readers — family totals, never a single domain's label child
# =============================================================================


def _counter_total(family: str, labels: dict[str, str] | None = None) -> float:
    """Sum a counter family's ``_total`` samples across every label set."""
    from prometheus_client import REGISTRY

    from baldur.adapters.prometheus_adapter import _family_name

    total = 0.0
    for metric in REGISTRY.collect():
        if metric.name not in (family, _family_name(family)):
            continue
        for sample in metric.samples:
            if not sample.name.endswith("_total"):
                continue
            if labels and any(sample.labels.get(k) != v for k, v in labels.items()):
                continue
            total += sample.value
    return total


def _histogram_observations(family: str) -> float:
    """Sum a histogram family's ``_count`` samples across every label set."""
    from prometheus_client import REGISTRY

    total = 0.0
    for metric in REGISTRY.collect():
        if metric.name != family:
            continue
        for sample in metric.samples:
            if sample.name.endswith("_count"):
                total += sample.value
    return total


class _MetricSnapshot:
    """Family totals taken before the call under test, diffed after it."""

    def __init__(self) -> None:
        self.attempts = _counter_total(_ATTEMPTS)
        self.attempts_single = _counter_total(
            _ATTEMPTS, {"replay_type": REPLAY_TYPE_SINGLE}
        )
        self.successes = _counter_total(_OUTCOMES, {"outcome": "success"})
        self.failures = _counter_total(_OUTCOMES, {"outcome": "failure"})
        self.durations = _histogram_observations(_REPLAY_DURATION)

    @property
    def attempts_delta(self) -> float:
        return _counter_total(_ATTEMPTS) - self.attempts

    @property
    def attempts_single_delta(self) -> float:
        return (
            _counter_total(_ATTEMPTS, {"replay_type": REPLAY_TYPE_SINGLE})
            - self.attempts_single
        )

    @property
    def success_delta(self) -> float:
        return _counter_total(_OUTCOMES, {"outcome": "success"}) - self.successes

    @property
    def failure_delta(self) -> float:
        return _counter_total(_OUTCOMES, {"outcome": "failure"}) - self.failures

    @property
    def outcome_delta(self) -> float:
        return self.success_delta + self.failure_delta

    @property
    def duration_delta(self) -> float:
        return _histogram_observations(_REPLAY_DURATION) - self.durations


# =============================================================================
# Fixtures
# =============================================================================


class _StubReplayHandler(ReplayHandler):
    """Deterministic handler so every exit of ``_execute_replay`` is reachable."""

    def __init__(
        self,
        domain: str,
        *,
        succeed: bool = True,
        raises: Exception | None = None,
        can_replay_reason: str | None = None,
    ) -> None:
        self._domain = domain
        self._succeed = succeed
        self._raises = raises
        self._can_replay_reason = can_replay_reason
        self.replay_calls: list[str] = []

    @property
    def domain(self) -> str:
        return self._domain

    def can_replay(self, failed_op: FailedOperationData) -> tuple[bool, str]:
        if self._can_replay_reason is not None:
            return False, self._can_replay_reason
        return True, ""

    def replay(self, failed_op: FailedOperationData) -> ReplayResult:
        self.replay_calls.append(failed_op.id)
        if self._raises is not None:
            raise self._raises
        if self._succeed:
            return ReplayResult.succeeded(failed_op.id, "stub success")
        return ReplayResult.failed(failed_op.id, "stub failure")


class _Executor(ReplayExecutionMixin):
    """Bare mixin host — ``_execute_replay`` reads nothing off ``self``."""


@pytest.fixture
def executor() -> _Executor:
    return _Executor()


@pytest.fixture
def register_handler():
    """Register stub replay handlers, restoring the registry afterwards."""
    from baldur.services.replay_service import handlers as _handlers

    snapshot = dict(_handlers._replay_handlers)

    def _register(domain: str, **kwargs) -> _StubReplayHandler:
        handler = _StubReplayHandler(domain, **kwargs)
        register_replay_handler(handler)
        return handler

    yield _register

    _handlers._replay_handlers.clear()
    _handlers._replay_handlers.update(snapshot)


@pytest.fixture(autouse=True)
def _metrics_backend_present():
    """Make sure the replay recorders exist before a snapshot is taken.

    ``BaldurMetrics`` constructs its families on first ``get_metrics()``, so a
    snapshot taken in a process that never called it would read 0 and then
    compare against a family that only appeared mid-test.
    """
    from baldur.metrics.prometheus import get_metrics

    get_metrics()


def _entry(
    domain: str,
    *,
    entry_id: str = "entry-1",
    truncated: bool = False,
) -> FailedOperationData:
    return FailedOperationData(
        id=entry_id,
        domain=domain,
        failure_type="timeout",
        status=FailedOperationStatus.REPLAYING.value,
        request_data={"_truncated": True} if truncated else {"payload": "intact"},
    )


# =============================================================================
# D3 — emission at the convergence point
# =============================================================================


class TestReplayExecutionEmission:
    """Attempts and outcomes move together, and only when the handler ran."""

    def test_successful_replay_records_one_attempt_and_one_success(
        self, executor, register_handler
    ):
        # Given a registered handler that succeeds
        handler = register_handler("emission_success")
        entry = _entry("emission_success")
        before = _MetricSnapshot()

        # When the operator surface replays the entry
        assert executor._execute_replay(entry) is True

        # Then the handler ran and both families moved by exactly one
        assert handler.replay_calls == [entry.id]
        assert before.attempts_delta == 1
        assert before.success_delta == 1
        assert before.failure_delta == 0

    def test_successful_replay_records_the_attempt_under_the_single_replay_type(
        self, executor, register_handler
    ):
        # "single" is the existing replay_type vocabulary — the operator
        # surface replays one entry per call, so no new published label value
        # enters the attempts family.
        register_handler("emission_label")
        before = _MetricSnapshot()

        executor._execute_replay(_entry("emission_label"))

        assert before.attempts_single_delta == 1

    def test_failed_replay_records_one_attempt_and_one_failure(
        self, executor, register_handler
    ):
        # Given a handler whose ReplayResult reports failure
        register_handler("emission_failure", succeed=False)
        before = _MetricSnapshot()

        # When
        assert executor._execute_replay(_entry("emission_failure")) is False

        # Then the attempt still counts — the handler DID run
        assert before.attempts_delta == 1
        assert before.failure_delta == 1
        assert before.success_delta == 0

    def test_crashing_handler_records_a_failure_outcome_and_propagates(
        self, executor, register_handler
    ):
        # Given a handler that raises rather than returning a result
        register_handler("emission_crash", raises=RuntimeError("handler exploded"))
        before = _MetricSnapshot()

        # When / Then: the caller's own error handling still sees the crash
        with pytest.raises(RuntimeError, match="handler exploded"):
            executor._execute_replay(_entry("emission_crash"))

        # ...and the completion event fired anyway, so the pair stays balanced
        assert before.attempts_delta == 1
        assert before.failure_delta == 1
        assert before.success_delta == 0

    def test_truncate_gate_block_emits_neither_attempt_nor_outcome(
        self, executor, register_handler
    ):
        # Given an entry whose forensic payload was capped on the write side
        handler = register_handler("emission_truncated")
        entry = _entry("emission_truncated", truncated=True)
        before = _MetricSnapshot()

        # When the framework-side gate refuses it
        assert executor._execute_replay(entry) is False

        # Then nothing ran and nothing was counted — an attempt here would
        # count an entry whose handler was never reached.
        assert handler.replay_calls == []
        assert before.attempts_delta == 0
        assert before.outcome_delta == 0

    def test_can_replay_block_emits_neither_attempt_nor_outcome(
        self, executor, register_handler
    ):
        # Given a handler that refuses the entry
        handler = register_handler(
            "emission_refused", can_replay_reason="entity already settled"
        )
        before = _MetricSnapshot()

        # When
        assert executor._execute_replay(_entry("emission_refused")) is False

        # Then
        assert handler.replay_calls == []
        assert before.attempts_delta == 0
        assert before.outcome_delta == 0

    def test_attempts_and_outcomes_stay_equal_across_a_mixed_sequence(
        self, executor, register_handler
    ):
        """The pairing invariant, over every exit at once.

        ``outcomes / attempts`` is a success-rate panel an operator builds on
        these two families. Emitting one without the other makes that ratio
        exceed 1 (or sink below the truth), which no single-exit assertion
        catches — the two counters can only be compared across a run.
        """
        # Given one registered handler per exit
        register_handler("pairing_ok")
        register_handler("pairing_bad", succeed=False)
        register_handler("pairing_crash", raises=RuntimeError("boom"))
        register_handler("pairing_refused", can_replay_reason="not allowed")
        before = _MetricSnapshot()

        # When each exit is taken
        executor._execute_replay(_entry("pairing_ok"))
        executor._execute_replay(_entry("pairing_bad"))
        with pytest.raises(RuntimeError):
            executor._execute_replay(_entry("pairing_crash"))
        executor._execute_replay(_entry("pairing_refused"))
        executor._execute_replay(_entry("pairing_ok", truncated=True))

        # Then: 3 handler runs, and the two families agree exactly
        assert before.attempts_delta == 3
        assert before.outcome_delta == 3
        assert before.attempts_delta == before.outcome_delta


# =============================================================================
# D11 — the duration histogram stops timing replays that never ran
# =============================================================================


class TestReplayDurationExcludesRefusals:
    """``baldur_dlq_replay_duration_seconds`` observes only real replays."""

    def test_successful_replay_observes_one_duration(self, executor, register_handler):
        register_handler("duration_success")
        before = _MetricSnapshot()

        executor._execute_replay(_entry("duration_success"))

        assert before.duration_delta == 1

    def test_crashing_handler_still_observes_its_duration(
        self, executor, register_handler
    ):
        # A replay that ran and failed took real time — that observation is
        # honest and must survive the refusal exclusion.
        register_handler("duration_crash", raises=RuntimeError("boom"))
        before = _MetricSnapshot()

        with pytest.raises(RuntimeError):
            executor._execute_replay(_entry("duration_crash"))

        assert before.duration_delta == 1

    def test_truncate_gate_block_observes_no_duration(self, executor, register_handler):
        register_handler("duration_truncated")
        before = _MetricSnapshot()

        executor._execute_replay(_entry("duration_truncated", truncated=True))

        # A microsecond refusal in this family drags the merged p95 toward zero
        # and the console would render it as a true replay latency.
        assert before.duration_delta == 0

    def test_can_replay_block_observes_no_duration(self, executor, register_handler):
        register_handler("duration_refused", can_replay_reason="not allowed")
        before = _MetricSnapshot()

        executor._execute_replay(_entry("duration_refused"))

        assert before.duration_delta == 0

    def test_truncate_gate_block_logs_its_own_elapsed_time(
        self, executor, register_handler
    ):
        # Removing the refusal samples also removed the only place a SLOW
        # refusal was observable, so the blocked WARNING carries the timing now.
        register_handler("blocked_log_truncated")

        with capture_logs() as captured:
            executor._execute_replay(_entry("blocked_log_truncated", truncated=True))

        blocked = [e for e in captured if e.get("event") == "dlq.replay_blocked"]
        assert len(blocked) == 1
        assert blocked[0]["log_level"] == "warning"
        assert blocked[0]["reason"] == "request_data_truncated"
        assert isinstance(blocked[0]["duration_ms"], float)
        assert blocked[0]["duration_ms"] >= 0

    def test_can_replay_block_logs_its_own_elapsed_time(
        self, executor, register_handler
    ):
        # The second gate is customer code and may do I/O, so this is the
        # branch where the timing is actually worth having.
        register_handler("blocked_log_refused", can_replay_reason="entity settled")

        with capture_logs() as captured:
            executor._execute_replay(_entry("blocked_log_refused"))

        blocked = [e for e in captured if e.get("event") == "dlq.replay_blocked"]
        assert len(blocked) == 1
        assert blocked[0]["reason"] == "entity settled"
        assert isinstance(blocked[0]["duration_ms"], float)
        assert blocked[0]["duration_ms"] >= 0
