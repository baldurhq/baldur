"""With the kill switch pulled, every protected path Baldur composes steps aside.

Target: the kill switch's one stated effect (802 D1-D3) across the entry points
an application reaches Baldur through — ``protect``, ``aprotect``,
``@protected`` (sync / async), ``@dlq_protect`` (sync / async), ``@retry``
(sync / async), the presets, the Celery failure paths and the 429 cascade: a
failing function runs exactly once per call, nothing is parked in the DLQ, a
breaker never refuses and never counts a failure. What the application
configured on the call — a fallback, a timeout, an idempotency key — stays in
force, and so does an operator's breaker Block. The preset pipelines and a
``ThrottleGovernanceGuard`` composition run the function instead of refusing it.

The switch is pulled for real (``kill_switch_active``), so every entry point is
reached through the one resolver it already asks — a new entry point that
honors dry-run honors the switch, and one that does not fails here.

Verification techniques applied (§8):
  - §8.12 Branch outcome — the same harness without the switch retries, trips
    and parks (the control that proves the harness can see an intervention)
  - §8.4 Side effects — DLQ store, breaker failure count, CB / DLQ recorders
  - §8.13 Proximate cause — the switch itself is the only difference between a
    control and its case
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest

from baldur import protect_facade
from baldur.adapters.celery.baldur_task import baldur_task
from baldur.adapters.celery.handlers.failure_handler import FailureHandler
from baldur.adapters.celery.signal_config import (
    SignalHooksSettings,
    reset_signal_hooks_settings,
)
from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.core.backoff import ConstantBackoff
from baldur.core.exceptions import (
    AdapterNotFoundError,
    IdempotencyDuplicateError,
    TimeoutPolicyError,
)
from baldur.core.execution_mode import clear_execution_mode_override
from baldur.decorators.dlq_protect import dlq_protect
from baldur.interfaces.governance import GovernanceChecker
from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)
from baldur.interfaces.resilience_policy import PolicyContext, PolicyOutcome
from baldur.models.dlq import DLQEntryResult
from baldur.protect_facade import aprotect, protect, protected
from baldur.resilience.policies.async_retry import retry
from baldur.resilience.policies.composer import compose
from baldur.resilience.policies.guards.governance import ThrottleGovernanceGuard
from baldur.resilience.policies.presets import standard_pipeline
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError
from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import RetryPolicy
from baldur.settings.protect import reset_protect_settings
from baldur.utils.time import utc_now
from tests.factories import (
    InMemoryCircuitBreakerRepository,
    InMemoryRateLimitTracker,
    dry_run_active,
    kill_switch_active,
)

NAME = "svc.reach"
_STORE = "baldur.services.retry_handler.sinks.store_to_dlq"
#: More failures than the breaker's threshold, so an intervening breaker opens.
_CALLS = 20
_FAILURE_THRESHOLD = 5
_ATTEMPTS = 3
_RELEASE_WAIT_SECONDS = 5.0


@pytest.fixture(autouse=True)
def _protect_state() -> Iterator[None]:
    """Fresh protect settings and facade caches around every test."""
    clear_execution_mode_override()
    reset_protect_settings()
    yield
    reset_protect_settings()
    clear_execution_mode_override()


@pytest.fixture
def dlq_store():
    """The DLQ store every sink calls — a parked failure shows up here."""
    with patch(
        _STORE, autospec=True, return_value=DLQEntryResult.created("dlq-1")
    ) as store:
        yield store


@pytest.fixture
def breaker_repo() -> InMemoryCircuitBreakerStateRepository:
    return InMemoryCircuitBreakerStateRepository()


@pytest.fixture
def breaker(breaker_repo) -> CircuitBreakerService:
    """An in-memory breaker for ``NAME`` that opens after five failures,
    shared by the sync and async facade paths through the per-name cache."""
    service = CircuitBreakerService(
        config=CircuitBreakerConfig(
            enabled=True,
            failure_threshold=_FAILURE_THRESHOLD,
            minimum_calls=1,
            failure_rate_threshold=0,
            recovery_timeout=60,
        ),
        repository=breaker_repo,
    )
    protect_facade._cb_policy_cache[NAME] = CircuitBreakerPolicy(
        service_name=NAME, cb_service=service, hooks=[]
    )
    return service


def _retry_config() -> RetryPolicyConfig:
    return RetryPolicyConfig(
        max_attempts=_ATTEMPTS,
        backoff_base=0,
        backoff_max=0,
        jitter_percent=0,
        enable_dlq=True,
        domain=NAME,
    )


def _failing(calls: list[int]) -> tuple[Callable[[], str], Callable[[], object]]:
    def fn() -> str:
        calls.append(1)
        raise ConnectionError("downstream refused")

    async def afn() -> str:
        calls.append(1)
        raise ConnectionError("downstream refused")

    return fn, afn


# One call through each entry point: (fn, afn) → the call's outcome (raises).
_FACADE = {
    "retry": _retry_config,
    "circuit_breaker": True,
    "dlq": True,
    "timeout": None,
}


def _via_protect(fn, afn):
    protect(NAME, fn, **{**_FACADE, "retry": _retry_config()})


def _via_aprotect(fn, afn):
    asyncio.run(aprotect(NAME, afn, **{**_FACADE, "retry": _retry_config()}))


def _via_protected_sync(fn, afn):
    protected(NAME, **{**_FACADE, "retry": _retry_config()})(fn)()


def _via_protected_async(fn, afn):
    asyncio.run(protected(NAME, **{**_FACADE, "retry": _retry_config()})(afn)())


def _via_dlq_protect_sync(fn, afn):
    dlq_protect(NAME, timeout=None)(fn)()


def _via_dlq_protect_async(fn, afn):
    asyncio.run(dlq_protect(NAME, timeout=None)(afn)())


def _via_retry_sync(fn, afn):
    retry(domain=NAME, max_attempts=_ATTEMPTS, backoff=ConstantBackoff(delay=0))(fn)()


def _via_retry_async(fn, afn):
    asyncio.run(
        retry(domain=NAME, max_attempts=_ATTEMPTS, backoff=ConstantBackoff(delay=0))(
            afn
        )()
    )


_ENTRY_POINTS = [
    _via_protect,
    _via_aprotect,
    _via_protected_sync,
    _via_protected_async,
    _via_dlq_protect_sync,
    _via_dlq_protect_async,
    _via_retry_sync,
    _via_retry_async,
]
_ENTRY_IDS = [
    "protect",
    "aprotect",
    "protected_sync",
    "protected_async",
    "dlq_protect_sync",
    "dlq_protect_async",
    "retry_sync",
    "retry_async",
]


def _call_repeatedly(entry, calls: list[int]) -> list[BaseException]:
    fn, afn = _failing(calls)
    raised: list[BaseException] = []
    for _ in range(_CALLS):
        with pytest.raises(Exception) as outcome:
            entry(fn, afn)
        raised.append(outcome.value)
    return raised


def _pinned_open(repo, name: str, *, expires_in_minutes: int = 30) -> None:
    """An operator's Block: OPEN under a manual pin still in force."""
    repo.hydrate_snapshot(
        CircuitBreakerStateData(
            service_name=name,
            state=CircuitBreakerStateEnum.OPEN.value,
            failure_count=_FAILURE_THRESHOLD,
            opened_at=utc_now(),
            manually_controlled=True,
            control_reason="operator block",
            manual_override_expires_at=utc_now()
            + timedelta(minutes=expires_in_minutes),
        )
    )


