"""Function replay: a parked ``replay=True`` job, re-run from its stored arguments.

Targets:
- ``baldur.services.replay_service.function_replay`` — ``FunctionReplayHandler``
  (arguments back from ``request_data``, the run under the decorator's own
  protections minus DLQ capture and fallback, the async runner).
- ``ReplayService.replay_on_circuit_close`` — the lane a handler declares
  (``auto_replay_failure_types``), and a recovery pass that ends each replay at
  its deadline and leaves the replay the deadline cut in the backlog.
- ``DLQCaptureService.store_failure`` — the larger argument cap of a
  replayable domain, on the sync and the outbox store paths.
- ``DLQSink`` + Celery's failure signal — one failed job inside a task is one
  entry.

The sweep runs for real: an in-memory DLQ repository, a ``ReplayService`` over
it with governance injected, and jobs armed by the decorator itself. The LLM
SDK is the fake client tree from ``tests.factories.llm_doubles`` behind a real
``baldur.llm.wrap``.

UNIT_TEST_GUIDELINES.md:
- No ``time.sleep`` (§6.3): the retry sleeper is patched out and coordinator
  waits are recorded by ``mock_sleep``. The deadline tests let a fake SDK call
  block on an event for the timeout it was handed, the way a real SDK call
  runs out its timeout; nothing in the tests themselves sleeps.
- §8.13: each lane test pins the lane by an entry only that lane could select.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage
from baldur.core.exceptions import LLMUnavailableError
from baldur.interfaces.governance import GovernanceChecker
from baldur.interfaces.repositories import FailedOperationData
from baldur.interfaces.resilience_policy import PolicyContext
from baldur.llm import Endpoint, wrap
from baldur.models.dlq import OPEN_CIRCUIT_FAILURE_TYPE, POLICY_CHAIN_CAPTURE_SOURCE
from baldur.models.governance import GovernanceCheckResult
from baldur.protect_facade import aprotected, protected
from baldur.scaling.deadline_context import deadline_scope, get_remaining_ms
from baldur.services.dlq_capture.service import DLQCaptureService
from baldur.services.dlq_outbox import outbox as outbox_module
from baldur.services.dlq_outbox.outbox import Outbox
from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.rate_limit_coordinator import RateLimitCoordinator
from baldur.services.rate_limit_coordinator.models import RateLimitCoordinatorConfig
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.function_replay import (
    _ASYNC_REPLAY_THREAD_NAME,
    _ERROR_PREVIEW_CHARS,
    FunctionReplayHandler,
)
from baldur.services.replay_service.handlers import (
    ReplayHandler,
    _replay_handlers,
    get_replay_handler,
    register_replay_handler,
)
from baldur.services.replay_service.models import ReplayResult
from baldur.services.retry_handler.sinks import retry_exhausted_failure_type
from baldur.settings.dlq import get_dlq_settings, reset_dlq_settings
from baldur.settings.dlq_outbox import reset_dlq_outbox_settings
from baldur.settings.protect import reset_protect_settings
from baldur.utils.domain_validation import resolve_stored_domain
from baldur.utils.serialization import fast_dumps_str
from tests.factories.llm_doubles import FakeLLMClient, FakeOpenAIError, LLMCall
from tests.factories.time_helpers import mock_sleep

_SYNC_RETRY_SLEEP = "baldur.services.retry_handler.policy._DEFAULT_SLEEPER"
_STORE = "baldur.services.retry_handler.sinks.store_to_dlq"
_RESOLVE_BACKING = "baldur.services.dlq_capture.resolve_dlq_backing"
_PROTECT = "baldur.protect_facade.protect"

NO_ENDPOINT = retry_exhausted_failure_type(LLMUnavailableError.__name__)
INVALID_REQUEST_PARK = retry_exhausted_failure_type("BadRequestError")

# The size of a replayed prompt: five times the ordinary 4 KiB cap.
_LARGE_ARGUMENT_CHARS = 20 * 1024


# =============================================================================
# Fixtures and helpers
# =============================================================================


@pytest.fixture(autouse=True)
def sandbox() -> Iterator[None]:
    """Registry restored; fresh protect caches; no retry sleep; a private coordinator."""
    before = dict(_replay_handlers)
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


@pytest.fixture
def repo() -> InMemoryFailedOperationRepository:
    """The DLQ the jobs are parked in and the sweep selects from."""
    return InMemoryFailedOperationRepository()


@pytest.fixture
def service(repo) -> ReplayService:
    """A real sweep over ``repo``, governance injected rather than patched."""
    replay_service = ReplayService(repository=repo, cache=InMemoryCacheAdapter())
    replay_service._event_bus = MagicMock(spec=BaldurEventBus)
    replay_service._governance = MagicMock(spec=GovernanceChecker)
    replay_service._governance.check_all_governance.return_value = (
        GovernanceCheckResult(allowed=True)
    )
    replay_service._governance_resolved = True
    return replay_service


def _job_name() -> str:
    return f"job.summarize_{uuid.uuid4().hex[:10]}"


def _park(
    repo,
    domain: str,
    doc_id: str,
    *,
    failure_type: str = NO_ENDPOINT,
    metadata: dict[str, Any] | None = None,
) -> str:
    return repo.create(
        domain=domain,
        failure_type=failure_type,
        error_message="parked",
        request_data={"doc_id": doc_id},
        metadata=metadata or {},
    ).id


def _arm_recording_job(name: str, done: list[str]):
    """A ``replay=True`` job that records each document it finished."""

    @protected(name, replay=True, circuit_breaker=False, timeout=None)
    def summarize(doc_id: str) -> str:
        done.append(doc_id)
        return f"summary of {doc_id}"

    return summarize


def _entry(request_data: Any, entry_id: str = "dlq-1") -> FailedOperationData:
    return FailedOperationData(
        id=entry_id,
        domain="job.summarize",
        failure_type=NO_ENDPOINT,
        status="replaying",
        request_data=request_data,
    )


def _handler(name: str) -> FunctionReplayHandler:
    handler = get_replay_handler(resolve_stored_domain(name))
    assert isinstance(handler, FunctionReplayHandler)
    return handler


class _DeclaringHandler(ReplayHandler):
    """A hand-written handler whose declared types the test chooses."""

    def __init__(self, domain: str, declared: Any) -> None:
        self._domain = domain
        self._declared = declared
        self.replayed: list[str] = []

    @property
    def domain(self) -> str:
        return self._domain

    @property
    def auto_replay_failure_types(self) -> tuple[str, ...]:
        if isinstance(self._declared, Exception):
            raise self._declared
        return self._declared

    def can_replay(self, failed_op: FailedOperationData) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op: FailedOperationData) -> ReplayResult:
        self.replayed.append(failed_op.id)
        return ReplayResult.succeeded(failed_op.id, "done")


# =============================================================================
# Behavior — FunctionReplayHandler
# =============================================================================


class TestFunctionReplayHandlerBehavior:
    """The stored arguments back into the function, under the decorator's own protections."""

    def test_replay_reruns_the_function_with_the_stored_arguments(self):
        """Keyword arguments come back exactly; keys the function does not take are ignored."""
        name = _job_name()
        received: list[dict] = []

        @protected(name, replay=True, circuit_breaker=False, timeout=None)
        def summarize(doc_id: str, pages: int, lang: str = "en") -> str:
            received.append({"doc_id": doc_id, "pages": pages, "lang": lang})
            return "ok"

        result = _handler(name).replay(
            _entry({"doc_id": "doc-1", "pages": 3, "not_a_parameter": "x"})
        )

        assert result.success is True
        assert result.message == f"re-ran {name}"
        assert received == [{"doc_id": "doc-1", "pages": 3, "lang": "en"}]

    @pytest.mark.parametrize(
        ("stored", "reason"),
        [
            ({"pages": 3}, "lack required parameter 'doc_id'"),
            ({"doc_id": "doc-1", "pages": 3, "options": [1, 2]}, "is a list"),
            (None, "no stored arguments"),
        ],
        ids=["missing_required", "unannotated_non_native", "nothing_stored"],
    )
    def test_arguments_that_cannot_come_back_exactly_fail_without_a_call(
        self, stored, reason
    ):
        """No call with guessed arguments: the replay fails and says why."""
        name = _job_name()
        received: list[Any] = []

        @protected(name, replay=True, circuit_breaker=False, timeout=None)
        def summarize(doc_id: str, pages: int, options=None) -> str:
            received.append(doc_id)
            return "ok"

        handler = _handler(name)
        entry = _entry(stored)

        result = handler.replay(entry)

        assert result.success is False
        assert reason in result.error
        assert handler.can_replay(entry)[0] is False
        assert reason in handler.can_replay(entry)[1]
        assert received == []

    def test_replay_runs_the_undecorated_function_with_the_decorators_options(self):
        """Same retry, breaker, timeout and idempotency; no DLQ capture, no fallback."""
        # Given
        name = _job_name()

        @protected(
            name,
            replay=True,
            retry=False,
            circuit_breaker=False,
            timeout=12.5,
            idempotency_key="doc_id",
            idempotency_fail_open=True,
            idempotency_ttl=timedelta(minutes=5),
            idempotency_execution_ttl=timedelta(seconds=90),
            fallback=lambda: "fallback",
        )
        def summarize(doc_id: str) -> str:
            return doc_id

        # When
        with patch(_PROTECT, autospec=True, return_value="re-run") as protect:
            result = _handler(name).replay(_entry({"doc_id": "doc-9"}))

        # Then
        assert result.success is True
        protect.assert_called_once()
        replayed_name, call = protect.call_args.args
        options = protect.call_args.kwargs
        assert replayed_name == name
        # The call protect runs is the undecorated function with the stored
        # arguments: running it bypasses the (patched) protection entirely.
        assert call() == "doc-9"
        assert {key: options[key] for key in options if key != "context"} == {
            "retry": False,
            "circuit_breaker": False,
            "timeout": 12.5,
            "idempotency_key": "doc_id",
            "idempotency_fail_open": True,
            "idempotency_ttl": timedelta(minutes=5),
            "idempotency_execution_ttl": timedelta(seconds=90),
            "dlq": False,
            "fallback": None,
        }
        assert isinstance(options["context"], PolicyContext)

    def test_failed_replay_parks_nothing_and_never_serves_the_fallback(self):
        """A replay that fails again adds no entry; a fallback cannot mark it done."""
        name = _job_name()

        @protected(
            name,
            replay=True,
            circuit_breaker=False,
            timeout=None,
            fallback=lambda: "fallback",
        )
        def summarize(doc_id: str) -> str:
            raise RuntimeError("provider still down")

        with patch(_STORE, autospec=True) as store:
            result = _handler(name).replay(_entry({"doc_id": "doc-1"}))

        assert result.success is False
        assert result.error == "RuntimeError: provider still down"
        store.assert_not_called()

    def test_failure_message_is_cut_to_its_preview_length(self):
        """A long error message does not ride into the replay result whole."""
        name = _job_name()

        @protected(name, replay=True, circuit_breaker=False, timeout=None)
        def summarize(doc_id: str) -> str:
            raise RuntimeError("x" * (_ERROR_PREVIEW_CHARS * 3))

        result = _handler(name).replay(_entry({"doc_id": "doc-1"}))

        assert result.error == "RuntimeError: " + "x" * _ERROR_PREVIEW_CHARS

    def test_declared_auto_replay_type_is_the_no_endpoint_park(self):
        """The handler declares the label the sink writes for ``LLMUnavailableError``."""
        name = _job_name()
        _arm_recording_job(name, [])

        assert _handler(name).auto_replay_failure_types == (NO_ENDPOINT,)

    def test_a_hand_written_handler_declares_nothing_by_default(self):
        """Opting a domain's parks in is explicit."""

        class Plain(ReplayHandler):
            domain = "job.plain"

            def can_replay(self, failed_op):
                return True, ""

            def replay(self, failed_op):
                return ReplayResult.succeeded(failed_op.id)

        assert Plain().auto_replay_failure_types == ()

    def test_async_job_replays_to_completion_from_sync_code(self):
        """A coroutine job is run to completion by the replaying thread."""
        name = _job_name()
        done: list[str] = []

        @aprotected(name, replay=True, circuit_breaker=False, timeout=None)
        async def summarize(doc_id: str) -> str:
            await asyncio.sleep(0)
            done.append(doc_id)
            return "ok"

        result = _handler(name).replay(_entry({"doc_id": "doc-3"}))

        assert result.success is True
        assert done == ["doc-3"]

    def test_async_job_replayed_inside_a_running_loop_runs_on_a_fresh_thread(self):
        """A thread already running a loop cannot start another, so a fresh one runs it."""
        name = _job_name()
        threads: list[str] = []

        @aprotected(name, replay=True, circuit_breaker=False, timeout=None)
        async def summarize(doc_id: str) -> str:
            threads.append(threading.current_thread().name)
            return "ok"

        async def replay_from_a_loop() -> ReplayResult:
            return _handler(name).replay(_entry({"doc_id": "doc-4"}))

        result = asyncio.run(replay_from_a_loop())

        assert result.success is True
        assert threads == [_ASYNC_REPLAY_THREAD_NAME]

    @pytest.mark.parametrize("inside_a_loop", [False, True], ids=["sync", "loop"])
    def test_async_replay_carries_the_callers_deadline(self, inside_a_loop):
        """The pass deadline reaches every await of the replayed coroutine."""
        name = _job_name()
        seen: list[float | None] = []

        @aprotected(name, replay=True, circuit_breaker=False, timeout=None)
        async def summarize(doc_id: str) -> str:
            seen.append(get_remaining_ms())
            return "ok"

        def replay() -> ReplayResult:
            return _handler(name).replay(_entry({"doc_id": "doc-5"}))

        async def replay_from_a_loop() -> ReplayResult:
            return replay()

        with deadline_scope(30_000):
            result = asyncio.run(replay_from_a_loop()) if inside_a_loop else replay()

        assert result.success is True
        assert seen[0] is not None
        assert 0 < seen[0] <= 30_000


