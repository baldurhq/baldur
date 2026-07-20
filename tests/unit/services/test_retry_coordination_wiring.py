"""Default-on outbound 429 coordination on the synchronous retry stage.

Target: services/retry_handler/policy.py
- ``_resolve_rate_limit_coordinator()``: the three-conjunct default resolution
  (kill switch -> config flag -> identity gate), injection precedence, fail-open
- ``_warn_unidentified_coordination_key()``: the once-per-key WARNING
- ``_notify_rate_limit_cooldown()``: passed-coordinator use + detection return
- ``execute()``: key sourcing, on_success gating, suppression paths, fault
  isolation, storage cost, and the bounded wait reached through the default

The pre-existing ``test_retry_policy.py`` covers the retry loop with an
*injected* coordinator. This file covers the wiring that makes a coordinator
appear without one being handed over, which is a different production reason to
fail: every test here that asserts coordination must be able to fail because the
resolution stopped happening, not because the loop stopped calling a collaborator
it was given.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage
from baldur.core.backoff import ConstantBackoff
from baldur.core.execution_mode import (
    ExecutionMode,
    clear_execution_mode_override,
    set_execution_mode,
)
from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.services.rate_limit_coordinator import RateLimitCoordinator
from baldur.services.rate_limit_coordinator.models import (
    RateLimitCoordinatorConfig,
    RateLimitResult,
)
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import (
    _UNIDENTIFIED_DOMAIN,
    RetryPolicy,
)
from baldur.settings.rate_limit_backoff import reset_rate_limit_backoff_settings
from baldur.settings.retry import reset_retry_settings
from tests.factories.time_helpers import mock_sleep

# A message the shared 429 classifier recognises
# (baldur.services.retry_handler.rate_limit_detection.RATE_LIMIT_INDICATORS).
_RATE_LIMIT_MESSAGE = "429 too many requests"

_COORDINATION_SWITCH_ENV = "BALDUR_RATE_LIMIT_BACKOFF_COORDINATION_ENABLED"


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def singleton_coordinator():
    """Stand a spec'd mock in for the process-wide coordinator singleton.

    Yields ``(coordinator, get_instance_mock)``. ``get_instance_mock`` is what
    the negative assertions key on: "no coordinator was resolved" is only
    provable at the resolution seam, since a policy that resolved one and then
    declined to call it would satisfy a mere ``wait_if_needed.assert_not_called``.

    ``wait_if_needed`` returns a real ``RateLimitResult`` rather than a bare
    MagicMock — an auto-generated ``.deferred`` attribute is truthy and would
    defer every call before any attempt ran.
    """
    coordinator = MagicMock(spec=RateLimitCoordinator)
    coordinator.wait_if_needed.return_value = RateLimitResult(waited=False)
    with patch.object(
        RateLimitCoordinator,
        "get_instance",
        autospec=True,
        return_value=coordinator,
    ) as get_instance:
        yield coordinator, get_instance


@pytest.fixture
def coordination_switch(monkeypatch):
    """Set the deployment kill switch and drop the cached settings node.

    The switch is read through ``get_rate_limit_backoff_settings()`` at use
    time, so the env var only takes effect once the cached ``scaling`` node is
    dropped. Reset again on teardown so the value cannot leak to the next test
    on this xdist worker.
    """

    def _set(enabled: bool) -> None:
        monkeypatch.setenv(_COORDINATION_SWITCH_ENV, "true" if enabled else "false")
        reset_rate_limit_backoff_settings()

    yield _set
    reset_rate_limit_backoff_settings()


@pytest.fixture
def no_cluster_broadcast():
    """Neutralise the Dormant-tier cluster broadcast seam on real coordinators.

    ``on_rate_limited`` fires ``_broadcast_to_cluster``, which eagerly attempts a
    broker connection. It is an explicit NON-GOAL of this wiring, and left live
    it makes every 429 test infra-dependent and ~2s slow.
    """
    with patch.object(RateLimitCoordinator, "_broadcast_to_cluster", autospec=True):
        yield


def _policy(**config_kwargs) -> RetryPolicy:
    """Retry policy with no injected coordinator — the wiring under test.

    Zero backoff and a no-op sleeper keep the loop's own delays out of every
    assertion; the only sleeps left are the ones coordination itself causes.
    """
    config_kwargs.setdefault("max_attempts", 1)
    return RetryPolicy(
        config=RetryPolicyConfig(**config_kwargs),
        backoff=ConstantBackoff(delay=0.0),
        sleeper=lambda _: None,
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


# =============================================================================
# Resolution — the lever matrix
# =============================================================================


class TestRetryCoordinatorResolutionBehavior:
    """Which levers turn the default coordinator resolution on and off."""

    def test_settings_derived_policy_resolves_the_singleton_by_default(
        self, singleton_coordinator
    ):
        """The headline: a policy handed no coordinator still coordinates.

        This is the whole feature — before the wiring, a ``RetryPolicy`` built
        from settings passed no coordinator and every worker ran its own backoff
        ladder against a rate-limited downstream.
        """
        coordinator, get_instance = singleton_coordinator

        result = _policy(domain="payment").execute(lambda: "ok")

        assert result.outcome == PolicyOutcome.SUCCESS
        get_instance.assert_called_once()
        coordinator.wait_if_needed.assert_called_once_with("payment", max_wait=None)

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
        """Both levers are sufficient on their own, and they compose.

        Negative assertion at the resolution seam: an opted-out call must not
        even build the singleton, because building it runs storage auto-detect
        and a Redis connect the operator has just declined to pay for.
        """
        _coordinator, get_instance = singleton_coordinator
        coordination_switch(switch_enabled)

        result = _policy(domain="payment", rate_limit_aware=rate_limit_aware).execute(
            lambda: "ok"
        )

        assert result.outcome == PolicyOutcome.SUCCESS
        get_instance.assert_not_called()

    def test_injected_coordinator_bypasses_both_levers(self, coordination_switch):
        """Explicit injection wins over the config flag and the kill switch.

        A caller who constructed a coordinator and handed it over asked for it;
        neither a per-policy opt-out nor a deployment-wide switch may quietly
        drop a collaborator that was passed in.
        """
        coordination_switch(False)
        injected = MagicMock(spec=RateLimitCoordinator)
        injected.wait_if_needed.return_value = RateLimitResult(waited=False)

        policy = RetryPolicy(
            config=RetryPolicyConfig(
                max_attempts=1, domain="payment", rate_limit_aware=False
            ),
            rate_limit_coordinator=injected,
            sleeper=lambda _: None,
        )
        policy.execute(lambda: "ok")

        injected.wait_if_needed.assert_called_once_with("payment", max_wait=None)

    def test_injection_wins_over_the_singleton(self, singleton_coordinator):
        """With both available, the injected instance is the one that is used."""
        singleton, get_instance = singleton_coordinator
        injected = MagicMock(spec=RateLimitCoordinator)
        injected.wait_if_needed.return_value = RateLimitResult(waited=False)

        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=1, domain="payment"),
            rate_limit_coordinator=injected,
            sleeper=lambda _: None,
        )
        policy.execute(lambda: "ok")

        injected.wait_if_needed.assert_called_once()
        singleton.wait_if_needed.assert_not_called()
        get_instance.assert_not_called()

    def test_get_instance_fault_degrades_to_no_coordination(self):
        """A singleton-construction fault fails open, it does not fail the call.

        Resolution reaches storage auto-detect and a Redis connect, so it is a
        real fault surface — and it sits on the business call's path.
        """
        with patch.object(
            RateLimitCoordinator,
            "get_instance",
            autospec=True,
            side_effect=RuntimeError("storage auto-detect exploded"),
        ):
            with capture_logs() as logs:
                result = _policy(domain="payment").execute(lambda: "ok")

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
        """A settings-read fault is inside the same fail-open wrap."""
        _coordinator, get_instance = singleton_coordinator

        with patch(
            "baldur.settings.rate_limit_backoff.get_rate_limit_backoff_settings",
            autospec=True,
            side_effect=RuntimeError("settings backend down"),
        ):
            result = _policy(domain="payment").execute(lambda: "ok")

        assert result.outcome == PolicyOutcome.SUCCESS
        get_instance.assert_not_called()


# =============================================================================
# Resolution — the identity gate
# =============================================================================


class TestRetryCoordinationIdentityGateBehavior:
    """The default refuses to coordinate on an unidentified coordination key."""

    def test_placeholder_domain_is_the_config_default(self):
        """The gate's sentinel and RetryPolicyConfig.domain's default are one value.

        If these ever drift, the gate silently stops firing for the callers it
        exists to protect (every surface that defaults its domain).
        """
        assert _UNIDENTIFIED_DOMAIN == RetryPolicyConfig().domain

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
        """Any of the two identity sources is enough, and the key is the one used."""
        coordinator, get_instance = singleton_coordinator

        _policy(domain=domain, rate_limit_key=rate_limit_key).execute(lambda: "ok")

        get_instance.assert_called_once()
        coordinator.wait_if_needed.assert_called_once_with(expected_key, max_wait=None)

    def test_placeholder_domain_without_a_key_does_not_coordinate(
        self, singleton_coordinator
    ):
        """The placeholder is shared by every caller who named nothing.

        Coordinating on it would merge unrelated downstreams into one cooldown
        record — a 429 from one provider would stall calls to another. Wrong
        coordination is worse than none, so the resolution refuses.
        """
        _coordinator, get_instance = singleton_coordinator

        result = _policy(domain=_UNIDENTIFIED_DOMAIN).execute(lambda: "ok")

        assert result.outcome == PolicyOutcome.SUCCESS
        get_instance.assert_not_called()

    def test_explicitly_named_default_domain_is_indistinguishable(
        self, singleton_coordinator
    ):
        """Boundary: a caller who genuinely names "default" gets no coordination.

        The accepted imprecision of a sentinel-valued default. Asserted so the
        trade-off is a decision on record rather than a surprise, and so the
        documented escape (``rate_limit_key``) is the thing that has to work —
        which ``key_rescues_placeholder`` above covers.
        """
        _coordinator, get_instance = singleton_coordinator

        _policy(domain="default").execute(lambda: "ok")

        get_instance.assert_not_called()


# =============================================================================
# Resolution — the unidentified-domain diagnostic
# =============================================================================


class TestRetryUnidentifiedDomainWarningBehavior:
    """The once-per-key WARNING that says a default-on protection is inert here."""

    def test_unidentified_domain_warns_at_warning_level(self, singleton_coordinator):
        """WARNING, not DEBUG — DEBUG is off under any production log config.

        This log is the only runtime signal that a deployment is running without
        the protection the feature advertises; at DEBUG the operator who needs it
        would never see it.
        """
        with capture_logs() as logs:
            _policy(domain=_UNIDENTIFIED_DOMAIN).execute(lambda: "ok")

        skipped = [
            entry
            for entry in logs
            if entry["event"] == "retry.rate_limit_coordination_skipped"
        ]
        assert len(skipped) == 1
        assert skipped[0]["log_level"] == "warning"
        assert skipped[0]["reason"] == "unidentified_domain"

    def test_warning_names_the_remedy(self, singleton_coordinator):
        """The line carries the fix, not just the complaint."""
        with capture_logs() as logs:
            _policy(domain=_UNIDENTIFIED_DOMAIN).execute(lambda: "ok")

        remedy = next(
            entry["remedy"]
            for entry in logs
            if entry["event"] == "retry.rate_limit_coordination_skipped"
        )
        assert "domain" in remedy
        assert "rate_limit_key" in remedy

    def test_warning_fires_once_per_key_per_process(self, singleton_coordinator):
        """Idempotency: the dedup is what makes WARNING affordable.

        Without it, a default-on protection would emit one WARNING per call on
        every unnamed call site in the process.
        """
        policy = _policy(domain=_UNIDENTIFIED_DOMAIN)

        with capture_logs() as logs:
            for _ in range(5):
                policy.execute(lambda: "ok")

        skipped = [
            entry
            for entry in logs
            if entry["event"] == "retry.rate_limit_coordination_skipped"
        ]
        assert len(skipped) == 1

    def test_the_dedup_is_process_wide_not_per_policy_instance(
        self, singleton_coordinator
    ):
        """A second unnamed call site does not re-emit the line.

        The dedup set is keyed on the domain, and the only domain that ever
        reaches this warn is the placeholder — the gate is not entered when a
        ``rate_limit_key`` was given, and a named domain does not warn at all.
        So "once per key" is, in practice, once per process, and holding the set
        on the instance instead would put the line back on every call site.
        """
        with capture_logs() as logs:
            _policy(domain=_UNIDENTIFIED_DOMAIN).execute(lambda: "ok")
            _policy(domain=_UNIDENTIFIED_DOMAIN).execute(lambda: "ok")

        skipped = [
            entry
            for entry in logs
            if entry["event"] == "retry.rate_limit_coordination_skipped"
        ]
        assert len(skipped) == 1

    @pytest.mark.parametrize(
        ("rate_limit_aware", "switch_enabled"),
        [(False, True), (True, False)],
        ids=["config_off", "switch_off"],
    )
    def test_opted_out_callers_are_not_warned(
        self,
        singleton_coordinator,
        coordination_switch,
        rate_limit_aware,
        switch_enabled,
    ):
        """Evaluation order is load-bearing: both levers are checked before the gate.

        The identity gate is the only conjunct that logs, so it must be last. An
        operator who deliberately turned coordination off must not then be told
        to configure the thing they disabled — and both levers reach this state
        with the placeholder domain in play.
        """
        coordination_switch(switch_enabled)

        with capture_logs() as logs:
            _policy(
                domain=_UNIDENTIFIED_DOMAIN, rate_limit_aware=rate_limit_aware
            ).execute(lambda: "ok")

        assert not [
            entry
            for entry in logs
            if entry["event"] == "retry.rate_limit_coordination_skipped"
        ]

    def test_identified_callers_are_not_warned(self, singleton_coordinator):
        """Negative: the diagnostic is scoped to the case it describes."""
        with capture_logs() as logs:
            _policy(domain="payment").execute(lambda: "ok")

        assert not [
            entry
            for entry in logs
            if entry["event"] == "retry.rate_limit_coordination_skipped"
        ]


# =============================================================================
# _notify_rate_limit_cooldown — passed coordinator + detection return
# =============================================================================


class TestRetryRateLimitNotifyBehavior:
    """The notify helper takes its coordinator and reports what it detected."""

    def test_detected_429_records_a_cooldown_and_returns_true(self):
        """The return value is the loop's rate-limit signal, not decoration.

        It is what gates the success-side reset, so a helper that recorded the
        cooldown but reported ``False`` would leave the counter standing.
        """
        coordinator = MagicMock(spec=RateLimitCoordinator)
        coordinator.on_rate_limited.return_value = 12.0
        policy = _policy(domain="payment")

        detected = policy._notify_rate_limit_cooldown(
            coordinator, "payment", Exception(_RATE_LIMIT_MESSAGE)
        )

        assert detected is True
        coordinator.on_rate_limited.assert_called_once()
        assert coordinator.on_rate_limited.call_args.kwargs["key"] == "payment"

    def test_non_429_failure_writes_nothing_and_returns_false(self):
        """Non-429 inertness: an ordinary failure must not touch storage.

        Every retry-staged call now runs through this helper, so a classifier
        that leaked would put a cooldown write on every ordinary error path.
        """
        coordinator = MagicMock(spec=RateLimitCoordinator)
        policy = _policy(domain="payment")

        detected = policy._notify_rate_limit_cooldown(
            coordinator, "payment", ConnectionError("connection reset")
        )

        assert detected is False
        coordinator.on_rate_limited.assert_not_called()

    def test_the_passed_coordinator_is_used_not_the_injected_one(self):
        """The effective coordinator is resolved per call and may not be the field.

        Reading ``self._rate_limit_coordinator`` here would have silently missed
        every locally resolved coordinator: the guard would fall through with no
        log and no exception, so the loop would wait on cooldowns it never
        recorded.
        """
        injected = MagicMock(spec=RateLimitCoordinator)
        resolved = MagicMock(spec=RateLimitCoordinator)
        resolved.on_rate_limited.return_value = 5.0
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=1, domain="payment"),
            rate_limit_coordinator=injected,
            sleeper=lambda _: None,
        )

        policy._notify_rate_limit_cooldown(
            resolved, "payment", Exception(_RATE_LIMIT_MESSAGE)
        )

        resolved.on_rate_limited.assert_called_once()
        injected.on_rate_limited.assert_not_called()

    def test_retry_after_is_forwarded_to_the_coordinator(self):
        """A provider's Retry-After survives the hop into the cooldown.

        Dropping it here would fall the coordinator back to its own default
        delay, so the fleet would ignore the one number the provider actually
        told it to wait.
        """

        class ThrottledError(Exception):
            retry_after = 30.0

        coordinator = MagicMock(spec=RateLimitCoordinator)
        coordinator.on_rate_limited.return_value = 30.0
        policy = _policy(domain="payment")

        policy._notify_rate_limit_cooldown(
            coordinator, "payment", ThrottledError(_RATE_LIMIT_MESSAGE)
        )

        assert coordinator.on_rate_limited.call_args.kwargs["retry_after"] == 30.0


# =============================================================================
# execute() — key sourcing across all three coordinator call sites
# =============================================================================


class TestRetryCoordinationKeyBehavior:
    """One key is chosen per call and every coordinator call site uses it."""

    def _drive_a_full_signal_cycle(self, coordinator, policy):
        """429 on attempt 1, success on attempt 2 — reaches all three call sites."""
        coordinator.wait_if_needed.return_value = RateLimitResult(waited=False)
        coordinator.on_rate_limited.return_value = 0.0
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception(_RATE_LIMIT_MESSAGE)
            return "ok"

        return policy.execute(flaky)

    def test_unset_key_falls_back_to_domain_on_every_call_site(
        self, singleton_coordinator
    ):
        """Default key sourcing, asserted at wait / notify / success together.

        A per-site divergence is the failure that matters: waiting on one key
        while recording the cooldown under another produces coordination that
        never converges.
        """
        coordinator, _ = singleton_coordinator
        policy = _policy(max_attempts=2, domain="payment")

        result = self._drive_a_full_signal_cycle(coordinator, policy)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert coordinator.wait_if_needed.call_args.args[0] == "payment"
        assert coordinator.on_rate_limited.call_args.kwargs["key"] == "payment"
        coordinator.on_success.assert_called_once_with("payment")

    def test_rate_limit_key_overrides_domain_on_every_call_site(
        self, singleton_coordinator
    ):
        """The override is the escape for callers whose domain is not the downstream.

        Two call sites hitting one provider under different domains do not share
        a cooldown unless they say so with this key.
        """
        coordinator, _ = singleton_coordinator
        policy = _policy(max_attempts=2, domain="payment", rate_limit_key="stripe-api")

        self._drive_a_full_signal_cycle(coordinator, policy)

        assert coordinator.wait_if_needed.call_args.args[0] == "stripe-api"
        assert coordinator.on_rate_limited.call_args.kwargs["key"] == "stripe-api"
        coordinator.on_success.assert_called_once_with("stripe-api")


# =============================================================================
# execute() — D5 on_success gating
# =============================================================================


class TestRetryOnSuccessGatingBehavior:
    """on_success is owed only once the call has observed a rate-limit signal."""

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
        """Either half of the wait result counts as a signal; neither means silence.

        ``on_success`` costs a storage read plus a conditional reset write, and a
        call that never met a cooldown owes neither.
        """
        coordinator, _ = singleton_coordinator
        coordinator.wait_if_needed.return_value = wait_result

        _policy(domain="payment").execute(lambda: "ok")

        assert coordinator.on_success.called is expected

    def test_a_detected_429_makes_the_later_success_report(self, singleton_coordinator):
        """The third signal source is the detection return, not the wait result."""
        coordinator, _ = singleton_coordinator
        coordinator.wait_if_needed.return_value = RateLimitResult(waited=False)
        coordinator.on_rate_limited.return_value = 0.0
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception(_RATE_LIMIT_MESSAGE)
            return "ok"

        _policy(max_attempts=2, domain="payment").execute(flaky)

        coordinator.on_success.assert_called_once_with("payment")

    def test_a_non_429_failure_then_success_reports_nothing(
        self, singleton_coordinator
    ):
        """Negative: an ordinary retry is not a rate-limit signal."""
        coordinator, _ = singleton_coordinator
        coordinator.wait_if_needed.return_value = RateLimitResult(waited=False)
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("connection reset")
            return "ok"

        _policy(max_attempts=2, domain="payment").execute(flaky)

        coordinator.on_success.assert_not_called()

    def test_clean_path_costs_one_storage_read_per_attempt_and_no_more(
        self, no_cluster_broadcast
    ):
        """The cost claim, measured at the storage adapter rather than assumed.

        Default-on adds this read to every retry-staged call, so the containment
        is the thing the tier baselines were re-checked against: one read per
        attempt for the cooldown consult, and zero success-side I/O.
        """
        storage = MagicMock(wraps=InMemoryRateLimitStorage())
        with patch.object(
            RateLimitCoordinator,
            "get_instance",
            autospec=True,
            return_value=_real_coordinator(storage),
        ):
            result = _policy(max_attempts=3, domain="payment").execute(lambda: "ok")

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.total_attempts == 1
        assert storage.get_state.call_count == 1
        storage.reset_consecutive_429s.assert_not_called()

    def test_a_signalled_call_does_pay_the_success_side_reset(
        self, no_cluster_broadcast
    ):
        """The gate withholds the reset; it must not lose it.

        A 429 that escalates the counter and is never reset leaves the next
        cooldown starting a rung too high for the rest of the process's life.
        """
        storage = MagicMock(wraps=InMemoryRateLimitStorage())
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception(_RATE_LIMIT_MESSAGE)
            return "ok"

        with patch.object(
            RateLimitCoordinator,
            "get_instance",
            autospec=True,
            return_value=_real_coordinator(storage, default_retry_after=0.0),
        ):
            with mock_sleep():
                result = _policy(max_attempts=3, domain="payment").execute(flaky)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert storage.get_state("payment").consecutive_429s == 0


# =============================================================================
# execute() — the suppression paths coordinate nothing
# =============================================================================


class TestRetrySuppressedCoordinationBehavior:
    """Retry-disabled and observe-only calls resolve no coordinator at all.

    Resolution sits after both early returns on purpose: each takes the
    single-attempt path, which coordinates nothing, so resolving at method entry
    would build the singleton — and with it storage auto-detect and a Redis
    connect — for a call that cannot use it.
    """

    def test_globally_disabled_retry_does_not_resolve(
        self, singleton_coordinator, monkeypatch
    ):
        """BALDUR_RETRY_ENABLED=false suppresses the intervention wholesale."""
        _coordinator, get_instance = singleton_coordinator
        monkeypatch.setenv("BALDUR_RETRY_ENABLED", "false")
        reset_retry_settings()
        try:
            policy = _policy(domain="payment")
            result = policy.execute(lambda: "ok")
        finally:
            reset_retry_settings()

        assert result.outcome == PolicyOutcome.SUCCESS
        get_instance.assert_not_called()

    def test_observe_only_mode_does_not_resolve(self, singleton_coordinator):
        """Shadow mode must stay side-effect free, and a cooldown wait is a side effect.

        Coordination would also *claim the canary slot* — an observe-only call
        starving a real one is exactly the production side effect the mode
        promises not to have.
        """
        _coordinator, get_instance = singleton_coordinator
        set_execution_mode(ExecutionMode.shadow())
        try:
            result = _policy(domain="payment").execute(lambda: "ok")
        finally:
            # The override is process-global and no autouse fixture clears it;
            # leaking shadow mode would silently suppress retry — and with it
            # coordination — in every test that follows on this worker.
            clear_execution_mode_override()

        assert result.outcome == PolicyOutcome.SUCCESS
        get_instance.assert_not_called()

    def test_observe_only_suppression_is_the_reason_not_a_missing_domain(
        self, singleton_coordinator
    ):
        """Discriminator: the same call coordinates once the mode is normal.

        Without this pair, the negative above would pass just as well if the
        domain had silently stopped being identified.
        """
        _coordinator, get_instance = singleton_coordinator

        _policy(domain="payment").execute(lambda: "ok")

        get_instance.assert_called_once()


# =============================================================================
# execute() — coordinator faults on the newly wired path
# =============================================================================


class TestRetryCoordinatorFaultBehavior:
    """A resolved coordinator's faults stay out of the business outcome.

    The pre-existing fail-open tests inject the coordinator. These drive the same
    call sites through the *default* resolution, which is the path that now
    carries every retry-staged call.
    """

    def test_wait_fault_on_a_resolved_coordinator_preserves_success(
        self, singleton_coordinator
    ):
        coordinator, _ = singleton_coordinator
        coordinator.wait_if_needed.side_effect = RuntimeError("coordinator down")

        result = _policy(domain="payment").execute(lambda: "ok")

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"

    def test_notify_fault_on_a_resolved_coordinator_preserves_the_business_error(
        self, singleton_coordinator
    ):
        """The 429 the caller actually got must not be replaced by our fault."""
        coordinator, _ = singleton_coordinator
        coordinator.on_rate_limited.side_effect = RuntimeError("coordinator down")
        business_error = Exception(_RATE_LIMIT_MESSAGE)

        result = _policy(domain="payment").execute(
            lambda: (_ for _ in ()).throw(business_error)
        )

        assert result.error is business_error

    def test_success_notify_fault_on_a_resolved_coordinator_preserves_success(
        self, singleton_coordinator
    ):
        """Site 3 is only reachable once a signal was observed, so one is staged."""
        coordinator, _ = singleton_coordinator
        coordinator.wait_if_needed.return_value = RateLimitResult(
            waited=True, wait_time=0.01, was_rate_limited=True
        )
        coordinator.on_success.side_effect = RuntimeError("coordinator down")

        result = _policy(domain="payment").execute(lambda: "ok")

        coordinator.on_success.assert_called_once()
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"
        assert result.error is None


# =============================================================================
# execute() — a slow storage backend
# =============================================================================


class TestRetrySlowStorageBoundBehavior:
    """The added per-attempt read is bounded by the Redis client, by nothing else.

    The fail-open wraps catch *exceptions*. A backend that is alive-but-slow
    raises nothing, so no wrap fires and the latency lands directly on the
    business call. ``RedisRateLimitStorage.get_state`` issues a 3-GET pipeline
    with no local cache and no adapter-level deadline, so the only bound in the
    stack is whatever socket timeout the deployment's client carries.
    """

    # The stub's simulated latency. Nothing asserts wall-clock against it:
    # Windows' ~15.6 ms monotonic granularity lets a real ``time.sleep`` return
    # measurably early, which made an "elapsed >= latency" assertion flaky for a
    # reason having nothing to do with the retry stage. The deterministic
    # equivalent is below — a deadline, had one existed, would have to either
    # abandon the slow read or discard its result, and both are observable.
    _STORAGE_LATENCY_SECONDS = 0.05

    def test_a_slow_read_runs_to_completion_and_its_result_is_honored(
        self, no_cluster_broadcast
    ):
        """Nothing in the retry stage cuts the storage read short.

        This is the honest form of the claim: the read is not abandoned and its
        answer is not discarded, so its whole latency lands on the business call
        and an operator sizing the feature must read the bound off their Redis
        client's socket timeout rather than off Baldur.

        The slow read returns an *active cooldown*, which is what makes the
        assertion sharp — a deadline that gave up on the read would leave the
        call to proceed uncoordinated, and this call visibly deferred on the
        state the slow read produced.
        """
        inner = InMemoryRateLimitStorage()
        inner.set_cooldown("payment", time.time() + 300.0)
        storage = MagicMock(wraps=inner)
        entered = {"n": 0}

        def slow_get_state(key):
            entered["n"] += 1
            time.sleep(self._STORAGE_LATENCY_SECONDS)
            return inner.get_state(key)

        storage.get_state.side_effect = slow_get_state
        calls = {"n": 0}

        def func():
            calls["n"] += 1
            return "ok"

        with patch.object(
            RateLimitCoordinator,
            "get_instance",
            autospec=True,
            return_value=_real_coordinator(storage),
        ):
            policy = RetryPolicy(
                config=RetryPolicyConfig(
                    max_attempts=3, domain="payment", max_elapsed=5.0
                ),
                backoff=ConstantBackoff(delay=0.0),
                sleeper=lambda _: None,
            )
            with mock_sleep():
                result = policy.execute(func)

        assert entered["n"] == 1  # the read was made, once, on the call's path
        # And it was waited on to completion: the cooldown it slowly reported is
        # the reason this call never ran.
        assert result.metadata["reason"] == "rate_limit_deferred"
        assert calls["n"] == 0

    def test_a_storage_timeout_is_absorbed_fail_open(self, no_cluster_broadcast):
        """Once the client timeout does fire, the wrap turns it into no coordination.

        The complement of the test above: slowness is unbounded by us, but the
        moment it surfaces as an exception the business call is protected.
        """
        storage = MagicMock(wraps=InMemoryRateLimitStorage())
        storage.get_state.side_effect = TimeoutError("redis socket timeout")

        with patch.object(
            RateLimitCoordinator,
            "get_instance",
            autospec=True,
            return_value=_real_coordinator(storage),
        ):
            result = _policy(domain="payment").execute(lambda: "ok")

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"


# =============================================================================
# execute() — the bounded wait, reached through the default wiring
# =============================================================================


class TestRetryWiredCooldownDeferralBehavior:
    """Default-on never reintroduces an unbounded wait.

    The bounded serve-or-defer semantics are already covered against an injected
    coordinator. What this asserts is that the *default* resolution lands on the
    same bounded contract — the risk being that default-on quietly spreads an
    unbounded sleep onto every canonical path.
    """

    def test_over_budget_cooldown_defers_instead_of_sleeping(
        self, no_cluster_broadcast
    ):
        storage = InMemoryRateLimitStorage()
        storage.set_cooldown("payment", time.time() + 300.0)

        with patch.object(
            RateLimitCoordinator,
            "get_instance",
            autospec=True,
            return_value=_real_coordinator(storage),
        ):
            policy = RetryPolicy(
                config=RetryPolicyConfig(
                    max_attempts=3, domain="payment", max_elapsed=5.0
                ),
                backoff=ConstantBackoff(delay=0.0),
                sleeper=lambda _: None,
            )
            calls = {"n": 0}

            def func():
                calls["n"] += 1
                return "ok"

            with mock_sleep() as sleep_mock:
                result = policy.execute(func)

        assert result.metadata["reason"] == "rate_limit_deferred"
        assert result.metadata["not_before"] is not None
        assert calls["n"] == 0  # the call was refused, not delayed
        assert sleep_mock.call_count == 0  # and nothing slept past the bound

    def test_a_cooldown_within_budget_is_served_not_deferred(
        self, no_cluster_broadcast
    ):
        """Discriminator: the deferral above is the bound firing, not blanket refusal."""
        storage = InMemoryRateLimitStorage()
        storage.set_cooldown("payment", time.time() + 0.5)

        with patch.object(
            RateLimitCoordinator,
            "get_instance",
            autospec=True,
            return_value=_real_coordinator(storage),
        ):
            policy = RetryPolicy(
                config=RetryPolicyConfig(
                    max_attempts=3, domain="payment", max_elapsed=30.0
                ),
                backoff=ConstantBackoff(delay=0.0),
                sleeper=lambda _: None,
            )
            with mock_sleep() as sleep_mock:
                result = policy.execute(lambda: "ok")

        assert result.outcome == PolicyOutcome.SUCCESS
        assert any(0.3 <= slept <= 0.6 for slept in sleep_mock.calls)