# =============================================================================
# Behavior — every entry point steps aside
# =============================================================================


class TestKillSwitchReachBehavior:
    """One attempt, no DLQ capture, no breaker record or refusal (SC1)."""

    @pytest.mark.parametrize("entry", _ENTRY_POINTS, ids=_ENTRY_IDS)
    def test_entry_point_under_kill_switch_runs_each_call_once_and_never_intervenes(
        self, entry, breaker, dlq_store
    ):
        """20 failing calls: 20 invocations, no DLQ entry, no refusal, no count."""
        # Given
        calls: list[int] = []

        # When
        with kill_switch_active():
            raised = _call_repeatedly(entry, calls)

        # Then
        assert len(calls) == _CALLS
        assert not any(isinstance(e, CircuitBreakerOpenError) for e in raised)
        dlq_store.assert_not_called()
        row = breaker.get_or_create_state(NAME)
        assert (row.state, row.failure_count) == (
            CircuitBreakerStateEnum.CLOSED.value,
            0,
        )

    def test_protect_without_kill_switch_retries_trips_and_parks(
        self, breaker, dlq_store
    ):
        """Control: the same harness sees retries, a refusal and DLQ entries."""
        calls: list[int] = []

        raised = _call_repeatedly(_via_protect, calls)

        # Every attempt of the first five calls, then the breaker refuses.
        assert len(calls) == _FAILURE_THRESHOLD * _ATTEMPTS
        assert any(isinstance(e, CircuitBreakerOpenError) for e in raised)
        assert dlq_store.call_count > 0

    def test_retry_decorator_without_kill_switch_retries(self):
        """Control: ``@retry`` makes every attempt while the switch is up."""
        calls: list[int] = []
        fn, afn = _failing(calls)

        with pytest.raises(Exception):
            _via_retry_sync(fn, afn)

        assert len(calls) == _ATTEMPTS

    def test_breaker_opened_before_the_flip_stops_refusing_while_the_switch_is_off(
        self, breaker, breaker_repo, dlq_store
    ):
        """An automatically opened breaker admits the call and keeps its stored state."""
        # Given: the breaker tripped on its own before the brake was pulled
        breaker_repo.hydrate_snapshot(
            CircuitBreakerStateData(
                service_name=NAME,
                state=CircuitBreakerStateEnum.OPEN.value,
                failure_count=_FAILURE_THRESHOLD,
                opened_at=utc_now(),
            )
        )

        # When
        with kill_switch_active():
            served = protect(NAME, lambda: "served", **_FACADE | {"retry": False})

        # Then
        assert served == "served"
        assert breaker.get_or_create_state(NAME).state == (
            CircuitBreakerStateEnum.OPEN.value
        )