# =============================================================================
# Behavior — the lanes a replay handler declares
# =============================================================================


class TestDeclaredReplayLaneBehavior:
    """The job's own no-endpoint parks replay when its breaker closes — only those."""

    def test_sweep_replays_no_endpoint_and_open_circuit_parks_with_their_arguments(
        self, repo, service
    ):
        """Both the job's no-endpoint parks and its open-circuit parks come back, as stored."""
        # Given
        name = _job_name()
        done: list[str] = []
        _arm_recording_job(name, done)
        no_endpoint = _park(repo, name, "doc-a1")
        open_circuit = _park(
            repo,
            name,
            "doc-a2",
            failure_type=OPEN_CIRCUIT_FAILURE_TYPE,
            metadata={"source": POLICY_CHAIN_CAPTURE_SOURCE},
        )

        # When
        result = service.replay_on_circuit_close(name, service_failure_type_map={})

        # Then
        assert done == ["doc-a1", "doc-a2"]
        assert (result.success_count, result.failed_count) == (2, 0)
        assert repo.get_by_id(no_endpoint).status == "resolved"
        assert repo.get_by_id(open_circuit).status == "resolved"

    def test_sweep_does_not_select_an_invalid_request_park_of_the_same_job(
        self, repo, service
    ):
        """A request the provider rejected stays parked for review."""
        name = _job_name()
        done: list[str] = []
        _arm_recording_job(name, done)
        rejected = _park(repo, name, "doc-bad", failure_type=INVALID_REQUEST_PARK)

        result = service.replay_on_circuit_close(name, service_failure_type_map={})

        assert done == []
        assert result.total == 0
        assert repo.get_by_id(rejected).status == "pending"

    def test_sweep_does_not_select_another_jobs_no_endpoint_park(self, repo, service):
        """The declared lane is scoped to the job whose breaker closed."""
        name, other = _job_name(), _job_name()
        done: list[str] = []
        other_done: list[str] = []
        _arm_recording_job(name, done)
        _arm_recording_job(other, other_done)
        mine = _park(repo, name, "doc-mine")
        theirs = _park(repo, other, "doc-theirs")

        service.replay_on_circuit_close(name, service_failure_type_map={})

        assert (done, other_done) == (["doc-mine"], [])
        assert repo.get_by_id(mine).status == "resolved"
        assert repo.get_by_id(theirs).status == "pending"

    def test_sweep_declared_lane_needs_no_capture_source_stamp(self, repo, service):
        """A long retry history can push ``source`` out of capped metadata; it still replays."""
        name = _job_name()
        done: list[str] = []
        _arm_recording_job(name, done)
        _park(
            repo,
            name,
            "doc-long-history",
            metadata={"_truncated": True, "original_size": 9000, "preview": "..."},
        )

        service.replay_on_circuit_close(name, service_failure_type_map={})

        assert done == ["doc-long-history"]

    def test_sweep_operator_mapped_type_is_not_selected_twice(self, repo, service):
        """When the operator already maps the type, its lane covers it: one replay, no failure."""
        name = _job_name()
        done: list[str] = []
        _arm_recording_job(name, done)
        _park(repo, name, "doc-mapped")

        result = service.replay_on_circuit_close(
            name, service_failure_type_map={name: [NO_ENDPOINT]}
        )

        assert done == ["doc-mapped"]
        assert (result.total, result.failed_count) == (1, 0)

    def test_sweep_declared_open_circuit_type_and_duplicates_are_dropped(
        self, repo, service
    ):
        """The open-circuit type keeps its own source-filtered lane; a repeat is one lane."""
        # Given — a handler declaring the open-circuit type and one type twice
        name = _job_name()
        domain = resolve_stored_domain(name)
        handler = _DeclaringHandler(
            domain, (OPEN_CIRCUIT_FAILURE_TYPE, "MAX_RETRIES_X", "MAX_RETRIES_X")
        )
        register_replay_handler(handler)
        declared = _park(repo, domain, "doc-x", failure_type="MAX_RETRIES_X")
        unstamped_rejection = _park(
            repo, domain, "doc-oc", failure_type=OPEN_CIRCUIT_FAILURE_TYPE
        )

        # When
        result = service.replay_on_circuit_close(name, service_failure_type_map={})

        # Then — the declared type once; an open-circuit park with no
        # policy-chain stamp is not taken by a declared lane either
        assert handler.replayed == [declared]
        assert (result.total, result.failed_count) == (1, 0)
        assert repo.get_by_id(unstamped_rejection).status == "pending"

    def test_sweep_unreadable_declaration_leaves_only_the_open_circuit_lane(
        self, repo, service
    ):
        """A handler whose declaration raises declares nothing, and the fault is logged."""
        name = _job_name()
        domain = resolve_stored_domain(name)
        handler = _DeclaringHandler(domain, RuntimeError("declaration broken"))
        register_replay_handler(handler)
        _park(repo, domain, "doc-declared", failure_type="MAX_RETRIES_X")
        rejected = _park(
            repo,
            domain,
            "doc-oc",
            failure_type=OPEN_CIRCUIT_FAILURE_TYPE,
            metadata={"source": POLICY_CHAIN_CAPTURE_SOURCE},
        )

        with capture_logs() as logs:
            service.replay_on_circuit_close(name, service_failure_type_map={})

        assert handler.replayed == [rejected]
        assert [
            (log["event"], log["log_level"])
            for log in logs
            if log["event"] == "replay_service.declared_failure_types_unreadable"
        ] == [("replay_service.declared_failure_types_unreadable", "warning")]

    def test_sweep_with_only_a_declared_lane_still_runs(self, repo, service):
        """The no-mapping early exit counts declared lanes: one is enough to sweep.

        No public input produces a declared lane without the open-circuit lane
        beside it (both need the same handler), so the open-circuit lane is
        switched off here to pin the early exit's own condition.
        """
        name = _job_name()
        done: list[str] = []
        _arm_recording_job(name, done)
        _park(repo, name, "doc-only-declared")

        from baldur.models.dlq import OPEN_CIRCUIT_FAILURE_TYPE
        from baldur.services.replay_service import service as service_module

        real_lanes = service_module.recovery_lanes

        def declared_only(domain, failure_type_map):
            return [
                lane
                for lane in real_lanes(domain, failure_type_map)
                if lane[0] != OPEN_CIRCUIT_FAILURE_TYPE
            ]

        with patch.object(service_module, "recovery_lanes", side_effect=declared_only):
            result = service.replay_on_circuit_close(name, service_failure_type_map={})

        assert done == ["doc-only-declared"]
        assert result.success_count == 1


