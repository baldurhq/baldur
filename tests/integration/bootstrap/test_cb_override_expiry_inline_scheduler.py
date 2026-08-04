"""741 D2 — a Celery-less install still lifts a manual block.

The only consumer of the expiry sweep used to be a Celery beat task, so on
Flask / FastAPI / plain-Python deployments — the majority shape — a manual
Block never cleared. ``_start_default_scheduler`` now registers
``cb_override_expiry`` on the inline scheduler alongside ``cb_recovery``.

This exercises the whole chain the fix depends on — default job list ->
tier filter -> synthetic-callable resolution -> ``get_circuit_breaker_service``
-> ``check_and_expire_manual_overrides`` -> repository — against a real
``LeaderScheduler`` and a real circuit breaker service, with no Celery task
module involved. A unit test on any single link cannot show that: the
registration table, the resolver and the service are three separate modules
that only meet here.

Mock-based: in-memory repository, no infra. The scheduler's own loop is not
started (its cadence is LeaderScheduler's contract, tested separately) — the
registered job is invoked directly.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.bootstrap import _start_default_scheduler
from baldur.coordination.scheduler import (
    DEFAULT_SCHEDULER_RESOURCE,
    LeaderScheduler,
    get_leader_scheduler,
    reset_schedulers,
)
from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.convenience import (
    configure_circuit_breaker_service,
    reset_circuit_breaker_service,
)
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.utils.time import utc_now

SERVICE = "payment-api"
JOB_NAME = "cb_override_expiry"


@pytest.fixture
def cb_repository() -> InMemoryCircuitBreakerStateRepository:
    """The repository the singleton circuit breaker service will write to."""
    repository = InMemoryCircuitBreakerStateRepository()
    service = CircuitBreakerService(
        config=CircuitBreakerConfig(enabled=True),
        repository=repository,
    )
    configure_circuit_breaker_service(service)
    try:
        yield repository
    finally:
        reset_circuit_breaker_service()


@pytest.fixture
def inline_scheduler(monkeypatch):
    """Register the default jobs on a real scheduler, without running its loop."""
    monkeypatch.setenv("BALDUR_SCHEDULER_AUTOSTART", "1")
    reset_schedulers()
    with patch.object(LeaderScheduler, "start", autospec=True):
        _start_default_scheduler(task_backend="inline")
    try:
        yield get_leader_scheduler(DEFAULT_SCHEDULER_RESOURCE)
    finally:
        reset_schedulers()


def _lapsed_block(repository: InMemoryCircuitBreakerStateRepository) -> None:
    """A manual Block whose promised lift time has already passed."""
    expires_at = utc_now() - timedelta(minutes=1)
    repository._storage[SERVICE] = CircuitBreakerStateData(
        id=1,
        service_name=SERVICE,
        state=CircuitBreakerStateEnum.CLOSED.value,
        manually_controlled=True,
        control_reason="maintenance",
        manual_override_expires_at=expires_at,
    )


class TestOverrideExpiryInlineScheduler:
    """The inline default scheduler owns the sweep on Celery-less installs."""

    def test_override_expiry_inline_job_is_registered(self, inline_scheduler):
        assert JOB_NAME in inline_scheduler.jobs

    def test_override_expiry_inline_job_needs_no_celery_task(self, inline_scheduler):
        """Resolution goes through the synthetic callable, not a task import.

        A Celery-bound delegator here would put the fix back where it started —
        working only on the deployments that already had it.
        """
        job = inline_scheduler.jobs[JOB_NAME]

        assert "celery" not in job.func.__name__

    def test_override_expiry_inline_job_clears_a_lapsed_block(
        self, inline_scheduler, cb_repository
    ):
        # Given: a manual Block whose lifetime has run out.
        _lapsed_block(cb_repository)

        # When: the registered job runs one pass.
        inline_scheduler.jobs[JOB_NAME].func()

        # Then: the pin is gone, through the whole registration chain.
        row = cb_repository.get_by_service_name(SERVICE)
        assert row.manually_controlled is False
        assert row.manual_override_expires_at is None

    def test_override_expiry_inline_job_leaves_a_live_block_alone(
        self, inline_scheduler, cb_repository
    ):
        """Negative half: the pass is a lift, not a blanket unpin."""
        cb_repository._storage[SERVICE] = CircuitBreakerStateData(
            id=1,
            service_name=SERVICE,
            state=CircuitBreakerStateEnum.CLOSED.value,
            manually_controlled=True,
            manual_override_expires_at=utc_now() + timedelta(minutes=30),
        )

        inline_scheduler.jobs[JOB_NAME].func()

        assert cb_repository.get_by_service_name(SERVICE).manually_controlled is True

    def test_override_expiry_inline_job_runs_at_the_same_cadence_as_recovery(
        self, inline_scheduler
    ):
        """A minute — so the console's "lifts after N minutes" stays honest."""
        assert (
            inline_scheduler.jobs[JOB_NAME].interval_seconds
            == inline_scheduler.jobs["cb_recovery"].interval_seconds
        )
