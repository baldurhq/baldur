"""The recovery trial: one parked job per job name, replayed as a trial.

Target: ``baldur.services.replay_service.recovery`` — ``RecoveryTrialRunner``
(the tick, its per-domain skips, the candidate walk, the pacing record, the
reads made immediately before a trial, the dispatch after a success), the
pacing constants and ``stale_release_minutes``.

The tick runs for real over an in-memory DLQ, a real ``ReplayService``
(governance injected) and a real ``CircuitBreakerService`` whose admission rule
judges the breaker rows. Only the shared breaker store's fleet read, the kill
switch and the integrity verdict are stand-ins — each is the process boundary
the tick reads across — and the dispatch of the recovery sweep is a spy (it
publishes a Celery task). The replay handler is a scripted double
(``tests.factories.replay_doubles``) filed in the real handler registry.

UNIT_TEST_GUIDELINES.md:
- No ``time.sleep`` (§6.3): the tick's wall clock is injected; the stale
  release, which ages entries by the repository's own clock, runs under
  ``freeze_time``.
- §8.12/§8.13: every skip and outcome is pinned by what the trial did to the
  entry (status, retry count, recovery-trial count) and to the pacing record,
  not only by the reported reason.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from collections.abc import Iterator
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.interfaces.governance import GovernanceChecker
from baldur.interfaces.repositories import (
    STALE_RELEASE_AT_CAP_NOTE,
    CircuitBreakerStateData,
    FailedOperationRepository,
    ReplayablePage,
    ResolutionTrigger,
    encode_replay_cursor,
)
from baldur.models.governance import GovernanceCheckResult
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.exceptions import (
    L2_QUARANTINED_REASON,
    UNREACHED_DEFAULT_STORE_REASON,
    CircuitBreakerStateUnavailableError,
)
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.handlers import (
    _replay_handlers,
    register_replay_handler,
)
from baldur.services.replay_service.models import BatchReplayResult, ReplayResult
from baldur.services.replay_service.recovery import (
    _LONGEST_REPLAY_TASK_MINUTES,
    _PACING_KEY_PREFIX,
    _TRIAL_BACKOFF,
    OUTCOME_BREAKER_REFUSED,
    OUTCOME_FAILED,
    OUTCOME_NOT_RUN,
    OUTCOME_STILL_RUNNING,
    OUTCOME_SUCCEEDED,
    RECOVERY_TICK_SECONDS,
    RECOVERY_TRIAL_BASE_SECONDS,
    RECOVERY_TRIAL_MAX_SECONDS,
    SKIP_BREAKER_REFUSING,
    SKIP_DEADLINE,
    SKIP_LOCK_UNAVAILABLE,
    SKIP_NO_CANDIDATE,
    SKIP_NO_LANES,
    SKIP_NOT_DUE,
    SKIP_OPERATOR_HOLD,
    SKIP_RECOVERY_RUNNING,
    TICK_BREAKER_STATE_UNAVAILABLE,
    TICK_COMPLETED,
    TICK_DISABLED,
    TICK_GOVERNANCE_BLOCKED,
    TICK_IDLE,
    TICK_INTEGRITY_BLOCKED,
    TICK_UNSUPPORTED,
    RecoveryTickResult,
    RecoveryTrialRunner,
    TrialRecord,
    _TrialPacing,
    reset_recovery_trial_state,
    stale_release_minutes,
)
from baldur.services.replay_service.service import (
    RECOVERY_TRIALS_METADATA_KEY,
    _lane_key,
)
from baldur.settings.dlq import reset_dlq_settings
from baldur.settings.replay_automation import reset_replay_automation_settings
from baldur.utils.time import utc_now
from tests.factories.replay_doubles import (
    STEP_BREAKER_REFUSED,
    STEP_DIE,
    STEP_FAIL,
    STEP_NOT_STARTED,
    STEP_RAISE,
    STEP_STILL_RUNNING,
    STEP_SUCCEED,
    ScriptedReplayHandler,
    WorkerDied,
)
from tests.factories.time_helpers import freeze_time

DOMAIN = "payment_api"
OTHER_DOMAIN = "orders_api"
# A breaker name that projects onto DOMAIN without being spelled like it.
DOMAIN_BREAKER = "Payment-API"
DECLARED = "MAX_RETRIES_TIMEOUTERROR"
DECLARED_B = "MAX_RETRIES_CONNECTIONERROR"
RECOVERY_TIMEOUT_SECONDS = 60
# The injected wall clock's starting point (epoch seconds).
T0 = 1_900_000_000.0

_INTEGRITY = "baldur.services.event_bus.integrity_gate.replay_integrity_verdict"
_DISPATCH = "baldur.services.replay_service.recovery.dispatch_recovery_sweep"
_FREEZE = "baldur.services.circuit_breaker.service.should_allow_cb_state_change"


# =============================================================================
# Stand-ins for the process boundaries the tick reads across
# =============================================================================


class _Clock:
    """The tick's injected wall clock (epoch seconds)."""

    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FleetStore:
    """The shared breaker store's fleet read, as the test sets it.

    ``rows`` answer every read; ``answers`` (consumed first, one per read) let
    a test change what a later read sees — a list of rows, or a reason string
    the read raises with.
    """

    def __init__(self, rows: list[CircuitBreakerStateData] | None = None) -> None:
        self.rows = list(rows or [])
        self.local_rows: list[CircuitBreakerStateData] = []
        self.answers: deque[Any] = deque()
        self.raise_reason: str | None = None
        self.reads = 0
        self.calls: list[str] | None = None

    def get_cluster_states(self) -> list[CircuitBreakerStateData]:
        self.reads += 1
        if self.calls is not None:
            self.calls.append("rows")
        answer: Any = self.answers.popleft() if self.answers else None
        reason = answer if isinstance(answer, str) else self.raise_reason
        if reason is not None:
            raise CircuitBreakerStateUnavailableError("get_cluster_states", reason)
        return list(answer if answer is not None else self.rows)

    def get_all_states(self) -> list[CircuitBreakerStateData]:
        return list(self.local_rows)


class _Switch:
    """The kill switch as a process reads it."""

    def __init__(self) -> None:
        self.known = True
        self.enabled = True
        self.raises = False
        self.calls: list[str] | None = None

    def is_state_known(self) -> bool:
        if self.calls is not None:
            self.calls.append("kill_switch")
        if self.raises:
            raise RuntimeError("state backend unreachable")
        return self.known

    def is_enabled(self) -> bool:
        return self.enabled


def _row(state: str, *, name: str = DOMAIN_BREAKER, **fields: Any):
    return CircuitBreakerStateData(service_name=name, state=state, **fields)


def _open_inside_timeout(name: str = DOMAIN_BREAKER) -> CircuitBreakerStateData:
    return _row("open", name=name, opened_at=utc_now() - timedelta(seconds=5))


def _open_past_timeout(name: str = DOMAIN_BREAKER) -> CircuitBreakerStateData:
    return _row("open", name=name, opened_at=utc_now() - timedelta(minutes=10))


def _blocked(name: str = DOMAIN_BREAKER) -> CircuitBreakerStateData:
    """An operator's Block: OPEN under a pin in force."""
    return _row(
        "open",
        name=name,
        opened_at=utc_now() - timedelta(minutes=10),
        manually_controlled=True,
        manual_override_expires_at=utc_now() + timedelta(minutes=30),
    )


def _pinned_closed(name: str = DOMAIN_BREAKER, *, lapsed: bool = False):
    """An operator's force-close pin on a CLOSED row (in force, or lapsed)."""
    expires = timedelta(minutes=-1) if lapsed else timedelta(minutes=30)
    return _row(
        "closed",
        name=name,
        manually_controlled=True,
        manual_override_expires_at=utc_now() + expires,
    )


# =============================================================================
# Harness
# =============================================================================


