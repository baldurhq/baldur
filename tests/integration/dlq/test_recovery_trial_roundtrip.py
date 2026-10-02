"""Recovery trials end to end: parked jobs come back with no breaker evidence,
no traffic and no beat (807).

Wired for real:

    a ``@protected(..., replay=True)`` job and its breaker → ``DLQCaptureService``
    → the DLQ (in-memory and SQLite) → ``RecoveryTrialRunner`` (the stale
    release, the breaker rows from the breaker's own store, the pacing record
    and the domain lock in the cache, the candidate walk) → ``ReplayService`` →
    ``FunctionReplayHandler`` → the job → on success the one dispatch path →
    the shipped chain task ``conditional_replay_on_circuit_close``, run eagerly
    pass by pass.

Stood in for: the job's dependency (a flag the job reads; while it is down the
job ends the way a wrap with every endpoint failed ends, with
``LLMUnavailableError``), the broker hop (each dispatch is captured, then run as
the next pass), the kill switch (a switch that reads on), governance (injected
as allowed), and the clocks — the tick's wall clock is injected; the stale
release reads the repository's own clock, run under ``freeze_time``.

Test Categories:
    A. A short outage the job's breaker never saw open, with no traffic:
        - the failed trials cost the parked jobs nothing; once the dependency
          answers, the next due trial replays one and its sweep the rest
    B. A breaker another process left OPEN past its timeout, with no traffic:
        - the trial is the breaker's half-open probe; when it closes the
          breaker the CLOSED event's sweep drains the rest, otherwise the
          trial's own sweep does
    C. A sweep whose worker died, with no beat:
        - the tick's stale release returns the entries it held; one taken on
          its last allowed attempt goes to review; the rest are replayed

Note: in-memory and SQLite stores — no Docker.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from baldur import protect_facade
from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.celery_tasks.dlq_tasks import conditional_replay_on_circuit_close
from baldur.core.exceptions import CircuitBreakerError, LLMUnavailableError
from baldur.interfaces.governance import GovernanceChecker
from baldur.interfaces.repositories import STALE_RELEASE_AT_CAP_NOTE
from baldur.models.dlq import OPEN_CIRCUIT_FAILURE_TYPE
from baldur.models.governance import GovernanceCheckResult
from baldur.protect_facade import protected
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.dlq_capture.service import DLQCaptureService
from baldur.services.event_bus import EventType
from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.handlers import _replay_handlers
from baldur.services.replay_service.recovery import (
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
    RECOVERY_TICK_SECONDS,
    RecoveryTickResult,
    RecoveryTrialRunner,
    reset_recovery_trial_state,
)
from baldur.services.retry_handler.sinks import retry_exhausted_failure_type
from baldur.settings.dlq_outbox import reset_dlq_outbox_settings
from baldur.settings.protect import reset_protect_settings
from baldur.utils.time import utc_now
from tests.factories.time_helpers import freeze_time, mock_sleep

_SYNC_RETRY_SLEEP = "baldur.services.retry_handler.policy._DEFAULT_SLEEPER"
_RESOLVE_BACKING = "baldur.services.dlq_capture.resolve_dlq_backing"
_TASK = "baldur.adapters.celery.tasks.conditional_replay_on_circuit_close"

NO_ENDPOINT = retry_exhausted_failure_type(LLMUnavailableError.__name__)
T0 = 1_900_000_000.0


def _job_breaker_config(*, failure_threshold: int, success_threshold: int = 1):
    return CircuitBreakerConfig(
        enabled=True,
        failure_threshold=failure_threshold,
        minimum_calls=1,
        failure_rate_threshold=0,
        recovery_timeout=60,
        success_threshold=success_threshold,
    )


# =============================================================================
# Fixtures and the world one recovery runs in
# =============================================================================


@pytest.fixture(autouse=True)
def sandbox(monkeypatch) -> Iterator[None]:
    """Fresh registries and caches; synchronous DLQ stores; no retry waits."""
    before = dict(_replay_handlers)
    monkeypatch.setenv("BALDUR_DLQ_OUTBOX_ENABLED", "false")
    reset_dlq_outbox_settings()
    reset_protect_settings()
    reset_recovery_trial_state()
    with patch(_SYNC_RETRY_SLEEP, lambda _seconds: None), mock_sleep():
        yield
    _replay_handlers.clear()
    _replay_handlers.update(before)
    reset_protect_settings()
    reset_dlq_outbox_settings()
    reset_recovery_trial_state()


@pytest.fixture(params=["memory", "sqlite"], ids=["memory", "sqlite"])
def repo(request):
    """The DLQ the jobs are parked in, over each store the tick walks."""
    if request.param == "memory":
        return InMemoryFailedOperationRepository()
    return request.getfixturevalue("sqlite_repo")


@dataclass
class _World:
    """One recovery's components, sharing one DLQ, one cache and one breaker store."""

    repo: Any
    service: ReplayService
    breakers: CircuitBreakerService
    clock: SimpleNamespace = field(default_factory=lambda: SimpleNamespace(now=T0))
    dependency_up: bool = False
    done: list[str] = field(default_factory=list)
    queued: list[dict] = field(default_factory=list)
    chain_outcomes: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.dispatch = MagicMock(spec=conditional_replay_on_circuit_close)
        self.dispatch.delay.side_effect = lambda **kwargs: self.queued.append(kwargs)
        self.dispatch.apply_async.side_effect = lambda kwargs, **_options: (
            self.queued.append(kwargs)
        )

    def arm(self, name: str, *, breaker: CircuitBreakerConfig | None = None):
        """Arm a replay=True job whose dependency is ``dependency_up``."""
        world = self

        @protected(name, replay=True, circuit_breaker=breaker is not None, timeout=None)
        def summarize(doc_id: str) -> str:
            if not world.dependency_up:
                raise LLMUnavailableError("every endpoint failed")
            world.done.append(doc_id)
            return doc_id

        if breaker is not None:
            self.breakers._pinned_config = breaker
            protect_facade._cb_policy_cache[name] = CircuitBreakerPolicy(
                service_name=name, cb_service=self.breakers, hooks=[]
            )
        return summarize

    def park(self, job, doc_id: str) -> None:
        """Call the job while its dependency is down: it is parked for real."""
        with patch(
            _RESOLVE_BACKING, return_value=DLQCaptureService(repository=self.repo)
        ):
            with pytest.raises((LLMUnavailableError, CircuitBreakerError)):
                job(doc_id)

    def tick(self) -> RecoveryTickResult:
        switch = SimpleNamespace(is_state_known=lambda: True, is_enabled=lambda: True)
        runner = RecoveryTrialRunner(
            replay_service=self.service,
            circuit_breaker_service=self.breakers,
            system_control=switch,
            clock=lambda: self.clock.now,
        )
        with patch(_TASK, self.dispatch):
            return runner.run(deadline=None)

    def run_chain(self, **first_pass: Any) -> None:
        """Run every queued pass (and ``first_pass``, if given) as the broker would."""
        if first_pass:
            self.queued.append(first_pass)
        with (
            patch("baldur.services.get_replay_service", return_value=self.service),
            patch(
                "baldur.services.circuit_breaker.get_circuit_breaker_service",
                return_value=self.breakers,
            ),
            patch(_TASK, self.dispatch),
        ):
            while self.queued:
                kwargs = self.queued.pop(0)
                outcome = conditional_replay_on_circuit_close.apply(kwargs=kwargs).get()
                self.chain_outcomes.append(outcome)

    def entries(self, name: str) -> dict[str, Any]:
        return {
            entry.request_data["doc_id"]: entry for entry in self.repo.find(domain=name)
        }

    def closed_events(self) -> list[dict]:
        return [
            call.kwargs["data"]
            for call in self.breakers._event_bus.emit.call_args_list
            if call.args and call.args[0] == EventType.CIRCUIT_BREAKER_CLOSED
        ]