# =============================================================================
# Behavior — a recovery pass ends each replay at its deadline
# =============================================================================


class _SlowProvider:
    """An SDK call that does not answer before its timeout, then raises as the SDK does.

    The wait is an event wait of exactly the timeout the wrap handed the call —
    the SDK's own behavior when a provider hangs, so the call ends when the
    deadline it was given does, not a moment later. ``answering`` makes it
    answer.
    """

    def __init__(self) -> None:
        self.timeouts: list[Any] = []
        self.answering = False
        self.answer_mode = "answer"
        self._never_set = threading.Event()

    def __call__(self, call: LLMCall) -> Any:
        self.timeouts.append(call.kwargs.get("timeout"))
        if self.answering:
            return "summary"
        if self.answer_mode == "quota":
            raise FakeOpenAIError(402)
        if self.answer_mode == "failing":
            raise FakeOpenAIError(500)
        timeout = call.kwargs.get("timeout")
        self._never_set.wait(timeout if timeout is not None else 5.0)
        raise FakeOpenAIError(None, message="Request timed out.")


class TestRecoveryPassDeadlineCutBehavior:
    """A replay the pass deadline cut goes back to the backlog, not to review."""

    _WINDOW_S = 0.3

    @pytest.fixture
    def provider(self) -> _SlowProvider:
        return _SlowProvider()

    @pytest.fixture
    def job(self, provider) -> tuple[str, list[str], list[float | None]]:
        """A ``replay=True`` job whose LLM call goes through the wrap to ``provider``."""
        name = _job_name()
        done: list[str] = []
        remaining: list[float | None] = []
        client = FakeLLMClient(
            answers=[provider], base_url=f"https://{uuid.uuid4().hex[:8]}.example.com"
        )
        llm = wrap(Endpoint(client, name=f"llm.endpoint_{uuid.uuid4().hex[:10]}"))

        @protected(name, replay=True, circuit_breaker=False, timeout=None)
        def summarize(doc_id: str) -> str:
            remaining.append(get_remaining_ms())
            llm.chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": doc_id}]
            )
            done.append(doc_id)
            return "ok"

        return name, done, remaining

    def _cut_pass(self, service, name, **kwargs):
        return service.replay_on_circuit_close(
            name,
            service_failure_type_map={},
            deadline=time.monotonic() + self._WINDOW_S,
            **kwargs,
        )

    def test_deadline_cut_replay_stays_pending_unescalated_and_uncounted(
        self, repo, service, provider, job
    ):
        """The pass returns ``capped`` and the cut entry is still in the backlog."""
        # Given
        name, done, _ = job
        first = _park(repo, name, "doc-1")
        second = _park(repo, name, "doc-2")

        # When
        result = self._cut_pass(service, name)

        # Then
        assert result.capped is True
        assert (result.total, result.failed_count, result.results) == (0, 0, [])
        assert result.deadline_cut_dlq_id == first
        cut = repo.get_by_id(first)
        assert (cut.status, cut.retry_count) == ("pending", 1)
        untouched = repo.get_by_id(second)
        assert (untouched.status, untouched.retry_count) == ("pending", 0)
        assert done == []

    def test_deadline_is_handed_to_the_sdk_as_the_call_timeout(
        self, repo, service, provider, job
    ):
        """The SDK call is told to stop by the pass deadline, and only one call is made."""
        name, _, _ = job
        _park(repo, name, "doc-1")

        self._cut_pass(service, name)

        assert len(provider.timeouts) == 1
        # (a microsecond of float noise from the scope's buffer arithmetic)
        assert 0 < provider.timeouts[0] <= self._WINDOW_S + 1e-6

    def test_deadline_continuation_replays_the_cut_entry_first(
        self, repo, service, provider, job
    ):
        """The next pass, from the returned cursors, starts with the replay that was cut."""
        # Given — a pass the deadline cut on the first entry
        name, done, _ = job
        first = _park(repo, name, "doc-1")
        second = _park(repo, name, "doc-2")
        cut = self._cut_pass(service, name)
        provider.answering = True

        # When — the continuation, with no deadline of its own
        result = service.replay_on_circuit_close(
            name,
            service_failure_type_map={},
            lane_cursors=cut.lane_cursors,
            continuation=1,
        )

        # Then
        assert done == ["doc-1", "doc-2"]
        assert result.success_count == 2
        assert repo.get_by_id(first).status == "resolved"
        assert repo.get_by_id(second).status == "resolved"

    def test_deadline_second_cut_at_the_replay_cap_goes_to_review(
        self, repo, service, provider, job
    ):
        """A job longer than a whole pass reaches review at its cap, not an endless chain."""
        name, _, _ = job
        first = _park(repo, name, "doc-1")
        cap = service.config["max_replay_attempts"]
        cut = None

        for continuation in range(cap):
            cut = self._cut_pass(
                service,
                name,
                lane_cursors=cut.lane_cursors if cut else None,
                continuation=continuation,
            )

        entry = repo.get_by_id(first)
        assert (entry.status, entry.retry_count) == ("requires_review", cap)

    def test_deadline_cut_by_the_retry_budget_before_the_deadline_stays_pending(
        self, repo, service, provider, job
    ):
        """A retry the deadline leaves no room for ends the replay early: a cut too."""
        # Given — the provider fails at once, and the next retry would outlast the pass
        name, done, _ = job
        provider.answer_mode = "failing"
        first = _park(repo, name, "doc-1")

        # When
        result = self._cut_pass(service, name)

        # Then — one attempt, ended before the deadline, back in the backlog
        assert len(provider.timeouts) == 1
        assert result.capped is True
        assert (result.failed_count, result.deadline_cut_dlq_id) == (0, first)
        cut = repo.get_by_id(first)
        assert (cut.status, cut.retry_count) == ("pending", 1)
        assert done == []

    def test_deadline_failure_before_the_deadline_is_still_escalated(
        self, repo, service, provider, job
    ):
        """Only a cut is spared review: a replay that failed in time is escalated as before."""
        name, _, _ = job
        provider.answer_mode = "quota"
        first = _park(repo, name, "doc-1")

        result = service.replay_on_circuit_close(
            name, service_failure_type_map={}, deadline=time.monotonic() + 30.0
        )

        assert (result.capped, result.failed_count) == (False, 1)
        assert result.deadline_cut_dlq_id is None
        assert repo.get_by_id(first).status == "requires_review"

    def test_deadline_scope_is_set_around_each_replay(
        self, repo, service, provider, job
    ):
        """Inside the pass the job sees the time left; outside a pass it sees no deadline."""
        name, _, remaining = job
        provider.answering = True
        _park(repo, name, "doc-1")
        _park(repo, name, "doc-2")

        service.replay_on_circuit_close(
            name, service_failure_type_map={}, deadline=time.monotonic() + 30.0
        )
        within_pass = list(remaining)
        _park(repo, name, "doc-3")
        service.replay_on_circuit_close(name, service_failure_type_map={})

        assert len(within_pass) == 2
        assert all(value is not None and 0 < value <= 30_000 for value in within_pass)
        assert remaining[2:] == [None]

    def test_deadline_absent_the_sdk_call_gets_no_timeout(
        self, repo, service, provider, job
    ):
        """Outside a pass deadline the wrap adds no ``timeout`` to the SDK call."""
        name, _, _ = job
        provider.answering = True
        _park(repo, name, "doc-1")

        service.replay_on_circuit_close(name, service_failure_type_map={})

        assert provider.timeouts == [None]