class TestKillSwitchPresetAndGuardBehavior:
    """Presets and the governance guard run the call instead of refusing it (D1)."""

    def test_standard_pipeline_under_kill_switch_calls_a_failing_function_once(
        self, dlq_store
    ):
        """No ``rejected_by=kill_switch``: one attempt, the failure is the app's own."""
        calls: list[int] = []
        fn, _ = _failing(calls)
        pipeline = standard_pipeline("svc.preset", max_retries=_ATTEMPTS)

        with kill_switch_active():
            result = pipeline.execute(fn)

        assert calls == [1]
        assert result.outcome is PolicyOutcome.FAILURE
        assert result.metadata.get("rejected_by") is None
        dlq_store.assert_not_called()

    def test_standard_pipeline_under_kill_switch_serves_a_healthy_call(self):
        """The preset no longer refuses the application's traffic."""
        pipeline = standard_pipeline("svc.preset_ok", max_retries=_ATTEMPTS)

        with kill_switch_active():
            result = pipeline.execute(lambda: "served")

        assert (result.outcome, result.value) == (PolicyOutcome.SUCCESS, "served")

    def test_throttle_governance_guard_composition_runs_under_kill_switch(self):
        """The guard no longer asks the switch; it never answers kill_switch_disabled."""
        # Given: a governance view that reports the switch pulled
        governance = create_autospec(GovernanceChecker, instance=True)
        governance.is_system_enabled.return_value = False
        governance.is_emergency_blocking.return_value = (False, "NORMAL")
        governance.is_error_budget_blocking.return_value = (False, 100.0, 10.0)
        guard = ThrottleGovernanceGuard()
        guard._governance = governance
        guard._governance_resolved = True
        composed = compose(
            RetryPolicy(
                config=_retry_config(),
                backoff=ConstantBackoff(delay=0),
                sleeper=lambda _: None,
            )
        ).add_guard(guard)

        # When
        with kill_switch_active():
            result = composed.execute(lambda: "served")

        # Then
        assert (result.outcome, result.value) == (PolicyOutcome.SUCCESS, "served")
        assert result.metadata.get("reason") != "kill_switch_disabled"
        governance.is_system_enabled.assert_not_called()