@pytest.fixture
def world(repo) -> _World:
    service = ReplayService(
        repository=repo,
        cache=InMemoryCacheAdapter(key_prefix=f"t807i:{uuid.uuid4().hex}:"),
    )
    service._event_bus = MagicMock(spec=BaldurEventBus)
    service._governance = MagicMock(spec=GovernanceChecker)
    service._governance.check_all_governance.return_value = GovernanceCheckResult(
        allowed=True
    )
    service._governance_resolved = True
    breakers = CircuitBreakerService(
        config=_job_breaker_config(failure_threshold=5),
        repository=InMemoryCircuitBreakerStateRepository(),
    )
    breakers._event_bus = MagicMock(spec=BaldurEventBus)
    return _World(repo=repo, service=service, breakers=breakers)


def _job_name() -> str:
    return f"job.summarize_{uuid.uuid4().hex[:10]}"


# =============================================================================
# A. A short outage the breaker never saw open, with no traffic
# =============================================================================


class TestShortOutageRecoveryTrialRoundTrip:
    """The trial is the only call that can show the dependency answers again."""

    @pytest.mark.parametrize(
        "with_breaker", [True, False], ids=["breaker", "no_breaker"]
    )
    def test_short_outage_no_traffic_parked_jobs_come_back_on_their_own(
        self, world, with_breaker
    ):
        """
        Purpose:
            Two jobs fail during an outage too short to open the job's breaker
            (or with no breaker at all); nothing calls the job afterwards.
        Expected:
            - the failed trial leaves both parked, each at zero replay attempts
            - once the dependency answers, the next due trial replays a job, and
              the sweep it dispatches replays the other: both resolved, none lost
            - the breaker (when there is one) never left CLOSED
        """
        # Given — two jobs parked by the outage
        name = _job_name()
        breaker = _job_breaker_config(failure_threshold=5) if with_breaker else None
        job = world.arm(name, breaker=breaker)
        world.park(job, "doc-1")
        world.park(job, "doc-2")

        # When — a tick during the outage, then one after the dependency answers
        during = world.tick()
        attempts_after_outage = {
            doc: entry.retry_count for doc, entry in world.entries(name).items()
        }
        world.dependency_up = True
        world.clock.now += RECOVERY_TICK_SECONDS
        after = world.tick()
        world.run_chain()

        # Then
        assert [t.outcome for t in during.trials] == [OUTCOME_FAILED]
        assert attempts_after_outage == {"doc-1": 0, "doc-2": 0}
        assert [t.outcome for t in after.trials] == [OUTCOME_SUCCEEDED]
        assert after.trials[0].dispatch == "dispatched"
        assert sorted(world.done) == ["doc-1", "doc-2"]
        assert {entry.status for entry in world.entries(name).values()} == {"resolved"}
        assert world.chain_outcomes[0]["success_count"] == 1
        if with_breaker:
            assert world.breakers.get_state(name) == "closed"
            assert world.closed_events() == []