# =============================================================================
# Behavior — one failed job in a Celery task is one entry
# =============================================================================


class TestCeleryCaptureSingleEntryBehavior:
    """The sink parks the job and marks it; Celery's failure hook then skips it."""

    def test_celery_single_entry_for_a_job_no_endpoint_answered(
        self, repo, monkeypatch
    ):
        """An eager task with Baldur's signal hooks: the job's failure is parked once."""
        # Given — a task the hook would park (no queue retries), wrapping a job
        celery = pytest.importorskip("celery")
        from baldur.adapters.celery.signal_hooks import (
            disconnect_baldur_signals,
            setup_baldur_signals,
        )

        monkeypatch.setenv("BALDUR_DLQ_OUTBOX_ENABLED", "false")
        reset_dlq_outbox_settings()
        name = _job_name()
        llm = wrap(
            Endpoint(
                FakeLLMClient(answers=[FakeOpenAIError(402)]),
                name=f"llm.endpoint_{uuid.uuid4().hex[:10]}",
            )
        )

        @protected(name, replay=True, circuit_breaker=False, timeout=None)
        def summarize(doc_id: str) -> str:
            return llm.chat.completions.create(model="gpt-4o", messages=[])

        app = celery.Celery(f"t{uuid.uuid4().hex[:8]}", set_as_current=False)
        app.conf.task_always_eager = True

        @app.task(name="jobs.summarize", max_retries=0)
        def summarize_task(doc_id: str) -> str:
            return summarize(doc_id)

        # When
        with patch(_RESOLVE_BACKING, return_value=DLQCaptureService(repository=repo)):
            setup_baldur_signals(
                app=app,
                cb_enabled=False,
                metrics_enabled=False,
                forensics_enabled=False,
            )
            try:
                outcome = summarize_task.apply(args=("doc-1",))
            finally:
                disconnect_baldur_signals()
                reset_dlq_outbox_settings()

        # Then
        assert isinstance(outcome.result, LLMUnavailableError)
        assert outcome.result.dlq_capture_dispatched is True
        entries = repo.find()
        assert [(e.domain, e.failure_type, e.request_data) for e in entries] == [
            (name, NO_ENDPOINT, {"doc_id": "doc-1"})
        ]


