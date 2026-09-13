"""Outbound 429-cooldown wait semantics — bounded wait, honor formula, deferral.

Covers the coordinator surface of the bounded serve-or-defer contract:
- ``RateLimitResult`` / ``RateLimitDeferredError`` shape (Contract)
- ``_plan_wait`` / ``_extended_past`` — the pure serve-or-defer step (Contract)
- ``RateLimitCoordinator._compute_cooldown`` honor-with-ceiling formula (Behavior)
- ``RateLimitCoordinator.wait_if_needed(key, max_wait=...)`` serve-vs-defer (Behavior)
- the re-check after each served segment, on both waits (Behavior)
- ``RateLimitCoordinator.await_if_needed`` — the awaitable twin's own rules (Behavior)
- ``@rate_limit_aware`` decorator deferral + fail-open, on ``def`` and ``async def`` (Behavior)

Retry-loop and tenacity-bridge surfaces are covered in
``services/test_retry_policy.py`` and ``bridges/tenacity/test_policy.py``.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage
from baldur.core.exceptions import BaldurError, ResilienceError
from baldur.interfaces.rate_limit_storage import RateLimitState
from baldur.services.rate_limit_coordinator import (
    RateLimitCoordinator,
    RateLimitDeferredError,
)
from baldur.services.rate_limit_coordinator.coordinator import (
    _extended_past,
    _plan_wait,
    _WaitKind,
)
from baldur.services.rate_limit_coordinator.models import (
    RateLimitCoordinatorConfig,
    RateLimitResult,
)
from tests.factories.rate_limit_doubles import (
    NetworkBackedRateLimitStorage,
    RaisingRateLimitStorage,
    ToThreadSpy,
)
from tests.factories.time_helpers import freeze_time, mock_sleep

# A cooldown far longer than any bound used below — its exact size is irrelevant
# because the served path's sleep is mocked, so it never actually waits.
_LONG_COOLDOWN_SECONDS = 300.0

# The awaitable wait's sleep primitive and its worker-thread hop, as the
# coordinator module resolves them.
_ASYNC_SLEEP = "baldur.services.rate_limit_coordinator.coordinator.asyncio.sleep"
_TO_THREAD = "baldur.services.rate_limit_coordinator.coordinator.asyncio.to_thread"
_RECORD_WAIT = (
    "baldur.services.rate_limit_coordinator.coordinator._record_rate_limit_wait"
)


def _make_coordinator(storage, **config_overrides) -> RateLimitCoordinator:
    """Coordinator over a given storage, debounce disabled for deterministic events."""
    config = RateLimitCoordinatorConfig(debounce_window_seconds=0.0, **config_overrides)
    return RateLimitCoordinator(storage=storage, config=config)


class _Response:
    """Minimal response double for the decorator's default 429 detector."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        self.headers: dict[str, str] = {}


# =============================================================================
# Result / exception shape (Contract)
# =============================================================================


class TestRateLimitResultContract:
    """The additive deferral fields default to the non-deferred shape.

    The defaults are load-bearing: every pre-change caller constructs
    ``RateLimitResult`` without them and must keep observing a served/idle result.
    """

    def test_deferral_fields_default_to_non_deferred(self):
        result = RateLimitResult()
        assert result.deferred is False
        assert result.not_before is None

    def test_existing_fields_unchanged(self):
        result = RateLimitResult()
        assert result.waited is False
        assert result.wait_time == 0.0
        assert result.was_rate_limited is False
        assert result.is_canary is False


class TestRateLimitDeferredErrorContract:
    """The outbound-cooldown deferral signal is a ResilienceError with defer context."""

    def test_inherits_resilience_error(self):
        """A bare ``except ResilienceError`` must catch a cooldown deferral."""
        err = RateLimitDeferredError(key="payment_api", not_before=123.0)
        assert isinstance(err, ResilienceError)
        assert isinstance(err, BaldurError)

    def test_extra_context_carries_key_and_not_before(self):
        err = RateLimitDeferredError(key="payment_api", not_before=123.0)
        assert err.extra_context() == {"key": "payment_api", "not_before": 123.0}

    def test_message_includes_key(self):
        err = RateLimitDeferredError(key="payment_api")
        assert "payment_api" in str(err)

    def test_the_package_export_is_the_core_exceptions_class(self):
        """One object under every import path, so ``except`` clauses keep matching."""
        from baldur.core.exceptions import RateLimitDeferredError as from_core
        from baldur.services.rate_limit_coordinator.models import (
            RateLimitDeferredError as from_models,
        )

        assert RateLimitDeferredError is from_core
        assert from_models is from_core


# =============================================================================
# Honor-with-ceiling formula (Behavior)
# =============================================================================


