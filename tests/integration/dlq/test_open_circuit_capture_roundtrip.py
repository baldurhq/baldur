"""Integration: an OPEN circuit's rejections are parked and replayed, without PRO.

This is the guarantee itself, driven end to end on a pure-OSS install: a call
``protect(dlq=True, circuit_breaker=True)`` makes while the breaker is OPEN is
rejected in microseconds, and the work it carried reaches the DLQ repository
anyway — then the circuit-close sweep finds it again.

The join between those two halves is the stored domain, and only the store
decides what that is: ``Payment-API`` fails plain validation outright yet
canonicalizes to ``payment_api``, so a reader that re-derives the projection by
hand either raises or searches for a name nothing was stored under. Neither
half's unit test can see that seam — this file is where it is provable.

Mock-based (no infra): the in-memory repository is injected through the capture
service's DI seam with both PRO registry slots empty, and the outbox drains
into that same instance.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.memory.failed_operation import InMemoryFailedOperationRepository
from baldur.audit.ring_buffer import RingBuffer
from baldur.interfaces.governance import GovernanceChecker
from baldur.interfaces.repositories import FailedOperationData
from baldur.models.dlq import OPEN_CIRCUIT_FAILURE_TYPE, POLICY_CHAIN_CAPTURE_SOURCE
from baldur.models.governance import GovernanceCheckResult
from baldur.protect_facade import aprotect, protect
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError
from baldur.services.dlq_outbox import outbox as outbox_module
from baldur.services.dlq_outbox.outbox import Outbox
from baldur.services.dlq_outbox.worker import DLQOutboxWorker
from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.handlers import (
    ReplayHandler,
    _replay_handlers,
    register_replay_handler,
)
from baldur.services.replay_service.models import ReplayResult
from baldur.services.retry_handler import sinks as baldur_sinks
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.settings.backpressure import BackpressureStrategy
from baldur.utils.domain_validation import resolve_stored_domain

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def repository() -> InMemoryFailedOperationRepository:
    """A fresh in-memory DLQ repository — the instance both halves share."""
    return InMemoryFailedOperationRepository()


@pytest.fixture
def oss_backing(monkeypatch, repository) -> Iterator[InMemoryFailedOperationRepository]:
    """Wire the repository behind the canonical chain with both slots empty.

    Simulates a pure-OSS install: ``resolve_dlq_backing()`` misses the PRO
    ``dlq_service`` slot and falls through to the OSS capture singleton, which
    is replaced here by one holding the test repository.
    """
    from baldur.factory.registry import ProviderRegistry
    from baldur.services.dlq_capture import service as capture_module
    from baldur.services.dlq_capture.service import (
        DLQCaptureService,
        reset_dlq_capture_service,
    )

    monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: None)
    monkeypatch.setattr(ProviderRegistry.dlq_repository, "safe_get", lambda: None)
    monkeypatch.setattr(
        capture_module,
        "_capture_service",
        DLQCaptureService(repository=repository),
    )
    yield repository
    reset_dlq_capture_service()


@pytest.fixture
def started_outbox(oss_backing) -> Iterator[Outbox]:
    """A real outbox draining into the OSS capture backing.

    The sink stores without a ``mode``, which resolves to the async outbox by
    default — the production path — so the drain has to be real for the entry
    to reach the repository at all.
    """
    from baldur.services.dlq_capture.service import resolve_dlq_backing

    def sync_writer(kwargs: dict) -> object:
        return resolve_dlq_backing().store_failure(mode="sync", **kwargs)

    buffer: RingBuffer = RingBuffer(
        capacity=100, strategy=BackpressureStrategy.DROP_OLDEST
    )
    # batch_size=1 makes the drain deterministic: every popped batch flushes.
    worker = DLQOutboxWorker(
        buffer=buffer,
        sync_writer=sync_writer,
        batch_size=1,
        flush_interval_seconds=0.01,
    )
    outbox = Outbox(buffer=buffer, worker=worker)
    outbox.start()
    outbox_module._outbox = outbox
    outbox_module._worker_dead = False

    yield outbox

    try:
        outbox.stop(timeout=1.0)
    except Exception:
        pass
    outbox_module._outbox = None
    outbox_module._worker_dead = False
    outbox_module._worker_dead_coercions = 0


@pytest.fixture
def open_circuit() -> Iterator[object]:
    """Force named circuits OPEN for the test and close them afterwards."""
    from baldur.services.circuit_breaker.convenience import (
        force_close_circuit,
        force_open_circuit,
    )

    opened: list[str] = []

    def _open(service_name: str) -> str:
        force_open_circuit(service_name, reason="integration test")
        opened.append(service_name)
        return service_name

    yield _open

    for service_name in opened:
        try:
            force_close_circuit(service_name, reason="integration test teardown")
        except Exception:
            pass


class _CollectingReplayHandler(ReplayHandler):
    """Replay handler that records what the sweep handed it."""

    def __init__(self, domain: str) -> None:
        self._domain = domain
        self.replayed: list[FailedOperationData] = []

    @property
    def domain(self) -> str:
        return self._domain

    def can_replay(self, failed_op: FailedOperationData) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op: FailedOperationData) -> ReplayResult:
        self.replayed.append(failed_op)
        return ReplayResult.succeeded(failed_op.id, "replayed")


@pytest.fixture
def replay_handler_for() -> Iterator[object]:
    """Register replay handlers for the test and remove exactly those after."""
    created: list[_CollectingReplayHandler] = []

    def _register(domain: str) -> _CollectingReplayHandler:
        handler = _CollectingReplayHandler(domain)
        register_replay_handler(handler)
        created.append(handler)
        return handler

    yield _register

    for handler in created:
        _replay_handlers.pop(handler.domain, None)


def _retry_cfg(domain: str) -> RetryPolicyConfig:
    return RetryPolicyConfig(
        max_attempts=2,
        backoff_base=0,
        backoff_max=0,
        jitter_percent=0,
        enable_dlq=True,
        domain=domain,
    )


def _wait_for_repo_count(
    repo: InMemoryFailedOperationRepository, expected: int, timeout: float = 3.0
) -> None:
    """Block until the worker has drained ``expected`` entries into ``repo``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and repo.count_all() < expected:
        time.sleep(0.02)