# =============================================================================
# Behavior — a replayable domain keeps its arguments
# =============================================================================


class _ParkingJob:
    """A ``replay=True`` job that fails while ``down`` and records what it was given."""

    def __init__(self, name: str) -> None:
        self.down = True
        self.received: list[str] = []

        @protected(name, replay=True, circuit_breaker=False, timeout=None)
        def summarize(prompt: str) -> str:
            self.received.append(prompt)
            if self.down:
                raise LLMUnavailableError("no endpoint answered")
            return "ok"

        self.summarize = summarize

    def fail_once(self, prompt: str) -> None:
        with pytest.raises(LLMUnavailableError):
            self.summarize(prompt)


class TestReplayRequestDataCapBehavior:
    """A job Baldur can replay is parked with its arguments whole, up to the replay cap."""

    @pytest.fixture(autouse=True)
    def _settings(self, monkeypatch) -> Iterator[None]:
        reset_dlq_settings()
        reset_dlq_outbox_settings()
        yield
        reset_dlq_settings()
        reset_dlq_outbox_settings()

    def _only_entry(self, repo) -> FailedOperationData:
        entries = repo.find()
        assert len(entries) == 1
        return entries[0]

    def test_large_argument_is_stored_and_replayed_exactly_on_the_sync_path(
        self, repo, monkeypatch
    ):
        """A 20 KiB prompt is parked whole and handed back to the job as it was."""
        # Given
        monkeypatch.setenv("BALDUR_DLQ_OUTBOX_ENABLED", "false")
        name = _job_name()
        job = _ParkingJob(name)
        prompt = "p" * _LARGE_ARGUMENT_CHARS
        with patch(_RESOLVE_BACKING, return_value=DLQCaptureService(repository=repo)):
            job.fail_once(prompt)
        entry = self._only_entry(repo)
        job.down = False

        # When
        result = _handler(name).replay(entry)

        # Then
        assert entry.request_data == {"prompt": prompt}
        assert result.success is True
        assert job.received[-1] == prompt

    def test_large_argument_survives_the_outbox_path(self, repo, monkeypatch):
        """Through the async outbox and its worker's store, the prompt is still whole."""
        # Given — the outbox takes the park; its worker stores it as it would
        monkeypatch.setenv("BALDUR_DLQ_OUTBOX_ENABLED", "true")
        name = _job_name()
        job = _ParkingJob(name)
        prompt = "q" * _LARGE_ARGUMENT_CHARS
        backing = DLQCaptureService(repository=repo)
        queued: list[dict[str, Any]] = []
        outbox = MagicMock(spec=Outbox)
        outbox.put.side_effect = lambda kwargs: queued.append(kwargs) or True
        with (
            patch(_RESOLVE_BACKING, return_value=backing),
            patch.object(outbox_module, "is_worker_dead", return_value=False),
            patch.object(outbox_module, "get_outbox", return_value=outbox),
        ):
            job.fail_once(prompt)
        assert len(queued) == 1
        backing.store_failure(mode="sync", **queued[0])
        entry = self._only_entry(repo)
        job.down = False

        # When
        result = _handler(name).replay(entry)

        # Then
        assert entry.request_data == {"prompt": prompt}
        assert result.success is True
        assert job.received[-1] == prompt

    @pytest.mark.parametrize(
        ("overshoot", "kept"), [(0, True), (1, False)], ids=["at_cap", "over_cap"]
    )
    def test_replay_cap_is_the_boundary(self, repo, monkeypatch, overshoot, kept):
        """At the replay cap the arguments are kept; one byte over, the marker is stored."""
        # Given — the smallest replay cap, and a payload sized to it
        monkeypatch.setenv("BALDUR_DLQ_REPLAY_REQUEST_DATA_MAX_BYTES", "4096")
        monkeypatch.setenv("BALDUR_DLQ_REQUEST_DATA_MAX_BYTES", "256")
        reset_dlq_settings()
        cap = get_dlq_settings().replay_request_data_max_bytes
        overhead = len(fast_dumps_str({"prompt": ""}).encode("utf-8"))
        prompt = "r" * (cap - overhead + overshoot)
        name = _job_name()
        _ParkingJob(name)

        # When
        with capture_logs() as logs:
            DLQCaptureService(repository=repo).store_failure(
                domain=name,
                failure_type=NO_ENDPOINT,
                request_data={"prompt": prompt},
                mode="sync",
            )

        # Then
        stored = self._only_entry(repo).request_data
        warned = [log for log in logs if log["event"] == "dlq.replay_payload_truncated"]
        if kept:
            assert stored == {"prompt": prompt}
            assert warned == []
        else:
            assert stored["_truncated"] is True
            assert stored["original_size"] == cap + 1
            assert [
                (w["log_level"], w["healing_domain"], w["max_bytes"], w["setting"])
                for w in warned
            ] == [
                (
                    "warning",
                    name,
                    cap,
                    "BALDUR_DLQ_REPLAY_REQUEST_DATA_MAX_BYTES",
                )
            ]

    def test_cut_short_arguments_are_refused_at_replay(self, repo, monkeypatch):
        """Over the replay cap the job cannot come back: the replay refuses, no call."""
        monkeypatch.setenv("BALDUR_DLQ_REPLAY_REQUEST_DATA_MAX_BYTES", "4096")
        reset_dlq_settings()
        name = _job_name()
        job = _ParkingJob(name)
        DLQCaptureService(repository=repo).store_failure(
            domain=name,
            failure_type=NO_ENDPOINT,
            request_data={"prompt": "s" * _LARGE_ARGUMENT_CHARS},
            mode="sync",
        )

        result = _handler(name).replay(self._only_entry(repo))

        assert result.success is False
        assert job.received == []

    def test_domain_without_a_replay_handler_keeps_the_ordinary_cap(
        self, repo, monkeypatch
    ):
        """Nothing replays an unarmed domain, so its forensic cap is unchanged — and quiet."""
        name = _job_name()

        with capture_logs() as logs:
            DLQCaptureService(repository=repo).store_failure(
                domain=name,
                failure_type=NO_ENDPOINT,
                request_data={"prompt": "t" * _LARGE_ARGUMENT_CHARS},
                mode="sync",
            )

        stored = self._only_entry(repo).request_data
        assert stored["_truncated"] is True
        assert [
            log for log in logs if log["event"] == "dlq.replay_payload_truncated"
        ] == []