class TestCooldownComputationBehavior:
    """``_compute_cooldown(key, consecutive, retry_after)`` -> (delay, honored, clamped).

    Uses jitter_percent=0 for a deterministic ladder where the exact stored delay
    can be pinned; jitter-sensitivity is covered by the seed-independence tests.
    """

    @pytest.fixture
    def coordinator(self, mock_storage):
        return _make_coordinator(mock_storage, jitter_percent=0.0)

    def _ladder(self, config: RateLimitCoordinatorConfig, consecutive: int) -> float:
        """Headerless ladder value at jitter=0, capped at max_delay (source formula)."""
        raw = config.default_retry_after * (
            config.backoff_multiplier ** (consecutive - 1)
        )
        return min(raw, config.max_delay)

    def test_headerless_uses_capped_ladder(self, coordinator):
        """No header -> the exponential ladder seeded from default_retry_after."""
        config = coordinator._config
        for consecutive in (1, 2, 3):
            delay, honored, clamped = coordinator._compute_cooldown(
                "k", consecutive, None
            )
            assert delay == pytest.approx(self._ladder(config, consecutive))
            assert honored is False
            assert clamped is False

    def test_headerless_saturates_at_max_delay(self, coordinator):
        """A high consecutive count saturates the ladder at max_delay."""
        delay, honored, clamped = coordinator._compute_cooldown("k", 5, None)
        assert delay == coordinator._config.max_delay
        assert honored is False

    def test_header_honored_beyond_max_delay(self, coordinator):
        """retry_after=3600 stores exactly 3600 with honored=True, clamped=False."""
        delay, honored, clamped = coordinator._compute_cooldown("k", 1, 3600.0)
        assert delay == 3600.0
        assert honored is True
        assert clamped is False

    def test_header_above_ceiling_is_clamped_and_marked(self, coordinator):
        """retry_after=7200 clamps to the 3600 ceiling with clamped=True."""
        ceiling = coordinator._config.retry_after_ceiling
        delay, honored, clamped = coordinator._compute_cooldown("k", 1, 7200.0)
        assert delay == ceiling
        assert clamped is True
        assert honored is True

    def test_header_acts_as_floor_below_max_delay(self, coordinator):
        """An in-range header wins over a smaller ladder, not marked honored."""
        # consecutive=1 ladder is default_retry_after (5.0) < 30.
        delay, honored, clamped = coordinator._compute_cooldown("k", 1, 30.0)
        assert delay == 30.0
        assert honored is False  # 30 <= max_delay
        assert clamped is False

    def test_ladder_overtakes_a_small_persistent_header(self, coordinator):
        """When the ladder exceeds the header, the ladder (lying-provider guard) wins."""
        # consecutive=5 ladder saturates at max_delay (60) > header 30.
        delay, _honored, _clamped = coordinator._compute_cooldown("k", 5, 30.0)
        assert delay == coordinator._config.max_delay

    @pytest.mark.parametrize("retry_after", [0.0, -1.0], ids=["zero", "negative"])
    def test_non_positive_header_is_ignored(self, coordinator, retry_after):
        """A non-positive Retry-After is treated as headerless."""
        delay, honored, clamped = coordinator._compute_cooldown("k", 1, retry_after)
        assert delay == pytest.approx(self._ladder(coordinator._config, 1))
        assert honored is False


class TestCooldownComputationSeedIndependenceBehavior:
    """With jitter ON, the header floor is never undercut regardless of jitter draw."""

    @pytest.fixture
    def coordinator(self, mock_storage):
        # Shipped default jitter (30%).
        return _make_coordinator(mock_storage)

    def test_header_floor_wins_exactly_across_seeds(self, coordinator):
        """retry_after=30, consecutive=3: the header floor stores exactly 30 every draw.

        The consecutive=3 ladder (~20 +/- jitter) never reaches 30, so the header
        floor wins exactly — and it never lands in the old header-seeded escalation
        band [42, 78] that pre-D3 code produced.
        """
        for _ in range(500):
            delay, _honored, _clamped = coordinator._compute_cooldown("k", 3, 30.0)
            assert delay == 30.0

    def test_header_is_never_undercut_by_downward_jitter(self, coordinator):
        """A stored cooldown is never below the provider-stated Retry-After."""
        for _ in range(500):
            delay, _honored, _clamped = coordinator._compute_cooldown("k", 1, 30.0)
            assert delay >= 30.0

    def test_ladder_saturates_above_header_across_seeds(self, coordinator):
        """consecutive=5 ladder (80 -> inward jitter [42, 60]) always exceeds a 30 header."""
        for _ in range(500):
            delay, _honored, _clamped = coordinator._compute_cooldown("k", 5, 30.0)
            assert delay > 30.0
            assert delay <= coordinator._config.max_delay


# =============================================================================
# Bounded serve-or-defer wait (Behavior)
# =============================================================================


class TestBoundedWaitBehavior:
    """``wait_if_needed(key, max_wait)`` sleeps a fitting cooldown, else defers."""

    def _cooldown(self, storage, key: str, seconds: float) -> float:
        cooldown_until = time.time() + seconds
        storage.set_cooldown(key, cooldown_until)
        return cooldown_until

    def test_served_when_remaining_fits_the_bound(self, mock_storage):
        """remaining <= bound -> sleep the full remaining, waited=True."""
        coord = _make_coordinator(mock_storage)
        self._cooldown(mock_storage, "k", 2.0)

        with mock_sleep() as sleep_mock:
            result = coord.wait_if_needed("k", max_wait=10.0)

        assert result.waited is True
        assert result.deferred is False
        assert sleep_mock.call_count == 1
        # Slept no more than the bound and no more than the remaining cooldown.
        assert sleep_mock.calls[0] <= 10.0
        assert sleep_mock.calls[0] == pytest.approx(result.wait_time)

    def test_deferred_when_remaining_exceeds_bound_sleeps_nothing(self, mock_storage):
        """remaining > bound -> deferred=True, not_before set, and NO sleep at all."""
        coord = _make_coordinator(mock_storage)
        cooldown_until = self._cooldown(mock_storage, "k", _LONG_COOLDOWN_SECONDS)

        with mock_sleep() as sleep_mock:
            result = coord.wait_if_needed("k", max_wait=1.0)

        assert result.deferred is True
        assert result.waited is False
        assert result.wait_time == 0.0
        assert result.not_before == cooldown_until
        # Negative: the deferral path must never sleep a partial slice.
        assert sleep_mock.call_count == 0

    def test_boundary_just_under_bound_serves_just_over_defers(self, mock_storage):
        """The serve-vs-defer split pins the ``remaining > bound`` comparison."""
        coord = _make_coordinator(mock_storage)

        # remaining ~5s, bound 10s -> served.
        self._cooldown(mock_storage, "under", 5.0)
        with mock_sleep():
            assert coord.wait_if_needed("under", max_wait=10.0).waited is True

        # remaining ~50s, bound 10s -> deferred.
        self._cooldown(mock_storage, "over", 50.0)
        with mock_sleep() as sleep_mock:
            assert coord.wait_if_needed("over", max_wait=10.0).deferred is True
            assert sleep_mock.call_count == 0

    def test_default_bound_is_max_delay(self, mock_storage):
        """max_wait=None uses config.max_delay as the serve bound."""
        coord = _make_coordinator(mock_storage, max_delay=10.0)

        # remaining ~5s < max_delay 10 -> served without an explicit bound.
        self._cooldown(mock_storage, "k", 5.0)
        with mock_sleep() as sleep_mock:
            result = coord.wait_if_needed("k")
        assert result.waited is True
        assert sleep_mock.call_count == 1

    def test_infinite_bound_always_serves(self, mock_storage):
        """max_wait=inf opts into an unbounded wait — even a very long cooldown serves."""
        coord = _make_coordinator(mock_storage, max_delay=10.0)
        self._cooldown(mock_storage, "k", _LONG_COOLDOWN_SECONDS)

        with mock_sleep() as sleep_mock:
            result = coord.wait_if_needed("k", max_wait=float("inf"))

        assert result.waited is True
        assert result.deferred is False
        assert sleep_mock.call_count == 1

    def test_no_cooldown_returns_idle_result(self, mock_storage):
        """Outside cooldown, neither serves nor defers."""
        coord = _make_coordinator(mock_storage)
        with mock_sleep() as sleep_mock:
            result = coord.wait_if_needed("k", max_wait=1.0)
        assert result.waited is False
        assert result.deferred is False
        assert sleep_mock.call_count == 0

    def test_deferral_does_not_mutate_stored_state(self, mock_storage):
        """A deferral leaves cooldown_until and consecutive_429s untouched (idempotent)."""
        coord = _make_coordinator(mock_storage)
        cooldown_until = self._cooldown(mock_storage, "k", _LONG_COOLDOWN_SECONDS)
        mock_storage.increment_consecutive_429s("k")
        before = mock_storage.get_state("k")
        before_consecutive = before.consecutive_429s

        with mock_sleep():
            coord.wait_if_needed("k", max_wait=1.0)

        after = mock_storage.get_state("k")
        assert after.cooldown_until == cooldown_until
        assert after.consecutive_429s == before_consecutive