@dataclass
class _Harness:
    """One tick's world: the DLQ, the replay service, the breaker, the clock."""

    repo: InMemoryFailedOperationRepository
    cache: InMemoryCacheAdapter
    service: ReplayService
    store: _FleetStore
    cb: CircuitBreakerService
    switch: _Switch
    clock: _Clock
    dispatched: Any
    integrity: Any
    governance: Any
    base_time: Any
    failure_type_map: dict[str, list[str]] = field(default_factory=dict)
    runtime_config: dict[str, Any] | None = None
    handlers: dict[str, ScriptedReplayHandler] = field(default_factory=dict)

    def handler(self, domain: str = DOMAIN, **kwargs: Any) -> ScriptedReplayHandler:
        kwargs.setdefault("declared", (DECLARED,))
        handler = ScriptedReplayHandler(domain, **kwargs)
        register_replay_handler(handler)
        self.handlers[domain] = handler
        return handler

    def park(
        self,
        domain: str = DOMAIN,
        *,
        failure_type: str = DECLARED,
        offset: float = 0.0,
        retry_count: int = 0,
        max_retries: int = 2,
    ) -> str:
        """Park one entry, ``offset`` seconds after the harness's base time."""
        entry = self.repo.create(
            domain=domain,
            failure_type=failure_type,
            error_message="parked",
            request_data={"doc": "x"},
            retry_count=retry_count,
            max_retries=max_retries,
        )
        self.repo._storage[entry.id] = replace(
            entry, created_at=self.base_time + timedelta(seconds=offset)
        )
        return entry.id

    def runner(self, **kwargs: Any) -> RecoveryTrialRunner:
        kwargs.setdefault("clock", self.clock)
        return RecoveryTrialRunner(
            replay_service=self.service,
            circuit_breaker_service=self.cb,
            system_control=self.switch,
            **kwargs,
        )

    def tick(self, deadline: float | None = None, **kwargs: Any) -> RecoveryTickResult:
        return self.runner(**kwargs).run(deadline=deadline)

    def entry(self, dlq_id: str):
        entry = self.repo.get_by_id(dlq_id)
        assert entry is not None
        return entry

    def pacing(self, domain: str = DOMAIN) -> _TrialPacing | None:
        value = self.cache.get(_PACING_KEY_PREFIX + domain)
        return None if value is None else _TrialPacing.from_value(value)

    def write_pacing(self, pacing: _TrialPacing, domain: str = DOMAIN) -> None:
        self.cache.set(_PACING_KEY_PREFIX + domain, pacing.to_value())

    def cursor_of(self, dlq_id: str) -> str:
        entry = self.entry(dlq_id)
        return encode_replay_cursor(entry.created_at, entry.id)


@pytest.fixture
def harness() -> Iterator[_Harness]:
    """A tick over a fresh DLQ, with the handler registry and switches isolated."""
    before = dict(_replay_handlers)
    _replay_handlers.clear()
    reset_recovery_trial_state()
    reset_replay_automation_settings()
    reset_dlq_settings()

    repo = InMemoryFailedOperationRepository()
    # A per-test prefix: in-memory locks live in one class-level registry.
    cache = InMemoryCacheAdapter(key_prefix=f"t807:{uuid.uuid4().hex}:")
    service = ReplayService(repository=repo, cache=cache)
    service._event_bus = MagicMock(spec=BaldurEventBus)
    governance = MagicMock(spec=GovernanceChecker)
    governance.check_all_governance.return_value = GovernanceCheckResult(allowed=True)
    service._governance = governance
    service._governance_resolved = True
    store = _FleetStore()
    cb = CircuitBreakerService(
        config=CircuitBreakerConfig(
            enabled=True, recovery_timeout=RECOVERY_TIMEOUT_SECONDS
        ),
        repository=store,
    )

    holder: dict[str, Any] = {}
    with (
        patch.object(
            ReplayService,
            "_get_replay_automation_config",
            autospec=True,
            side_effect=lambda _self: holder["h"].runtime_config,
        ),
        patch.object(
            ReplayService,
            "_load_failure_type_map",
            autospec=True,
            side_effect=lambda _self: holder["h"].failure_type_map,
        ),
        patch(_INTEGRITY, return_value=True) as integrity,
        patch(_DISPATCH, autospec=True, return_value="dispatched") as dispatched,
    ):
        h = _Harness(
            repo=repo,
            cache=cache,
            service=service,
            store=store,
            cb=cb,
            switch=_Switch(),
            clock=_Clock(),
            dispatched=dispatched,
            integrity=integrity,
            governance=governance,
            base_time=utc_now() - timedelta(days=1),
        )
        holder["h"] = h
        yield h
        for handler in h.handlers.values():
            handler.settle()

    _replay_handlers.clear()
    _replay_handlers.update(before)
    reset_recovery_trial_state()
    reset_replay_automation_settings()
    reset_dlq_settings()


def _events(logs: list[dict], name: str) -> list[dict]:
    return [entry for entry in logs if entry["event"] == name]


# =============================================================================
# Contract — pacing and the release window
# =============================================================================


class TestRecoveryPacingContract:
    """The spacing promise, asserted literally (D5)."""

    def test_recovery_tick_runs_every_sixty_seconds(self):
        assert RECOVERY_TICK_SECONDS == 60

    def test_trial_spacing_base_and_cap(self):
        assert RECOVERY_TRIAL_BASE_SECONDS == 60
        assert RECOVERY_TRIAL_MAX_SECONDS == 540

    @pytest.mark.parametrize(
        ("streak", "spacing"),
        [(1, 60.0), (2, 120.0), (3, 240.0), (4, 480.0), (5, 540.0), (9, 540.0)],
        ids=["first", "second", "third", "fourth", "capped", "stays_capped"],
    )
    def test_trial_spacing_doubles_from_sixty_to_the_cap(self, streak, spacing):
        """60 -> 120 -> 240 -> 480 -> 540 s, jitter-free."""
        assert _TRIAL_BACKOFF.calculate(streak) == spacing

    def test_trial_spacing_is_jitter_free(self):
        """The due test must give the same answer on consecutive ticks."""
        assert {_TRIAL_BACKOFF.calculate(3) for _ in range(20)} == {240.0}

    def test_cap_plus_one_tick_is_the_ten_minute_latency_promise(self):
        assert RECOVERY_TRIAL_MAX_SECONDS + RECOVERY_TICK_SECONDS == 600

    def test_longest_replay_task_floor_is_ten_minutes(self):
        assert _LONGEST_REPLAY_TASK_MINUTES == 10

    @pytest.mark.parametrize(
        ("configured", "effective"),
        [(5, 10), (9, 10), (10, 10), (11, 11), (30, 30)],
        ids=["below_floor", "just_below", "at_floor", "just_above", "default"],
    )
    def test_stale_release_minutes_floors_the_setting_at_ten(
        self, monkeypatch, configured, effective
    ):
        """A release sooner than the longest replay task could hand back an
        entry a living replay still holds."""
        monkeypatch.setenv(
            "BALDUR_DLQ_STALE_REPLAYING_TIMEOUT_MINUTES", str(configured)
        )
        reset_dlq_settings()
        try:
            assert stale_release_minutes() == effective
        finally:
            reset_dlq_settings()


# =============================================================================
# Behavior — the tick's own exits (D3)
# =============================================================================


