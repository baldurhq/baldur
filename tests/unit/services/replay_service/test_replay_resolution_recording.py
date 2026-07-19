"""Tests for the replay pipeline's DLQ resolution recording.

Targets ``baldur.services.replay_service.service``:

    - ``_resolution_wall_time`` — the failure-to-resolution duration handed to
      the recovery-duration histogram.
    - ``_record_item_resolved`` at the two success-resolution exits of
      ``_execute_replay``.

Every other resolution path reaches ``DLQMetricEventHandler.on_item_resolved``
through ``resolve_entry``; the replay pipeline finalizes entries through
``complete_replay`` instead and so used to skip the pending-gauge decrement,
the recovery-duration histogram, and the digest's resolved count entirely.

The load-bearing invariant is **exactly one record per resolved entry**: the
two exits that resolve must each record once, and every exit that leaves the
entry pending, under review, or untouched must record nothing. Both halves are
asserted here — a recording added to a failure exit would decrement the
pending gauge for an entry that is still pending.

Covers:
- TestResolutionWallTimeBehavior: the duration's edge cases and fail-open.
- TestReplayResolutionRecordingBehavior: the exit-path inventory, the
  duplicate_skip exit, the one-per-entry invariant across the three replay
  flows, and the fail-open posture.

Posture: governance is pinned to the OSS NoOp checker rather than resolved
from the registry — in a PRO-present monorepo run an unpinned slot silently
exercises the PRO checker (the 709 false-pass lesson).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.core.idempotency_gate import (
    IdempotencyCheckResult,
    IdempotencyDecision,
    IdempotencyGate,
)
from baldur.interfaces.governance import NoOpGovernanceChecker
from baldur.interfaces.repositories import (
    FailedOperationData,
    FailedOperationRepository,
    ResolutionTrigger,
)
from baldur.metrics.event_handlers import DLQMetricEventHandler
from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.replay_service import (
    ReplayResult,
    ReplayService,
    _replay_handlers,
)
from baldur.services.replay_service.handlers import ReplayHandler
from baldur.services.replay_service.service import _resolution_wall_time
from baldur.utils.time import utc_now
from tests.factories.time_helpers import freeze_time

DOMAIN = "payment"
DLQ_ID = "dlq-1"

# =============================================================================
# Harness
# =============================================================================


def _entry(**overrides) -> FailedOperationData:
    """A pending DLQ entry the replay pipeline can acquire."""
    defaults = {
        "id": DLQ_ID,
        "domain": DOMAIN,
        "failure_type": "PG_TIMEOUT",
        "status": "pending",
        "retry_count": 0,
        "max_retries": 2,
        "created_at": utc_now() - timedelta(minutes=5),
    }
    defaults.update(overrides)
    return FailedOperationData(**defaults)


class _Handler(ReplayHandler):
    """Replay handler whose outcome each test chooses.

    A hardcoded happy-path double would make the failed-replay branch
    unreachable, so the outcome is a constructor parameter.
    """

    def __init__(self, outcome: ReplayResult | Exception):
        self._outcome = outcome
        self.calls: list[str] = []

    @property
    def domain(self) -> str:
        return DOMAIN

    def can_replay(self, failed_op) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op) -> ReplayResult:
        self.calls.append(failed_op.id)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


@pytest.fixture(autouse=True)
def _clear_handler_registry():
    _replay_handlers.clear()
    yield
    _replay_handlers.clear()


@pytest.fixture
def repository():
    repo = MagicMock(spec=FailedOperationRepository)
    repo.try_acquire_for_replay.return_value = _entry()
    repo.get_by_id.return_value = _entry()
    repo.find_replayable.return_value = []
    return repo


@pytest.fixture
def service(repository):
    """A ReplayService in pure-OSS posture with a stubbed repository."""
    svc = ReplayService(repository=repository)
    svc._event_bus = MagicMock(spec=BaldurEventBus)
    # See module docstring — pin OSS governance, never resolve the registry.
    svc._governance = NoOpGovernanceChecker()
    svc._governance_resolved = True
    return svc


@pytest.fixture
def recorded():
    """Intercept `on_item_resolved`; yield the mock standing in for it."""
    with patch.object(DLQMetricEventHandler, "on_item_resolved", autospec=True) as mock:
        yield mock


def _register(outcome) -> _Handler:
    handler = _Handler(outcome)
    _replay_handlers[DOMAIN] = handler
    return handler


def _skip_gate(decision: IdempotencyDecision):
    """Patch the idempotency gate to return a fixed decision."""
    gate = MagicMock(spec=IdempotencyGate)
    gate.check_and_acquire.return_value = IdempotencyCheckResult(
        decision=decision, retry_count=0
    )
    return patch("baldur.core.idempotency_gate.get_idempotency_gate", return_value=gate)


# =============================================================================
# _resolution_wall_time — Behavior
# =============================================================================


class TestResolutionWallTimeBehavior:
    """The failure-to-resolution duration, and its degrade-to-None contract.

    The duration is a bonus signal for the recovery-duration histogram; the
    resolution record itself is not. So every uncomputable input must yield
    None rather than raising and losing the resolution.
    """

    def test_aware_created_at_yields_elapsed_seconds(self):
        """Wall time is measured from the original failure, not the replay."""
        with freeze_time("2026-07-19 12:00:00"):
            entry = _entry(created_at=utc_now() - timedelta(seconds=90))

            assert _resolution_wall_time(entry) == pytest.approx(90.0, abs=1.0)

    def test_naive_created_at_is_treated_as_utc(self):
        """A repository storing naive timestamps still yields a duration.

        Without normalization the subtraction raises TypeError and the
        duration silently degrades to None for every such repository.
        """
        with freeze_time("2026-07-19 12:00:00"):
            naive = (utc_now() - timedelta(seconds=30)).replace(tzinfo=None)
            entry = _entry(created_at=naive)

            assert _resolution_wall_time(entry) == pytest.approx(30.0, abs=1.0)

    def test_missing_created_at_yields_none(self):
        """A repository supplying no timestamp yields no duration."""
        assert _resolution_wall_time(_entry(created_at=None)) is None

    def test_future_created_at_clamps_to_zero(self):
        """Clock skew must not produce a negative histogram observation."""
        with freeze_time("2026-07-19 12:00:00"):
            entry = _entry(created_at=utc_now() + timedelta(hours=1))

            assert _resolution_wall_time(entry) == 0.0

    def test_non_datetime_created_at_yields_none_without_raising(self):
        """A malformed timestamp degrades rather than failing the replay."""
        assert _resolution_wall_time(_entry(created_at="yesterday")) is None


# =============================================================================
# _execute_replay resolution exits — Behavior
# =============================================================================


class TestReplayResolutionRecordingBehavior:
    """Exactly the two resolving exits record; every other exit is silent."""

    # --- the two recording exits -------------------------------------------

    def test_successful_replay_records_the_resolution(self, service, recorded):
        """The main completion exit resolves the entry, so it records."""
        _register(ReplayResult.succeeded(DLQ_ID, "OK"))

        result = service._execute_replay(DLQ_ID)

        assert result.success is True
        recorded.assert_called_once()

    def test_successful_replay_records_domain_and_trigger_as_resolution_type(
        self, service, recorded
    ):
        """The recorded resolution type is the trigger stamped into the DB.

        A replay recorded under a different resolution type than the one
        persisted would make the metric and the compliance record disagree.
        """
        _register(ReplayResult.succeeded(DLQ_ID, "OK"))

        service._execute_replay(
            DLQ_ID, trigger=ResolutionTrigger.AUTO_REPLAY_CIRCUIT_CLOSE
        )

        kwargs = recorded.call_args.kwargs
        assert kwargs["domain"] == DOMAIN
        assert kwargs["resolution_type"] == "auto_replay_circuit_close"

    def test_successful_replay_records_failure_to_resolution_duration(
        self, service, repository, recorded
    ):
        """The duration is the entry's age, not the handler's runtime."""
        with freeze_time("2026-07-19 12:00:00"):
            repository.try_acquire_for_replay.return_value = _entry(
                created_at=utc_now() - timedelta(seconds=120)
            )
            _register(ReplayResult.succeeded(DLQ_ID, "OK"))

            service._execute_replay(DLQ_ID)

        assert recorded.call_args.kwargs["duration_seconds"] == pytest.approx(
            120.0, abs=1.0
        )

    def test_duplicate_skip_records_the_resolution(self, service, recorded):
        """The idempotency duplicate exit finalizes the entry, so it records.

        It resolves through `complete_replay` and returns before the handler
        runs, so skipping it would leave the pending gauge counting an entry
        that is no longer pending.
        """
        _register(ReplayResult.succeeded(DLQ_ID, "OK"))

        with _skip_gate(IdempotencyDecision.SKIP):
            result = service._execute_replay(DLQ_ID)

        assert result.skipped is True
        recorded.assert_called_once()
        assert recorded.call_args.kwargs["resolution_type"] == "duplicate_skip"

    def test_duplicate_skip_does_not_run_the_handler(self, service, recorded):
        """The duplicate exit records a resolution it did not perform itself."""
        handler = _register(ReplayResult.succeeded(DLQ_ID, "OK"))

        with _skip_gate(IdempotencyDecision.SKIP):
            service._execute_replay(DLQ_ID)

        assert handler.calls == []
        recorded.assert_called_once()

    # --- the non-recording exits -------------------------------------------

    def test_entry_not_found_records_nothing(self, service, repository, recorded):
        """Nothing was resolved — there is no entry at all."""
        repository.try_acquire_for_replay.return_value = None
        repository.get_by_id.return_value = None

        result = service._execute_replay(DLQ_ID)

        assert result.success is False
        recorded.assert_not_called()

    def test_already_processed_entry_records_nothing(
        self, service, repository, recorded
    ):
        """A terminal entry was counted when it actually resolved.

        Recording here would double-count every retry of an already-resolved
        entry and drive the pending gauge below the true backlog.
        """
        repository.try_acquire_for_replay.return_value = None
        repository.get_by_id.return_value = _entry(status="resolved")

        result = service._execute_replay(DLQ_ID)

        assert result.success is False
        recorded.assert_not_called()

    def test_max_attempts_block_records_nothing(self, service, repository, recorded):
        """A blocked entry stays PENDING, so the gauge must keep counting it."""
        repository.try_acquire_for_replay.return_value = None
        repository.get_by_id.return_value = _entry(status="pending", retry_count=9)

        result = service._execute_replay(DLQ_ID)

        assert result.error == "max_replays_exceeded"
        recorded.assert_not_called()

    def test_idempotency_abort_records_nothing(self, service, recorded):
        """Another worker holds the replay; this call resolved nothing."""
        _register(ReplayResult.succeeded(DLQ_ID, "OK"))

        with _skip_gate(IdempotencyDecision.ABORT):
            result = service._execute_replay(DLQ_ID)

        assert result.skipped is True
        recorded.assert_not_called()

    def test_truncate_gate_block_records_nothing(self, service, repository, recorded):
        """A gate-blocked entry stays PENDING and was never replayed."""
        repository.try_acquire_for_replay.return_value = _entry(
            request_data={"_truncated": True, "original_size": 9000}
        )
        _register(ReplayResult.succeeded(DLQ_ID, "OK"))

        result = service._execute_replay(DLQ_ID)

        assert result.skipped is True
        recorded.assert_not_called()

    def test_handler_crash_records_nothing(self, service, recorded):
        """A crashed replay escalates the entry; it did not resolve it."""
        _register(RuntimeError("gateway exploded"))

        result = service._execute_replay(DLQ_ID)

        assert result.success is False
        recorded.assert_not_called()

    def test_failed_replay_records_nothing(self, service, recorded):
        """A handler reporting failure leaves the entry unresolved."""
        _register(ReplayResult.failed(DLQ_ID, "still down"))

        result = service._execute_replay(DLQ_ID)

        assert result.success is False
        recorded.assert_not_called()

    # --- one record per resolved entry, across the three flows --------------

    def test_replay_single_records_once_per_resolved_entry(self, service, recorded):
        """The single-entry flow routes through the one recording site."""
        _register(ReplayResult.succeeded(DLQ_ID, "OK"))

        service.replay_single(DLQ_ID)

        recorded.assert_called_once()

    def test_replay_batch_records_once_per_successful_entry_only(
        self, service, repository, recorded
    ):
        """A mixed batch records the successes and only the successes.

        Pins the count rather than merely "was called": a record moved into
        the loop body instead of the success branch still passes a
        was-called assertion but inflates the resolved count by the failures.
        """
        # Given: three entries, the middle one failing to replay
        entries = [_entry(id=f"dlq-{i}") for i in range(3)]
        repository.find_replayable.return_value = entries
        repository.try_acquire_for_replay.side_effect = entries
        outcomes = [
            ReplayResult.succeeded("dlq-0", "OK"),
            ReplayResult.failed("dlq-1", "still down"),
            ReplayResult.succeeded("dlq-2", "OK"),
        ]
        handler = _Handler(None)
        handler.replay = lambda failed_op: outcomes.pop(0)
        _replay_handlers[DOMAIN] = handler

        # When
        result = service.replay_batch(domain=DOMAIN, max_items=3)

        # Then
        assert result.success_count == 2
        assert recorded.call_count == 2

    def test_circuit_close_sweep_records_once_per_resolved_entry(
        self, service, repository, recorded
    ):
        """The on-recovery sweep's resolutions reach the same recording site."""
        # Given
        entries = [_entry(id=f"dlq-{i}") for i in range(2)]
        repository.find_replayable.return_value = entries
        repository.try_acquire_for_replay.side_effect = entries
        _register(ReplayResult.succeeded(DLQ_ID, "OK"))

        # When
        result = service.replay_on_circuit_close(
            service_name="payment_api",
            max_items=2,
            service_failure_type_map={"payment_api": ["PG_TIMEOUT"]},
        )

        # Then
        assert result.success_count == 2
        assert recorded.call_count == 2
        assert {call.kwargs["resolution_type"] for call in recorded.call_args_list} == {
            "auto_replay_circuit_close"
        }

    # --- fail-open ---------------------------------------------------------

    def test_recording_failure_does_not_fail_a_successful_replay(
        self, service, recorded
    ):
        """Observability never turns a completed replay into a failed one."""
        recorded.side_effect = RuntimeError("metrics backend down")
        _register(ReplayResult.succeeded(DLQ_ID, "OK"))

        result = service._execute_replay(DLQ_ID)

        assert result.success is True

    def test_recording_failure_logs_warning_with_resolution_type(
        self, service, recorded
    ):
        """The swallowed failure still surfaces at WARNING with its context."""
        recorded.side_effect = RuntimeError("metrics backend down")
        _register(ReplayResult.succeeded(DLQ_ID, "OK"))

        with capture_logs() as logs:
            service._execute_replay(DLQ_ID, trigger=ResolutionTrigger.TRAFFIC_AWARE)

        record = next(
            log
            for log in logs
            if log["event"] == "replay_service.resolution_metrics_failed"
        )
        assert record["log_level"] == "warning"
        assert record["resolution_type"] == "traffic_aware"
