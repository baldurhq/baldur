"""Recovery of parked jobs once their dependency answers again.

Two parts, and the circuit read both share:

- **The recovery trial** (:func:`run_recovery_trials`). A periodic tick that,
  for every job name (stored domain) with parked work, replays one parked job
  as a trial — the only call that can show the job's dependency answers, since
  a breaker that never opened reads CLOSED throughout an outage, a breaker
  left OPEN with no traffic never closes, and a job with no breaker has no
  breaker evidence at all. At most one trial per job name is started per trial
  interval (60 s, doubling to 540 s while trials keep failing). A trial whose
  job still fails, is cut off by its own timeout, or is refused by its own
  breaker costs the job none of its replay attempts; a trial whose worker dies
  counts as one. A trial that succeeds resolves its job and dispatches the
  recovery sweep for the rest of the backlog.
- **The one dispatch path** for a recovery sweep
  (:func:`dispatch_recovery_sweep`): the breaker's CLOSED event, a successful
  trial and an operator's close-with-replay all queue the same chain.

No trial overrides a decision not to replay: an operator's manual pin on a
breaker projecting onto the domain, a breaker that refuses calls by its own
admission rule, the kill switch and governance, and the integrity gate are all
asked before every trial, and the handler's own ``can_replay`` before the entry
is taken.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog

from baldur.core.backoff import ExponentialBackoff
from baldur.core.process_utils import fork_safe_lock
from baldur.interfaces.repositories import (
    FailedOperationRepository,
    ResolutionTrigger,
    decode_replay_cursor,
    encode_replay_cursor,
    replay_cursor_position,
)
from baldur.utils.domain_validation import resolve_stored_domain

if TYPE_CHECKING:
    from baldur.interfaces.repositories import (
        CircuitBreakerStateData,
        FailedOperationData,
    )
    from baldur.services.replay_service.service import ReplayService

logger = structlog.get_logger()

__all__ = [
    "RECOVERY_TICK_SECONDS",
    "RECOVERY_TRIAL_BASE_SECONDS",
    "RECOVERY_TRIAL_MAX_SECONDS",
    "RecoveryTickResult",
    "RecoveryTrialRunner",
    "TrialRecord",
    "dispatch_recovery_sweep",
    "dispatch_recovery_tick",
    "get_recovery_trial_state",
    "read_fleet_breaker_rows",
    "recovery_trials_enabled",
    "reset_recovery_trial_state",
    "run_recovery_trials",
    "stale_release_minutes",
]

# How often the recovery tick runs, on both scheduling surfaces (Celery beat
# and the leader scheduler).
RECOVERY_TICK_SECONDS = 60

# Spacing between trials of one job name while they keep failing: the first
# retry after 60 s, doubling to the cap. The cap plus one tick is the latency
# promise — a recovery starts within ten minutes of the dependency answering.
# Constants, not settings: nobody should have to tune them.
RECOVERY_TRIAL_BASE_SECONDS = 60
RECOVERY_TRIAL_MAX_SECONDS = 540

# Entries one lane page of the candidate walk reads at a time.
_TRIAL_CANDIDATE_PAGE = 20

# Hard time limit of the longest replay task (the batch replays' 600 s). A
# stale release sooner than this could hand back an entry a living replay
# still holds, which the next replay would then run beside it.
_LONGEST_REPLAY_TASK_MINUTES = 10

# Cache key of one job name's trial pacing record.
_PACING_KEY_PREFIX = "replay:recovery_trial:"

# Lane the recovery trial reports on the daily report's auto-replay line.
_DAILY_REPORT_SERVICE_NAME = "recovery_trial"

# Pacing is the exponential spacing, jitter-free: the due test must give the
# same answer on consecutive ticks.
_TRIAL_BACKOFF = ExponentialBackoff(
    base_delay=float(RECOVERY_TRIAL_BASE_SECONDS),
    max_delay=float(RECOVERY_TRIAL_MAX_SECONDS),
    multiplier=2.0,
    jitter=False,
)

# Tick outcomes.
TICK_DISABLED = "disabled"
TICK_IDLE = "idle"
TICK_COMPLETED = "completed"
TICK_UNSUPPORTED = "unsupported"
TICK_BREAKER_STATE_UNAVAILABLE = "breaker_state_unavailable"
TICK_GOVERNANCE_BLOCKED = "governance_blocked"
TICK_INTEGRITY_BLOCKED = "integrity_blocked"

# Why one job name got no trial this tick.
SKIP_BREAKER_REFUSING = "breaker_refusing"
SKIP_OPERATOR_HOLD = "operator_hold"
SKIP_NO_LANES = "no_lanes"
SKIP_NOT_DUE = "not_due"
SKIP_RECOVERY_RUNNING = "recovery_running"
SKIP_LOCK_UNAVAILABLE = "lock_unavailable"
SKIP_NO_CANDIDATE = "no_candidate"
SKIP_DEADLINE = "deadline"

# How one trial ended.
OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_FAILED = "failed"
OUTCOME_STILL_RUNNING = "still_running"
OUTCOME_BREAKER_REFUSED = "breaker_refused"
OUTCOME_NOT_RUN = "not_run"

# What a successful trial did about the rest of the backlog besides dispatch.
_DISPATCH_LEFT_TO_CLOSED_EVENT = "closed_by_trial"
_DISPATCH_HELD_BY_REFUSAL = "breaker_refusing"
_DISPATCH_HELD_BY_PIN = "operator_hold"


@dataclass(frozen=True)
class TrialRecord:
    """One trial a tick started.

    Attributes:
        domain: The job name (stored domain) the trial was for.
        dlq_id: The parked entry it replayed.
        outcome: How it ended (``succeeded`` / ``failed`` / ``still_running`` /
            ``breaker_refused`` / ``not_run``).
        dispatch: For a success, what happened to the rest of the backlog: a
            dispatch outcome, ``closed_by_trial`` (the trial closed the
            breaker and its CLOSED event dispatches), ``breaker_refusing``, or
            ``operator_hold`` (an operator pinned a breaker while it ran).
    """

    domain: str
    dlq_id: str
    outcome: str
    dispatch: str | None = None


@dataclass
class RecoveryTickResult:
    """What one recovery tick did.

    Attributes:
        status: ``completed``, or why the tick did nothing or stopped early
            (``disabled``, ``idle``, ``unsupported``,
            ``breaker_state_unavailable``, ``governance_blocked``,
            ``integrity_blocked``).
        released: Entries the stale release moved out of REPLAYING.
        trials: Every trial the tick started.
        skipped: Job names that got no trial, with the reason.
    """

    status: str
    released: int = 0
    trials: list[TrialRecord] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)


@dataclass
class _TrialPacing:
    """One job name's trial pacing record (shared through the cache provider).

    ``after`` holds one cursor per lane: where the candidate walk stopped.
    A missing record is due at once with no streak.
    """

    streak: int = 0
    next_at: float = 0.0
    after: dict[str, str] = field(default_factory=dict)

    def is_due(self, now: float) -> bool:
        return now >= self.next_at

    def to_value(self) -> dict[str, Any]:
        return {"streak": self.streak, "next_at": self.next_at, "after": self.after}

    @classmethod
    def from_value(cls, value: Any) -> _TrialPacing:
        """Parse a stored record; anything unreadable reads as missing."""
        if not isinstance(value, dict):
            return cls()
        streak = value.get("streak", 0)
        next_at = value.get("next_at", 0.0)
        after = value.get("after", {})
        if not isinstance(streak, int) or isinstance(streak, bool) or streak < 0:
            streak = 0
        if not isinstance(next_at, (int, float)) or isinstance(next_at, bool):
            next_at = 0.0
        if not isinstance(after, dict):
            after = {}
        return cls(
            streak=streak,
            next_at=float(next_at),
            after={str(k): str(v) for k, v in after.items() if v},
        )


@dataclass
class _CandidateWalk:
    """Where one domain's candidate walk ended."""

    candidate: FailedOperationData | None = None
    cursors: dict[str, str] = field(default_factory=dict)
    reached_deadline: bool = False


