"""Default-on outbound 429 coordination on the asynchronous retry stage.

Target: resilience/policies/async_retry.py
- ``_resolve_rate_limit_coordinator()``: the shared admission rule reached
  through ``aget_instance``, injection precedence, fail-open
- ``execute()``: key sourcing across wait / notify / success / deferral, the
  deferral exit, the budget-bounded wait, on_success gating, suppression
  paths, fault isolation, and the memory-store / network-backed store shapes
- the observation scope crossing ``AsyncTimeoutPolicy`` and the breaker
  frame's worker-thread hop

Twin of ``services/test_retry_coordination_wiring.py``: every test here must be
able to fail because the *async* stage stopped coordinating — the parity this
stage did not have — never because a collaborator it was handed stopped being
called.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage
from baldur.core.backoff import ConstantBackoff
from baldur.core.exceptions import RateLimitDeferredError
from baldur.core.execution_mode import (
    ExecutionMode,
    clear_execution_mode_override,
    set_execution_mode,
)
from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.resilience.policies.async_retry import AsyncRetryPolicy
from baldur.services.circuit_breaker.rate_limit_observation import (
    close_scope,
    current_scope,
    open_scope,
)
from baldur.services.rate_limit_coordinator import RateLimitCoordinator
from baldur.services.rate_limit_coordinator.models import (
    RateLimitCoordinatorConfig,
    RateLimitResult,
)
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.rate_limit_detection import (
    UNIDENTIFIED_COORDINATION_KEY as _UNIDENTIFIED_DOMAIN,
)
from baldur.settings.rate_limit_backoff import reset_rate_limit_backoff_settings
from baldur.settings.retry import reset_retry_settings
from tests.factories.rate_limit_doubles import (
    NetworkBackedRateLimitStorage,
    ToThreadSpy,
)

# A message the shared 429 classifier recognises.
_RATE_LIMIT_MESSAGE = "429 too many requests"

_COORDINATION_SWITCH_ENV = "BALDUR_RATE_LIMIT_BACKOFF_COORDINATION_ENABLED"

# The coordinator module's own sleep and hop, patched so a real coordinator's
# served cooldown costs no wall-clock time here.
_COORDINATOR_SLEEP = "baldur.services.rate_limit_coordinator.coordinator.asyncio.sleep"
_COORDINATOR_TO_THREAD = (
    "baldur.services.rate_limit_coordinator.coordinator.asyncio.to_thread"
)
_BREAKER_TO_THREAD = "baldur.services.circuit_breaker.policy.asyncio.to_thread"

_TRACKER = "baldur.services.circuit_breaker.rate_limit_tracker.get_rate_limit_tracker"
_CB_SERVICE = "baldur.services.circuit_breaker.convenience.get_circuit_breaker_service"


# =============================================================================
# Fixtures
# =============================================================================


def _spec_coordinator() -> MagicMock:
    """A spec'd coordinator whose awaitable twins admit every attempt.

    ``MagicMock(spec=...)`` types the coroutine methods as ``AsyncMock``; the
    wait returns a real ``RateLimitResult`` because an auto-generated
    ``.deferred`` attribute is truthy and would defer every call.
    """
    coordinator = MagicMock(spec=RateLimitCoordinator)
    coordinator.await_if_needed.return_value = RateLimitResult(waited=False)
    coordinator.aon_rate_limited.return_value = 0.0
    return coordinator


@pytest.fixture
def singleton_coordinator():
    """Stand a spec'd mock in for the process-wide coordinator singleton.

    Yields ``(coordinator, get_instance_mock)``. The async stage resolves
    through ``aget_instance``, whose empty-slot path is a worker-thread call
    to ``get_instance`` — so patching ``get_instance`` is the seam, and the
    negative assertions key on it: "no coordinator was resolved" is only
    provable there.
    """
    coordinator = _spec_coordinator()
    with patch.object(
        RateLimitCoordinator,
        "get_instance",
        autospec=True,
        return_value=coordinator,
    ) as get_instance:
        yield coordinator, get_instance


@pytest.fixture
def coordination_switch(monkeypatch):
    """Set the deployment kill switch and drop the cached settings node."""

    def _set(enabled: bool) -> None:
        monkeypatch.setenv(_COORDINATION_SWITCH_ENV, "true" if enabled else "false")
        reset_rate_limit_backoff_settings()

    yield _set
    reset_rate_limit_backoff_settings()


@pytest.fixture
def no_cluster_broadcast():
    """Neutralise the Dormant-tier cluster broadcast on real coordinators."""
    with patch.object(RateLimitCoordinator, "_broadcast_to_cluster", autospec=True):
        yield


def _policy(**config_kwargs) -> AsyncRetryPolicy:
    """The async stage built the way the facade builds it — off a config.

    ``from_policy_config`` is where the two coordination fields are mapped, so
    the wiring under test is the one every ``aprotect`` caller reaches. Zero
    backoff keeps the loop's own delays out of every assertion.
    """
    config_kwargs.setdefault("max_attempts", 1)
    return AsyncRetryPolicy.from_policy_config(
        RetryPolicyConfig(**config_kwargs), backoff=ConstantBackoff(delay=0.0)
    )


def _real_coordinator(storage, *, default_retry_after: float = 10.0):
    """Deterministic real coordinator (no jitter, no event debounce) over storage."""
    return RateLimitCoordinator(
        storage=storage,
        config=RateLimitCoordinatorConfig(
            jitter_percent=0.0,
            debounce_window_seconds=0.0,
            default_retry_after=default_retry_after,
        ),
    )


async def _ok():
    return "ok"


def _run(policy, func=_ok):
    return asyncio.run(policy.execute(func))


def _raising(error):
    async def func():
        raise error

    return func


def _counting():
    """An awaitable that counts its calls: ``(func, calls)``."""
    calls = {"n": 0}

    async def func():
        calls["n"] += 1
        return "ok"

    return func, calls


# =============================================================================
# Resolution — the lever matrix, through aget_instance
# =============================================================================


class TestAsyncRetryCoordinatorResolutionBehavior:
    """Which levers turn the default coordinator resolution on and off."""

    def test_a_config_built_policy_resolves_the_singleton_by_default(
        self, singleton_coordinator
    ):
        """The headline: an async policy handed no coordinator still coordinates.

        Before the parity work this stage never read the shared cooldown, so
        eight async workers kept calling into a cooldown their sync peers were
        honouring.
        """
        coordinator, get_instance = singleton_coordinator

        result = _run(_policy(domain="payment"))

        assert result.outcome == PolicyOutcome.SUCCESS
        get_instance.assert_called_once()
        coordinator.await_if_needed.assert_awaited_once_with("payment", max_wait=None)

    def test_resolution_goes_through_the_awaitable_instance_read(self):
        """The seam is ``aget_instance``: the first construction leaves the loop."""
        coordinator = _spec_coordinator()

        with patch.object(
            RateLimitCoordinator,
            "aget_instance",
            new_callable=AsyncMock,
            return_value=coordinator,
        ) as aget_instance:
            _run(_policy(domain="payment"))

        aget_instance.assert_awaited_once()
        coordinator.await_if_needed.assert_awaited_once()

    @pytest.mark.parametrize(
        ("rate_limit_aware", "switch_enabled"),
        [
            (False, True),
            (True, False),
            (False, False),
        ],
        ids=["config_off", "switch_off", "both_off"],
    )
    def test_either_opt_out_lever_prevents_resolution(
        self,
        singleton_coordinator,
        coordination_switch,
        rate_limit_aware,
        switch_enabled,
    ):
        """Both levers are sufficient on their own, and they compose."""
        _coordinator, get_instance = singleton_coordinator
        coordination_switch(switch_enabled)

        result = _run(_policy(domain="payment", rate_limit_aware=rate_limit_aware))

        assert result.outcome == PolicyOutcome.SUCCESS
        get_instance.assert_not_called()

    def test_injected_coordinator_bypasses_both_levers(self, coordination_switch):
        """Explicit injection wins over the config flag and the kill switch."""
        coordination_switch(False)
        injected = _spec_coordinator()
        policy = AsyncRetryPolicy(
            max_retries=0,
            domain="payment",
            rate_limit_aware=False,
            rate_limit_coordinator=injected,
        )

        _run(policy)

        injected.await_if_needed.assert_awaited_once_with("payment", max_wait=None)

    def test_injection_wins_over_the_singleton(self, singleton_coordinator):
        singleton, get_instance = singleton_coordinator
        injected = _spec_coordinator()
        policy = AsyncRetryPolicy(
            max_retries=0, domain="payment", rate_limit_coordinator=injected
        )

        _run(policy)

        injected.await_if_needed.assert_awaited_once()
        singleton.await_if_needed.assert_not_called()
        get_instance.assert_not_called()

    def test_get_instance_fault_degrades_to_no_coordination(self):
        """A singleton-construction fault fails open, it does not fail the call."""
        with patch.object(
            RateLimitCoordinator,
            "get_instance",
            autospec=True,
            side_effect=RuntimeError("storage auto-detect exploded"),
        ):
            with capture_logs() as logs:
                result = _run(_policy(domain="payment"))

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"
        warnings = [
            entry
            for entry in logs
            if entry["event"] == "retry.rate_limit_coordinator_resolution_failed"
        ]
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"
        assert warnings[0]["domain"] == "payment"

    def test_settings_read_fault_degrades_to_no_coordination(
        self, singleton_coordinator
    ):
        _coordinator, get_instance = singleton_coordinator

        with patch(
            "baldur.settings.rate_limit_backoff.get_rate_limit_backoff_settings",
            autospec=True,
            side_effect=RuntimeError("settings backend down"),
        ):
            result = _run(_policy(domain="payment"))

        assert result.outcome == PolicyOutcome.SUCCESS
        get_instance.assert_not_called()

    def test_resolution_is_per_call_never_cached_on_the_instance(
        self, singleton_coordinator
    ):
        """Two calls on one policy resolve twice; the field stays empty."""
        _coordinator, get_instance = singleton_coordinator
        policy = _policy(domain="payment")

        _run(policy)
        _run(policy)

        assert get_instance.call_count == 2
        assert policy._rate_limit_coordinator is None


# =============================================================================
# Resolution — the identity gate
# =============================================================================


class TestAsyncRetryCoordinationIdentityGateBehavior:
    """The default refuses to coordinate on an unidentified coordination key."""

    @pytest.mark.parametrize(
        ("domain", "rate_limit_key", "expected_key"),
        [
            ("payment", None, "payment"),
            ("payment", "provider-a", "provider-a"),
            (_UNIDENTIFIED_DOMAIN, "provider-a", "provider-a"),
        ],
        ids=["named_domain", "key_overrides_domain", "key_rescues_placeholder"],
    )
    def test_identified_key_coordinates(
        self, singleton_coordinator, domain, rate_limit_key, expected_key
    ):
        coordinator, get_instance = singleton_coordinator

        _run(_policy(domain=domain, rate_limit_key=rate_limit_key))

        get_instance.assert_called_once()
        coordinator.await_if_needed.assert_awaited_once_with(
            expected_key, max_wait=None
        )

    def test_placeholder_domain_without_a_key_does_not_coordinate(
        self, singleton_coordinator
    ):
        _coordinator, get_instance = singleton_coordinator

        with capture_logs() as logs:
            result = _run(_policy(domain=_UNIDENTIFIED_DOMAIN))

        assert result.outcome == PolicyOutcome.SUCCESS
        get_instance.assert_not_called()
        skipped = [
            entry
            for entry in logs
            if entry["event"] == "retry.rate_limit_coordination_skipped"
        ]
        assert len(skipped) == 1
        assert skipped[0]["log_level"] == "warning"

    def test_an_empty_override_is_not_an_identity(self, singleton_coordinator):
        _coordinator, get_instance = singleton_coordinator

        _run(_policy(domain=_UNIDENTIFIED_DOMAIN, rate_limit_key=""))

        get_instance.assert_not_called()

    def test_an_empty_override_still_coordinates_under_a_named_domain(
        self, singleton_coordinator
    ):
        coordinator, _ = singleton_coordinator

        _run(_policy(domain="payment", rate_limit_key=""))

        coordinator.await_if_needed.assert_awaited_once_with("payment", max_wait=None)


# =============================================================================
# execute() — key sourcing across every coordinator call site
# =============================================================================


class TestAsyncRetryCoordinationKeyBehavior:
    """One key is chosen per call and every coordinator call site uses it."""

    def _drive_a_full_signal_cycle(self, coordinator, policy):
        """429 on attempt 1, success on attempt 2 — reaches wait / notify / success."""
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception(_RATE_LIMIT_MESSAGE)
            return "ok"

        return _run(policy, flaky)

    def test_unset_key_falls_back_to_domain_on_every_call_site(
        self, singleton_coordinator
    ):
        coordinator, _ = singleton_coordinator

        result = self._drive_a_full_signal_cycle(
            coordinator, _policy(max_attempts=2, domain="payment")
        )

        assert result.outcome == PolicyOutcome.SUCCESS
        assert coordinator.await_if_needed.call_args.args[0] == "payment"
        assert coordinator.aon_rate_limited.call_args.kwargs["key"] == "payment"
        coordinator.aon_success.assert_awaited_once_with("payment")

    def test_rate_limit_key_overrides_domain_on_every_call_site(
        self, singleton_coordinator
    ):
        coordinator, _ = singleton_coordinator

        self._drive_a_full_signal_cycle(
            coordinator,
            _policy(max_attempts=2, domain="payment", rate_limit_key="stripe-api"),
        )

        assert coordinator.await_if_needed.call_args.args[0] == "stripe-api"
        assert coordinator.aon_rate_limited.call_args.kwargs["key"] == "stripe-api"
        coordinator.aon_success.assert_awaited_once_with("stripe-api")

    def test_the_deferral_error_names_the_key_it_deferred_on(
        self, singleton_coordinator
    ):
        """The fourth site: the synthesised error carries the key, not the domain."""
        coordinator, _ = singleton_coordinator
        coordinator.await_if_needed.return_value = RateLimitResult(
            deferred=True, not_before=1_700_000_000.0
        )

        result = _run(
            _policy(max_attempts=2, domain="payment", rate_limit_key="stripe-api")
        )

        assert type(result.error) is RateLimitDeferredError
        assert result.error.key == "stripe-api"
        assert result.metadata["rate_limit_key"] == "stripe-api"


# =============================================================================
# execute() — the deferral exit and the budget-bounded wait
# =============================================================================


class TestAsyncRetryDeferralExitBehavior:
    """A cooldown that outlasts the budget refuses the attempt with the defer vocabulary."""

    def test_a_deferral_runs_the_function_zero_times(self, singleton_coordinator):
        coordinator, _ = singleton_coordinator
        coordinator.await_if_needed.return_value = RateLimitResult(
            deferred=True, not_before=1_700_000_000.0
        )
        func, calls = _counting()

        result = _run(_policy(max_attempts=3, domain="payment"), func)

        assert calls["n"] == 0
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.total_attempts == 1
        assert result.metadata["reason"] == "rate_limit_deferred"
        assert result.metadata["not_before"] == 1_700_000_000.0
        assert result.metadata["rate_limit_key"] == "payment"

    def test_a_deferral_on_the_first_attempt_synthesises_the_error(
        self, singleton_coordinator
    ):
        coordinator, _ = singleton_coordinator
        coordinator.await_if_needed.return_value = RateLimitResult(
            deferred=True, not_before=1_700_000_000.0
        )

        result = _run(_policy(max_attempts=3, domain="payment"))

        assert type(result.error) is RateLimitDeferredError
        assert result.error.not_before == 1_700_000_000.0

    def test_a_deferral_after_a_real_429_keeps_the_429_as_the_error(
        self, singleton_coordinator
    ):
        """The attempt that ran owns the reported error; the metadata says deferred.

        The breaker above must keep counting the provider's answer, so the
        loop never replaces a real last error with its own refusal — the
        decorator synthesises the deferral from the metadata instead.
        """
        coordinator, _ = singleton_coordinator
        coordinator.await_if_needed.side_effect = [
            RateLimitResult(waited=False),
            RateLimitResult(deferred=True, not_before=1_700_000_000.0),
        ]
        raised = Exception(_RATE_LIMIT_MESSAGE)

        result = _run(_policy(max_attempts=3, domain="payment"), _raising(raised))

        assert result.error is raised
        assert result.metadata["reason"] == "rate_limit_deferred"
        assert result.metadata["not_before"] == 1_700_000_000.0
        assert result.metadata["rate_limit_key"] == "payment"
        assert result.total_attempts == 2

    def test_the_wait_is_bounded_by_the_remaining_budget(self, singleton_coordinator):
        """``rl_bound`` is what is left of ``max_elapsed``, never the whole knob."""
        coordinator, _ = singleton_coordinator

        _run(_policy(max_attempts=1, domain="payment", max_elapsed=5.0))

        bound = coordinator.await_if_needed.call_args.kwargs["max_wait"]
        assert 4.0 < bound <= 5.0

    def test_an_unbudgeted_call_passes_no_bound(self, singleton_coordinator):
        """``None`` lets the coordinator apply its own ``max_delay``."""
        coordinator, _ = singleton_coordinator

        _run(_policy(max_attempts=1, domain="payment"))

        assert coordinator.await_if_needed.call_args.kwargs["max_wait"] is None

    def test_a_served_wait_logs_the_cooldown_waited_event(self, singleton_coordinator):
        """Observability parity: the sync stage's event name, on this stage."""
        coordinator, _ = singleton_coordinator
        coordinator.await_if_needed.return_value = RateLimitResult(
            waited=True, wait_time=0.25, was_rate_limited=True
        )

        with capture_logs() as logs:
            _run(_policy(domain="payment"))

        waited = [
            entry
            for entry in logs
            if entry["event"] == "retry.rate_limit_cooldown_waited"
        ]
        assert len(waited) == 1
        assert waited[0]["wait_time"] == 0.25