class TestKillSwitchWorkerAndCascadeBehavior:
    """The Celery failure paths and the 429 cascade step aside too."""

    @pytest.fixture
    def celery_recorders(self):
        reset_signal_hooks_settings()
        with (
            patch(
                "baldur.adapters.celery.handlers.failure_handler.CircuitBreakerRecorder",
                autospec=True,
            ) as handler_cb,
            patch(
                "baldur.adapters.celery.handlers.failure_handler.DLQRecorder",
                autospec=True,
            ) as handler_dlq,
            patch(
                "baldur.adapters.celery.handlers.failure_handler.MetricRecorder",
                autospec=True,
            ),
            patch(
                "baldur.adapters.celery.handlers.failure_handler.ForensicCapture",
                autospec=True,
            ),
            patch(
                "baldur.adapters.celery.baldur_task.CircuitBreakerRecorder",
                autospec=True,
            ) as task_cb,
            patch(
                "baldur.adapters.celery.baldur_task.DLQRecorder", autospec=True
            ) as task_dlq,
            patch(
                "baldur.adapters.celery.baldur_task.get_signal_hooks_settings",
                return_value=SignalHooksSettings(),
            ),
        ):
            yield SimpleNamespace(
                handler_cb=handler_cb.return_value,
                handler_dlq=handler_dlq.return_value,
                task_cb=task_cb.return_value,
                task_dlq=task_dlq.return_value,
            )
        reset_signal_hooks_settings()

    @staticmethod
    def _exhausted_sender() -> SimpleNamespace:
        return SimpleNamespace(
            name="app.tasks.charge",
            max_retries=3,
            request=SimpleNamespace(retries=3),
        )

    @pytest.mark.parametrize(
        ("switch", "records"),
        [(kill_switch_active, False), (nullcontext, True)],
        ids=["kill_switch", "control_switch_up"],
    )
    def test_celery_failure_signal_records_nothing_under_kill_switch(
        self, celery_recorders, switch, records
    ):
        """No breaker record and no DLQ store for an exhausted task failure."""
        handler = FailureHandler(SignalHooksSettings())

        with switch():
            handler.handle(
                sender=self._exhausted_sender(),
                task_id="task-1",
                exception=RuntimeError("boom"),
            )

        assert celery_recorders.handler_cb.record_failure.called is records
        assert celery_recorders.handler_dlq.store.called is records

    def test_baldur_task_failure_records_nothing_under_kill_switch(
        self, celery_recorders
    ):
        """The decorator's except path steps aside; the task's error still propagates."""

        @baldur_task()
        def failing_task() -> None:
            raise RuntimeError("boom")

        with kill_switch_active(), pytest.raises(RuntimeError, match="boom"):
            failing_task()

        celery_recorders.task_cb.record_failure.assert_not_called()
        celery_recorders.task_dlq.store.assert_not_called()

    def test_rate_limit_cascade_under_kill_switch_never_force_opens(self):
        """A 429 cascade over threshold is observed, never tripped."""
        # Given: 15 rate limits out of 100 requests, over the 10% cascade rate
        tracker = create_autospec(InMemoryRateLimitTracker, instance=True)
        tracker.get_rate_limit_count.return_value = 15
        tracker.get_request_count.return_value = 100
        service = CircuitBreakerService(
            config=CircuitBreakerConfig(
                enabled=True,
                rate_limit_cascade_threshold=10,
                rate_limit_cascade_window_seconds=60,
                rate_limit_cascade_rate=10.0,
                rate_limit_cascade_minimum_calls=20,
            ),
            repository=InMemoryCircuitBreakerRepository(),
        )

        # When
        with (
            patch(
                "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
                return_value=tracker,
            ),
            patch.object(service, "_trip_circuit_open") as trip,
            kill_switch_active(),
        ):
            result = service.record_rate_limit_response("payment-api")

        # Then
        assert result is None
        trip.assert_not_called()
        tracker.record_rate_limit.assert_called_once_with("payment-api")