# =============================================================================
# Decorator surface: deferral raise + fail-open (Behavior)
# =============================================================================


class TestRateLimitAwareDecoratorBehavior:
    """``@rate_limit_aware`` raises on deferral and stays fail-open on coordinator faults."""

    def test_deferral_raises_and_skips_the_wrapped_call(self, mock_storage):
        """Over-bound cooldown -> RateLimitDeferredError, func never called."""
        coord = _make_coordinator(mock_storage)
        cooldown_until = time.time() + _LONG_COOLDOWN_SECONDS
        mock_storage.set_cooldown("k", cooldown_until)
        calls = []

        @coord.rate_limit_aware("k", max_wait=1.0)
        def protected():
            calls.append(1)
            return _Response(200)

        with mock_sleep():
            with pytest.raises(RateLimitDeferredError) as exc_info:
                protected()

        assert exc_info.value.not_before == cooldown_until
        assert calls == []  # the wrapped call was skipped

    def test_wait_fault_is_fail_open(self, mock_storage):
        """A coordinator fault at the wait site proceeds to the call (result preserved)."""
        storage = RaisingRateLimitStorage(mock_storage, fail_on="get_state")
        coord = _make_coordinator(storage)

        @coord.rate_limit_aware("k")
        def protected():
            return _Response(200)

        # Fails the test only if the coordinator fault propagates.
        result = protected()
        assert result.status_code == 200

    def test_on_rate_limited_fault_is_fail_open(self, mock_storage):
        """A fault while recording a 429 cooldown does not replace the business result."""
        storage = RaisingRateLimitStorage(
            mock_storage, fail_on="increment_consecutive_429s"
        )
        coord = _make_coordinator(storage)

        @coord.rate_limit_aware("k")
        def protected():
            return _Response(429)

        result = protected()
        assert result.status_code == 429

    def test_on_success_fault_is_fail_open(self, mock_storage):
        """A fault while resetting the counter after success preserves the result."""
        # Seed a prior 429 so on_success reaches reset_consecutive_429s.
        mock_storage.increment_consecutive_429s("k")
        storage = RaisingRateLimitStorage(
            mock_storage, fail_on="reset_consecutive_429s"
        )
        coord = _make_coordinator(storage)

        @coord.rate_limit_aware("k")
        def protected():
            return _Response(200)

        result = protected()
        assert result.status_code == 200

    def test_user_predicate_exception_still_propagates(self, mock_storage):
        """The user's is_429/get_retry_after callables stay OUTSIDE the fail-open wrap."""
        coord = _make_coordinator(mock_storage)

        def broken_is_429(_response):
            raise ValueError("user predicate bug")

        @coord.rate_limit_aware("k", is_429=broken_is_429)
        def protected():
            return _Response(200)

        with pytest.raises(ValueError, match="user predicate bug"):
            protected()

    def test_deferral_is_not_detected_as_a_provider_429(self, mock_storage):
        """A deferral must never be read back as evidence of a provider 429.

        The deferral means the provider was never contacted. A decorated client
        composed inside a retry loop raises this error into the loop's 429
        classifier, which matches on the exception's *type name* — so nothing in
        the message can prevent it. Left unguarded, Baldur's own refusal escalates
        ``consecutive_429s`` and installs a phantom cooldown on the loop's domain.
        """
        from baldur.services.retry_handler.rate_limit_detection import (
            detect_rate_limit,
        )

        err = RateLimitDeferredError(key="payment_api", not_before=time.time() + 300)

        is_rate_limited, retry_after = detect_rate_limit(err)

        assert is_rate_limited is False
        assert retry_after is None
        # The naive heuristic would match on either of these — pin why the guard
        # cannot be replaced by message wording alone.
        assert "rate limit" in str(err).lower()
        assert "ratelimit" in type(err).__name__.lower()

    def test_deferral_survives_a_healthy_coordinator(self, mock_storage):
        """The deferral raise is never downgraded to a fail-open no-op (D9 ordering)."""
        coord = _make_coordinator(mock_storage)
        mock_storage.set_cooldown("k", time.time() + _LONG_COOLDOWN_SECONDS)
        calls = []

        @coord.rate_limit_aware("k", max_wait=1.0)
        def protected():
            calls.append(1)
            return _Response(200)

        with mock_sleep():
            with pytest.raises(RateLimitDeferredError):
                protected()
        assert calls == []

    def test_the_decorator_claims_the_scope_so_one_429_counts_once(self, mock_storage):
        """A code-level opt-in makes its own coordinator calls, so it claims the call.

        Without the claim the breaker stage above would notify for the same 429,
        and the consecutive counter would advance twice per throttled call —
        doubling the cooldown ladder's climb rate for every decorated client.
        """
        from baldur.services.circuit_breaker.rate_limit_observation import (
            close_scope,
            open_scope,
        )

        coord = _make_coordinator(mock_storage)

        @coord.rate_limit_aware("k")
        def protected():
            return _Response(429)

        token, scope = open_scope("k")
        try:
            protected()
        finally:
            close_scope(token)

        assert scope.coordination_claimed is True
        assert mock_storage.get_state("k").consecutive_429s == 1

    def test_a_call_with_no_scope_open_still_coordinates(self, mock_storage):
        """The decorator does not depend on a breaker stage being above it."""
        coord = _make_coordinator(mock_storage)

        @coord.rate_limit_aware("k")
        def protected():
            return _Response(429)

        protected()

        assert mock_storage.get_state("k").consecutive_429s == 1

    def test_the_default_verdict_is_the_shared_classifier(self, mock_storage):
        """Neither override supplied: one vocabulary answers both halves.

        A decorated call that disagreed with the breaker stage about what a 429
        is would install a cooldown the cascade never counted, or the reverse.
        """
        coord = _make_coordinator(mock_storage)
        response = _Response(429)
        response.headers["Retry-After"] = "45"

        @coord.rate_limit_aware("k")
        def protected():
            return response

        protected()

        assert mock_storage.get_state("k").consecutive_429s == 1
        assert mock_storage.get_state("k").cooldown_until is not None

    def test_an_is_429_override_replaces_only_the_verdict(self, mock_storage):
        """The wait still comes from the shared header reader.

        Each override replaces exactly its own default and neither composes with
        it, so a caller who only wants a different verdict does not silently
        lose the provider's stated wait.
        """
        coord = _make_coordinator(mock_storage)
        response = _Response(200)
        response.headers["Retry-After"] = "45"

        @coord.rate_limit_aware("k", is_429=lambda r: True)
        def protected():
            return response

        protected()

        state = mock_storage.get_state("k")
        assert state.consecutive_429s == 1
        assert state.cooldown_until == pytest.approx(time.time() + 45.0, abs=5.0)

    def test_a_get_retry_after_override_replaces_only_the_wait(self, mock_storage):
        """The verdict still comes from the shared classifier."""
        coord = _make_coordinator(mock_storage)

        @coord.rate_limit_aware("k", get_retry_after=lambda r: 90.0)
        def protected():
            return _Response(429)

        protected()

        state = mock_storage.get_state("k")
        assert state.consecutive_429s == 1
        assert state.cooldown_until == pytest.approx(time.time() + 90.0, abs=5.0)

    def test_a_get_retry_after_override_is_not_consulted_for_a_non_429(
        self, mock_storage
    ):
        """The wait override never turns a success into a rate-limit answer."""
        coord = _make_coordinator(mock_storage)

        @coord.rate_limit_aware("k", get_retry_after=lambda r: 90.0)
        def protected():
            return _Response(200)

        protected()

        assert mock_storage.get_state("k").consecutive_429s == 0

    def test_both_overrides_leave_the_shared_classifier_unconsulted(self, mock_storage):
        """A caller who supplied both owns the whole classification."""
        coord = _make_coordinator(mock_storage)

        @coord.rate_limit_aware(
            "k", is_429=lambda r: True, get_retry_after=lambda r: 30.0
        )
        def protected():
            return _Response(200)

        protected()

        state = mock_storage.get_state("k")
        assert state.consecutive_429s == 1
        assert state.cooldown_until == pytest.approx(time.time() + 30.0, abs=5.0)


