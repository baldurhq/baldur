"""LLM outage end to end: jobs a provider outage stopped come back when the job's breaker closes.

Wired for real, from the SDK down to the DLQ and back:

    the ``openai`` SDK → a local OpenAI-compatible server → ``baldur.llm.wrap``
    (the endpoint's own retry, breaker and shared wait) → a
    ``@protected(..., replay=True)`` job (its breaker, its DLQ sink) →
    ``DLQCaptureService`` → the DLQ repository → the shipped recovery task
    ``conditional_replay_on_circuit_close`` (eager) → ``ReplayService`` lanes
    → ``FunctionReplayHandler`` → ``protect()`` → the wrap → the server again.

Stood in for: the broker hop (the task runs eagerly and its continuation is
captured, then run), the governance check (injected as allowed), and the
breaker's wall clock for its recovery wait (the elapsed-time read is moved past
``recovery_timeout`` instead of sleeping through it). The CLOSED event's own
dispatch to the task is covered by the event-bus routing tests; here the event
is asserted and the task is run as that dispatch would run it.

Test Categories:
    A. Outage → park → close → replay:
        - every job the outage stopped is parked with its arguments — after
          every endpoint failed, or by the job's open breaker — and none under
          the endpoint's name
        - the breaker closes on a live probe, and one recovery run replays every
          parked job with the same arguments: none lost
    B. The pass deadline against a provider that hangs:
        - the real SDK is handed the time left as its request timeout, the pass
          ends ``capped`` and the cut job stays pending, unescalated
        - the continuation is queued even though the cut replay was the pass's
          first (on a store whose empty lanes report no cursor movement too)
        - the continuation replays the cut job first, then the rest
    C. A short outage, and a breaker left half-open with no traffic:
        - a breaker that never opened gives no CLOSED event; the recovery
          trial replays a parked job and its sweep the rest
        - a half-open breaker nothing probes is closed by the trial, whose
          CLOSED event's sweep drains the rest; the failed trial before it
          cost the parked jobs nothing

Note: in-memory and SQLite stores, a local HTTP server — no Docker.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from baldur import protect_facade
from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage
from baldur.adapters.sql.base import SchemaVersionManager
from baldur.adapters.sql.failed_operation import SQLFailedOperationRepository
from baldur.celery_tasks.dlq_tasks import (
    _CIRCUIT_CLOSE_DEADLINE_MARGIN_SECONDS,
    conditional_replay_on_circuit_close,
)
from baldur.core.exceptions import CircuitBreakerError, LLMUnavailableError
from baldur.interfaces.governance import GovernanceChecker
from baldur.llm import Endpoint, wrap
from baldur.models.dlq import OPEN_CIRCUIT_FAILURE_TYPE
from baldur.models.governance import GovernanceCheckResult
from baldur.protect_facade import protected
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.dlq_capture.service import DLQCaptureService
from baldur.services.event_bus import EventType
from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.rate_limit_coordinator import RateLimitCoordinator
from baldur.services.rate_limit_coordinator.models import RateLimitCoordinatorConfig
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.handlers import _replay_handlers
from baldur.services.retry_handler.sinks import retry_exhausted_failure_type
from baldur.settings.dlq_outbox import reset_dlq_outbox_settings
from baldur.settings.protect import reset_protect_settings
from baldur.settings.sql import reset_sql_settings
from baldur.utils.time import utc_now
from tests.factories.llm_doubles import (
    HANG,
    OpenAICompatibleStub,
    completion_body,
    error_body,
)
from tests.factories.time_helpers import mock_sleep

openai = pytest.importorskip("openai")

_SYNC_RETRY_SLEEP = "baldur.services.retry_handler.policy._DEFAULT_SLEEPER"
_RESOLVE_BACKING = "baldur.services.dlq_capture.resolve_dlq_backing"
_BREAKER_CLOCK = "baldur.services.circuit_breaker.service.utc_now"

NO_ENDPOINT = retry_exhausted_failure_type(LLMUnavailableError.__name__)

# The job's breaker: open after two failed jobs, close on the first probe.
_JOB_BREAKER = CircuitBreakerConfig(
    enabled=True,
    failure_threshold=2,
    minimum_calls=1,
    failure_rate_threshold=0,
    recovery_timeout=60,
    success_threshold=1,
)


# =============================================================================
# Fixtures and helpers
# =============================================================================


@pytest.fixture(autouse=True)
def sandbox(monkeypatch) -> Iterator[None]:
    """Fresh protect caches and handler registry; synchronous DLQ stores; no waits slept."""
    before = dict(_replay_handlers)
    monkeypatch.setenv("BALDUR_DLQ_OUTBOX_ENABLED", "false")
    reset_dlq_outbox_settings()
    reset_protect_settings()
    coordinator = RateLimitCoordinator(
        storage=InMemoryRateLimitStorage(),
        config=RateLimitCoordinatorConfig(
            jitter_percent=0.0, debounce_window_seconds=0.0, default_retry_after=0.5
        ),
    )
    with (
        patch(_SYNC_RETRY_SLEEP, lambda _seconds: None),
        patch.object(RateLimitCoordinator, "_instance", coordinator),
        mock_sleep(),
    ):
        yield
        RateLimitCoordinator.reset_instance()
    _replay_handlers.clear()
    _replay_handlers.update(before)
    reset_protect_settings()
    reset_dlq_outbox_settings()


@pytest.fixture
def provider() -> Iterator[OpenAICompatibleStub]:
    with OpenAICompatibleStub() as stub:
        yield stub


@pytest.fixture
def sqlite_repo(monkeypatch) -> Iterator[SQLFailedOperationRepository]:
    monkeypatch.setenv("BALDUR_SQL_DSN", "sqlite:///:memory:")
    reset_sql_settings()
    SchemaVersionManager._reset_applied_cache()
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        yield SQLFailedOperationRepository(lambda: conn)
    finally:
        conn.close()
        reset_sql_settings()
        SchemaVersionManager._reset_applied_cache()


@pytest.fixture(params=["memory", "sqlite"], ids=["memory", "sqlite"])
def repo(request):
    """The DLQ the jobs are parked in, over each store the sweep walks."""
    if request.param == "memory":
        return InMemoryFailedOperationRepository()
    return request.getfixturevalue("sqlite_repo")


def _ok(content: str) -> Any:
    return 200, completion_body(f"summary of {content}"), {}


def _down(_content: str) -> Any:
    return 503, error_body("The server is overloaded.", "server_error"), {}


def _hang(_content: str) -> Any:
    return HANG


def _job_breaker(name: str) -> CircuitBreakerService:
    """Install the job's breaker in protect()'s per-name cache; its events are captured."""
    service = CircuitBreakerService(
        config=_JOB_BREAKER, repository=InMemoryCircuitBreakerStateRepository()
    )
    service._event_bus = MagicMock(spec=BaldurEventBus)
    protect_facade._cb_policy_cache[name] = CircuitBreakerPolicy(
        service_name=name, cb_service=service, hooks=[]
    )
    return service


def _replay_service(repo) -> ReplayService:
    service = ReplayService(repository=repo, cache=InMemoryCacheAdapter())
    service._event_bus = MagicMock(spec=BaldurEventBus)
    service._governance = MagicMock(spec=GovernanceChecker)
    service._governance.check_all_governance.return_value = GovernanceCheckResult(
        allowed=True
    )
    service._governance_resolved = True
    return service


class _Job:
    """A ``replay=True`` summarize job calling the provider through ``baldur.llm.wrap``."""

    def __init__(self, provider: OpenAICompatibleStub) -> None:
        self.name = f"job.summarize_{uuid.uuid4().hex[:10]}"
        self.endpoint = f"llm.provider_{uuid.uuid4().hex[:10]}"
        self.done: list[str] = []
        client = openai.OpenAI(api_key="test-key", base_url=provider.base_url)
        llm = wrap(Endpoint(client, name=self.endpoint))
        done = self.done

        @protected(self.name, replay=True, timeout=None)
        def summarize(doc_id: str) -> str:
            response = llm.chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": doc_id}]
            )
            done.append(doc_id)
            return str(response.choices[0].message.content)

        self.summarize = summarize


def _run_recovery(
    service: ReplayService,
    breakers: CircuitBreakerService,
    name: str,
    *,
    between_passes: Callable[[], None] | None = None,
    soft_time_limit: float | None = None,
    first_pass: dict | None = None,
) -> list[tuple[dict, float]]:
    """Run the shipped recovery task as the CLOSED event's dispatch would, pass by pass.

    Each queued continuation is captured instead of sent to a broker, then run
    as the next pass with the kwargs it was queued with. ``first_pass`` is the
    kwargs another dispatch (a recovery trial's) queued the chain with.
    """
    queued: list[dict] = []
    dispatch = MagicMock(spec=conditional_replay_on_circuit_close)
    dispatch.delay.side_effect = lambda **kwargs: queued.append(kwargs)
    limit = (
        patch.object(
            conditional_replay_on_circuit_close, "soft_time_limit", soft_time_limit
        )
        if soft_time_limit is not None
        else patch.object(conditional_replay_on_circuit_close, "soft_time_limit", 290)
    )
    passes: list[tuple[dict, float]] = []
    kwargs: dict = first_pass or {
        "service_name": name,
        "max_items": 50,
        "max_continuations": 5,
    }
    with (
        patch("baldur.services.get_replay_service", return_value=service),
        patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=breakers,
        ),
        patch(
            "baldur.adapters.celery.tasks.conditional_replay_on_circuit_close", dispatch
        ),
        limit,
    ):
        for _ in range(kwargs["max_continuations"] + 1):
            started = time.monotonic()
            outcome = conditional_replay_on_circuit_close.apply(kwargs=kwargs).get()
            passes.append((outcome, time.monotonic() - started))
            if not queued:
                break
            if between_passes is not None:
                between_passes()
            kwargs = queued.pop(0)
    return passes


# =============================================================================
# A. Outage → park → close → replay
# =============================================================================


class TestLlmOutageReplayLoop:
    """Every job an outage stopped comes back when the job's breaker closes."""

    def test_outage_parks_every_job_and_the_closing_breaker_replays_them_all(
        self, provider
    ):
        """
        Purpose:
            The whole loop with the real SDK: a provider outage stops jobs, they
            are parked with their arguments, the breaker closes once the provider
            is back, and the recovery run re-runs every parked job.
        Expected:
            - two jobs end after every endpoint failed (``LLMUnavailableError``)
              and are parked as ``MAX_RETRIES_LLMUNAVAILABLEERROR``; the job's
              breaker then opens and two more are rejected without calling the
              provider and parked as open-circuit entries — all with ``doc_id``
            - nothing is parked under the endpoint's name
            - a live probe closes the breaker (a CLOSED event for the job)
            - one recovery run replays all four with their own ``doc_id``;
              every entry resolved, none lost
        """
        # Given — the provider is down
        repo = InMemoryFailedOperationRepository()
        job = _Job(provider)
        breakers = _job_breaker(job.name)
        provider.answer = _down
        stopped: dict[str, type] = {}

        # When — four jobs arrive during the outage
        with patch(_RESOLVE_BACKING, return_value=DLQCaptureService(repository=repo)):
            for doc_id in ("doc-1", "doc-2", "doc-3", "doc-4"):
                try:
                    job.summarize(doc_id)
                except (LLMUnavailableError, CircuitBreakerError) as error:
                    stopped[doc_id] = type(error)
        parked = {entry.request_data["doc_id"]: entry for entry in repo.find()}

        # Then — two after every endpoint failed, two by the open breaker
        assert stopped["doc-1"] is LLMUnavailableError
        assert stopped["doc-2"] is LLMUnavailableError
        assert issubclass(stopped["doc-3"], CircuitBreakerError)
        assert issubclass(stopped["doc-4"], CircuitBreakerError)
        assert {doc: entry.failure_type for doc, entry in parked.items()} == {
            "doc-1": NO_ENDPOINT,
            "doc-2": NO_ENDPOINT,
            "doc-3": OPEN_CIRCUIT_FAILURE_TYPE,
            "doc-4": OPEN_CIRCUIT_FAILURE_TYPE,
        }
        assert {entry.domain for entry in parked.values()} == {job.name}
        assert provider.contents().count("doc-3") == 0

        # When — the provider is back; past the recovery wait a live job probes
        provider.answer = _ok
        later = utc_now() + timedelta(seconds=_JOB_BREAKER.recovery_timeout + 1)
        with patch(_BREAKER_CLOCK, return_value=later):
            job.summarize("doc-live")
        closed = [
            call
            for call in breakers._event_bus.emit.call_args_list
            if call.args and call.args[0] == EventType.CIRCUIT_BREAKER_CLOSED
        ]
        passes = _run_recovery(_replay_service(repo), breakers, job.name)

        # Then — the CLOSED event fired for the job, and every parked job came back
        assert breakers.get_state(job.name) == "closed"
        assert [call.kwargs["data"]["service_name"] for call in closed] == [job.name]
        assert sorted(job.done) == ["doc-1", "doc-2", "doc-3", "doc-4", "doc-live"]
        assert {entry.status for entry in repo.find()} == {"resolved"}
        assert sum(outcome["success_count"] for outcome, _ in passes) == 4


# =============================================================================
# B. The pass deadline against a provider that hangs
# =============================================================================


class TestLlmRecoveryPassDeadline:
    """A replay the pass deadline cut is handed back to the next pass, which replays it first."""

    def test_hanging_provider_is_cut_at_the_pass_deadline_and_replayed_by_the_continuation(
        self, provider, repo
    ):
        """
        Purpose:
            A recovery pass whose replay hangs inside the real SDK call ends by
            its own deadline instead of the task's soft time limit, and the
            backlog it could not finish is picked up by its continuation.
        Expected:
            - the first pass returns well inside the soft time limit, ``capped``
              with nothing counted, and queues its continuation although its
              first replay was the one cut
            - the cut job is still pending (one replay attempt used), not in
              review
            - once the provider answers, the continuation replays the cut job
              first and then the other: both resolved, none lost
        """
        # Given — two parked jobs; the provider accepts the request and never answers
        job = _Job(provider)
        breakers = _job_breaker(job.name)
        first = repo.create(
            domain=job.name, failure_type=NO_ENDPOINT, request_data={"doc_id": "doc-1"}
        ).id
        second = repo.create(
            domain=job.name, failure_type=NO_ENDPOINT, request_data={"doc_id": "doc-2"}
        ).id
        provider.answer = _hang
        pass_deadline = 1.0
        after_first_pass: dict[str, Any] = {}

        def provider_back() -> None:
            if not after_first_pass:
                entry = repo.get_by_id(first)
                after_first_pass.update(
                    status=entry.status, retry_count=entry.retry_count
                )
            provider.answer = _ok

        # When — the recovery runs with a one-second pass deadline
        passes = _run_recovery(
            _replay_service(repo),
            breakers,
            job.name,
            between_passes=provider_back,
            soft_time_limit=_CIRCUIT_CLOSE_DEADLINE_MARGIN_SECONDS + pass_deadline,
        )

        # Then — the first pass was cut at its deadline and continued
        (first_pass, first_elapsed), *rest = passes
        assert first_pass["capped"] is True
        assert (first_pass["total"], first_pass["failed_count"]) == (0, 0)
        assert first_pass["continued"] is True
        assert pass_deadline <= first_elapsed < pass_deadline + 5.0
        assert after_first_pass == {"status": "pending", "retry_count": 1}
        assert rest

        # Then — the continuation replayed the cut job first, then the other
        assert job.done == ["doc-1", "doc-2"]
        assert repo.get_by_id(first).status == "resolved"
        assert repo.get_by_id(second).status == "resolved"


# =============================================================================
# C. A short outage, and a breaker left half-open with no traffic (807)
# =============================================================================

_TRIAL_TASK = "baldur.adapters.celery.tasks.conditional_replay_on_circuit_close"


def _recovery_tick(
    service: ReplayService, breakers: CircuitBreakerService, *, now: float
):
    """One recovery tick as the leader's or beat's tick runs it; its dispatch captured."""
    from types import SimpleNamespace

    from baldur.services.replay_service.recovery import RecoveryTrialRunner

    queued: list[dict] = []
    dispatch = MagicMock(spec=conditional_replay_on_circuit_close)
    dispatch.delay.side_effect = lambda **kwargs: queued.append(kwargs)
    runner = RecoveryTrialRunner(
        replay_service=service,
        circuit_breaker_service=breakers,
        system_control=SimpleNamespace(
            is_state_known=lambda: True, is_enabled=lambda: True
        ),
        clock=lambda: now,
    )
    with patch(_TRIAL_TASK, dispatch):
        result = runner.run(deadline=None)
    return result, queued


class TestLlmShortOutageRecoveryTrial:
    """No traffic and no CLOSED transition: the recovery trial brings jobs back."""

    def test_short_outage_never_opened_breaker_jobs_come_back_through_a_trial(
        self, provider
    ):
        """
        Purpose:
            An outage too short to open the job's breaker parks a job, and
            nothing calls the job again. The breaker reads CLOSED throughout,
            so no CLOSED event will ever come.
        Expected:
            - one job is parked after every endpoint failed; the breaker stays
              CLOSED (one failure, below its threshold)
            - once the provider answers, a recovery tick replays a parked job as
              a trial through the real SDK, and the sweep it dispatches
              (no escalation) replays the rest — every entry resolved
            - no CLOSED event: the breaker never left CLOSED
        """
        # Given — one job fails during a short outage; another was parked earlier
        repo = InMemoryFailedOperationRepository()
        job = _Job(provider)
        breakers = _job_breaker(job.name)
        provider.answer = _down
        with patch(_RESOLVE_BACKING, return_value=DLQCaptureService(repository=repo)):
            with pytest.raises(LLMUnavailableError):
                job.summarize("doc-1")
        repo.create(
            domain=job.name, failure_type=NO_ENDPOINT, request_data={"doc_id": "doc-2"}
        )
        assert breakers.get_state(job.name) == "closed"

        # When — the provider answers; the tick, then the sweep it dispatched
        provider.answer = _ok
        service = _replay_service(repo)
        tick, queued = _recovery_tick(service, breakers, now=time.time())
        passes = _run_recovery(service, breakers, job.name, first_pass=queued[0])

        # Then
        assert [trial.outcome for trial in tick.trials] == ["succeeded"]
        assert tick.trials[0].dispatch == "dispatched"
        assert queued[0]["trigger"] == "auto_replay_recovery"
        assert queued[0]["escalate_failures"] is False
        assert sorted(job.done) == ["doc-1", "doc-2"]
        assert {entry.status for entry in repo.find()} == {"resolved"}
        assert sum(outcome.get("success_count", 0) for outcome, _ in passes) == 1
        assert not [
            call
            for call in breakers._event_bus.emit.call_args_list
            if call.args and call.args[0] == EventType.CIRCUIT_BREAKER_CLOSED
        ]

    def test_short_outage_half_open_breaker_with_no_traffic_is_closed_by_trials(
        self, provider
    ):
        """
        Purpose:
            The outage opened the job's breaker; it went HALF_OPEN with no
            traffic to probe it, so it would never close on its own.
        Expected:
            - while the provider is still down the trial is refused or fails
              and costs the parked job none of its replay attempts
            - once the provider answers, the trial is the breaker's probe and
              closes it: the CLOSED event fires and the tick leaves the sweep to
              it (no second chain from the tick)
            - the CLOSED event's sweep replays the rest — every entry resolved
        """
        # Given — the outage opens the breaker and parks four jobs
        repo = InMemoryFailedOperationRepository()
        job = _Job(provider)
        breakers = _job_breaker(job.name)
        provider.answer = _down
        with patch(_RESOLVE_BACKING, return_value=DLQCaptureService(repository=repo)):
            for doc_id in ("doc-1", "doc-2", "doc-3", "doc-4"):
                with pytest.raises((LLMUnavailableError, CircuitBreakerError)):
                    job.summarize(doc_id)
        assert breakers.get_state(job.name) == "open"
        # ...the periodic recovery moves it to HALF_OPEN; nothing calls the job.
        store = breakers.repository
        store._storage[job.name] = replace(
            store.get_or_create(job.name), state="half_open"
        )
        service = _replay_service(repo)

        # When — a tick while the provider is still down
        started = time.time()
        down_tick, down_queued = _recovery_tick(service, breakers, now=started)
        attempts_after_down_tick = sorted(e.retry_count for e in repo.find())

        # When — the provider answers; the next tick past the trial spacing
        provider.answer = _ok
        later = utc_now() + timedelta(seconds=_JOB_BREAKER.recovery_timeout + 1)
        with patch(_BREAKER_CLOCK, return_value=later):
            up_tick, up_queued = _recovery_tick(service, breakers, now=started + 60)
        passes = _run_recovery(service, breakers, job.name)

        # Then — the failed trial cost nothing; the probe closed the breaker
        assert down_queued == []
        assert [trial.outcome for trial in down_tick.trials] == ["failed"]
        assert attempts_after_down_tick == [0, 0, 0, 0]
        assert [trial.outcome for trial in up_tick.trials] == ["succeeded"]
        assert up_tick.trials[0].dispatch == "closed_by_trial"
        assert up_queued == []
        assert breakers.get_state(job.name) == "closed"
        closed = [
            call
            for call in breakers._event_bus.emit.call_args_list
            if call.args and call.args[0] == EventType.CIRCUIT_BREAKER_CLOSED
        ]
        assert [call.kwargs["data"]["service_name"] for call in closed] == [job.name]
        assert sorted(job.done) == ["doc-1", "doc-2", "doc-3", "doc-4"]
        assert {entry.status for entry in repo.find()} == {"resolved"}
        assert sum(outcome.get("success_count", 0) for outcome, _ in passes) == 3
