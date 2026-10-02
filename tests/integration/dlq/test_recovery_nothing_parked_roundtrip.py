"""Integration: a recovery is quiet exactly when nothing is parked under its name.

A breaker with no replay lane — every LLM endpoint ``baldur.llm.wrap`` calls,
which runs each endpoint with ``dlq=False`` — used to report "replay blocked"
on every recovery. The fix decides from a count of what is parked under the
breaker's stored domain, so its correctness is a property of the seam between
two halves no unit test holds together:

    what ``protect(..., dlq=True)`` stores (through the stored-domain
    projection and the outbox drain) is exactly what the recovery counts.

If the two disagreed — a capture filed under a spelling the count never asks
for — a recovery that leaves work behind would go quiet. Both halves are real
here: the capture chain parks through the shipped outbox into an in-memory
repository, and the recovery pass and the CLOSED handler read that same
instance. Only the broker hop and the circuit read the pass affirms against are
stood in for.

Test Categories:
    A. The recovery pass:
        - an LLM endpoint that parked nothing ends idle, beside a job's work
        - a name with a parked entry and no handler reports it, pending=1
    B. The CLOSED handler without Celery:
        - nothing parked under the name: DEBUG, no run-a-worker WARNING
        - an entry parked under the name: the WARNING

Note: All tests use the in-memory repository - no infra dependency.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.adapters.memory.failed_operation import InMemoryFailedOperationRepository
from baldur.celery_tasks.dlq_tasks import conditional_replay_on_circuit_close
from baldur.interfaces.repositories import FailedOperationStatus
from baldur.protect_facade import protect
from baldur.services.circuit_breaker import CircuitBreakerService
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError
from baldur.services.event_bus import BaldurEvent
from baldur.services.event_bus.bus._cb_handlers import _on_circuit_breaker_closed
from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.arming import reset_dispatch_ledger
from baldur.services.replay_service.service import REASON_NO_REPLAY_HANDLER
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.utils.domain_validation import resolve_stored_domain

# An LLM endpoint identity, shaped the way the wrap derives one.
LLM_ENDPOINT = "llm.api_example_com.gpt_4o"
# The job that called it: its work is parked under its own name.
JOB = "summarize_job"
# A raw name the store files under its canonical spelling (billing_sync), with
# no replay handler registered anywhere in this process.
UNHANDLED = "Billing-Sync"

_CELERY_TASKS_MODULE = "baldur.adapters.celery.tasks"


# =============================================================================
# Helpers
# =============================================================================


@pytest.fixture(autouse=True)
def _pristine_dispatch_ledger() -> Iterator[None]:
    """The CLOSED handler writes the process-global arming ledger."""
    reset_dispatch_ledger()
    yield
    reset_dispatch_ledger()


def _reject(name: str, *, dlq: bool) -> None:
    """One call the OPEN breaker rejects; ``dlq`` decides whether it parks."""
    with pytest.raises(CircuitBreakerOpenError):
        protect(
            name,
            lambda: None,
            dlq=dlq,
            retry=RetryPolicyConfig(
                max_attempts=2,
                backoff_base=0,
                backoff_max=0,
                jitter_percent=0,
                enable_dlq=dlq,
                domain=name,
            ),
            circuit_breaker=True,
            timeout=None,
        )


def _wait_for_parked(
    repo: InMemoryFailedOperationRepository, expected: int, timeout: float = 3.0
) -> None:
    """Block until the outbox worker has drained ``expected`` entries."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and repo.count_all() < expected:
        time.sleep(0.02)
    assert repo.count_all() == expected


def _replay_service(repo: InMemoryFailedOperationRepository) -> ReplayService:
    """The replay side, over the repository the capture chain wrote."""
    service = ReplayService(repository=repo, cache=InMemoryCacheAdapter())
    service._event_bus = MagicMock(spec=BaldurEventBus)
    return service


def _run_recovery_pass(service: ReplayService, name: str):
    """Run the shipped recovery task once, as the broker would."""
    circuits = MagicMock(spec=CircuitBreakerService)
    circuits.repository = MagicMock(spec=[])
    circuits.get_all_states.return_value = [{"service_name": name, "state": "closed"}]
    with (
        patch("baldur.services.get_replay_service", return_value=service),
        patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=circuits,
        ),
        patch(
            "baldur.adapters.celery.tasks.conditional_replay_on_circuit_close",
            MagicMock(spec=conditional_replay_on_circuit_close),
        ),
        patch(
            "baldur.services.replay_service.service.log_dlq_replay_blocked_audit",
            autospec=True,
        ) as audit,
        capture_logs() as logs,
    ):
        result = conditional_replay_on_circuit_close.apply(
            kwargs={"service_name": name, "max_items": 50, "max_continuations": 10}
        ).get()
    return result, logs, audit


def _blocked_events(service: ReplayService) -> list[dict]:
    return [
        c.kwargs["data"]
        for c in service._event_bus.emit.call_args_list
        if c.args[0] == EventType.DLQ_REPLAY_BLOCKED
    ]


def _closed_event(name: str) -> BaldurEvent:
    return BaldurEvent(
        event_type=EventType.CIRCUIT_BREAKER_CLOSED,
        data={"service_name": name, "previous_state": "half_open", "trigger": "auto"},
        source="circuit_breaker_service",
    )