# =============================================================================
# The pure serve-or-defer step (Contract)
# =============================================================================


def _state_with_remaining(seconds: float) -> RateLimitState:
    """A state whose cooldown ends exactly ``seconds`` from the (frozen) clock."""
    return RateLimitState(key="k", cooldown_until=time.time() + seconds)


class TestWaitPlannerContract:
    """``_plan_wait`` is the one step both waits take; its edges are the contract.

    A remaining cooldown that *equals* the bound is served — only a remaining
    strictly greater than the bound is refused — and a zero remaining is idle.
    The step is pure: it neither sleeps nor writes, so the two waits cannot
    disagree about serve-or-defer whichever sleep primitive they use.
    """

    @pytest.mark.parametrize(
        ("remaining", "bound", "expected_kind"),
        [
            (10.0, 10.0, _WaitKind.SERVE),
            (10.01, 10.0, _WaitKind.DEFER),
            (9.99, 10.0, _WaitKind.SERVE),
            (0.0, 10.0, _WaitKind.IDLE),
        ],
        ids=["at_bound_serves", "over_bound_defers", "under_bound_serves", "zero_idle"],
    )
    def test_plan_wait_boundary_at_the_bound(self, remaining, bound, expected_kind):
        """The comparison is ``remaining > bound``: equality still serves."""
        with freeze_time("2026-01-01 00:00:00"):
            plan = _plan_wait(_state_with_remaining(remaining), bound)

        assert plan.kind is expected_kind

    def test_a_served_plan_carries_the_full_remaining_cooldown(self):
        """A fitting cooldown is slept in full — never a slice of it."""
        with freeze_time("2026-01-01 00:00:00"):
            plan = _plan_wait(_state_with_remaining(7.5), 60.0)

        assert plan.kind is _WaitKind.SERVE
        assert plan.seconds == pytest.approx(7.5)
        assert plan.not_before is None

    def test_a_deferred_plan_carries_the_expiry_it_refused_to_wait_for(self):
        """The refusal names ``not_before`` so the caller can requeue on it."""
        with freeze_time("2026-01-01 00:00:00"):
            state = _state_with_remaining(120.0)
            plan = _plan_wait(state, 10.0)

        assert plan.kind is _WaitKind.DEFER
        assert plan.not_before == state.cooldown_until
        assert plan.seconds == 0.0

    def test_plan_wait_has_no_side_effect_on_the_state(self):
        """Purity: two reads of one state plan the same step and change nothing."""
        with freeze_time("2026-01-01 00:00:00"):
            state = _state_with_remaining(5.0)
            before = (state.cooldown_until, state.consecutive_429s)

            first = _plan_wait(state, 10.0)
            second = _plan_wait(state, 10.0)

        assert first == second
        assert (state.cooldown_until, state.consecutive_429s) == before

    @pytest.mark.parametrize(
        ("stored_until", "target_until", "expected"),
        [
            (100.0, 90.0, True),
            (90.0, 90.0, False),
            (80.0, 90.0, False),
        ],
        ids=["later_is_an_extension", "same_is_not", "earlier_is_not"],
    )
    def test_extended_past_reads_only_a_later_expiry_as_an_extension(
        self, stored_until, target_until, expected
    ):
        """The re-check continues only past the expiry this waiter slept toward.

        The stored cooldown is monotonic, so a peer's extension is always a
        strictly later expiry; an equal one is the segment just slept, and the
        clock's own granularity must never cost a phantom second sleep.
        """
        state = RateLimitState(key="k", cooldown_until=stored_until)

        assert _extended_past(state, target_until) is expected