class TestRecoveryTickExitsBehavior:
    """Each way a whole tick ends before, or instead of, its trials."""

    @pytest.mark.parametrize(
        ("env", "runtime_config"),
        [
            ({"BALDUR_REPLAY_AUTOMATION_ON_RECOVERY_ENABLED": "false"}, None),
            ({"BALDUR_REPLAY_AUTOMATION_RECOVERY_TRIAL_ENABLED": "false"}, None),
            ({}, {"on_recovery_enabled": False}),
        ],
        ids=["on_recovery_off", "recovery_trial_off", "runtime_config_off"],
    )
    def test_tick_disabled_by_either_switch_runs_nothing(
        self, harness, monkeypatch, env, runtime_config
    ):
        # Given: a parked job that would otherwise be trialed.
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        reset_replay_automation_settings()
        harness.runtime_config = runtime_config
        handler = harness.handler()
        dlq_id = harness.park()

        # When
        with patch.object(
            InMemoryFailedOperationRepository,
            "release_stale_replaying",
            autospec=True,
            return_value=0,
        ) as release:
            result = harness.tick()

        # Then
        assert result.status == TICK_DISABLED
        release.assert_not_called()
        assert handler.replayed == []
        assert harness.entry(dlq_id).status == "pending"

    def test_tick_runs_the_stale_release_first_with_the_floored_window(self, harness):
        with patch.object(
            InMemoryFailedOperationRepository,
            "release_stale_replaying",
            autospec=True,
            return_value=3,
        ) as release:
            result = harness.tick()

        release.assert_called_once_with(
            harness.repo, older_than_minutes=stale_release_minutes()
        )
        assert result.released == 3

    def test_tick_release_failure_is_reported_and_the_tick_goes_on(self, harness):
        handler = harness.handler()
        dlq_id = harness.park()

        with (
            patch.object(
                InMemoryFailedOperationRepository,
                "release_stale_replaying",
                autospec=True,
                side_effect=RuntimeError("store down"),
            ),
            capture_logs() as logs,
        ):
            result = harness.tick()

        assert result.released == 0
        assert [t.dlq_id for t in result.trials] == [dlq_id]
        assert handler.replayed == [dlq_id]
        failed = _events(logs, "replay_service.recovery_trial_release_failed")
        assert len(failed) == 1
        assert failed[0]["log_level"] == "warning"

    def test_idle_tick_reads_no_breaker_state(self, harness):
        """Nothing parked under any handler domain: no breaker read at all."""
        harness.handler()

        result = harness.tick()

        assert result.status == TICK_IDLE
        assert harness.store.reads == 0
        assert result.trials == []

    def test_tick_skips_a_domain_whose_strict_count_is_zero(self, harness):
        busy = harness.handler(DOMAIN)
        idle = harness.handler(OTHER_DOMAIN)
        dlq_id = harness.park(DOMAIN)

        result = harness.tick()

        assert [t.domain for t in result.trials] == [DOMAIN]
        assert OTHER_DOMAIN not in result.skipped
        assert busy.replayed == [dlq_id]
        assert idle.asked == []

    def test_tick_count_that_cannot_be_read_counts_as_work(self, harness):
        handler = harness.handler()
        dlq_id = harness.park()

        with patch.object(
            InMemoryFailedOperationRepository,
            "get_cluster_pending_count_by_domain",
            autospec=True,
            side_effect=RuntimeError("count unavailable"),
        ):
            result = harness.tick()

        assert [t.dlq_id for t in result.trials] == [dlq_id]
        assert handler.replayed == [dlq_id]

    def test_tick_rotates_the_domain_order_by_the_wall_clock_minute(self, harness):
        """A tick that runs out of time reaches a different job name first next time."""
        order: list[str] = []
        harness.handler(DOMAIN, on_replay=lambda e: order.append(e.domain))
        harness.handler(OTHER_DOMAIN, on_replay=lambda e: order.append(e.domain))
        harness.park(DOMAIN)
        harness.park(OTHER_DOMAIN)
        # The registered domains are walked sorted, rotated by minute.
        harness.clock.now = 60.0 * 1_000_000  # an even minute: no rotation

        harness.tick()

        assert order == sorted([DOMAIN, OTHER_DOMAIN])

    def test_tick_on_an_odd_minute_starts_from_the_second_domain(self, harness):
        order: list[str] = []
        harness.handler(DOMAIN, on_replay=lambda e: order.append(e.domain))
        harness.handler(OTHER_DOMAIN, on_replay=lambda e: order.append(e.domain))
        harness.park(DOMAIN)
        harness.park(OTHER_DOMAIN)
        harness.clock.now = 60.0 * 1_000_001  # an odd minute: rotated by one

        harness.tick()

        assert order == sorted([DOMAIN, OTHER_DOMAIN], reverse=True)

    def test_tick_past_its_deadline_skips_every_remaining_domain(self, harness):
        harness.handler(DOMAIN)
        harness.handler(OTHER_DOMAIN)
        harness.park(DOMAIN)
        harness.park(OTHER_DOMAIN)

        result = harness.tick(deadline=time.monotonic() - 1.0)

        assert result.skipped == {DOMAIN: SKIP_DEADLINE, OTHER_DOMAIN: SKIP_DEADLINE}
        assert result.trials == []

    def test_unsupported_repository_warns_once_and_trials_nothing(self, harness):
        """A store that cannot give an attempt back would make every failed
        trial cost the job an attempt — the trial does not run on it."""

        class _NoGiveBack(InMemoryFailedOperationRepository):
            return_replay_attempt = FailedOperationRepository.return_replay_attempt

        repo = _NoGiveBack()
        harness.service._repository = repo
        harness.repo = repo
        handler = harness.handler()
        harness.park()

        with capture_logs() as logs:
            first = harness.tick()
            second = harness.tick()

        assert first.status == second.status == TICK_UNSUPPORTED
        assert handler.replayed == []
        warned = _events(logs, "replay_service.recovery_trial_unsupported")
        assert len(warned) == 1
        assert warned[0]["repository"] == "_NoGiveBack"

    def test_breaker_state_unavailable_warns_on_the_transition_only(self, harness):
        handler = harness.handler(default=STEP_FAIL)
        harness.park()
        harness.store.raise_reason = "backend_degraded"

        with capture_logs() as logs:
            first = harness.tick()
            second = harness.tick()
            harness.store.raise_reason = None
            harness.tick()
            harness.store.raise_reason = "backend_degraded"
            harness.clock.advance(RECOVERY_TRIAL_MAX_SECONDS)
            fourth = harness.tick()

        assert first.status == second.status == fourth.status
        assert first.status == TICK_BREAKER_STATE_UNAVAILABLE
        warned = _events(logs, "replay_service.recovery_trial_state_unavailable")
        # Warned when it began, again when it began a second time.
        assert len(warned) == 2
        assert warned[0]["reason"] == "backend_degraded"
        continuing = _events(
            logs, "replay_service.recovery_trial_state_unavailable_continuing"
        )
        assert len(continuing) == 1
        # Only the tick in between, with the store answering, ran a trial.
        assert len(handler.replayed) == 1

    def test_tick_records_the_trials_that_ran_in_the_daily_report(self, harness):
        harness.handler(steps=[STEP_SUCCEED])
        harness.handler(OTHER_DOMAIN, steps=[STEP_FAIL])
        harness.park(DOMAIN)
        harness.park(OTHER_DOMAIN)

        with patch.object(
            ReplayService, "_record_sweep_in_daily_report", autospec=True
        ) as report:
            harness.tick()

        report.assert_called_once_with(
            harness.service,
            "recovery_trial",
            BatchReplayResult(total=2, success_count=1, failed_count=1),
        )

    def test_tick_whose_trials_all_did_not_run_records_no_daily_report(self, harness):
        harness.handler(steps=[STEP_NOT_STARTED])
        harness.park()

        with patch.object(
            ReplayService, "_record_sweep_in_daily_report", autospec=True
        ) as report:
            result = harness.tick()

        assert [t.outcome for t in result.trials] == [OUTCOME_NOT_RUN]
        report.assert_not_called()


# =============================================================================
# Behavior — why one job name got no trial
# =============================================================================