def _sweeping_service(repo: InMemoryFailedOperationRepository) -> ReplayService:
    """ReplayService over the same repository, with governance allowed."""
    svc = ReplayService(repository=repo)
    svc._event_bus = MagicMock(spec=BaldurEventBus)
    svc._governance = MagicMock(spec=GovernanceChecker)
    svc._governance.check_all_governance.return_value = GovernanceCheckResult(
        allowed=True
    )
    svc._governance_resolved = True
    return svc


# =============================================================================
# Capture — the rejected call reaches the repository
# =============================================================================


class TestOpenCircuitCaptureWithoutPro:
    """A pure-OSS install parks what the breaker rejects."""

    def test_rejected_call_is_parked_and_the_caller_still_sees_the_rejection(
        self, oss_backing, started_outbox, open_circuit
    ):
        service_name = open_circuit("oss_open_charge")
        ran: list[int] = []

        with pytest.raises(CircuitBreakerOpenError):
            protect(
                service_name,
                lambda: ran.append(1),
                dlq=True,
                retry=_retry_cfg(service_name),
                circuit_breaker=True,
                timeout=None,
            )

        # The breaker rejected before the call ran, and the work was parked.
        assert ran == []
        _wait_for_repo_count(oss_backing, 1)
        assert oss_backing.count_all() == 1

        entry = oss_backing.get_pending_by_domain(service_name, limit=10)[0]
        assert entry.domain == service_name
        assert entry.failure_type == OPEN_CIRCUIT_FAILURE_TYPE
        assert (entry.metadata or {}).get("source") == POLICY_CHAIN_CAPTURE_SOURCE
        assert entry.recommended_action == "replay"

    def test_capture_disabled_parks_nothing(
        self, oss_backing, started_outbox, open_circuit, monkeypatch
    ):
        """Negative half through the whole chain, not just at the sink."""
        from baldur.settings.dlq import reset_dlq_settings

        monkeypatch.setenv("BALDUR_DLQ_OPEN_CIRCUIT_CAPTURE_ENABLED", "false")
        reset_dlq_settings()
        service_name = open_circuit("oss_open_disabled")
        try:
            with pytest.raises(CircuitBreakerOpenError):
                protect(
                    service_name,
                    lambda: None,
                    dlq=True,
                    retry=_retry_cfg(service_name),
                    circuit_breaker=True,
                    timeout=None,
                )

            _wait_for_repo_count(oss_backing, 1, timeout=0.3)
            assert oss_backing.count_all() == 0
        finally:
            monkeypatch.delenv("BALDUR_DLQ_OPEN_CIRCUIT_CAPTURE_ENABLED")
            reset_dlq_settings()

    def test_dlq_false_parks_nothing(self, oss_backing, started_outbox, open_circuit):
        """`dlq=True` stays the per-call-site opt-in — an unarmed composer's
        rejection reaches no sink at all."""
        service_name = open_circuit("oss_open_nodlq")

        with pytest.raises(CircuitBreakerOpenError):
            protect(
                service_name,
                lambda: None,
                dlq=False,
                retry=_retry_cfg(service_name),
                circuit_breaker=True,
                timeout=None,
            )

        _wait_for_repo_count(oss_backing, 1, timeout=0.3)
        assert oss_backing.count_all() == 0


# =============================================================================
# Recovery — the sweep finds what the store filed
# =============================================================================


