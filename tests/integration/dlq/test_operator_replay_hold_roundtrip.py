"""Integration: a console retry of a job still running holds its entry.

A parked ``@protected(..., replay=True, timeout=...)`` charge is retried from
the console while its dependency hangs. The pieces under test meet across a
thread boundary: ``retry_entry`` acquires the entry and runs the operator
replay inside a work scope; the function-replay handler re-runs the charge
through ``protect``; the timeout stage — on its own timeout, or because a soft
time limit cut its wait short — stops waiting while the charge runs on the
shared timeout executor and records it into that scope; the scope still holding
at close leaves the entry REPLAYING instead of returning it to the queue.

Test Categories:
    A. Console retry of a charge cut off while it runs (in-memory and SQLite):
        - its own timeout fires: the response says ``replaying``; the entry
          stays REPLAYING after the charge ends; a second retry is refused
        - a soft time limit cuts the wait: the same hold
        - the charge fails fast with nothing left running: back to PENDING

Note: in-memory and SQLite stores — no Docker. The job is parked for real
through ``protect``'s DLQ capture (synchronous stores, outbox off).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.core.exceptions import DLQStateConflictError
from baldur.interfaces.repositories import FailedOperationStatus
from baldur.models.dlq import DLQConfig
from baldur.protect_facade import protected, reset_protect_caches
from baldur.services.dlq_capture.service import DLQCaptureService
from baldur.services.dlq_read import DLQReadService
from baldur.services.replay_service.handlers import _replay_handlers
from baldur.settings.dlq_outbox import reset_dlq_outbox_settings
from baldur.settings.protect import reset_protect_settings
from tests.factories.interruptions import (
    SoftTimeLimitExceeded,
    interrupted_timeout_wait,
)

PENDING = FailedOperationStatus.PENDING.value
REPLAYING = FailedOperationStatus.REPLAYING.value

_RESOLVE_BACKING = "baldur.services.dlq_capture.resolve_dlq_backing"
# The job's own timeout: fires while the charge is already running (an idle
# shared-executor worker picks it up long before).
_RUNNING_TIMEOUT_S = 1.0
_HOLD_S = 5.0


@pytest.fixture(autouse=True)
def sandbox(monkeypatch) -> Iterator[None]:
    """Synchronous DLQ stores; the replay handler registry restored after."""
    before = dict(_replay_handlers)
    monkeypatch.setenv("BALDUR_DLQ_OUTBOX_ENABLED", "false")
    reset_dlq_outbox_settings()
    reset_protect_settings()
    reset_protect_caches()
    yield
    _replay_handlers.clear()
    _replay_handlers.update(before)
    reset_protect_settings()
    reset_protect_caches()
    reset_dlq_outbox_settings()


@pytest.fixture(params=["memory", "sqlite"], ids=["memory", "sqlite"])
def repo(request):
    """The DLQ the charge is parked in, over each store."""
    if request.param == "memory":
        return InMemoryFailedOperationRepository()
    return request.getfixturevalue("sqlite_repo")


class _Dependency:
    """The charge's dependency: down (fails at once) or hanging until released."""

    def __init__(self) -> None:
        self.mode = "down"
        self.entered = threading.Event()
        self.release = threading.Event()
        self.charged: list[str] = []
        self.finished = threading.Event()

    def call(self, order_id: str) -> str:
        if self.mode == "down":
            raise ConnectionError("gateway down")
        self.entered.set()
        try:
            self.release.wait(_HOLD_S)
            self.charged.append(order_id)
            return order_id
        finally:
            self.finished.set()


@pytest.fixture
def dependency() -> Iterator[_Dependency]:
    made = _Dependency()
    yield made
    made.release.set()
    made.finished.wait(_HOLD_S)


def _parked_charge(repo, dependency: _Dependency):
    """Arm the replay=True charge and park one call of it for real."""

    @protected(
        "payment.parked",
        replay=True,
        retry=False,
        circuit_breaker=False,
        timeout=_RUNNING_TIMEOUT_S,
    )
    def charge(order_id: str) -> str:
        return dependency.call(order_id)

    with patch(_RESOLVE_BACKING, return_value=DLQCaptureService(repository=repo)):
        with pytest.raises(ConnectionError):
            charge("o-1")
    (entry,) = repo.find(domain="payment.parked")
    return entry


def _console(repo) -> DLQReadService:
    service = DLQReadService(
        config=DLQConfig(enabled=True, max_replay_attempts=3), repository=repo
    )
    service._log_dlq_audit = lambda **kwargs: None  # type: ignore[method-assign]
    return service


class TestConsoleRetryOfChargeStillRunning:
    """A console retry whose charge was cut off while it ran holds the entry."""

    def test_own_timeout_holds_entry_replaying_until_the_stale_release(
        self, repo, dependency
    ):
        """
        Purpose:
            Verify that a console retry of a parked charge whose own
            ``timeout=`` cuts it off while it runs leaves the entry REPLAYING,
            and that nothing — the charge ending, or a second retry — hands it
            back to the queue.
        Expected:
            - the retry answers ``success=False`` with status ``replaying``
            - the entry stays REPLAYING after the charge finishes
            - a second retry is refused (409), so the charge never runs twice
        """
        # Given — a parked charge whose dependency now hangs.
        entry = _parked_charge(repo, dependency)
        console = _console(repo)
        dependency.mode = "hang"

        # When — the console retries; the charge outlives its timeout.
        result = console.retry_entry(entry.id)
        held_while_running = repo.get_by_id(entry.id).status
        dependency.release.set()
        assert dependency.finished.wait(_HOLD_S)

        # Then
        assert result["success"] is False
        assert result["status"] == REPLAYING
        assert held_while_running == REPLAYING
        assert repo.get_by_id(entry.id).status == REPLAYING
        with pytest.raises(DLQStateConflictError):
            console.retry_entry(entry.id)
        assert dependency.charged == ["o-1"]

    def test_soft_time_limit_cut_holds_entry_replaying(self, repo, dependency):
        """
        Purpose:
            Verify that a soft time limit cutting the charge's wait short
            during a console retry holds the entry like its own timeout.
        Expected:
            - the retry answers status ``replaying``
            - the entry stays REPLAYING while and after the charge runs
        """
        # Given
        entry = _parked_charge(repo, dependency)
        console = _console(repo)
        dependency.mode = "hang"

        # When — the wait is cut while the charge runs.
        with interrupted_timeout_wait(
            SoftTimeLimitExceeded(), entered=dependency.entered
        ):
            result = console.retry_entry(entry.id)
        held_while_running = repo.get_by_id(entry.id).status
        dependency.release.set()
        assert dependency.finished.wait(_HOLD_S)

        # Then
        assert result["status"] == REPLAYING
        assert held_while_running == REPLAYING
        assert repo.get_by_id(entry.id).status == REPLAYING

    def test_fast_failure_with_nothing_running_returns_entry_to_pending(
        self, repo, dependency
    ):
        """
        Purpose:
            Verify the negative: a retry whose charge failed with nothing left
            running completes the entry as before.
        Expected:
            - the retry answers status ``pending``; the entry is PENDING
        """
        entry = _parked_charge(repo, dependency)
        console = _console(repo)

        result = console.retry_entry(entry.id)

        assert result["status"] == PENDING
        assert repo.get_by_id(entry.id).status == PENDING