class TestRecoveryTrialSkipBehavior:
    """Every per-domain skip leaves the parked entry exactly as it was."""

    @pytest.mark.parametrize(
        ("make_rows", "reason"),
        [
            (lambda: [_open_inside_timeout()], SKIP_BREAKER_REFUSING),
            (lambda: [_row("open", opened_at=None)], SKIP_BREAKER_REFUSING),
            (lambda: [_blocked()], SKIP_BREAKER_REFUSING),
            (lambda: [_pinned_closed()], SKIP_OPERATOR_HOLD),
            (
                lambda: [_row("closed", name="payment_api"), _open_inside_timeout()],
                SKIP_BREAKER_REFUSING,
            ),
        ],
        ids=[
            "open_inside_timeout",
            "open_without_opened_at",
            "operator_block",
            "operator_hold_force_close_pin",
            "one_of_two_projecting_rows_refusing",
        ],
    )
    def test_skip_for_a_projecting_breaker_row(self, harness, make_rows, reason):
        # Given
        handler = harness.handler()
        dlq_id = harness.park()
        harness.store.rows = make_rows()

        # When
        result = harness.tick()

        # Then
        assert result.skipped == {DOMAIN: reason}
        assert result.trials == []
        assert handler.replayed == []
        assert handler.asked == []
        entry = harness.entry(dlq_id)
        assert (entry.status, entry.retry_count) == ("pending", 0)
        assert harness.pacing() is None

    def test_skip_for_a_frozen_breaker_that_would_admit_a_probe(self, harness):
        """A freeze withholds the automatic OPEN -> HALF_OPEN the trial's call
        would make, so a HALF_OPEN row refuses while it holds."""
        handler = harness.handler()
        harness.park()
        harness.store.rows = [_row("half_open")]

        with patch(_FREEZE, return_value=False):
            result = harness.tick()

        assert result.skipped == {DOMAIN: SKIP_BREAKER_REFUSING}
        assert handler.replayed == []

    def test_operator_hold_on_a_row_under_another_spelling_holds_the_domain(
        self, harness
    ):
        handler = harness.handler()
        harness.park()
        harness.store.rows = [_pinned_closed(name="payment-api")]

        result = harness.tick()

        assert result.skipped == {DOMAIN: SKIP_OPERATOR_HOLD}
        assert handler.replayed == []

    def test_operator_hold_pin_lapses_and_the_job_comes_back(self, harness):
        """After the pin lapses the job comes back with no operator action."""
        handler = harness.handler()
        dlq_id = harness.park()
        harness.store.rows = [_pinned_closed()]
        held = harness.tick()

        harness.store.rows = [_pinned_closed(lapsed=True)]
        resumed = harness.tick()

        assert held.skipped == {DOMAIN: SKIP_OPERATOR_HOLD}
        assert [t.outcome for t in resumed.trials] == [OUTCOME_SUCCEEDED]
        assert handler.replayed == [dlq_id]

    def test_skip_when_the_domain_gets_no_lane(self, harness):
        handler = harness.handler()
        harness.park()

        with patch(
            "baldur.services.replay_service.service.recovery_lanes", return_value=[]
        ):
            result = harness.tick()

        assert result.skipped == {DOMAIN: SKIP_NO_LANES}
        assert handler.asked == []

    def test_skip_when_not_due_on_the_pre_check(self, harness):
        handler = harness.handler()
        dlq_id = harness.park()
        pacing = _TrialPacing(streak=2, next_at=T0 + 30.0, after={})
        harness.write_pacing(pacing)

        result = harness.tick()

        assert result.skipped == {DOMAIN: SKIP_NOT_DUE}
        assert handler.asked == []
        assert harness.entry(dlq_id).status == "pending"
        assert harness.pacing() == pacing

    def test_not_due_boundary_is_due_at_the_paced_instant(self, harness):
        handler = harness.handler()
        harness.park()
        harness.write_pacing(_TrialPacing(streak=1, next_at=T0, after={}))

        result = harness.tick()

        assert [t.outcome for t in result.trials] == [OUTCOME_SUCCEEDED]
        assert len(handler.replayed) == 1

    def test_skip_when_a_peer_tick_trialed_between_the_pre_check_and_the_lock(
        self, harness
    ):
        """The decision is made on the record as it stands under the lock."""
        handler = harness.handler()
        harness.park()
        cache = harness.cache
        original_get_lock = cache.get_lock

        class _PeerTrialsFirst:
            def __init__(self, lock):
                self._lock = lock

            def acquire(self, blocking=True):
                acquired = self._lock.acquire(blocking=blocking)
                if acquired:
                    cache.set(
                        _PACING_KEY_PREFIX + DOMAIN,
                        _TrialPacing(streak=1, next_at=T0 + 60.0).to_value(),
                    )
                return acquired

            def release(self):
                self._lock.release()

        with patch.object(
            cache,
            "get_lock",
            side_effect=lambda **kw: _PeerTrialsFirst(original_get_lock(**kw)),
        ):
            result = harness.tick()

        assert result.skipped == {DOMAIN: SKIP_NOT_DUE}
        assert handler.asked == []

    def test_skip_while_another_recovery_of_the_domain_holds_its_lock(self, harness):
        handler = harness.handler()
        dlq_id = harness.park()
        lock, _state, _error = harness.service.try_acquire_recovery_lock(DOMAIN)
        try:
            result = harness.tick()
        finally:
            harness.service.release_recovery_lock(lock, DOMAIN)

        assert result.skipped == {DOMAIN: SKIP_RECOVERY_RUNNING}
        assert handler.asked == []
        assert harness.entry(dlq_id).status == "pending"

    def test_lock_unavailable_skips_and_warns_on_the_transition_only(self, harness):
        handler = harness.handler(default=STEP_FAIL)
        harness.park()
        cache = harness.cache
        original_get_lock = cache.get_lock
        broken = {"on": True}

        def _get_lock(**kwargs):
            if broken["on"]:
                raise RuntimeError("cache down")
            return original_get_lock(**kwargs)

        with (
            patch.object(cache, "get_lock", side_effect=_get_lock),
            capture_logs() as logs,
        ):
            first = harness.tick()
            second = harness.tick()
            broken["on"] = False
            third = harness.tick()
            broken["on"] = True
            harness.clock.advance(RECOVERY_TRIAL_MAX_SECONDS)
            fourth = harness.tick()

        assert first.skipped == second.skipped == {DOMAIN: SKIP_LOCK_UNAVAILABLE}
        assert fourth.skipped == {DOMAIN: SKIP_LOCK_UNAVAILABLE}
        assert [t.outcome for t in third.trials] == [OUTCOME_FAILED]
        warned = [
            e
            for e in _events(logs, "replay_service.inflight_cache_unavailable")
            if e.get("lane") == "recovery_trial"
        ]
        assert len(warned) == 2
        assert warned[0]["log_level"] == "warning"
        assert warned[0]["error"] == "cache down"
        assert (
            len(_events(logs, "replay_service.inflight_cache_unavailable_continuing"))
            == 1
        )
        assert len(handler.replayed) == 1

    def test_skip_when_no_candidate_is_allowed_keeps_the_streak_and_moves_the_walk(
        self, harness
    ):
        # Given: every parked entry refused by the handler's can_replay.
        first = harness.park(offset=1)
        last = harness.park(offset=2)
        handler = harness.handler(refused={first, last})
        harness.write_pacing(_TrialPacing(streak=2, next_at=T0 - 1.0, after={}))

        # When
        result = harness.tick()

        # Then
        assert result.skipped == {DOMAIN: SKIP_NO_CANDIDATE}
        assert handler.replayed == []
        assert handler.asked == [first, last]
        for dlq_id in (first, last):
            entry = harness.entry(dlq_id)
            assert (entry.status, entry.retry_count) == ("pending", 0)
        pacing = harness.pacing()
        assert (pacing.streak, pacing.next_at) == (2, T0 - 1.0)
        assert pacing.after == {_lane_key(DECLARED, DOMAIN): harness.cursor_of(last)}


# =============================================================================
# Behavior — how one trial ended
# =============================================================================