def _close_without_celery(service: ReplayService, name: str) -> list[dict]:
    """Deliver the CLOSED event on an install without the Celery extra."""
    with (
        patch(
            "baldur.services.replay_service.get_replay_service", return_value=service
        ),
        patch.dict("sys.modules", {_CELERY_TASKS_MODULE: None}),
        capture_logs() as logs,
    ):
        _on_circuit_breaker_closed(_closed_event(name))
    return logs


def _warnings(logs: list[dict]) -> list[dict]:
    return [e for e in logs if e["log_level"] not in ("debug", "info")]


# =============================================================================
# A. The recovery pass
# =============================================================================


class TestRecoveryPassNothingParkedRoundtrip:
    """The pass decides from what the capture chain actually stored."""

    def test_llm_endpoint_that_parked_nothing_ends_idle_beside_its_jobs_work(
        self, oss_backing, started_outbox, open_circuit
    ):
        """
        Purpose:
            An endpoint called with dlq=False parks nothing under its own
            name; the job that called it parks under the job's name. The
            endpoint's recovery must be quiet without touching the job's work.
        Expected:
            - the pass returns nothing_parked and logs nothing above INFO
            - no DLQ_REPLAY_BLOCKED event and no blocked audit
            - the job's entry is still pending
        """
        # Given
        open_circuit(JOB)
        open_circuit(LLM_ENDPOINT)
        _reject(JOB, dlq=True)
        _reject(LLM_ENDPOINT, dlq=False)
        _wait_for_parked(oss_backing, 1)
        service = _replay_service(oss_backing)

        # When
        result, logs, audit = _run_recovery_pass(service, LLM_ENDPOINT)

        # Then
        assert result["nothing_parked"] is True
        assert _warnings(logs) == []
        assert _blocked_events(service) == []
        audit.assert_not_called()
        job_entries = oss_backing.get_pending_by_domain(
            resolve_stored_domain(JOB), limit=10
        )
        assert [e.status for e in job_entries] == [FailedOperationStatus.PENDING.value]

    def test_name_with_a_parked_entry_and_no_handler_reports_it_on_recovery(
        self, oss_backing, started_outbox, open_circuit
    ):
        """
        Purpose:
            The count must find what the capture filed under the projected
            spelling, so a recovery that leaves it behind stays loud.
        Expected:
            - the pass runs (no nothing_parked)
            - WARNING circuit_close_replay_blocked naming the missing handler,
              for the stored domain, pending=1
            - the DLQ_REPLAY_BLOCKED event carries the same count
        """
        # Given
        open_circuit(UNHANDLED)
        _reject(UNHANDLED, dlq=True)
        _wait_for_parked(oss_backing, 1)
        service = _replay_service(oss_backing)

        # When
        result, logs, _ = _run_recovery_pass(service, UNHANDLED)

        # Then
        assert "nothing_parked" not in result
        blocked = [
            e
            for e in logs
            if e["event"] == "replay_service.circuit_close_replay_blocked"
        ]
        assert len(blocked) == 1
        assert blocked[0]["log_level"] == "warning"
        assert blocked[0]["block_reason"] == REASON_NO_REPLAY_HANDLER
        assert blocked[0]["healing_domain"] == resolve_stored_domain(UNHANDLED)
        assert blocked[0]["pending"] == 1
        assert [event["pending"] for event in _blocked_events(service)] == [1]


# =============================================================================
# B. The CLOSED handler without Celery
# =============================================================================


class TestCeleryMissingRecoveryRoundtrip:
    """Without a worker, only a recovery that left work behind says so."""

    def test_recovery_with_nothing_parked_is_not_told_to_run_a_worker(
        self, oss_backing, started_outbox, open_circuit
    ):
        """
        Purpose:
            The endpoint parked nothing; a job's entry elsewhere in the store
            must not make the endpoint's recovery ask for a worker.
        Expected:
            - DEBUG replay_dispatch_skipped with nothing_parked
            - no replay_dispatch_blocked WARNING
        """
        open_circuit(JOB)
        open_circuit(LLM_ENDPOINT)
        _reject(JOB, dlq=True)
        _reject(LLM_ENDPOINT, dlq=False)
        _wait_for_parked(oss_backing, 1)

        logs = _close_without_celery(_replay_service(oss_backing), LLM_ENDPOINT)

        skipped = [
            e for e in logs if e["event"] == "event_handler.replay_dispatch_skipped"
        ]
        assert len(skipped) == 1
        assert skipped[0]["nothing_parked"] is True
        assert [
            e for e in logs if e["event"] == "event_handler.replay_dispatch_blocked"
        ] == []

    def test_recovery_that_left_work_parked_is_told_to_run_a_worker(
        self, oss_backing, started_outbox, open_circuit
    ):
        """
        Purpose:
            Work parked under the closing name waits for a worker that does
            not exist; that recovery keeps its WARNING.
        Expected:
            - one WARNING replay_dispatch_blocked with reason celery_missing
        """
        open_circuit(UNHANDLED)
        _reject(UNHANDLED, dlq=True)
        _wait_for_parked(oss_backing, 1)

        logs = _close_without_celery(_replay_service(oss_backing), UNHANDLED)

        blocked = [
            e for e in logs if e["event"] == "event_handler.replay_dispatch_blocked"
        ]
        assert len(blocked) == 1
        assert blocked[0]["log_level"] == "warning"
        assert blocked[0]["reason"] == "celery_missing"