class TestOpenCircuitRecoveryWithoutPro:
    """The circuit-close sweep reaches the entries the capture created."""

    def test_reprojected_name_round_trips_from_capture_to_replay(
        self, oss_backing, started_outbox, open_circuit, replay_handler_for
    ):
        """The derivation, end to end.

        The protect name is ``Payment-API``; the store files the entry under
        ``payment_api``; the closing circuit still finds it, with no
        failure-type map configured anywhere — which is what a plain
        ``dlq=True`` deployment has.
        """
        protect_name = open_circuit("Payment-API")
        stored_domain = resolve_stored_domain(protect_name)
        assert stored_domain == "payment_api"
        handler = replay_handler_for(stored_domain)

        with pytest.raises(CircuitBreakerOpenError):
            protect(
                protect_name,
                lambda: None,
                dlq=True,
                retry=_retry_cfg(protect_name),
                circuit_breaker=True,
                timeout=None,
            )

        _wait_for_repo_count(oss_backing, 1)
        assert oss_backing.count_by_domain(stored_domain) == 1

        result = _sweeping_service(oss_backing).replay_on_circuit_close(
            service_name=protect_name, service_failure_type_map={}
        )

        assert result.success_count == 1
        assert len(handler.replayed) == 1
        assert handler.replayed[0].failure_type == OPEN_CIRCUIT_FAILURE_TYPE

    def test_a_still_open_service_entries_keep_their_budget(
        self, oss_backing, started_outbox, open_circuit, replay_handler_for
    ):
        """Two dependencies fail; one recovers. The other's parked work must
        not be driven back into it and spend its replay budget."""
        recovered = open_circuit("oss_recovered")
        still_open = open_circuit("oss_still_open")
        handler = replay_handler_for(recovered)
        replay_handler_for(still_open)

        for name in (recovered, still_open):
            with pytest.raises(CircuitBreakerOpenError):
                protect(
                    name,
                    lambda: None,
                    dlq=True,
                    retry=_retry_cfg(name),
                    circuit_breaker=True,
                    timeout=None,
                )

        _wait_for_repo_count(oss_backing, 2)

        _sweeping_service(oss_backing).replay_on_circuit_close(
            service_name=recovered, service_failure_type_map={}
        )

        assert [entry.domain for entry in handler.replayed] == [recovered]
        stranded = oss_backing.get_pending_by_domain(still_open, limit=10)
        assert len(stranded) == 1
        assert stranded[0].retry_count == 0

    def test_without_a_registered_handler_the_entry_waits_for_an_operator(
        self, oss_backing, started_outbox, open_circuit
    ):
        """The automatic lane calls ``replay()`` without consulting
        ``can_replay``, so an unregistered domain would spend every entry's
        budget on a handler that always fails. It must not run."""
        service_name = open_circuit("oss_no_handler")

        with pytest.raises(CircuitBreakerOpenError):
            protect(
                service_name,
                lambda: None,
                dlq=True,
                retry=_retry_cfg(service_name),
                circuit_breaker=True,
                timeout=None,
            )

        _wait_for_repo_count(oss_backing, 1)

        result = _sweeping_service(oss_backing).replay_on_circuit_close(
            service_name=service_name, service_failure_type_map={}
        )

        assert result.total == 0
        pending = oss_backing.get_pending_by_domain(service_name, limit=10)
        assert len(pending) == 1
        assert pending[0].retry_count == 0


# =============================================================================
# ASGI — the loop keeps serving while a rejection is captured
# =============================================================================


class TestAsyncOpenCircuitCaptureWithoutPro:
    """The async composer's capture must not stall the event loop."""

    def test_the_event_loop_serves_a_concurrent_request_during_capture(
        self, oss_backing, started_outbox, open_circuit
    ):
        """A blocking store on the loop thread would stall every other request
        during exactly the incident the queue exists for. The rejection travels
        the composer's normalized sink channel, so the store runs on the
        offload thread and the loop keeps making progress.

        The assertion is progress made DURING the store, not a wall-clock
        bound: the concurrent ticker must accumulate ticks while the capture is
        still in flight, and a blocked loop admits at most the first one.
        """
        service_name = open_circuit("oss_async_gw")
        store_duration = 0.4
        real_store = baldur_sinks.store_to_dlq

        def slow_store(**kwargs):
            time.sleep(store_duration)
            return real_store(**kwargs)

        ticks: list[int] = []

        async def ticker() -> None:
            deadline = time.monotonic() + store_duration
            while time.monotonic() < deadline:
                ticks.append(1)
                await asyncio.sleep(0.01)

        async def rejected() -> None:
            async def never_runs() -> None:
                return None

            with pytest.raises(CircuitBreakerOpenError):
                await aprotect(
                    service_name,
                    never_runs,
                    dlq=True,
                    retry=_retry_cfg(service_name),
                    circuit_breaker=True,
                    timeout=None,
                )

        async def _run() -> None:
            await asyncio.gather(rejected(), ticker())

        with patch.object(baldur_sinks, "store_to_dlq", slow_store):
            asyncio.run(_run())

        assert len(ticks) > 5
        _wait_for_repo_count(oss_backing, 1)
        assert oss_backing.count_by_domain(service_name) == 1