class TestDeferredResultContract:
    """The deferral the coordinator builds after a served segment.

    ``deferred=True`` and ``waited=True`` coexist on the fourth exit — a peer's
    extension outgrew what was left of the bound after this call already
    slept — and ``wait_time`` carries the slept amount so the caller's budget
    accounting stays exact. Read ``deferred`` before ``waited``.
    """

    def test_a_deferral_after_a_served_segment_reports_both_flags(self):
        state = RateLimitState(key="k", cooldown_until=time.time() + 60.0)

        result = RateLimitCoordinator._deferred_result("k", state, 15.0, 10.0)

        assert result.deferred is True
        assert result.waited is True
        assert result.wait_time == 10.0
        assert result.not_before == state.cooldown_until
        assert result.was_rate_limited is True
        assert result.is_canary is False

    def test_a_deferral_at_entry_reports_nothing_slept(self):
        """First-iteration semantics are unchanged: ``waited=False``, ``wait_time=0``."""
        state = RateLimitState(key="k", cooldown_until=time.time() + 60.0)

        result = RateLimitCoordinator._deferred_result("k", state, 15.0, 0.0)

        assert result.deferred is True
        assert result.waited is False
        assert result.wait_time == 0.0


# =============================================================================
# The re-check after each served segment — both waits (Behavior)
# =============================================================================


class _RecordingSleep:
    """A sleep stand-in that records each imposed segment and can act as a peer.

    ``on_first`` runs once, inside the first sleep — the moment a peer's 429
    lands while this waiter is asleep. The clock does not advance, so a
    re-read after a segment with no peer write shows the very expiry that was
    slept toward, which is what the expiry-based re-check reads as "served".
    """

    def __init__(self, on_first=None):
        self.calls: list[float] = []
        self._on_first = on_first

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if len(self.calls) == 1 and self._on_first is not None:
            self._on_first()


class _RecordingAsyncSleep(_RecordingSleep):
    """The awaitable twin of :class:`_RecordingSleep`."""

    async def __call__(self, seconds: float) -> None:  # type: ignore[override]
        super().__call__(seconds)


_WAIT_SURFACES = pytest.mark.parametrize(
    "is_async", [False, True], ids=["sync_wait", "awaitable_wait"]
)


def _wait(coord, key, *, max_wait, is_async, on_first_sleep=None):
    """Drive one wait through either surface with its sleep primitive faked.

    Returns ``(result, sleeps)`` — the segments the wait imposed, in order.
    """
    if is_async:
        async_sleep = _RecordingAsyncSleep(on_first_sleep)
        with patch(_ASYNC_SLEEP, new=async_sleep):
            result = asyncio.run(coord.await_if_needed(key, max_wait=max_wait))
        return result, async_sleep.calls

    sleep = _RecordingSleep(on_first_sleep)
    with patch("time.sleep", new=sleep):
        result = coord.wait_if_needed(key, max_wait=max_wait)
    return result, sleep.calls