# =============================================================================
# B. A breaker another process left OPEN past its timeout
# =============================================================================


class TestOpenBreakerRecoveryTrialRoundTrip:
    """A breaker left OPEN with no traffic is probed by the trial itself."""

    @pytest.mark.parametrize(
        ("success_threshold", "drained_by"),
        [(1, "closed_event"), (2, "trial_sweep")],
        ids=["trial_closes_the_breaker", "breaker_still_half_open"],
    )
    def test_open_past_timeout_breaker_is_probed_by_the_trial_and_drained(
        self, world, success_threshold, drained_by
    ):
        """
        Purpose:
            A breaker opened in another process stays OPEN past its recovery
            timeout because nothing calls the job; the trial's call is its probe.
        Expected:
            - the trial runs (the row admits a half-open probe) and succeeds
            - a probe that closes the breaker leaves the rest to the CLOSED
              event's sweep (the tick dispatches nothing); a breaker still
              HALF_OPEN after it gets the trial's own sweep, which probes
              through the job's breaker
            - every parked job is resolved, none lost
        """
        # Given — the breaker opens and rejects two jobs, parked as open-circuit
        name = _job_name()
        config = _job_breaker_config(
            failure_threshold=1, success_threshold=success_threshold
        )
        job = world.arm(name, breaker=config)
        world.park(job, "doc-1")
        world.park(job, "doc-2")
        world.park(job, "doc-3")
        assert world.breakers.get_state(name) == "open"
        assert {e.failure_type for e in world.entries(name).values()} >= {
            OPEN_CIRCUIT_FAILURE_TYPE
        }
        # ...and nothing calls the job until well past the recovery timeout.
        store = world.breakers.repository
        row = store.get_or_create(name)
        store._storage[name] = replace(
            row, opened_at=utc_now() - timedelta(seconds=config.recovery_timeout * 10)
        )

        # When — the dependency answers; one tick, then the sweep it led to
        world.dependency_up = True
        result = world.tick()
        if drained_by == "closed_event":
            world.run_chain(
                service_name=name,
                max_items=50,
                max_continuations=5,
                trigger="auto_replay_circuit_close",
                escalate_failures=True,
            )
        else:
            world.run_chain()

        # Then
        assert [t.outcome for t in result.trials] == [OUTCOME_SUCCEEDED]
        if drained_by == "closed_event":
            assert result.trials[0].dispatch == "closed_by_trial"
            assert [e["service_name"] for e in world.closed_events()] == [name]
        else:
            assert result.trials[0].dispatch == "dispatched"
        assert sorted(world.done) == ["doc-1", "doc-2", "doc-3"]
        assert {entry.status for entry in world.entries(name).values()} == {"resolved"}