@dataclass
class _LaneWalk:
    """One lane's progress through a candidate walk — at most once around.

    The walk starts strictly after ``start`` (the lane's stored cursor), runs
    to the lane's end, then, when it started past the beginning, wraps to the
    start and stops at entries past ``start``.
    """

    lane: tuple[str, str | None, str | None]
    key: str
    cursor: str | None
    start: tuple[float, str] | None
    wrapped: bool
    done: bool = False
    last_page: bool = False

    @classmethod
    def begin(
        cls, lane: tuple[str, str | None, str | None], after: dict[str, str]
    ) -> _LaneWalk:
        from baldur.services.replay_service.service import _lane_key

        key = _lane_key(lane[0], lane[1])
        start = after.get(key)
        return cls(
            lane=lane,
            key=key,
            cursor=start,
            start=decode_replay_cursor(start),
            wrapped=start is None,
        )

    def move_to(self, cursor: str, walk: _CandidateWalk) -> None:
        """Record the lane's new position, on the walk the tick persists."""
        self.cursor = cursor
        walk.cursors[self.key] = cursor

    def end_reached(self) -> None:
        """The lane ran out: wrap to its start once, or finish."""
        if self.wrapped:
            self.done = True
            return
        self.wrapped = True
        self.cursor = None

    def finish_page(self) -> None:
        """After the page's entries were examined: a short page was the end."""
        if self.last_page and not self.done:
            self.last_page = False
            self.end_reached()

    def read_page(
        self, repository: Any, max_replays: int, walk: _CandidateWalk
    ) -> list[tuple[tuple[float, str], _LaneWalk, FailedOperationData]]:
        """Read the lane's next page; return its entries with their positions."""
        failure_type, lane_domain, lane_source = self.lane
        page = repository.find_replayable_page(
            max_retries=max_replays,
            domain=lane_domain,
            failure_type=failure_type,
            source=lane_source,
            limit=_TRIAL_CANDIDATE_PAGE,
            cursor=self.cursor,
        )
        if not page.entries:
            if page.scan_exhausted and page.next_cursor:
                self.move_to(page.next_cursor, walk)
            else:
                self.end_reached()
            return []
        found = self._positioned(page.entries)
        if self.done:
            return found
        if not found:
            # Nothing on the page can carry a position: move past it.
            if page.next_cursor and page.next_cursor != self.cursor:
                self.move_to(page.next_cursor, walk)
            else:
                self.end_reached()
            return []
        self.last_page = (
            len(page.entries) < _TRIAL_CANDIDATE_PAGE and not page.scan_exhausted
        )
        return found

    def _positioned(
        self, entries: list[FailedOperationData]
    ) -> list[tuple[tuple[float, str], _LaneWalk, FailedOperationData]]:
        found: list[tuple[tuple[float, str], _LaneWalk, FailedOperationData]] = []
        for entry in entries:
            if entry.created_at is None:
                continue
            position = replay_cursor_position(entry.created_at, entry.id)
            if self.wrapped and self.start is not None and position > self.start:
                # Past where this lane began: walked all the way round.
                self.done = True
                break
            found.append((position, self, entry))
        return found