# =============================================================================
# execute() — on_success gating
# =============================================================================


class TestAsyncRetryOnSuccessGatingBehavior:
    """``aon_success`` is owed only once the call has observed a rate-limit signal."""

    @pytest.mark.parametrize(
        ("wait_result", "expected"),
        [
            (RateLimitResult(waited=False), False),
            (RateLimitResult(waited=True, wait_time=0.01), True),
            (RateLimitResult(waited=False, was_rate_limited=True), True),
        ],
        ids=["no_signal", "waited", "was_rate_limited"],
    )
    def test_wait_result_decides_whether_success_is_reported(
        self, singleton_coordinator, wait_result, expected
    ):
        coordinator, _ = singleton_coordinator
        coordinator.await_if_needed.return_value = wait_result

        _run(_policy(domain="payment"))

        assert coordinator.aon_success.called is expected

    def test_a_detected_429_makes_the_later_success_report(self, singleton_coordinator):
        coordinator, _ = singleton_coordinator
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception(_RATE_LIMIT_MESSAGE)
            return "ok"

        _run(_policy(max_attempts=2, domain="payment"), flaky)

        coordinator.aon_success.assert_awaited_once_with("payment")

    def test_a_non_429_failure_then_success_reports_nothing(
        self, singleton_coordinator
    ):
        coordinator, _ = singleton_coordinator
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("connection reset")
            return "ok"

        _run(_policy(max_attempts=2, domain="payment"), flaky)

        coordinator.aon_success.assert_not_called()

    def test_clean_path_costs_one_storage_read_per_attempt_and_no_more(
        self, no_cluster_broadcast
    ):
        """The cost claim, measured at the storage adapter rather than assumed."""
        storage = MagicMock(wraps=InMemoryRateLimitStorage())
        storage.storage_type = InMemoryRateLimitStorage().storage_type
        with patch.object(
            RateLimitCoordinator,
            "get_instance",
            autospec=True,
            return_value=_real_coordinator(storage),
        ):
            result = _run(_policy(max_attempts=3, domain="payment"))

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.total_attempts == 1
        assert storage.get_state.call_count == 1
        storage.reset_consecutive_429s.assert_not_called()

    def test_a_signalled_call_does_pay_the_success_side_reset(
        self, no_cluster_broadcast
    ):
        """The gate withholds the reset; it must not lose it."""
        storage = InMemoryRateLimitStorage()
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception(_RATE_LIMIT_MESSAGE)
            return "ok"

        with (
            patch.object(
                RateLimitCoordinator,
                "get_instance",
                autospec=True,
                return_value=_real_coordinator(storage, default_retry_after=0.0),
            ),
            patch(_COORDINATOR_SLEEP, new_callable=AsyncMock),
        ):
            result = _run(_policy(max_attempts=3, domain="payment"), flaky)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert storage.get_state("payment").consecutive_429s == 0