# =============================================================================
# C. A sweep whose worker died, with no beat
# =============================================================================


class TestKilledSweepRecoveryTrialRoundTrip:
    """The tick's own stale release hands back what a dead sweep held."""

    def test_killed_sweep_is_recovered_by_the_tick_and_its_last_attempt_reviewed(
        self, world
    ):
        """
        Purpose:
            A sweep took three parked jobs — one on its last allowed attempt —
            and its worker died holding the domain's recovery lock. No beat runs.
        Expected:
            - past the release window the tick returns the two below their cap
              to PENDING and sends the one at its cap to review, with the note
            - the lock's TTL has long passed: the tick trials a job, and its
              sweep replays the other — both resolved
        """
        # Given — at 10:00 the sweep takes three jobs and dies
        name = _job_name()
        job = world.arm(name)
        with freeze_time("2026-10-02 10:00:00"):
            world.park(job, "doc-1")
            world.park(job, "doc-2")
            world.park(job, "doc-3")
            entries = world.entries(name)
            last_attempt = entries["doc-3"].id
            for doc_id, entry in entries.items():
                if doc_id == "doc-3":
                    # One earlier replay already used an attempt.
                    world.repo.try_acquire_for_replay(entry.id, 2)
                    world.repo.complete_replay(entry.id, success=False, note="down")
                assert world.repo.try_acquire_for_replay(entry.id, 2) is not None
            lock, state, _ = world.service.try_acquire_recovery_lock(name)
            assert state == "acquired"

        # When — the first tick after the window, with the dependency back
        world.dependency_up = True
        with freeze_time("2026-10-02 10:31:00"):
            result = world.tick()
            world.run_chain()

        # Then
        assert result.released == 3
        assert [t.outcome for t in result.trials] == [OUTCOME_SUCCEEDED]
        assert sorted(world.done) == ["doc-1", "doc-2"]
        statuses = {doc: e.status for doc, e in world.entries(name).items()}
        assert statuses == {
            "doc-1": "resolved",
            "doc-2": "resolved",
            "doc-3": "requires_review",
        }
        assert world.repo.get_by_id(last_attempt).resolution_note == (
            STALE_RELEASE_AT_CAP_NOTE
        )