class TestCooldownWaitExtensionBehavior:
    """A peer that extends the cooldown mid-sleep is served or deferred, never resumed into.

    The stale-sleep defect: the remaining cooldown was computed once at entry
    and slept, so a worker honouring ``Retry-After: 10`` woke at t=10 and called
    into a cooldown a peer had meanwhile extended to t=60 — the self-DDoS the
    coordinator exists to prevent. Both waits now re-read after every served
    segment. Each row runs on the synchronous and the awaitable wait.
    """

    @pytest.fixture
    def storage(self):
        """The real in-process adapter: ``get_state`` returns a snapshot.

        A double that hands back its live record would show the peer's
        extension on the object read *before* the sleep, which is not what any
        shipped adapter does — every one builds a fresh state per read.
        """
        return InMemoryRateLimitStorage()

    _INITIAL_SECONDS = 10.0
    _EXTENDED_SECONDS = 60.0

    def _seed(self, storage, key: str = "k") -> None:
        storage.set_cooldown(key, time.time() + self._INITIAL_SECONDS)

    def _peer_extension(self, storage, key: str = "k"):
        """The peer's 429 landing while this waiter sleeps."""

        def extend():
            storage.extend_cooldown(key, time.time() + self._EXTENDED_SECONDS)

        return extend

    @_WAIT_SURFACES
    def test_an_extension_that_fits_the_bound_is_served_as_a_second_segment(
        self, storage, is_async
    ):
        """Served exit after an extension: two sleeps, the sum in ``wait_time``."""
        coord = _make_coordinator(storage)
        self._seed(storage)

        result, sleeps = _wait(
            coord,
            "k",
            max_wait=100.0,
            is_async=is_async,
            on_first_sleep=self._peer_extension(storage),
        )

        assert result.waited is True
        assert result.deferred is False
        assert len(sleeps) == 2
        assert sleeps[0] == pytest.approx(self._INITIAL_SECONDS, abs=0.5)
        assert sleeps[1] == pytest.approx(self._EXTENDED_SECONDS, abs=0.5)
        assert result.wait_time == pytest.approx(sum(sleeps))

    @_WAIT_SURFACES
    def test_an_extension_past_the_bound_is_deferred_after_the_slept_segment(
        self, storage, is_async
    ):
        """Deferred-after-extension exit: ``deferred`` and ``waited`` both set.

        The extension does not fit what is left of the bound, so the wait
        sleeps nothing further and reports the segment it already slept.
        """
        coord = _make_coordinator(storage)
        self._seed(storage)

        result, sleeps = _wait(
            coord,
            "k",
            max_wait=15.0,
            is_async=is_async,
            on_first_sleep=self._peer_extension(storage),
        )

        assert result.deferred is True
        assert result.waited is True
        assert len(sleeps) == 1
        assert result.wait_time == pytest.approx(sleeps[0])
        assert result.wait_time == pytest.approx(self._INITIAL_SECONDS, abs=0.5)
        # The fresh expiry, not the one this call entered with.
        assert result.not_before == storage.get_state("k").cooldown_until
        assert result.not_before > time.time() + self._INITIAL_SECONDS + 1.0

    @_WAIT_SURFACES
    def test_a_segment_with_no_extension_is_served_after_one_sleep(
        self, storage, is_async
    ):
        """Served exit with no peer: the re-read sees the slept-toward expiry and stops.

        Negative half of the re-check: without a later expiry there is no
        second segment, so a re-check that keyed on "still in cooldown" (the
        clock never advanced under the faked sleep) would sleep a phantom one.
        """
        coord = _make_coordinator(storage)
        self._seed(storage)

        result, sleeps = _wait(coord, "k", max_wait=100.0, is_async=is_async)

        assert result.waited is True
        assert result.deferred is False
        assert len(sleeps) == 1
        assert result.wait_time == pytest.approx(sleeps[0])

    @_WAIT_SURFACES
    def test_the_idle_and_entry_deferral_exits_sleep_nothing(self, storage, is_async):
        """The first two exits are byte-for-byte the pre-change behaviour."""
        coord = _make_coordinator(storage)

        idle, idle_sleeps = _wait(coord, "idle", max_wait=1.0, is_async=is_async)

        storage.set_cooldown("far", time.time() + _LONG_COOLDOWN_SECONDS)
        deferred, deferred_sleeps = _wait(coord, "far", max_wait=1.0, is_async=is_async)

        assert (idle.waited, idle.deferred, idle_sleeps) == (False, False, [])
        assert deferred.deferred is True
        assert deferred.waited is False
        assert deferred.wait_time == 0.0
        assert deferred_sleeps == []

    @_WAIT_SURFACES
    def test_each_served_segment_is_observed_once_before_it_is_slept(
        self, storage, is_async
    ):
        """One imposed-wait observation per segment, and each precedes its sleep.

        The histogram's help text says *imposed*: a peer's extension served
        after a re-read is a second imposed wait, observed as one — and a
        caller killed inside either sleep still had that segment imposed.
        """
        coord = _make_coordinator(storage)
        self._seed(storage)
        observed: list[float] = []
        order: list[str] = []

        def record(*, key, wait_seconds):
            observed.append(wait_seconds)
            order.append("observe")

        def extend_and_mark():
            order.append("sleep")
            self._peer_extension(storage)()

        with patch(_RECORD_WAIT, autospec=True, side_effect=record):
            _result, sleeps = _wait(
                coord,
                "k",
                max_wait=100.0,
                is_async=is_async,
                on_first_sleep=extend_and_mark,
            )

        assert len(observed) == 2
        assert observed == pytest.approx(sleeps)
        assert order[:2] == ["observe", "sleep"]

    @_WAIT_SURFACES
    def test_the_total_sleep_never_exceeds_the_bound(self, storage, is_async):
        """Each segment is bounded by what is left, so the sum stays under the bound."""
        coord = _make_coordinator(storage)
        self._seed(storage)
        bound = 40.0

        result, sleeps = _wait(
            coord,
            "k",
            max_wait=bound,
            is_async=is_async,
            on_first_sleep=self._peer_extension(storage),
        )

        # The 60 s extension does not fit 30 s of remaining bound: deferred.
        assert result.deferred is True
        assert sum(sleeps) <= bound
        assert result.wait_time <= bound

    @_WAIT_SURFACES
    def test_the_function_is_not_called_before_the_extension_elapses(
        self, storage, is_async
    ):
        """The negative the fix exists for: no call at t=10 when the peer extended to t=60.

        Read at the decorator, the nearest caller: the wrapped function must
        see both segments slept before it runs, never just the first.
        """
        coord = _make_coordinator(storage)
        self._seed(storage)
        seen: dict[str, int] = {}

        if is_async:
            async_sleep = _RecordingAsyncSleep(self._peer_extension(storage))

            @coord.rate_limit_aware("k", max_wait=100.0)
            async def protected():
                seen["segments_slept"] = len(async_sleep.calls)
                return _Response(200)

            with patch(_ASYNC_SLEEP, new=async_sleep):
                asyncio.run(protected())
        else:
            sleep = _RecordingSleep(self._peer_extension(storage))

            @coord.rate_limit_aware("k", max_wait=100.0)
            def protected():
                seen["segments_slept"] = len(sleep.calls)
                return _Response(200)

            with patch("time.sleep", new=sleep):
                protected()

        assert seen["segments_slept"] == 2
        assert seen["segments_slept"] != 1