# =============================================================================
# Behavior — what stays in force under the switch
# =============================================================================


class TestKillSwitchKeepsWhatTheCallConfiguredBehavior:
    """Fallback, timeout, idempotency key and an operator's Block stay (D1, D3; SC2)."""

    @pytest.mark.parametrize(
        "switch", [kill_switch_active, dry_run_active], ids=["kill_switch", "dry_run"]
    )
    def test_operator_block_is_refused_through_protect(
        self, switch, breaker, breaker_repo, dlq_store
    ):
        """A service the operator cut off stays cut off, and nothing is parked."""
        # Given
        _pinned_open(breaker_repo, NAME)
        calls: list[int] = []
        fn, _ = _failing(calls)

        # When
        with switch(), pytest.raises(CircuitBreakerOpenError):
            protect(NAME, fn, **_FACADE | {"retry": _retry_config()})

        # Then
        assert calls == []
        dlq_store.assert_not_called()

    def test_expired_operator_block_steps_aside_under_kill_switch(
        self, breaker, breaker_repo
    ):
        """A lapsed pin is no longer a Block: the call runs."""
        _pinned_open(breaker_repo, NAME, expires_in_minutes=-1)

        with kill_switch_active():
            served = protect(NAME, lambda: "served", **_FACADE | {"retry": False})

        assert served == "served"

    def test_fallback_still_serves_under_kill_switch(self):
        """The application's fallback answers the failure, as configured."""
        calls: list[int] = []
        fn, _ = _failing(calls)

        with kill_switch_active():
            served = protect(
                "svc.fallback",
                fn,
                fallback=lambda: "degraded",
                retry=_retry_config(),
                circuit_breaker=False,
                dlq=False,
                timeout=None,
            )

        assert served == "degraded"
        assert calls == [1]

    def test_timeout_still_cuts_a_slow_call_under_kill_switch(self):
        """The call's own timeout is enforced; the slow call is released after."""
        release = threading.Event()

        def slow() -> str:
            release.wait(_RELEASE_WAIT_SECONDS)
            return "late"

        try:
            with kill_switch_active(), pytest.raises(TimeoutPolicyError):
                protect(
                    "svc.timeout",
                    slow,
                    timeout=0.05,
                    retry=False,
                    circuit_breaker=False,
                    dlq=False,
                )
        finally:
            release.set()

    def test_idempotency_key_still_blocks_a_repeat_under_kill_switch(self):
        """A duplicate key is refused; the function ran once."""
        calls: list[int] = []

        def fn() -> str:
            calls.append(1)
            return "charged"

        def charge() -> str:
            return protect(
                "svc.idempotent",
                fn,
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                circuit_breaker=False,
                timeout=None,
            )

        protect_facade.reset_protect_caches()
        try:
            with (
                patch(
                    "baldur.factory.registry.ProviderRegistry.get_cache",
                    side_effect=AdapterNotFoundError(adapter_type="cache"),
                ),
                kill_switch_active(),
            ):
                first = charge()
                with pytest.raises(IdempotencyDuplicateError):
                    charge()
        finally:
            protect_facade.reset_protect_caches()

        assert first == "charged"
        assert calls == [1]
