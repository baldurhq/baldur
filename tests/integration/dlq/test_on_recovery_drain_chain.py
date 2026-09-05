"""Integration: one circuit close drains the whole backlog it parked.

The unit suites can prove the pieces — the page selector resumes from a cursor,
the fill loop spends its budget, the task re-dispatches while work is reachable
— but not the coupling that makes the chain *correct*, because a mock
repository hands back whatever the test tells it to. The real coupling is:

    the cursor a pass carries forward is only safe because acquisition already
    moved the entries that pass processed out of the selectable set.

Both halves are real here. ``try_acquire_for_replay`` performs the
PENDING→REPLAYING transition against the same store the next pass selects from,
so a pass that carried its cursor one position wrong shows up as an entry
replayed twice or never — not as a mock returning the wrong page.

Driven through the shipped task, so the continuation predicate, the per-pass
circuit affirmation and the cursor hand-off are the production ones; only the
broker hop and the circuit store are stood in for.

Adapters: in-memory and sqlite need no infra; the Redis arm is marked, because
its keyset walk resolves against a live composite index.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.adapters.memory.failed_operation import InMemoryFailedOperationRepository
from baldur.adapters.sql.base import SchemaVersionManager
from baldur.adapters.sql.failed_operation import SQLFailedOperationRepository
from baldur.celery_tasks.dlq_tasks import conditional_replay_on_circuit_close
from baldur.interfaces.governance import GovernanceChecker
from baldur.interfaces.repositories import (
    FailedOperationData,
    FailedOperationStatus,
)
from baldur.models.dlq import OPEN_CIRCUIT_FAILURE_TYPE, POLICY_CHAIN_CAPTURE_SOURCE
from baldur.models.governance import GovernanceCheckResult
from baldur.services.circuit_breaker import CircuitBreakerService
from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.handlers import (
    ReplayHandler,
    _replay_handlers,
    register_replay_handler,
)
from baldur.services.replay_service.models import ReplayResult
from baldur.settings.sql import reset_sql_settings

# The raw ``protect()`` name; the store files its entries under the projection.
CIRCUIT_NAME = "Payment-API"
STORED_DOMAIN = "payment_api"
PENDING = FailedOperationStatus.PENDING.value


class _RecordingHandler(ReplayHandler):
    """A registered handler that records what it was actually handed."""

    def __init__(self, *, succeed: bool = True) -> None:
        self.replayed: list[str] = []
        self._succeed = succeed

    @property
    def domain(self) -> str:
        return STORED_DOMAIN

    def can_replay(self, failed_op: FailedOperationData) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op: FailedOperationData) -> ReplayResult:
        self.replayed.append(failed_op.id)
        if self._succeed:
            return ReplayResult.succeeded(failed_op.id, "done")
        return ReplayResult.failed(failed_op.id, "downstream still refusing")


@pytest.fixture
def handler():
    """Register a real handler and remove exactly it afterwards."""
    created = _RecordingHandler()
    register_replay_handler(created)
    yield created
    _replay_handlers.pop(STORED_DOMAIN, None)


@pytest.fixture(
    params=[
        pytest.param("memory", id="memory"),
        pytest.param("sqlite", id="sqlite"),
        pytest.param("redis", id="redis", marks=pytest.mark.requires_redis),
    ]
)
def dlq(request, monkeypatch):
    """The same drain, over each backing store that implements the walk."""
    if request.param == "memory":
        yield InMemoryFailedOperationRepository()
        return
    if request.param == "redis":
        yield request.getfixturevalue("redis_dlq_repository")
        return

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


def _capture(dlq, count, *, source=POLICY_CHAIN_CAPTURE_SOURCE, domain=STORED_DOMAIN):
    """Park ``count`` open-circuit rejections, as the capture layer would."""
    return [
        dlq.create(
            domain=domain,
            failure_type=OPEN_CIRCUIT_FAILURE_TYPE,
            error_message="circuit open",
            metadata={"source": source},
            max_retries=3,
        ).id
        for _ in range(count)
    ]


def _service(dlq) -> ReplayService:
    """A real sweep over ``dlq``, with governance injected rather than patched.

    Injecting states the same thing whether the private tier is installed or
    not, which is what makes this file's verdict portable.
    """
    service = ReplayService(repository=dlq, cache=InMemoryCacheAdapter())
    service._event_bus = MagicMock(spec=BaldurEventBus)
    service._governance = MagicMock(spec=GovernanceChecker)
    service._governance.check_all_governance.return_value = GovernanceCheckResult(
        allowed=True
    )
    service._governance_resolved = True
    return service


def _drive_chain(service, *, max_items, max_continuations=50, circuit_state="closed"):
    """Run the chain the broker would, one pass at a time.

    The re-dispatch is captured rather than queued, then fed back in as the
    next pass's kwargs — so the continuation counter, the cursors and the
    per-pass affirmation all travel the production path.
    """
    queued: list[dict] = []
    proxy = MagicMock(spec=conditional_replay_on_circuit_close)
    proxy.delay.side_effect = lambda **kwargs: queued.append(kwargs)

    circuits = MagicMock(spec=CircuitBreakerService)
    circuits.repository = MagicMock(spec=[])
    circuits.get_all_states.return_value = [
        {"service_name": CIRCUIT_NAME, "state": circuit_state}
    ]

    passes: list[dict] = []
    kwargs = {
        "service_name": CIRCUIT_NAME,
        "max_items": max_items,
        "max_continuations": max_continuations,
    }
    with (
        patch("baldur.services.get_replay_service", return_value=service),
        patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=circuits,
        ),
        patch(
            "baldur.adapters.celery.tasks.conditional_replay_on_circuit_close", proxy
        ),
    ):
        for _ in range(max_continuations + 2):
            passes.append(
                conditional_replay_on_circuit_close.apply(kwargs=kwargs).get()
            )
            if not queued:
                break
            kwargs = queued.pop(0)
    return passes


def _statuses(dlq, entry_ids) -> list[str]:
    return [dlq.get_by_id(entry_id).status for entry_id in entry_ids]


class TestOnRecoveryDrainChain:
    """A backlog larger than one pass, drained by one circuit close."""

    def test_a_backlog_many_passes_deep_is_drained_exactly_once(self, dlq, handler):
        """The guarantee: the chain is about the backlog, not one budget of it.

        Each entry must be replayed exactly once — twice means a cursor did not
        advance past what acquisition had already claimed, never means it
        advanced over entries the pass left behind.
        """
        captured = _capture(dlq, 25)

        passes = _drive_chain(_service(dlq), max_items=5)

        assert sorted(handler.replayed) == sorted(captured)
        assert len(handler.replayed) == len(set(handler.replayed))
        assert passes[-1]["continued"] is False

    def test_the_drain_ends_with_nothing_left_pending(self, dlq, handler):
        captured = _capture(dlq, 12)

        _drive_chain(_service(dlq), max_items=4)

        assert PENDING not in _statuses(dlq, captured)

    def test_one_pass_moves_no_more_than_its_own_budget(self, dlq, handler):
        """``on_recovery_max_items`` bounds a pass now, not a recovery."""
        _capture(dlq, 25)

        passes = _drive_chain(_service(dlq), max_items=5)

        assert all(single["total"] <= 5 for single in passes)
        assert sum(single["total"] for single in passes) == 25

    def test_the_chain_continues_only_while_work_is_reachable(self, dlq, handler):
        """A backlog that fits in one pass gets one extra cursor-cleared pass
        and then stops — not a pass per continuation budget."""
        _capture(dlq, 3)

        passes = _drive_chain(_service(dlq), max_items=10, max_continuations=50)

        assert len(passes) <= 2
        assert passes[-1]["continued"] is False

    def test_a_capture_from_another_layer_in_the_same_domain_is_left_alone(
        self, dlq, handler
    ):
        """A request-boundary layer files the same failure type under a
        path-inferred domain that may name a different, still-dead circuit."""
        policy_chain = _capture(dlq, 6)
        middleware = _capture(dlq, 4, source="middleware")

        _drive_chain(_service(dlq), max_items=3)

        assert sorted(handler.replayed) == sorted(policy_chain)
        assert _statuses(dlq, middleware) == [PENDING] * len(middleware)

    def test_a_failing_replay_escalates_and_the_chain_still_terminates(self, dlq):
        """Escalation empties the pending pool without draining it, so a chain
        that keyed on "the pool shrank" would run forever here."""
        failing = _RecordingHandler(succeed=False)
        register_replay_handler(failing)
        try:
            captured = _capture(dlq, 10)

            passes = _drive_chain(_service(dlq), max_items=3, max_continuations=20)

            assert len(failing.replayed) == len(captured)
            assert passes[-1]["continued"] is False
            assert set(_statuses(dlq, captured)) == {
                FailedOperationStatus.REQUIRES_REVIEW.value
            }
        finally:
            _replay_handlers.pop(STORED_DOMAIN, None)

    def test_the_bound_stops_a_chain_that_would_otherwise_keep_going(
        self, dlq, handler
    ):
        """The bound is what makes an unbounded self-dispatch safe to ship."""
        _capture(dlq, 40)

        passes = _drive_chain(_service(dlq), max_items=5, max_continuations=3)

        assert len(passes) == 3
        assert passes[-1]["continued"] is False
        assert len(handler.replayed) == 15

    def test_a_circuit_that_reopened_stops_the_chain_before_the_next_pass(
        self, dlq, handler
    ):
        """Walking a peer's backlog into a dependency that is still down would
        escalate every entry on its first failure."""
        captured = _capture(dlq, 20)

        passes = _drive_chain(_service(dlq), max_items=5, circuit_state="open")

        assert handler.replayed == []
        assert passes[0]["block_reason"] == "circuit_reopened"
        assert _statuses(dlq, captured) == [PENDING] * len(captured)

    def test_the_projected_domain_is_what_the_join_uses(self, dlq, handler):
        """``Payment-API`` is stored as ``payment_api``; a join that re-derived
        the projection by hand would search for a name nothing was filed under."""
        captured = _capture(dlq, 4)

        _drive_chain(_service(dlq), max_items=10)

        assert sorted(handler.replayed) == sorted(captured)


class TestOnRecoveryDeadlineHandoff:
    """A pass cut short must hand the tail to its successor, not step over it."""

    def test_a_deadline_stop_leaves_the_tail_pending_for_the_next_pass(
        self, dlq, handler
    ):
        """Acquisition happens per entry inside the replay, so everything past
        the stop is still PENDING at a position BELOW the cursor the pass would
        otherwise carry forward."""
        captured = _capture(dlq, 10)
        service = _service(dlq)
        clock = _Clock()
        original_replay = service._execute_replay

        def _slow_replay(entry_id, **kwargs):
            clock.advance(1.0)
            return original_replay(entry_id, **kwargs)

        service._execute_replay = _slow_replay

        with patch("baldur.services.replay_service.service.time.monotonic", new=clock):
            first = service.replay_on_circuit_close(
                service_name=CIRCUIT_NAME,
                max_items=10,
                deadline=clock.now + 3.5,
            )

        assert first.capped is True
        assert first.total == 4
        assert _statuses(dlq, captured).count(PENDING) == 6

        second = service.replay_on_circuit_close(
            service_name=CIRCUIT_NAME,
            max_items=10,
            lane_cursors=first.lane_cursors,
            continuation=1,
        )

        assert second.total == 6
        assert sorted(handler.replayed) == sorted(captured)
        assert len(handler.replayed) == len(set(handler.replayed))


class _Clock:
    """Monotonic stand-in that only advances when the replay does."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