# =============================================================================
# The awaitable wait's own rules (Behavior)
# =============================================================================


class TestAwaitIfNeededBehavior:
    """``await_if_needed`` — same decision as the sync wait, off-loop reads, on-loop sleep."""

    def test_the_idle_read_decides_the_canary_like_the_sync_wait(self, mock_storage):
        """The first idle reader after a storm is the canary on this surface too."""
        coord = _make_coordinator(mock_storage)
        mock_storage.increment_consecutive_429s("k")

        result = asyncio.run(coord.await_if_needed("k", max_wait=1.0))

        assert result.waited is False
        assert result.was_rate_limited is True
        assert result.is_canary is True

    def test_a_memory_store_is_read_inline_with_no_thread_hop(self):
        """The in-process store is a dict read under a lock: a hop would cost more."""
        coord = _make_coordinator(InMemoryRateLimitStorage())
        spy = ToThreadSpy()

        with patch(_TO_THREAD, new=spy):
            asyncio.run(coord.await_if_needed("k", max_wait=1.0))

        assert spy.calls == []

    def test_a_network_backed_store_is_read_on_a_worker_thread(self):
        """Every store read for a network client leaves the event loop."""
        coord = _make_coordinator(NetworkBackedRateLimitStorage())
        spy = ToThreadSpy()

        with patch(_TO_THREAD, new=spy):
            asyncio.run(coord.await_if_needed("k", max_wait=1.0))

        assert spy.hopped("get_state") is True
        assert spy.hopped("ensure_running") is True

    def test_a_served_segment_re_reads_on_a_worker_thread_too(self):
        """The re-check after a sleep is a store read and follows the same rule."""
        storage = NetworkBackedRateLimitStorage()
        storage.set_cooldown("k", time.time() + 2.0)
        coord = _make_coordinator(storage)
        spy = ToThreadSpy()

        with (
            patch(_TO_THREAD, new=spy),
            patch(_ASYNC_SLEEP, new=_RecordingAsyncSleep()),
        ):
            result = asyncio.run(coord.await_if_needed("k", max_wait=10.0))

        assert result.waited is True
        reads = [fn for fn in spy.calls if getattr(fn, "__name__", "") == "get_state"]
        assert len(reads) == 2

    def test_cancellation_during_the_sleep_propagates(self):
        """A cancelled waiter is cancelled — the sleep is an ordinary ``await``."""
        storage = InMemoryRateLimitStorage()
        storage.set_cooldown("k", time.time() + _LONG_COOLDOWN_SECONDS)
        coord = _make_coordinator(storage)

        async def scenario():
            task = asyncio.create_task(
                coord.await_if_needed("k", max_wait=float("inf"))
            )
            await asyncio.sleep(0.01)
            task.cancel()
            await task

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(scenario())

    def test_a_deferral_is_a_result_not_an_exception(self, mock_storage):
        """The caller reads ``deferred`` off the result; nothing is raised."""
        coord = _make_coordinator(mock_storage)
        mock_storage.set_cooldown("k", time.time() + _LONG_COOLDOWN_SECONDS)

        result = asyncio.run(coord.await_if_needed("k", max_wait=1.0))

        assert result.deferred is True
        assert result.not_before == mock_storage.get_state("k").cooldown_until

    def test_a_store_fault_raises_to_the_caller(self, mock_storage):
        """The coordinator does not wrap its store: each caller owns its fail-open."""
        coord = _make_coordinator(
            RaisingRateLimitStorage(mock_storage, fail_on="get_state")
        )

        with pytest.raises(RuntimeError, match="storage down"):
            asyncio.run(coord.await_if_needed("k", max_wait=1.0))


# =============================================================================
# Decorator surface on an ``async def`` (Behavior)
# =============================================================================