class TestRecoveryTrialOutcomeBehavior:
    """The entry, its recovery-trial count and the pacing record, per outcome."""

    @pytest.mark.parametrize(
        ("step", "outcome", "status", "retry_count", "trials", "paced"),
        [
            (STEP_SUCCEED, OUTCOME_SUCCEEDED, "resolved", 1, None, None),
            (STEP_FAIL, OUTCOME_FAILED, "pending", 0, 1, (1, T0 + 60.0)),
            (STEP_RAISE, OUTCOME_FAILED, "pending", 0, 1, (1, T0 + 60.0)),
            (
                STEP_BREAKER_REFUSED,
                OUTCOME_BREAKER_REFUSED,
                "pending",
                0,
                None,
                (1, T0 + 60.0),
            ),
            (STEP_NOT_STARTED, OUTCOME_NOT_RUN, "pending", 0, None, (0, 0.0)),
            (
                STEP_STILL_RUNNING,
                OUTCOME_STILL_RUNNING,
                "replaying",
                0,
                None,
                (1, T0 + 60.0),
            ),
        ],
        ids=[
            "succeeded",
            "failed",
            "handler_raised",
            "own_breaker_refused",
            "not_run",
            "still_running",
        ],
    )
    def test_trial_outcome_settles_the_entry_and_the_pacing_record(
        self, harness, step, outcome, status, retry_count, trials, paced
    ):
        # Given
        handler = harness.handler(steps=[step])
        dlq_id = harness.park()

        # When
        result = harness.tick()

        # Then: the record, the entry, the pacing.
        assert [(t.domain, t.dlq_id, t.outcome) for t in result.trials] == [
            (DOMAIN, dlq_id, outcome)
        ]
        assert handler.replayed == [dlq_id]
        entry = harness.entry(dlq_id)
        assert (entry.status, entry.retry_count) == (status, retry_count)
        assert entry.metadata.get(RECOVERY_TRIALS_METADATA_KEY) == trials
        pacing = harness.pacing()
        if paced is None:
            assert pacing is None
        else:
            assert (pacing.streak, pacing.next_at) == paced
            assert pacing.after == {
                _lane_key(DECLARED, DOMAIN): harness.cursor_of(dlq_id)
            }

    def test_trial_outcome_succeeded_resolves_with_the_recovery_provenance(
        self, harness
    ):
        harness.handler(steps=[STEP_SUCCEED])
        dlq_id = harness.park()

        harness.tick()

        assert harness.entry(dlq_id).resolution_type == (
            ResolutionTrigger.AUTO_REPLAY_RECOVERY.value
        )

    def test_trial_outcome_failed_twice_counts_two_trials_and_doubles_the_spacing(
        self, harness
    ):
        harness.handler(default=STEP_FAIL)
        dlq_id = harness.park()

        harness.tick()
        harness.clock.advance(RECOVERY_TRIAL_BASE_SECONDS)
        harness.tick()

        entry = harness.entry(dlq_id)
        assert (entry.status, entry.retry_count) == ("pending", 0)
        assert entry.metadata[RECOVERY_TRIALS_METADATA_KEY] == 2
        pacing = harness.pacing()
        assert pacing.streak == 2
        assert pacing.next_at == harness.clock.now + _TRIAL_BACKOFF.calculate(2)

    def test_trial_outcome_failed_is_not_retried_before_its_paced_time(self, harness):
        handler = harness.handler(default=STEP_FAIL)
        harness.park()

        harness.tick()
        harness.clock.advance(RECOVERY_TRIAL_BASE_SECONDS - 1)
        early = harness.tick()

        assert early.skipped == {DOMAIN: SKIP_NOT_DUE}
        assert len(handler.replayed) == 1

    def test_trial_outcome_succeeded_clears_a_long_streak(self, harness):
        harness.handler(steps=[STEP_SUCCEED])
        harness.park()
        harness.write_pacing(_TrialPacing(streak=7, next_at=T0 - 1.0, after={}))

        harness.tick()

        assert harness.pacing() is None

    def test_trial_outcome_logs_one_info_line_per_trial(self, harness):
        harness.handler(steps=[STEP_FAIL])
        dlq_id = harness.park()

        with capture_logs() as logs:
            harness.tick()

        completed = _events(logs, "replay_service.recovery_trial_completed")
        assert len(completed) == 1
        assert completed[0]["log_level"] == "info"
        assert completed[0]["dlq_id"] == dlq_id
        assert completed[0]["outcome"] == OUTCOME_FAILED
        assert completed[0]["streak"] == 1

    def test_trial_resolved_by_its_completed_key_without_running_is_not_run(
        self, harness
    ):
        """A job its own completed key resolved never called the dependency:
        the entry is resolved, but the trial is no evidence of a recovery — no
        sweep, and the pacing stands as it was."""

        def _already_done(entry):
            return ReplayResult.succeeded(
                entry.id,
                "already done under its idempotency key",
                data={"job_started": False, "rejected_by_breaker": False},
            )

        harness.handler(steps=[_already_done])
        dlq_id = harness.park()
        prior = _TrialPacing(streak=2, next_at=T0 - 5.0, after={})
        harness.write_pacing(prior)

        result = harness.tick()

        assert [t.outcome for t in result.trials] == [OUTCOME_NOT_RUN]
        assert result.trials[0].dispatch is None
        harness.dispatched.assert_not_called()
        assert harness.entry(dlq_id).status == "resolved"
        pacing = harness.pacing()
        assert (pacing.streak, pacing.next_at) == (prior.streak, prior.next_at)

    def test_worker_died_trial_leaves_the_entry_taken_and_the_domain_paced(
        self, harness
    ):
        """A trial whose worker dies counts as one attempt (S5): nothing gives
        it back, and the pacing written before the trial stands."""
        harness.handler(steps=[STEP_DIE])
        dlq_id = harness.park()

        with pytest.raises(WorkerDied):
            harness.tick()

        entry = harness.entry(dlq_id)
        assert (entry.status, entry.retry_count) == ("replaying", 1)
        pacing = harness.pacing()
        assert (pacing.streak, pacing.next_at) == (1, T0 + 60.0)


# =============================================================================
# Behavior — the candidate walk (one cursor per lane)
# =============================================================================