# =============================================================================
# execute() — the suppression paths coordinate nothing
# =============================================================================


class TestAsyncRetrySuppressedCoordinationBehavior:
    """Retry-disabled and observe-only calls resolve no coordinator at all."""

    def test_globally_disabled_retry_does_not_resolve(
        self, singleton_coordinator, monkeypatch
    ):
        _coordinator, get_instance = singleton_coordinator
        monkeypatch.setenv("BALDUR_RETRY_ENABLED", "false")
        reset_retry_settings()
        try:
            result = _run(_policy(domain="payment"))
        finally:
            reset_retry_settings()

        assert result.outcome == PolicyOutcome.SUCCESS
        get_instance.assert_not_called()

    def test_observe_only_mode_does_not_resolve(self, singleton_coordinator):
        _coordinator, get_instance = singleton_coordinator
        set_execution_mode(ExecutionMode.shadow())
        try:
            result = _run(_policy(domain="payment"))
        finally:
            clear_execution_mode_override()

        assert result.outcome == PolicyOutcome.SUCCESS
        get_instance.assert_not_called()

    def test_observe_only_suppression_is_the_reason_not_a_missing_domain(
        self, singleton_coordinator
    ):
        """Discriminator: the same call coordinates once the mode is normal."""
        _coordinator, get_instance = singleton_coordinator

        _run(_policy(domain="payment"))

        get_instance.assert_called_once()