# Sentinel ``_take_domain_lock`` returns when the domain is skipped (a held or
# unavailable lock); a real lock may be None when no lock object is needed.
_NO_LOCK = object()
# Process-local transition memory for the tick's WARNING lines: each condition
# warns when it begins and drops to DEBUG while it lasts.
_state_lock = fork_safe_lock()
_state: dict[str, bool] = {}
_STATE_BREAKER_UNAVAILABLE = "breaker_state_unavailable"
_STATE_LOCK_UNAVAILABLE = "lock_unavailable"
_STATE_INTEGRITY_BLOCKED = "integrity_blocked"
_STATE_UNSUPPORTED_WARNED = "unsupported_warned"
_STATE_CELERY_MISSING_LOGGED = "celery_missing_logged"
_STATE_PUBLISH_FAILED_LOGGED = "publish_failed_logged"


def get_recovery_trial_state() -> dict[str, bool]:
    """Return the tick's process-local transition memory (read accessor)."""
    with _state_lock:
        return dict(_state)


def reset_recovery_trial_state() -> None:
    """Clear the tick's process-local transition memory (test isolation)."""
    with _state_lock:
        _state.clear()


def _transition(key: str, active: bool) -> bool:
    """Record a condition's state; True when it just became active."""
    with _state_lock:
        was_active = _state.get(key, False)
        _state[key] = active
    return active and not was_active


def stale_release_minutes() -> int:
    """Age past which the stale release returns a REPLAYING entry, in minutes.

    The configured ``stale_replaying_timeout_minutes``, floored at the hard
    time limit of the longest replay task: a release sooner than that could
    hand back an entry a living replay still holds.
    """
    from baldur.settings.dlq import get_dlq_settings

    return max(
        int(get_dlq_settings().stale_replaying_timeout_minutes),
        _LONGEST_REPLAY_TASK_MINUTES,
    )


def read_fleet_breaker_rows(repository: Any) -> list[CircuitBreakerStateData]:
    """Read every breaker row from the shared store, or raise.

    The fleet read (``get_cluster_states``), which raises instead of answering
    from a local or partial view, with two substitutions that are not failures:

    - nobody named a shared store — this process's own rows are the cluster;
    - this process quarantined its link to the shared store, which has no
      automatic exit — the store is read past the quarantine
      (``get_store_cluster_states``), which raises like the first read when
      the store does not answer.

    Raises:
        CircuitBreakerStateUnavailableError: the shared store cannot be read.
    """
    from baldur.services.circuit_breaker.exceptions import (
        L2_QUARANTINED_REASON,
        UNREACHED_DEFAULT_STORE_REASON,
        CircuitBreakerStateUnavailableError,
    )

    try:
        return list(repository.get_cluster_states())
    except CircuitBreakerStateUnavailableError as e:
        if e.reason == UNREACHED_DEFAULT_STORE_REASON:
            return list(repository.get_all_states())
        if e.reason == L2_QUARANTINED_REASON:
            store_read = getattr(repository, "get_store_cluster_states", None)
            if callable(store_read):
                return list(store_read())
        raise


def _rows_by_domain(
    rows: list[CircuitBreakerStateData],
) -> dict[str, list[CircuitBreakerStateData]]:
    """Group breaker rows by the stored domain their name projects onto."""
    grouped: dict[str, list[CircuitBreakerStateData]] = {}
    for row in rows:
        grouped.setdefault(resolve_stored_domain(row.service_name), []).append(row)
    return grouped


def _is_pinned(row: CircuitBreakerStateData) -> bool:
    from baldur.services.circuit_breaker.manual_control import is_manual_pin_active

    return is_manual_pin_active(row)


def _replay_automation_config(service: ReplayService | None) -> dict[str, Any]:
    """The replay-automation RuntimeConfig block, or {} without one."""
    if service is None:
        return {}
    return service._get_replay_automation_config() or {}


def _on_recovery_enabled(service: ReplayService) -> bool:
    """On-recovery replay switch: RuntimeConfig (present) → static settings."""
    from baldur.settings.replay_automation import get_replay_automation_settings

    config = _replay_automation_config(service)
    return bool(
        config.get(
            "on_recovery_enabled",
            get_replay_automation_settings().on_recovery_enabled,
        )
    )


def _recovery_trial_enabled() -> bool:
    from baldur.settings.replay_automation import get_replay_automation_settings

    return bool(get_replay_automation_settings().recovery_trial_enabled)


def _repository_gives_back(repository: Any) -> bool:
    """Whether the repository's class gives a replay attempt back."""
    implementation = getattr(type(repository), "return_replay_attempt", None)
    return (
        implementation is not None
        and implementation is not FailedOperationRepository.return_replay_attempt
    )


def _classify_trial(result: Any) -> str:
    """How a trial ended, read off the replay result."""
    from baldur.services.replay_service.service import (
        REASON_BREAKER_REFUSED,
        _job_start_flags,
        _skip_reason,
    )

    if not result.handler_ran:
        return OUTCOME_NOT_RUN
    skip_reason = _skip_reason(result)
    if skip_reason == REASON_BREAKER_REFUSED:
        return OUTCOME_BREAKER_REFUSED
    if result.skipped:
        return OUTCOME_NOT_RUN
    began, _ = _job_start_flags(result)
    if result.success:
        # A job its own completed key resolved without running says nothing
        # about the dependency.
        return OUTCOME_SUCCEEDED if began else OUTCOME_NOT_RUN
    if result.work_may_continue:
        return OUTCOME_STILL_RUNNING
    return OUTCOME_FAILED