class TestRecoveryCandidateWalkBehavior:
    """Which parked entry a tick trials, and where each lane's cursor stops."""

    def test_end_of_backlog_single_parked_job_is_trialed_again_on_the_next_due_tick(
        self, harness
    ):
        """No tick is spent on the end of the backlog: the walk wraps."""
        handler = harness.handler(steps=[STEP_FAIL, STEP_FAIL])
        dlq_id = harness.park()

        first = harness.tick()
        harness.clock.advance(RECOVERY_TRIAL_BASE_SECONDS)
        second = harness.tick()

        assert [t.dlq_id for t in first.trials] == [dlq_id]
        assert [t.dlq_id for t in second.trials] == [dlq_id]
        assert handler.replayed == [dlq_id, dlq_id]

    def test_end_of_backlog_wraps_to_the_oldest_after_the_newest_was_trialed(
        self, harness
    ):
        handler = harness.handler(default=STEP_FAIL)
        oldest = harness.park(offset=1)
        newest = harness.park(offset=2)

        harness.tick()
        harness.clock.advance(RECOVERY_TRIAL_BASE_SECONDS)
        harness.tick()
        harness.clock.advance(_TRIAL_BACKOFF.calculate(2))
        harness.tick()

        assert handler.replayed == [oldest, newest, oldest]

    def test_lane_cursor_two_lanes_one_all_refused_passes_over_no_entry_of_the_other(
        self, harness
    ):
        # Given: lane A holds 30 refused entries; lane B one allowed entry
        # parked between A's 6th and 7th.
        lane_a = [harness.park(failure_type=DECLARED, offset=i) for i in range(30)]
        allowed = harness.park(failure_type=DECLARED_B, offset=5.5)
        handler = harness.handler(
            declared=(DECLARED, DECLARED_B), refused=set(lane_a), default=STEP_FAIL
        )

        # When: two due ticks.
        first = harness.tick()
        cursors_after_first = harness.pacing().after
        harness.clock.advance(RECOVERY_TRIAL_BASE_SECONDS)
        second = harness.tick()

        # Then: B's entry is trialed both times, never passed over.
        assert [t.dlq_id for t in first.trials] == [allowed]
        assert [t.dlq_id for t in second.trials] == [allowed]
        assert cursors_after_first == {
            _lane_key(DECLARED, DOMAIN): harness.cursor_of(lane_a[5]),
            _lane_key(DECLARED_B, DOMAIN): harness.cursor_of(allowed),
        }
        # The first tick examined A's entries only up to B's.
        assert handler.asked[:7] == [*lane_a[:6], allowed]

    def test_refused_run_three_pages_ahead_of_an_allowed_one_is_crossed_in_one_tick(
        self, harness
    ):
        refused = [harness.park(offset=i) for i in range(60)]
        allowed = harness.park(offset=60)
        handler = harness.handler(refused=set(refused), default=STEP_FAIL)

        result = harness.tick()

        assert [t.dlq_id for t in result.trials] == [allowed]
        # The walk asks every entry once; the replay's own gate asks the
        # candidate again before taking it.
        assert handler.asked == [*refused, allowed, allowed]
        assert harness.pacing().after == {
            _lane_key(DECLARED, DOMAIN): harness.cursor_of(allowed)
        }

    def test_scan_exhausted_empty_page_is_continued_not_taken_as_the_end(self, harness):
        """A page that stopped on its scan bound with nothing matching (the
        Redis selector) carries a cursor to continue from."""
        skipped_by_scan = harness.park(offset=1)
        allowed = harness.park(offset=2)
        handler = harness.handler(default=STEP_FAIL)
        bound_cursor = harness.cursor_of(skipped_by_scan)
        real_page = InMemoryFailedOperationRepository.find_replayable_page
        first_call = {"done": False}

        def _find_page(self, **kwargs):
            if kwargs["failure_type"] == DECLARED and not first_call["done"]:
                first_call["done"] = True
                return ReplayablePage(
                    entries=[], next_cursor=bound_cursor, scan_exhausted=True
                )
            return real_page(self, **kwargs)

        with patch.object(
            InMemoryFailedOperationRepository,
            "find_replayable_page",
            autospec=True,
            side_effect=_find_page,
        ):
            result = harness.tick()

        assert [t.dlq_id for t in result.trials] == [allowed]
        assert handler.asked == [allowed, allowed]

    def test_not_run_cursor_job_key_abort_ahead_of_an_allowed_one(self, harness):
        """An entry whose job its own key kept from starting does not block the
        entries behind it: the next tick walks past it, and the record's streak
        and next time are what they were before the trial."""
        # Given
        held = harness.park(offset=1)
        allowed = harness.park(offset=2)
        handler = harness.handler(steps=[STEP_NOT_STARTED, STEP_FAIL])

        # When: two ticks at the same instant (the not-run trial paced nothing).
        first = harness.tick()
        pacing_after_first = harness.pacing()
        second = harness.tick()

        # Then
        assert [(t.dlq_id, t.outcome) for t in first.trials] == [
            (held, OUTCOME_NOT_RUN)
        ]
        assert (pacing_after_first.streak, pacing_after_first.next_at) == (0, 0.0)
        assert pacing_after_first.after == {
            _lane_key(DECLARED, DOMAIN): harness.cursor_of(held)
        }
        assert [t.dlq_id for t in second.trials] == [allowed]
        assert handler.replayed == [held, allowed]
        held_entry = harness.entry(held)
        assert (held_entry.status, held_entry.retry_count) == ("pending", 0)

    def test_not_run_cursor_restores_a_prior_streak_and_next_time(self, harness):
        harness.handler(steps=[STEP_NOT_STARTED])
        held = harness.park()
        prior = _TrialPacing(streak=3, next_at=T0 - 5.0, after={})
        harness.write_pacing(prior)

        harness.tick()

        pacing = harness.pacing()
        assert (pacing.streak, pacing.next_at) == (prior.streak, prior.next_at)
        assert pacing.after == {_lane_key(DECLARED, DOMAIN): harness.cursor_of(held)}

    def test_walk_reaching_the_deadline_skips_with_the_cursors_it_reached(
        self, harness
    ):
        refused = [harness.park(offset=i) for i in range(3)]
        handler = harness.handler(refused=set(refused))
        ticks = iter([0.0, 0.0, 0.0, 0.0, 10.0])

        result = harness.tick(deadline=5.0, monotonic=lambda: next(ticks, 10.0))

        assert result.skipped == {DOMAIN: SKIP_DEADLINE}
        assert handler.replayed == []
        assert harness.pacing().after != {}


# =============================================================================
# Behavior — breaker rows from the shared store
# =============================================================================


class _LayeredOverFakeL2:
    """A real layered breaker repository over a stand-in shared store."""

    def __init__(self, l2_rows: list[CircuitBreakerStateData]) -> None:
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
            LayeredCircuitBreakerStateRepository,
        )

        self.l2 = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
        self.l2.get_all_states.return_value = []
        self.l2.get_cluster_states.return_value = l2_rows
        with (
            patch(
                "baldur.adapters.memory.layered_repository.drift_operations."
                "DriftOperationsMixin._schedule_drift_reconciliation",
                return_value=None,
            ),
            patch(
                "baldur.adapters.memory.layered_repository.base."
                "LayeredRepositoryBase._ensure_l2_warmup_once",
                return_value=None,
            ),
        ):
            self.repo = LayeredCircuitBreakerStateRepository(
                l2_repo=self.l2, adapter_type="redis"
            )
        self.repo._l2_healthy = False  # quarantined: no automatic exit
        # The bounded L2 read runs on the shared pool in production; inline
        # here, so no pool thread outlives the test.
        self.repo._get_executor = _InlineExecutor


class _InlineExecutor:
    """Runs a submitted read at once; its future answers as the pool's would."""

    def submit(self, fn, *args, **kwargs):
        future: Future[Any] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001 - re-raised from result()
            future.set_exception(exc)
        return future


class TestRecoveryTrialBreakerRowsBehavior:
    """The rows the tick judges, and where it reads them from (S6)."""

    @pytest.mark.parametrize(
        "make_rows",
        [lambda: [_open_past_timeout()], lambda: [_row("half_open")]],
        ids=["open_past_timeout", "half_open"],
    )
    def test_open_past_timeout_or_half_open_row_is_trialed(self, harness, make_rows):
        """Such a row admits a call — the job's breaker takes it as a probe —
        so a breaker another process left OPEN with no traffic is not skipped
        forever."""
        handler = harness.handler()
        dlq_id = harness.park()
        harness.store.rows = make_rows()

        result = harness.tick()

        assert [(t.dlq_id, t.outcome) for t in result.trials] == [
            (dlq_id, OUTCOME_SUCCEEDED)
        ]
        assert handler.replayed == [dlq_id]

    @pytest.mark.parametrize(
        ("make_local_rows", "trialed"),
        [(lambda: [_row("closed")], True), (lambda: [_open_inside_timeout()], False)],
        ids=["local_closed", "local_refusing"],
    )
    def test_unreached_default_store_judges_this_process_s_rows(
        self, harness, make_local_rows, trialed
    ):
        """Nobody named a shared store: this process's rows are the cluster."""
        handler = harness.handler()
        harness.park()
        harness.store.raise_reason = UNREACHED_DEFAULT_STORE_REASON
        harness.store.local_rows = make_local_rows()

        result = harness.tick()

        assert bool(handler.replayed) is trialed
        assert result.status == TICK_COMPLETED

    @pytest.mark.parametrize(
        ("make_l2_rows", "trialed", "skip"),
        [
            (lambda: [_pinned_closed()], False, SKIP_OPERATOR_HOLD),
            (lambda: [_row("closed")], True, None),
        ],
        ids=["store_holds_a_pin", "store_answers_no_pin"],
    )
    def test_quarantined_link_reads_the_store_past_the_quarantine(
        self, harness, make_l2_rows, trialed, skip
    ):
        # Given: this process quarantined its link to the shared store.
        layered = _LayeredOverFakeL2(make_l2_rows())
        harness.cb._repository = layered.repo
        handler = harness.handler()
        harness.park()

        # When
        result = harness.tick()

        # Then: the store decided, and the quarantine is untouched.
        assert bool(handler.replayed) is trialed
        assert result.skipped.get(DOMAIN) == skip
        assert layered.repo._l2_healthy is False
        assert layered.l2.get_cluster_states.call_count >= 1

    def test_quarantined_link_whose_store_read_fails_starts_no_trial(self, harness):
        layered = _LayeredOverFakeL2([])
        layered.l2.get_cluster_states.side_effect = RuntimeError("connection reset")
        harness.cb._repository = layered.repo
        handler = harness.handler()
        harness.park()

        result = harness.tick()

        assert result.status == TICK_BREAKER_STATE_UNAVAILABLE
        assert handler.replayed == []
        assert layered.repo._l2_healthy is False

    def test_quarantine_reason_is_the_one_the_layered_read_raises(self):
        """Guards the substitution against a renamed reason."""
        assert L2_QUARANTINED_REASON == "l2_quarantined"

    def test_backend_degraded_read_starts_no_trial(self, harness):
        handler = harness.handler()
        dlq_id = harness.park()
        harness.store.raise_reason = "backend_degraded"

        result = harness.tick()

        assert result.status == TICK_BREAKER_STATE_UNAVAILABLE
        assert handler.replayed == []
        assert harness.entry(dlq_id).retry_count == 0