# =============================================================================
# execute() — coordinator faults on the wired path
# =============================================================================


class TestAsyncRetryCoordinatorFaultBehavior:
    """A resolved coordinator's faults stay out of the business outcome."""

    def test_wait_fault_preserves_success_and_logs(self, singleton_coordinator):
        coordinator, _ = singleton_coordinator
        coordinator.await_if_needed.side_effect = RuntimeError("coordinator down")

        with capture_logs() as logs:
            result = _run(_policy(domain="payment"))

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"
        failed = [
            entry for entry in logs if entry["event"] == "retry.rate_limit_wait_failed"
        ]
        assert len(failed) == 1
        assert failed[0]["log_level"] == "warning"

    def test_notify_fault_preserves_the_business_error_and_logs(
        self, singleton_coordinator
    ):
        coordinator, _ = singleton_coordinator
        coordinator.aon_rate_limited.side_effect = RuntimeError("coordinator down")
        business_error = Exception(_RATE_LIMIT_MESSAGE)

        with capture_logs() as logs:
            result = _run(_policy(domain="payment"), _raising(business_error))

        assert result.error is business_error
        failed = [
            entry
            for entry in logs
            if entry["event"] == "retry.rate_limit_cooldown_notify_failed"
        ]
        assert len(failed) == 1

    def test_success_notify_fault_preserves_success_and_logs(
        self, singleton_coordinator
    ):
        coordinator, _ = singleton_coordinator
        coordinator.await_if_needed.return_value = RateLimitResult(
            waited=True, wait_time=0.01, was_rate_limited=True
        )
        coordinator.aon_success.side_effect = RuntimeError("coordinator down")

        with capture_logs() as logs:
            result = _run(_policy(domain="payment"))

        coordinator.aon_success.assert_awaited_once()
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"
        assert result.error is None
        failed = [
            entry
            for entry in logs
            if entry["event"] == "retry.rate_limit_success_notify_failed"
        ]
        assert len(failed) == 1

    def test_cancellation_inside_the_wait_propagates_untouched(self):
        """A cancelled request is cancelled; the wait's wrap never swallows it.

        The cooldown sits inside the coordinator's default wait bound, so the
        stage sleeps it rather than refusing it, and the cancel is delivered
        only once that sleep has been entered — a cooldown past the bound is
        deferred before any wait exists, and a fixed pre-cancel delay would
        race the loop's thread hops instead of landing in the wait.
        """
        storage = InMemoryRateLimitStorage()
        storage.set_cooldown("payment", time.time() + 30.0)
        func, calls = _counting()
        policy = AsyncRetryPolicy(
            max_retries=0,
            domain="payment",
            rate_limit_coordinator=_real_coordinator(storage),
        )
        real_sleep = asyncio.sleep

        async def scenario():
            entered_wait = asyncio.Event()

            async def sleep_and_signal(seconds, *args, **kwargs):
                entered_wait.set()
                await real_sleep(seconds, *args, **kwargs)

            with patch(_COORDINATOR_SLEEP, new=sleep_and_signal):
                task = asyncio.create_task(policy.execute(func))
                await entered_wait.wait()
                task.cancel()
                await task

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(scenario())
        assert calls["n"] == 0