class RecoveryTrialRunner:
    """One recovery tick over every job name with parked work.

    Dependencies are injectable for tests; each defaults to the process
    singleton.

    Args:
        replay_service: The replay service whose repository, cache, governance
            and replay pipeline the tick uses.
        circuit_breaker_service: Source of the breaker rows and of the
            breaker's own admission rule.
        system_control: Source of the kill switch.
        clock: Wall clock in epoch seconds (pacing, minute rotation).
        monotonic: Monotonic clock the deadline is measured on.
    """

    def __init__(
        self,
        *,
        replay_service: ReplayService | None = None,
        circuit_breaker_service: Any = None,
        system_control: Any = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if replay_service is None:
            from baldur.services.replay_service.service import get_replay_service

            replay_service = get_replay_service()
        self._service = replay_service
        self._cb_service = circuit_breaker_service
        self._system_control = system_control
        self._clock = clock
        self._monotonic = monotonic

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def run(self, *, deadline: float | None) -> RecoveryTickResult:
        """Run one tick, stopping before ``deadline`` (a monotonic value)."""
        if not (_on_recovery_enabled(self._service) and _recovery_trial_enabled()):
            return RecoveryTickResult(status=TICK_DISABLED)

        result = RecoveryTickResult(status=TICK_COMPLETED)
        result.released = self._release_stale()

        domains = self._domains_with_work()
        if not domains:
            result.status = TICK_IDLE
            return result

        if not _repository_gives_back(self._service.repository):
            if _transition(_STATE_UNSUPPORTED_WARNED, True):
                logger.warning(
                    "replay_service.recovery_trial_unsupported",
                    repository=type(self._service.repository).__name__,
                )
            result.status = TICK_UNSUPPORTED
            return result

        try:
            rows = _rows_by_domain(self._read_rows())
        except Exception as exc:
            self._note_rows_unavailable(exc)
            result.status = TICK_BREAKER_STATE_UNAVAILABLE
            return result

        failure_type_map = self._service._load_failure_type_map()
        try:
            for index, domain in enumerate(domains):
                if self._past(deadline):
                    for remaining in domains[index:]:
                        result.skipped[remaining] = SKIP_DEADLINE
                    break
                stop = self._run_domain(
                    domain, rows.get(domain, []), failure_type_map, deadline, result
                )
                if stop is not None:
                    result.status = stop
                    break
        finally:
            self._record_in_daily_report(result)

        logger.debug(
            "replay_service.recovery_tick_completed",
            status=result.status,
            released=result.released,
            trials=len(result.trials),
            skipped=dict(result.skipped),
        )
        return result

    def _run_domain(
        self,
        domain: str,
        projecting: list[CircuitBreakerStateData],
        failure_type_map: dict[str, list[str]],
        deadline: float | None,
        result: RecoveryTickResult,
    ) -> str | None:
        """Trial one job name; return a tick status when the tick must stop."""
        from baldur.services.replay_service.service import recovery_lanes

        if self._skip_for_rows(domain, projecting, result):
            return None

        lanes = recovery_lanes(domain, failure_type_map)
        if not lanes:
            self._skip(result, domain, SKIP_NO_LANES)
            return None

        if not self._read_pacing(domain).is_due(self._clock()):
            self._skip(result, domain, SKIP_NOT_DUE)
            return None

        lock = self._take_domain_lock(domain, result)
        if lock is _NO_LOCK:
            return None
        try:
            stop, record, rows_before = self._trial_under_lock(
                domain, lanes, deadline, result
            )
        finally:
            self._service.release_recovery_lock(lock, domain)

        if record is not None and record.outcome == OUTCOME_SUCCEEDED:
            record = TrialRecord(
                domain=record.domain,
                dlq_id=record.dlq_id,
                outcome=record.outcome,
                dispatch=self._dispatch_after_success(domain, rows_before),
            )
        if record is not None:
            result.trials.append(record)
        return stop

    def _take_domain_lock(self, domain: str, result: RecoveryTickResult) -> Any:
        """The domain's inflight lock, or ``_NO_LOCK`` (the domain is skipped)."""
        from baldur.services.replay_service.service import (
            RECOVERY_LOCK_HELD,
            RECOVERY_LOCK_UNAVAILABLE,
        )

        lock, lock_state, lock_error = self._service.try_acquire_recovery_lock(domain)
        if lock_state == RECOVERY_LOCK_HELD:
            self._skip(result, domain, SKIP_RECOVERY_RUNNING)
            return _NO_LOCK
        if lock_state == RECOVERY_LOCK_UNAVAILABLE:
            self._note_lock_unavailable(domain, lock_error)
            self._skip(result, domain, SKIP_LOCK_UNAVAILABLE)
            return _NO_LOCK
        _transition(_STATE_LOCK_UNAVAILABLE, False)
        return lock

    def _trial_under_lock(
        self,
        domain: str,
        lanes: list[tuple[str, str | None, str | None]],
        deadline: float | None,
        result: RecoveryTickResult,
    ) -> tuple[str | None, TrialRecord | None, list[CircuitBreakerStateData]]:
        """Decide on the state current under the lock, then run the trial.

        Returns ``(tick stop status or None, the trial record or None, the
        projecting rows read just before the trial)``.
        """
        # The first read was a cheap pre-check; the decision is made on the
        # record as it stands under the lock.
        pacing = self._read_pacing(domain)
        if not pacing.is_due(self._clock()):
            self._skip(result, domain, SKIP_NOT_DUE)
            return None, None, []

        walk = self._walk_candidates(domain, lanes, pacing, deadline)
        cursors = {**pacing.after, **walk.cursors}
        if walk.candidate is None:
            if cursors != pacing.after:
                self._write_pacing(
                    domain, _TrialPacing(pacing.streak, pacing.next_at, cursors)
                )
            self._skip(
                result,
                domain,
                SKIP_DEADLINE if walk.reached_deadline else SKIP_NO_CANDIDATE,
            )
            return None, None, []

        stop, rows_before = self._last_checks_before_trial(domain, result)
        if stop is not None or rows_before is None:
            return stop, None, []

        record = self._run_trial(domain, pacing, cursors, walk.candidate, deadline)
        return None, record, rows_before

    def _last_checks_before_trial(
        self, domain: str, result: RecoveryTickResult
    ) -> tuple[str | None, list[CircuitBreakerStateData] | None]:
        """The reads made immediately before a trial, in order.

        Returns ``(tick stop status, None)`` when the tick must stop,
        ``(None, None)`` when this domain is skipped, and ``(None, rows)`` when
        the trial may start.
        """
        if not self._integrity_allows(domain):
            return TICK_INTEGRITY_BLOCKED, None

        # The walk, the governance and integrity reads can take seconds: the
        # rows are read again immediately before the trial.
        try:
            rows_before = _rows_by_domain(self._read_rows()).get(domain, [])
        except Exception as exc:
            self._note_rows_unavailable(exc)
            return TICK_BREAKER_STATE_UNAVAILABLE, None
        if self._skip_for_rows(domain, rows_before, result):
            return None, None

        # The kill switch and governance are the last reads before the trial:
        # a switch pulled while the tick walked is honoured.
        if not self._governance_allows(domain):
            return TICK_GOVERNANCE_BLOCKED, None
        return None, rows_before

    def _run_trial(
        self,
        domain: str,
        pacing: _TrialPacing,
        cursors: dict[str, str],
        candidate: FailedOperationData,
        deadline: float | None,
    ) -> TrialRecord:
        """Pace the domain, replay the candidate as a trial, record the outcome."""
        # Written before the trial as if it will fail, so a trial whose worker
        # dies still leaves the domain paced.
        streak = pacing.streak + 1
        self._write_pacing(
            domain,
            _TrialPacing(
                streak=streak,
                next_at=self._clock() + _TRIAL_BACKOFF.calculate(streak),
                after=cursors,
            ),
        )

        trial_result, _cut = self._service._execute_replay_within(
            candidate.id,
            deadline,
            trigger=ResolutionTrigger.AUTO_REPLAY_RECOVERY,
            entry=candidate,
            trial=True,
        )
        outcome = _classify_trial(trial_result)
        if outcome == OUTCOME_SUCCEEDED:
            self._delete_pacing(domain)
        elif outcome == OUTCOME_NOT_RUN:
            # Nothing ran: the streak and next time stand as they were, and the
            # cursors stay where the walk left them, so the next tick walks
            # past the entry that did not start.
            self._write_pacing(
                domain, _TrialPacing(pacing.streak, pacing.next_at, cursors)
            )
        logger.info(
            "replay_service.recovery_trial_completed",
            healing_domain=domain,
            dlq_id=candidate.id,
            outcome=outcome,
            streak=streak,
        )
        return TrialRecord(domain=domain, dlq_id=candidate.id, outcome=outcome)

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def _release_stale(self) -> int:
        """Run the stale release (a sweep whose worker died gives entries back)."""
        try:
            return int(
                self._service.repository.release_stale_replaying(
                    older_than_minutes=stale_release_minutes()
                )
                or 0
            )
        except Exception as exc:
            logger.warning(
                "replay_service.recovery_trial_release_failed",
                error=str(exc),
            )
            return 0

    def _domains_with_work(self) -> list[str]:
        """Handler domains whose strict pending count may be above zero, rotated.

        A count that cannot be read counts as "may have entries". The list is
        rotated by the wall-clock minute so a tick that runs out of time
        reaches a different job name first next time.
        """
        from baldur.services.replay_service.handlers import registered_replay_domains

        with_work: list[str] = []
        for domain in registered_replay_domains():
            try:
                count = self._service.repository.get_cluster_pending_count_by_domain(
                    domain
                )
            except Exception:
                count = None
            if count == 0 and isinstance(count, int) and not isinstance(count, bool):
                continue
            with_work.append(domain)
        if not with_work:
            return []
        start = int(self._clock() // 60) % len(with_work)
        return with_work[start:] + with_work[:start]

    def _read_rows(self) -> list[CircuitBreakerStateData]:
        # Resolved at the first read, which comes only once there is work: an
        # idle tick leaves the breaker alone, and a breaker service that
        # cannot be built reads as breaker state unavailable.
        if self._cb_service is None:
            from baldur.services.circuit_breaker import get_circuit_breaker_service

            self._cb_service = get_circuit_breaker_service()
        rows = read_fleet_breaker_rows(self._cb_service.repository)
        _transition(_STATE_BREAKER_UNAVAILABLE, False)
        return rows

    def _note_rows_unavailable(self, exc: Exception) -> None:
        if _transition(_STATE_BREAKER_UNAVAILABLE, True):
            logger.warning(
                "replay_service.recovery_trial_state_unavailable",
                error=str(exc),
                reason=getattr(exc, "reason", None),
            )
        else:
            logger.debug(
                "replay_service.recovery_trial_state_unavailable_continuing",
                error=str(exc),
            )

    def _note_lock_unavailable(self, domain: str, error: str | None) -> None:
        if _transition(_STATE_LOCK_UNAVAILABLE, True):
            logger.warning(
                "replay_service.inflight_cache_unavailable",
                reason="lock_unavailable",
                lane="recovery_trial",
                healing_domain=domain,
                error=error,
            )
        else:
            logger.debug(
                "replay_service.inflight_cache_unavailable_continuing",
                lane="recovery_trial",
                healing_domain=domain,
            )

    def _skip_for_rows(
        self,
        domain: str,
        projecting: list[CircuitBreakerStateData],
        result: RecoveryTickResult,
    ) -> bool:
        """Skip the domain when a projecting row refuses calls or is pinned."""
        cb_service = self._cb_service
        if any(cb_service.refuses_calls(row) for row in projecting):
            self._skip(result, domain, SKIP_BREAKER_REFUSING)
            return True
        if any(_is_pinned(row) for row in projecting):
            self._skip(result, domain, SKIP_OPERATOR_HOLD)
            return True
        return False

    def _integrity_allows(self, domain: str) -> bool:
        from baldur.services.event_bus.integrity_gate import replay_integrity_verdict

        allowed = replay_integrity_verdict(domain)
        if _transition(_STATE_INTEGRITY_BLOCKED, not allowed):
            logger.warning(
                "replay_service.recovery_trial_integrity_blocked",
                healing_domain=domain,
            )
        elif allowed:
            logger.debug(
                "replay_service.recovery_trial_integrity_ok", healing_domain=domain
            )
        return allowed

    def _governance_allows(self, domain: str) -> bool:
        """The kill switch (its state known) and governance allow a trial now."""
        control = self._system_control
        if control is None:
            from baldur.services.system_control import get_system_control

            control = get_system_control()
        try:
            switch_open = control.is_state_known() and control.is_enabled()
        except Exception as exc:
            logger.debug("replay_service.recovery_trial_switch_unread", error=str(exc))
            switch_open = False
        if not switch_open:
            logger.debug(
                "replay_service.recovery_trial_governance_skipped",
                healing_domain=domain,
                reason="kill_switch",
            )
            return False
        governance = self._service._get_governance().check_all_governance(
            check_kill_switch=True,
            check_emergency=True,
            emergency_min_level=2,
            check_error_budget=True,
            operation_name="recovery_trial",
            service_name="ReplayService",
            domain=domain,
            audit_on_block=False,
        )
        if not governance.allowed:
            logger.debug(
                "replay_service.recovery_trial_governance_skipped",
                healing_domain=domain,
                reason=governance.block_message,
            )
            return False
        return True

    def _dispatch_after_success(
        self, domain: str, rows_before: list[CircuitBreakerStateData]
    ) -> str:
        """Dispatch the sweep for the rest of the backlog after a successful trial.

        Not when the trial itself closed a projecting breaker — its CLOSED
        event dispatches the (escalating) sweep — not while a projecting row
        refuses calls, and not while one carries an operator's pin (placed
        while the trial ran: the pins were read before it). A HALF_OPEN row
        does not hold the dispatch: every replay of the sweep goes through the
        job's own breaker. Rows that cannot be read again do not hold it
        either: the chain's first pass reads them and applies the same rules.
        """
        try:
            rows_after = _rows_by_domain(self._read_rows()).get(domain, [])
        except Exception as exc:
            self._note_rows_unavailable(exc)
            rows_after = None
        if rows_after is not None:
            from baldur.services.circuit_breaker import CircuitState

            state_before = {row.service_name: row.state for row in rows_before}
            closed_by_trial = any(
                row.state == CircuitState.CLOSED
                and row.service_name in state_before
                and state_before[row.service_name] != CircuitState.CLOSED
                for row in rows_after
            )
            if closed_by_trial:
                return _DISPATCH_LEFT_TO_CLOSED_EVENT
            cb_service = self._cb_service
            if any(cb_service.refuses_calls(row) for row in rows_after):
                return _DISPATCH_HELD_BY_REFUSAL
            if any(_is_pinned(row) for row in rows_after):
                return _DISPATCH_HELD_BY_PIN
        return dispatch_recovery_sweep(
            domain,
            trigger=ResolutionTrigger.AUTO_REPLAY_RECOVERY,
            escalate_failures=False,
            service=self._service,
        )

    # ------------------------------------------------------------------
    # Candidate walk
    # ------------------------------------------------------------------

    def _walk_candidates(
        self,
        domain: str,
        lanes: list[tuple[str, str | None, str | None]],
        pacing: _TrialPacing,
        deadline: float | None,
    ) -> _CandidateWalk:
        """Find the next entry the gates allow, one cursor per lane.

        Each lane is read a page at a time from its own cursor; the pages are
        merged by ``(created_at, id)`` and the first entry whose truncate gate
        allows it and whose handler's ``can_replay`` agrees is the candidate.
        A lane is walked at most once around per tick: from its cursor to its
        end, then — when it started past its beginning — from its start up to
        where it began. An empty page that stopped on its scan bound is
        continued from its ``next_cursor``, never taken as the end. A lane's
        cursor moves to the last of its entries examined (the candidate itself
        in its lane); a lane not examined keeps its cursor.
        """
        from baldur.services.replay_service.handlers import get_replay_handler

        handler = get_replay_handler(domain)
        max_replays = self._service.config["max_replay_attempts"]
        walk = _CandidateWalk()
        lane_walks = [_LaneWalk.begin(lane, pacing.after) for lane in lanes]

        while True:
            active = [lane_walk for lane_walk in lane_walks if not lane_walk.done]
            if not active:
                return walk
            if self._past(deadline):
                walk.reached_deadline = True
                return walk

            merged: list[tuple[tuple[float, str], _LaneWalk, FailedOperationData]] = []
            for lane_walk in active:
                merged.extend(
                    lane_walk.read_page(self._service.repository, max_replays, walk)
                )
            merged.sort(key=lambda item: item[0])
            if self._examine(merged, handler, walk, deadline):
                return walk
            for lane_walk in active:
                lane_walk.finish_page()

    def _examine(
        self,
        merged: list[tuple[tuple[float, str], _LaneWalk, FailedOperationData]],
        handler: Any,
        walk: _CandidateWalk,
        deadline: float | None,
    ) -> bool:
        """Examine merged entries in order; True when the walk ends here."""
        from baldur.services.replay_service.handlers import _truncate_gate
        from baldur.services.replay_service.service import _handler_refusal

        for _position, lane_walk, entry in merged:
            lane_walk.move_to(encode_replay_cursor(entry.created_at, entry.id), walk)
            allowed, _ = _truncate_gate(entry)
            if allowed and _handler_refusal(handler, entry) is None:
                walk.candidate = entry
                return True
            if self._past(deadline):
                walk.reached_deadline = True
                return True
        return False

    # ------------------------------------------------------------------
    # Pacing record
    # ------------------------------------------------------------------

    def _read_pacing(self, domain: str) -> _TrialPacing:
        """Read the record; a missing record or a read fault is due now."""
        cache = self._service.cache
        if cache is None:
            return _TrialPacing()
        try:
            return _TrialPacing.from_value(cache.get(_PACING_KEY_PREFIX + domain))
        except Exception as exc:
            logger.debug(
                "replay_service.recovery_trial_pacing_read_failed",
                healing_domain=domain,
                error=str(exc),
            )
            return _TrialPacing()

    def _write_pacing(self, domain: str, pacing: _TrialPacing) -> None:
        cache = self._service.cache
        if cache is None:
            return
        try:
            cache.set(
                _PACING_KEY_PREFIX + domain,
                pacing.to_value(),
                ttl=timedelta(seconds=2 * RECOVERY_TRIAL_MAX_SECONDS),
            )
        except Exception as exc:
            logger.debug(
                "replay_service.recovery_trial_pacing_write_failed",
                healing_domain=domain,
                error=str(exc),
            )

    def _delete_pacing(self, domain: str) -> None:
        cache = self._service.cache
        if cache is None:
            return
        try:
            cache.delete(_PACING_KEY_PREFIX + domain)
        except Exception as exc:
            logger.debug(
                "replay_service.recovery_trial_pacing_write_failed",
                healing_domain=domain,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _past(self, deadline: float | None) -> bool:
        return deadline is not None and self._monotonic() >= deadline

    @staticmethod
    def _skip(result: RecoveryTickResult, domain: str, reason: str) -> None:
        result.skipped[domain] = reason
        logger.debug(
            "replay_service.recovery_trial_skipped",
            healing_domain=domain,
            reason=reason,
        )

    def _record_in_daily_report(self, result: RecoveryTickResult) -> None:
        """One auto-replay result per tick whose trials ran a job (fail-open).

        A trial whose job never began — not run, or refused by its own
        breaker — reached no dependency, so it is neither recovered nor failed:
        the sweep leaves a replay its breaker refused out of its count too.
        """
        from baldur.services.replay_service.models import BatchReplayResult

        never_began = (OUTCOME_NOT_RUN, OUTCOME_BREAKER_REFUSED)
        ran = [trial for trial in result.trials if trial.outcome not in never_began]
        if not ran:
            return
        succeeded = sum(1 for trial in ran if trial.outcome == OUTCOME_SUCCEEDED)
        self._service._record_sweep_in_daily_report(
            _DAILY_REPORT_SERVICE_NAME,
            BatchReplayResult(
                total=len(ran),
                success_count=succeeded,
                failed_count=len(ran) - succeeded,
            ),
        )


def recovery_trials_enabled(service: ReplayService | None = None) -> bool:
    """Both switches the recovery tick needs: on-recovery replay and the trial."""
    if service is None:
        from baldur.services.replay_service.service import get_replay_service

        service = get_replay_service()
    return _on_recovery_enabled(service) and _recovery_trial_enabled()


def dispatch_recovery_tick() -> str:
    """Queue one recovery tick if a worker could act on it (leader scheduler).

    The leader scheduler's path to the tick, run off the scheduler thread: it
    may wait on the DLQ store, the worker probe and the broker. Queues nothing
    when Celery is absent, either switch is off, the DLQ holds no PENDING or
    REPLAYING entry (the tick's trial candidates and its stale release), or no
    worker consumes ``dlq_processing`` — ``expires`` does not bound a queue on
    a Redis broker, whose messages carry no TTL.

    Returns:
        ``dispatched``, or why not: ``celery_missing``, ``disabled``,
        ``nothing_parked``, ``count_unavailable``, ``worker_missing``,
        ``publish_failed``.
    """
    try:
        from baldur.adapters.celery.tasks import recover_parked_jobs
    except ImportError:
        if _transition(_STATE_CELERY_MISSING_LOGGED, True):
            logger.debug("replay_service.recovery_tick_dispatch_celery_missing")
        return "celery_missing"

    if not recovery_trials_enabled():
        return "disabled"

    try:
        from baldur.services.dlq_capture.service import resolve_dlq_backing

        repository = resolve_dlq_backing().repository
        has_work = repository.count(status="pending") > 0 or (
            repository.count(status="replaying") > 0
        )
    except Exception as exc:
        logger.debug(
            "replay_service.recovery_tick_dispatch_count_unavailable",
            error=str(exc),
        )
        return "count_unavailable"
    if not has_work:
        return "nothing_parked"

    from baldur.services.replay_service.arming import _cached_worker_state

    if _cached_worker_state() == "missing":
        return "worker_missing"

    try:
        recover_parked_jobs.apply_async(expires=2 * RECOVERY_TICK_SECONDS, retry=False)
    except Exception as exc:
        if _transition(_STATE_PUBLISH_FAILED_LOGGED, True):
            logger.warning(
                "replay_service.recovery_tick_dispatch_failed",
                error=str(exc),
            )
        else:
            logger.debug(
                "replay_service.recovery_tick_dispatch_failed_continuing",
                error=str(exc),
            )
        return "publish_failed"
    return "dispatched"


def run_recovery_trials(*, deadline: float | None) -> RecoveryTickResult:
    """Run one recovery tick with the process singletons.

    Args:
        deadline: ``time.monotonic()`` value past which no further trial
            starts; each trial is itself bounded by it.
    """
    return RecoveryTrialRunner().run(deadline=deadline)


def dispatch_recovery_sweep(
    service_name: str,
    *,
    trigger: ResolutionTrigger | str,
    escalate_failures: bool,
    operator_requested: bool = False,
    service: ReplayService | None = None,
) -> str:
    """Queue the recovery sweep for a name — the one dispatch path.

    Used by the breaker's CLOSED event, a successful recovery trial, and an
    operator's close-with-replay on a breaker that was already closed. Reads
    the on-recovery switch and the pass budgets (RuntimeConfig → settings),
    queues the chain's first pass, and records the outcome in the arming
    ledger.

    Args:
        service_name: The breaker name (or stored domain) that recovered.
        trigger: Provenance the sweep stamps on what it replays.
        escalate_failures: Escalate a replay whose job ran and failed to
            review (the CLOSED-transition lane), or leave it for its next
            attempt (a trial's weaker evidence).
        operator_requested: The operator asked for this sweep: their manual
            pin does not stop its chain, and a held lock re-queues it.
        service: The replay service to read configuration through.

    Returns:
        ``dispatched``, ``skipped_disabled``, ``celery_missing`` or ``error``.
    """
    from baldur.settings.replay_automation import get_replay_automation_settings

    if service is None:
        # Configuration and the parked count are read through the replay
        # service; the dispatch must still dispatch, or warn, without one.
        try:
            from baldur.services.replay_service.service import get_replay_service

            service = get_replay_service()
        except Exception as exc:
            logger.debug(
                "replay_service.recovery_dispatch_service_unavailable",
                error=str(exc),
            )

    settings = get_replay_automation_settings()
    config = _replay_automation_config(service)
    if not config.get("on_recovery_enabled", settings.on_recovery_enabled):
        logger.info(
            "event_handler.circuit_breaker_closed_track",
            service_name=service_name,
        )
        _record_dispatch_outcome("skipped_disabled", service_name=service_name)
        return "skipped_disabled"

    max_items = config.get("on_recovery_max_items", settings.on_recovery_max_items)
    # Resolved once, here: continuations carry the bound they were dispatched
    # with, so a chain runs to the budget it started with and a console edit
    # takes effect at the next recovery rather than mid-drain.
    max_continuations = config.get(
        "on_recovery_max_continuations", settings.on_recovery_max_continuations
    )
    trigger_value = (
        trigger.value if isinstance(trigger, ResolutionTrigger) else str(trigger)
    )

    try:
        from baldur.adapters.celery.tasks import (
            conditional_replay_on_circuit_close,
        )

        conditional_replay_on_circuit_close.delay(
            service_name=service_name,
            max_items=max_items,
            max_continuations=max_continuations,
            trigger=trigger_value,
            escalate_failures=escalate_failures,
            operator_requested=operator_requested,
        )
        logger.info(
            "event_handler.circuit_breaker_closed_triggered",
            service_name=service_name,
            max_items=max_items,
            max_continuations=max_continuations,
            trigger=trigger_value,
        )
        _record_dispatch_outcome("dispatched", service_name=service_name)
        return "dispatched"
    except ImportError:
        # Armed (enabled) but the Celery task is unavailable — the guarantee
        # is undeliverable for whatever this recovery left parked. WARNING
        # with remediation rather than a silent DEBUG skip, unless nothing is
        # parked under the name: then no worker had anything to do.
        if _nothing_parked(service, service_name):
            logger.debug(
                "event_handler.replay_dispatch_skipped",
                service_name=service_name,
                reason="celery_missing",
                nothing_parked=True,
            )
        else:
            logger.warning(
                "event_handler.replay_dispatch_blocked",
                service_name=service_name,
                reason="celery_missing",
                queue="dlq_processing",
                worker_command="celery -A <app> worker -Q dlq_processing",
                remediation=(
                    "Run a Celery worker consuming the 'dlq_processing' queue, or "
                    "drain the DLQ manually via the console Replay action."
                ),
            )
        _record_dispatch_outcome("celery_missing", service_name=service_name)
        return "celery_missing"
    except Exception as e:
        logger.exception(
            "event_handler.trigger_track_replay_failed",
            service_name=service_name,
            error=e,
        )
        _record_dispatch_outcome("error", service_name=service_name, error=str(e))
        return "error"


def _nothing_parked(service: ReplayService | None, service_name: str) -> bool:
    """Does the store answer that nothing is parked under this name? (fail-loud)

    Lanes are not consulted: with no worker nothing runs a lane, and this
    process's replay handler registry is not a worker's. Any failure answers
    False, which keeps the warning.
    """
    if service is None:
        return False
    try:
        return service.parked_count_for_recovery(service_name) == 0
    except Exception:
        return False


def _record_dispatch_outcome(
    outcome: str, *, service_name: str, error: str | None = None
) -> None:
    """Hand one dispatch evaluation to the arming ledger (fail-open)."""
    try:
        from baldur.services.replay_service.arming import record_dispatch_outcome

        record_dispatch_outcome(outcome, service_name=service_name, error=error)
    except Exception:
        pass