# =============================================================================
# Behavior — the reads made immediately before a trial, in order
# =============================================================================


class TestRecoveryTrialGovernanceOrderBehavior:
    """Integrity, the rows again, then the kill switch and governance — last."""

    def test_reads_before_a_trial_run_in_order_kill_switch_and_governance_last(
        self, harness
    ):
        # Given: every read logs itself.
        calls: list[str] = []
        harness.store.calls = calls
        harness.switch.calls = calls
        harness.integrity.side_effect = lambda domain: calls.append("integrity") or True
        harness.governance.check_all_governance.side_effect = lambda **kw: (
            calls.append("governance") or GovernanceCheckResult(allowed=True)
        )
        harness.handler(on_replay=lambda _e: calls.append("trial"))
        harness.park()

        # When
        harness.tick()

        # Then
        assert calls == [
            "rows",
            "integrity",
            "rows",
            "kill_switch",
            "governance",
            "trial",
            "rows",
        ]
        harness.integrity.assert_called_once_with(DOMAIN)
        harness.governance.check_all_governance.assert_called_once_with(
            check_kill_switch=True,
            check_emergency=True,
            emergency_min_level=2,
            check_error_budget=True,
            operation_name="recovery_trial",
            service_name="ReplayService",
            domain=DOMAIN,
            audit_on_block=False,
        )

    def test_kill_switch_pulled_during_the_candidate_walk_starts_no_trial(
        self, harness
    ):
        dlq_id = harness.park()
        handler = harness.handler()
        real_page = InMemoryFailedOperationRepository.find_replayable_page

        def _pull_switch_then_read(self, **kwargs):
            harness.switch.enabled = False
            return real_page(self, **kwargs)

        with patch.object(
            InMemoryFailedOperationRepository,
            "find_replayable_page",
            autospec=True,
            side_effect=_pull_switch_then_read,
        ):
            result = harness.tick()

        assert result.status == TICK_GOVERNANCE_BLOCKED
        assert handler.replayed == []
        entry = harness.entry(dlq_id)
        assert (entry.status, entry.retry_count) == ("pending", 0)

    @pytest.mark.parametrize(
        "switch_state",
        ["state_unknown", "kill_switch_on", "kill_switch_read_raises"],
    )
    def test_kill_switch_state_unknown_or_on_starts_no_trial(
        self, harness, switch_state
    ):
        handler = harness.handler()
        harness.park()
        if switch_state == "state_unknown":
            harness.switch.known = False
        elif switch_state == "kill_switch_on":
            harness.switch.enabled = False
        else:
            harness.switch.raises = True

        result = harness.tick()

        assert result.status == TICK_GOVERNANCE_BLOCKED
        assert handler.replayed == []
        harness.governance.check_all_governance.assert_not_called()

    def test_governance_block_starts_no_trial(self, harness):
        handler = harness.handler()
        dlq_id = harness.park()
        harness.governance.check_all_governance.return_value = GovernanceCheckResult(
            allowed=False, block_message="emergency level 2"
        )

        result = harness.tick()

        assert result.status == TICK_GOVERNANCE_BLOCKED
        assert handler.replayed == []
        assert harness.entry(dlq_id).retry_count == 0
        assert harness.pacing() is None

    def test_governance_block_stops_the_tick_for_every_later_domain(self, harness):
        first = harness.handler(DOMAIN)
        second = harness.handler(OTHER_DOMAIN)
        harness.park(DOMAIN)
        harness.park(OTHER_DOMAIN)
        harness.governance.check_all_governance.return_value = GovernanceCheckResult(
            allowed=False
        )

        result = harness.tick()

        assert result.status == TICK_GOVERNANCE_BLOCKED
        assert first.replayed == second.replayed == []
        assert len(harness.governance.check_all_governance.call_args_list) == 1

    def test_integrity_blocked_starts_no_trial_and_warns_on_the_transition(
        self, harness
    ):
        handler = harness.handler()
        harness.park()
        harness.integrity.return_value = False

        with capture_logs() as logs:
            first = harness.tick()
            second = harness.tick()
            harness.integrity.return_value = True
            third = harness.tick()

        assert first.status == second.status == TICK_INTEGRITY_BLOCKED
        assert [t.outcome for t in third.trials] == [OUTCOME_SUCCEEDED]
        warned = _events(logs, "replay_service.recovery_trial_integrity_blocked")
        assert len(warned) == 1
        assert warned[0]["log_level"] == "warning"
        assert len(handler.replayed) == 1

    def test_breaker_state_unavailable_at_the_read_before_the_trial_starts_none(
        self, harness
    ):
        handler = harness.handler()
        dlq_id = harness.park()
        harness.store.answers.extend([[], "backend_degraded"])

        result = harness.tick()

        assert result.status == TICK_BREAKER_STATE_UNAVAILABLE
        assert handler.replayed == []
        assert harness.entry(dlq_id).retry_count == 0

    def test_breaker_tripped_during_the_walk_skips_the_domain(self, harness):
        handler = harness.handler()
        harness.park()
        harness.store.answers.extend([[_row("closed")], [_open_inside_timeout()]])

        result = harness.tick()

        assert result.skipped == {DOMAIN: SKIP_BREAKER_REFUSING}
        assert handler.replayed == []


# =============================================================================
# Behavior — the rest of the backlog after a successful trial
# =============================================================================


class TestRecoveryDispatchAfterSuccessBehavior:
    """A success sweeps the rest — unless the trial itself closed the breaker."""

    @pytest.mark.parametrize(
        ("make_before", "make_after", "dispatch"),
        [
            (lambda: [_row("closed")], lambda: [_row("closed")], "dispatched"),
            (lambda: [_row("half_open")], lambda: [_row("half_open")], "dispatched"),
            (lambda: [_row("half_open")], lambda: [_row("closed")], "closed_by_trial"),
            (list, list, "dispatched"),
            (
                lambda: [_row("half_open")],
                lambda: [_open_inside_timeout()],
                "breaker_refusing",
            ),
        ],
        ids=[
            "closed_closed",
            "half_open_half_open",
            "half_open_closed",
            "no_breaker",
            "reopened_by_the_trial",
        ],
    )
    def test_dispatch_after_success_rows_before_and_after_decide_the_sweep(
        self, harness, make_before, make_after, dispatch
    ):
        # Given: the trial's call moves the job's breaker row.
        harness.store.rows = make_before()
        after = make_after()

        def _job_moves_the_row(_entry):
            harness.store.rows = after

        harness.handler(on_replay=_job_moves_the_row)
        dlq_id = harness.park()

        # When
        result = harness.tick()

        # Then
        assert result.trials == [
            TrialRecord(
                domain=DOMAIN,
                dlq_id=dlq_id,
                outcome=OUTCOME_SUCCEEDED,
                dispatch=dispatch,
            )
        ]
        if dispatch == "dispatched":
            harness.dispatched.assert_called_once_with(
                DOMAIN,
                trigger=ResolutionTrigger.AUTO_REPLAY_RECOVERY,
                escalate_failures=False,
                service=harness.service,
            )
        else:
            harness.dispatched.assert_not_called()

    def test_dispatch_after_success_with_the_rows_unreadable_still_dispatches(
        self, harness
    ):
        harness.handler()
        harness.park()
        harness.store.answers.extend([[], [], "backend_degraded"])

        result = harness.tick()

        assert result.trials[0].dispatch == "dispatched"
        harness.dispatched.assert_called_once()

    @pytest.mark.parametrize(
        "step", [STEP_FAIL, STEP_BREAKER_REFUSED, STEP_NOT_STARTED, STEP_STILL_RUNNING]
    )
    def test_dispatch_after_success_only_a_success_dispatches(self, harness, step):
        harness.handler(steps=[step])
        harness.park()

        result = harness.tick()

        assert result.trials[0].dispatch is None
        harness.dispatched.assert_not_called()