# =============================================================================
# execute() — the store shapes reached through the default wiring
# =============================================================================


class TestAsyncRetryStoreShapeBehavior:
    """The bounded wait lands the same way on the memory store and a network-backed one."""

    def _policy_with_budget(self):
        return _policy(max_attempts=3, domain="payment", max_elapsed=5.0)

    def test_an_over_budget_cooldown_on_the_memory_store_defers(
        self, no_cluster_broadcast
    ):
        storage = InMemoryRateLimitStorage()
        storage.set_cooldown("payment", time.time() + 300.0)
        func, calls = _counting()
        spy = ToThreadSpy()

        with (
            patch.object(
                RateLimitCoordinator,
                "get_instance",
                autospec=True,
                return_value=_real_coordinator(storage),
            ),
            patch(_COORDINATOR_TO_THREAD, new=spy),
        ):
            result = _run(self._policy_with_budget(), func)

        assert result.metadata["reason"] == "rate_limit_deferred"
        assert result.metadata["not_before"] is not None
        assert calls["n"] == 0
        # The store itself was read inline: no store call left the loop (the
        # singleton construction and the exhaustion emit are the only hops).
        assert spy.hopped("get_state") is False
        assert spy.hopped("ensure_running") is False

    def test_an_over_budget_cooldown_on_a_network_backed_store_defers_off_loop(
        self, no_cluster_broadcast
    ):
        """Same decision; every store read hopped to a worker thread."""
        storage = NetworkBackedRateLimitStorage()
        storage.set_cooldown("payment", time.time() + 300.0)
        func, calls = _counting()
        spy = ToThreadSpy()

        with (
            patch.object(
                RateLimitCoordinator,
                "get_instance",
                autospec=True,
                return_value=_real_coordinator(storage),
            ),
            patch(_COORDINATOR_TO_THREAD, new=spy),
        ):
            result = _run(self._policy_with_budget(), func)

        assert result.metadata["reason"] == "rate_limit_deferred"
        assert calls["n"] == 0
        assert spy.hopped("get_state") is True

    def test_a_cooldown_within_budget_is_served_not_deferred(
        self, no_cluster_broadcast
    ):
        """Discriminator: the deferral above is the bound firing, not blanket refusal."""
        storage = InMemoryRateLimitStorage()
        storage.set_cooldown("payment", time.time() + 0.5)

        with (
            patch.object(
                RateLimitCoordinator,
                "get_instance",
                autospec=True,
                return_value=_real_coordinator(storage),
            ),
            patch(_COORDINATOR_SLEEP, new_callable=AsyncMock) as coordinator_sleep,
        ):
            result = _run(_policy(max_attempts=3, domain="payment", max_elapsed=30.0))

        assert result.outcome == PolicyOutcome.SUCCESS
        slept = [call.args[0] for call in coordinator_sleep.await_args_list]
        assert any(0.3 <= seconds <= 0.6 for seconds in slept)