class TestRateLimitAwareAsyncDecoratorBehavior:
    """``@rate_limit_aware`` on an ``async def`` mirrors the ``def`` rows, loop-free.

    The wrapper is itself a coroutine function; its wait is ``await_if_needed``
    and its notifications are the awaitable twins, so a cooldown never blocks
    the event loop and every fail-open rule of the synchronous wrapper holds.
    """

    @pytest.fixture(autouse=True)
    def _no_cluster_broadcast(self):
        """Neutralise the Dormant-tier cluster broadcast a real 429 would fire.

        ``on_rate_limited`` reaches ``_broadcast_to_cluster``, which eagerly
        attempts a broker connection where the Kafka adapter is installed — an
        explicit NON-GOAL of this surface, and ~2 s of connect timeout per 429.
        """
        from baldur.services.rate_limit_coordinator import RateLimitCoordinator

        with patch.object(RateLimitCoordinator, "_broadcast_to_cluster", autospec=True):
            yield

    def test_the_wrapper_is_a_coroutine_function(self, mock_storage):
        """Dual dispatch: decorating an ``async def`` yields an ``async def``."""
        coord = _make_coordinator(mock_storage)

        @coord.rate_limit_aware("k")
        async def protected():
            return _Response(200)

        assert asyncio.iscoroutinefunction(protected)

    def test_deferral_raises_and_skips_the_wrapped_coroutine(self, mock_storage):
        """Over-bound cooldown -> RateLimitDeferredError, the coroutine never runs."""
        coord = _make_coordinator(mock_storage)
        cooldown_until = time.time() + _LONG_COOLDOWN_SECONDS
        mock_storage.set_cooldown("k", cooldown_until)
        calls = []

        @coord.rate_limit_aware("k", max_wait=1.0)
        async def protected():
            calls.append(1)
            return _Response(200)

        with pytest.raises(RateLimitDeferredError) as exc_info:
            asyncio.run(protected())

        assert exc_info.value.not_before == cooldown_until
        assert exc_info.value.key == "k"
        assert calls == []

    def test_a_fitting_cooldown_is_awaited_not_slept(self, mock_storage):
        """The wait is an ``asyncio.sleep`` of the remaining cooldown."""
        coord = _make_coordinator(mock_storage)
        mock_storage.set_cooldown("k", time.time() + 2.0)
        async_sleep = _RecordingAsyncSleep()

        @coord.rate_limit_aware("k", max_wait=10.0)
        async def protected():
            return _Response(200)

        with patch(_ASYNC_SLEEP, new=async_sleep), mock_sleep() as blocking_sleep:
            result = asyncio.run(protected())

        assert result.status_code == 200
        assert len(async_sleep.calls) == 1
        assert async_sleep.calls[0] == pytest.approx(2.0, abs=0.5)
        assert blocking_sleep.call_count == 0

    def test_wait_fault_is_fail_open(self, mock_storage):
        """A coordinator fault at the wait site proceeds to the call."""
        coord = _make_coordinator(
            RaisingRateLimitStorage(mock_storage, fail_on="get_state")
        )

        @coord.rate_limit_aware("k")
        async def protected():
            return _Response(200)

        result = asyncio.run(protected())

        assert result.status_code == 200

    def test_on_rate_limited_fault_is_fail_open(self, mock_storage):
        """A fault while recording a returned 429 does not replace the result."""
        coord = _make_coordinator(
            RaisingRateLimitStorage(mock_storage, fail_on="increment_consecutive_429s")
        )

        @coord.rate_limit_aware("k")
        async def protected():
            return _Response(429)

        result = asyncio.run(protected())

        assert result.status_code == 429

    def test_on_success_fault_is_fail_open(self, mock_storage):
        """A fault while resetting the counter after success preserves the result."""
        mock_storage.increment_consecutive_429s("k")
        coord = _make_coordinator(
            RaisingRateLimitStorage(mock_storage, fail_on="reset_consecutive_429s")
        )

        @coord.rate_limit_aware("k")
        async def protected():
            return _Response(200)

        result = asyncio.run(protected())

        assert result.status_code == 200

    def test_user_predicate_exception_still_propagates(self, mock_storage):
        """The user's predicates stay OUTSIDE the fail-open wrap on this path too."""
        coord = _make_coordinator(mock_storage)

        def broken_is_429(_response):
            raise ValueError("user predicate bug")

        @coord.rate_limit_aware("k", is_429=broken_is_429)
        async def protected():
            return _Response(200)

        with pytest.raises(ValueError, match="user predicate bug"):
            asyncio.run(protected())

    def test_a_returned_429_installs_one_cooldown_through_the_thread_hop(
        self, mock_storage
    ):
        """The write is ``aon_rate_limited``: always a hop, because it publishes."""
        coord = _make_coordinator(mock_storage)
        spy = ToThreadSpy()

        @coord.rate_limit_aware("k")
        async def protected():
            return _Response(429)

        with patch(_TO_THREAD, new=spy):
            asyncio.run(protected())

        assert mock_storage.get_state("k").consecutive_429s == 1
        assert spy.hopped("on_rate_limited") is True

    def test_a_non_429_return_resets_the_counter(self, mock_storage):
        """The success side is ``aon_success``: the counter goes back to zero."""
        coord = _make_coordinator(mock_storage)
        mock_storage.increment_consecutive_429s("k")

        @coord.rate_limit_aware("k")
        async def protected():
            return _Response(200)

        asyncio.run(protected())

        assert mock_storage.get_state("k").consecutive_429s == 0

    def test_the_decorator_claims_the_scope_and_marks_what_it_classified(
        self, mock_storage
    ):
        """Claim so the breaker stage installs no second cooldown; mark for the loop.

        The mark is what an enclosing retry loop reads to detect the value for
        its success gating only — one returned 429, one cooldown.
        """
        from baldur.services.circuit_breaker.rate_limit_observation import (
            close_scope,
            open_scope,
        )

        coord = _make_coordinator(mock_storage)
        response = _Response(429)

        @coord.rate_limit_aware("k")
        async def protected():
            return response

        token, scope = open_scope("k")
        try:
            asyncio.run(protected())
        finally:
            close_scope(token)

        assert scope.coordination_claimed is True
        assert scope.was_classified(response) is True
        assert mock_storage.get_state("k").consecutive_429s == 1

    def test_a_call_with_no_scope_open_still_coordinates(self, mock_storage):
        """The decorator does not depend on a breaker stage being above it."""
        coord = _make_coordinator(mock_storage)

        @coord.rate_limit_aware("k")
        async def protected():
            return _Response(429)

        asyncio.run(protected())

        assert mock_storage.get_state("k").consecutive_429s == 1

    def test_the_sync_wrapper_also_marks_what_it_classified(self, mock_storage):
        """The classify-once ownership mark lands on the ``def`` wrapper too."""
        from baldur.services.circuit_breaker.rate_limit_observation import (
            close_scope,
            open_scope,
        )

        coord = _make_coordinator(mock_storage)
        response = _Response(429)

        @coord.rate_limit_aware("k")
        def protected():
            return response

        token, scope = open_scope("k")
        try:
            protected()
        finally:
            close_scope(token)

        assert scope.was_classified(response) is True
