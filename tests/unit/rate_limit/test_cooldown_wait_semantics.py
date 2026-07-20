"""Outbound 429-cooldown wait semantics — bounded wait, honor formula, deferral.

Covers the coordinator surface of the bounded serve-or-defer contract:
- ``RateLimitResult`` / ``RateLimitDeferredError`` shape (Contract)
- ``RateLimitCoordinator._compute_cooldown`` honor-with-ceiling formula (Behavior)
- ``RateLimitCoordinator.wait_if_needed(key, max_wait=...)`` serve-vs-defer (Behavior)
- ``@rate_limit_aware`` decorator deferral + fail-open (Behavior)

Retry-loop and tenacity-bridge surfaces are covered in
``services/test_retry_policy.py`` and ``bridges/tenacity/test_policy.py``.
"""

from __future__ import annotations

import time

import pytest

from baldur.core.exceptions import BaldurError, ResilienceError
from baldur.services.rate_limit_coordinator import (
    RateLimitCoordinator,
    RateLimitDeferredError,
)
from baldur.services.rate_limit_coordinator.models import (
    RateLimitCoordinatorConfig,
    RateLimitResult,
)
from tests.factories.time_helpers import mock_sleep

# A cooldown far longer than any bound used below — its exact size is irrelevant
# because the served path's sleep is mocked, so it never actually waits.
_LONG_COOLDOWN_SECONDS = 300.0


def _make_coordinator(storage, **config_overrides) -> RateLimitCoordinator:
    """Coordinator over a given storage, debounce disabled for deterministic events."""
    config = RateLimitCoordinatorConfig(debounce_window_seconds=0.0, **config_overrides)
    return RateLimitCoordinator(storage=storage, config=config)


class _RaisingStorage:
    """Storage double that raises on ONE named method, delegating the rest.

    Models a backend fault (Redis down / thread exhaustion) at a single call
    site so each coordinator fail-open wrap can be exercised in isolation. A
    spec-less dynamic wrapper by design — it forwards every real method except
    the one under fault — so the delegation cannot silently drift from the inner
    double's surface.
    """

    def __init__(self, inner, fail_on: str):
        self._inner = inner
        self._fail_on = fail_on

    def __getattr__(self, name):
        if name == self._fail_on:

            def _raise(*args, **kwargs):
                raise RuntimeError(f"storage down: {name}")

            return _raise
        return getattr(self._inner, name)


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
        storage = _RaisingStorage(mock_storage, fail_on="get_state")
        coord = _make_coordinator(storage)

        @coord.rate_limit_aware("k")
        def protected():
            return _Response(200)

        # Fails the test only if the coordinator fault propagates.
        result = protected()
        assert result.status_code == 200

    def test_on_rate_limited_fault_is_fail_open(self, mock_storage):
        """A fault while recording a 429 cooldown does not replace the business result."""
        storage = _RaisingStorage(mock_storage, fail_on="increment_consecutive_429s")
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
        storage = _RaisingStorage(mock_storage, fail_on="reset_consecutive_429s")
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