# =============================================================================
# The observation scope across the timeout stage and the breaker's hop
# =============================================================================


class TestObservationScopePropagationBehavior:
    """The scope the breaker frame opens is the object every inner write lands on.

    ``AsyncTimeoutPolicy`` runs the inner chain as a Task under a copied
    context, and the breaker frame records a rate-limited final outcome on a
    worker thread under another copy. Both copies reference the *same* scope
    object, and every inner write mutates it in place — so the claim, the
    marks and the 429 notes made inside the async stage are visible to the
    frame that opened it, and the breaker neither counts a second 429 nor
    installs a second cooldown.
    """

    @staticmethod
    def _admitting_breaker():
        from baldur.interfaces.repositories import CircuitBreakerStateData
        from baldur.services.circuit_breaker.config import CircuitBreakerDecision
        from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        cb_service = MagicMock(spec=CircuitBreakerService)
        cb_service.is_enabled = True
        cb_service.should_allow_with_state.return_value = CircuitBreakerDecision(
            allowed=True,
            state=CircuitBreakerStateData(service_name="payment", state="closed"),
        )
        return CircuitBreakerPolicy(service_name="payment", cb_service=cb_service)

    def test_writes_made_under_the_timeout_task_and_the_hop_reach_the_opened_scope(
        self,
    ):
        from baldur.resilience.policies.composer import AsyncPolicyComposer
        from baldur.resilience.policies.timeout import AsyncTimeoutPolicy
        from baldur.services.circuit_breaker.policy import AsyncCircuitBreakerPolicy
        from baldur.services.circuit_breaker.rate_limit_tracker import RateLimitTracker
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        coordinator = _spec_coordinator()
        retry = AsyncRetryPolicy(
            max_retries=1,
            domain="payment",
            backoff=ConstantBackoff(delay=0.0),
            rate_limit_coordinator=coordinator,
        )
        breaker = self._admitting_breaker()
        composer: AsyncPolicyComposer = AsyncPolicyComposer()
        composer.add(AsyncCircuitBreakerPolicy(breaker))
        composer.add(AsyncTimeoutPolicy(timeout_seconds=5.0))
        composer.add(retry)
        seen: dict = {}
        errors: list[Exception] = []

        async def throttled():
            # A fresh exception per attempt, as a real client raises one.
            seen["scope"] = current_scope()
            errors.append(Exception(_RATE_LIMIT_MESSAGE))
            raise errors[-1]

        tracker = MagicMock(spec=RateLimitTracker)
        shared_service = MagicMock(spec=CircuitBreakerService)
        breaker_hop = ToThreadSpy()
        with (
            patch(_TRACKER, return_value=tracker),
            patch(_CB_SERVICE, return_value=shared_service),
            patch(_BREAKER_TO_THREAD, new=breaker_hop),
            patch.object(
                RateLimitCoordinator, "get_instance", autospec=True
            ) as singleton,
        ):
            result = asyncio.run(composer.execute(throttled))

        scope = seen["scope"]
        assert result.outcome == PolicyOutcome.FAILURE
        assert scope is not None
        assert scope.breaker_key == "payment"
        # The inner stage's writes, read back on the frame that opened the scope.
        assert scope.coordination_claimed is True
        assert scope.attempts == 2
        assert scope.rate_limited == 2
        assert all(scope.was_classified(raised) for raised in errors)
        assert result.error is errors[-1]
        # The breaker frame recorded the final 429 on a worker thread ...
        assert breaker_hop.hopped("_on_failure") is True
        # ... and, reading the same scope there, counted no third 429 and
        # installed no cooldown of its own. Both cascade notes land on the
        # breaker's own service, which the scope carries, and that breaker
        # decides the cascade once after the record; the shared one is never
        # asked.
        assert breaker.cb_service.record_rate_limit_observation.call_count == 2
        breaker.cb_service.evaluate_rate_limit_cascade.assert_called_once_with(
            "payment"
        )
        shared_service.record_rate_limit_observation.assert_not_called()
        shared_service.evaluate_rate_limit_cascade.assert_not_called()
        assert coordinator.aon_rate_limited.await_count == 2
        singleton.assert_not_called()
        assert tracker.record_request.call_count == 2

    def test_a_scope_opened_outside_is_mutated_in_place_never_replaced(self):
        """The inner stage never ``set``s the ContextVar: the caller's object changes."""
        from baldur.resilience.policies.timeout import AsyncTimeoutPolicy
        from baldur.services.circuit_breaker.rate_limit_tracker import RateLimitTracker

        coordinator = _spec_coordinator()
        retry = AsyncRetryPolicy(
            max_retries=0, domain="payment", rate_limit_coordinator=coordinator
        )
        timeout = AsyncTimeoutPolicy(timeout_seconds=5.0)

        async def scenario():
            token, scope = open_scope("payment")
            try:
                await timeout.execute(lambda: retry.execute(_ok))
            finally:
                close_scope(token)
            return scope

        with patch(_TRACKER, return_value=MagicMock(spec=RateLimitTracker)):
            scope = asyncio.run(scenario())

        assert scope.coordination_claimed is True
        assert scope.attempts == 1
        assert current_scope() is None