# =============================================================================
# Behavior — whole recoveries over the in-memory store
# =============================================================================


class TestRecoveryTrialScenarioBehavior:
    """An outage, its end, and what the trials cost the parked jobs."""

    def test_short_outage_no_traffic_job_is_replayed_within_ten_minutes(self, harness):
        """A breaker that never opened reads CLOSED throughout; nothing calls
        the dependency until the trial does."""
        # Given: the dependency answers again just after a trial at full
        # spacing failed — the worst case for the latency promise.
        up_at = T0 + 1441.0
        dependency = {"up": False}

        steps = ScriptedReplayHandler(DOMAIN)

        def _job(entry):
            return steps.play(STEP_SUCCEED if dependency["up"] else STEP_FAIL, entry.id)

        harness.store.rows = [_row("closed")]
        harness.handler(default=_job)
        parked = [harness.park(offset=1), harness.park(offset=2)]

        # When: one tick a minute for an hour.
        succeeded_at = None
        while harness.clock.now < T0 + 3600 and succeeded_at is None:
            dependency["up"] = harness.clock.now >= up_at
            result = harness.tick()
            if any(t.outcome == OUTCOME_SUCCEEDED for t in result.trials):
                succeeded_at = harness.clock.now
            harness.clock.advance(RECOVERY_TICK_SECONDS)

        # Then
        assert succeeded_at is not None
        assert 0 < succeeded_at - up_at <= 600
        # The trials walked both entries; the one that succeeded is resolved,
        # the other is left to the sweep with every replay attempt.
        settled = sorted(
            (harness.entry(dlq_id).status, harness.entry(dlq_id).retry_count)
            for dlq_id in parked
        )
        assert settled == [("pending", 0), ("resolved", 1)]
        harness.dispatched.assert_called_once_with(
            DOMAIN,
            trigger=ResolutionTrigger.AUTO_REPLAY_RECOVERY,
            escalate_failures=False,
            service=harness.service,
        )

    @pytest.mark.parametrize(
        "step", [STEP_FAIL, STEP_RAISE, STEP_BREAKER_REFUSED], ids=lambda s: s
    )
    def test_long_outage_never_reviewed(self, harness, step):
        """However many trials an outage takes, a job whose dependency still
        fails, or whose own breaker refuses, keeps every replay attempt."""
        handler = harness.handler(default=step)
        dlq_id = harness.park()

        for _ in range(180):
            harness.tick()
            harness.clock.advance(RECOVERY_TICK_SECONDS)

        entry = harness.entry(dlq_id)
        assert (entry.status, entry.retry_count) == ("pending", 0)
        assert len(handler.replayed) > 20
        expected_trials = (
            None if step == STEP_BREAKER_REFUSED else len(handler.replayed)
        )
        assert entry.metadata.get(RECOVERY_TRIALS_METADATA_KEY) == expected_trials

    def test_worker_died_trial_costs_exactly_one_attempt(self, harness):
        # Given: the worker dies mid-trial at 10:00.
        handler = harness.handler(steps=[STEP_DIE], default=STEP_FAIL)
        with freeze_time("2026-10-02 10:00:00"):
            dlq_id = harness.park()
            with pytest.raises(WorkerDied):
                harness.tick()

        # When: a later tick, past the release window, releases and trials it.
        with freeze_time("2026-10-02 10:31:00"):
            harness.clock.advance(RECOVERY_TRIAL_MAX_SECONDS)
            result = harness.tick()

        # Then: the dead trial cost one attempt; the failed one cost none.
        assert result.released == 1
        assert [t.outcome for t in result.trials] == [OUTCOME_FAILED]
        entry = harness.entry(dlq_id)
        assert (entry.status, entry.retry_count) == ("pending", 1)
        assert handler.replayed == [dlq_id, dlq_id]

    def test_no_breaker_job_is_replayed_after_its_dependency_answers(self, harness):
        """A job with no breaker has no breaker evidence at all; the trial needs none."""
        handler = harness.handler(steps=[STEP_FAIL], default=STEP_SUCCEED)
        trial_entry = harness.park(offset=1)
        behind = harness.park(offset=2)

        failed = harness.tick()
        harness.clock.advance(RECOVERY_TRIAL_BASE_SECONDS)
        succeeded = harness.tick()

        assert harness.store.rows == []
        assert [t.outcome for t in failed.trials] == [OUTCOME_FAILED]
        assert [t.outcome for t in succeeded.trials] == [OUTCOME_SUCCEEDED]
        # The walk moved past the entry the failed trial left behind it.
        assert handler.replayed == [trial_entry, behind]
        first = harness.entry(trial_entry)
        assert (first.status, first.retry_count) == ("pending", 0)
        assert harness.entry(behind).status == "resolved"
        harness.dispatched.assert_called_once()

    def test_killed_sweep_entries_left_replaying_and_lock_held_are_recovered_with_no_beat(
        self, harness
    ):
        # Given: at 10:00 a sweep took three entries — one on its last allowed
        # attempt — and its worker died holding the domain's recovery lock.
        handler = harness.handler(default=STEP_SUCCEED)
        with freeze_time("2026-10-02 10:00:00"):
            first = harness.park(offset=1)
            second = harness.park(offset=2)
            last_attempt = harness.park(offset=3, retry_count=1, max_retries=2)
            for dlq_id in (first, second, last_attempt):
                assert harness.repo.try_acquire_for_replay(dlq_id, 2) is not None
            lock, state, _ = harness.service.try_acquire_recovery_lock(DOMAIN)
            assert lock is not None
            assert state == "acquired"

        # When: the next tick after the release window, with no beat at all.
        with freeze_time("2026-10-02 10:31:00"):
            result = harness.tick()

        # Then: released, the lock's TTL long gone, a trial, and the sweep.
        assert result.released == 3
        assert [(t.dlq_id, t.outcome) for t in result.trials] == [
            (first, OUTCOME_SUCCEEDED)
        ]
        assert handler.replayed == [first]
        assert harness.entry(second).status == "pending"
        at_cap = harness.entry(last_attempt)
        assert at_cap.status == "requires_review"
        assert at_cap.resolution_note == STALE_RELEASE_AT_CAP_NOTE
        harness.dispatched.assert_called_once()

    def test_work_may_continue_trial_holds_the_entry_until_the_stale_release(
        self, harness
    ):
        """A trial whose job may still be running leaves it REPLAYING: no lane
        starts that job again before the stale release."""
        handler = harness.handler(steps=[STEP_STILL_RUNNING], default=STEP_SUCCEED)
        with freeze_time("2026-10-02 10:00:00"):
            dlq_id = harness.park()
            first = harness.tick()
            harness.clock.advance(RECOVERY_TRIAL_BASE_SECONDS)
            during = harness.tick()

        assert [t.outcome for t in first.trials] == [OUTCOME_STILL_RUNNING]
        # Nothing PENDING is left under the job name: the tick has no work.
        assert (during.status, during.trials) == (TICK_IDLE, [])
        assert handler.replayed == [dlq_id]
        assert harness.entry(dlq_id).status == "replaying"
        pacing = harness.pacing()
        assert (pacing.streak, pacing.next_at) == (1, T0 + 60.0)
