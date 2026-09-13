"""Unified ``@retry`` decorator unit tests (670 D6).

Target:
- resilience/policies/async_retry.py (``retry`` decorator)

The unified ``@retry`` supersedes the previous split retry decorators (a
sync-only one and an async-only one) with a single call-style-safe surface that
dual-dispatches on ``asyncio.iscoroutinefunction``:

- an ``async def`` is wrapped by AsyncRetryPolicy,
- a plain ``def`` is wrapped by the synchronous RetryPolicy.

Both branches derive their config from ``RetryPolicyConfig.from_settings(domain)``
with the passed overrides applied, return the unwrapped value on success, and
raise ``MaxRetriesExceededError`` (carrying ``last_error``) on exhaustion.

UNIT_TEST_GUIDELINES.md:
- Behavior: source-referenced attempt counts / exception type, no hardcoded magic.
- §8.5 dependency interaction — call-count assertions.
- §8.7 state transition — success-on-attempt-k vs exhaustion vs non-retryable.
- Time dependency (§6.3): a zero-delay ConstantBackoff makes both branches'
  between-attempt sleep instant and deterministic (no real wall-clock wait).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from baldur.core.backoff import ConstantBackoff
from baldur.resilience.policies.async_retry import retry
from baldur.services.retry_handler.models import MaxRetriesExceededError

# Zero-delay backoff — between-attempt sleep is instant (time.sleep(0) /
# asyncio.sleep(0)), so exhaustion tests do not wait real wall-clock time.
_NO_DELAY = ConstantBackoff(delay=0.0, jitter=False)

_DISPATCH = pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])


def _make_decorated(
    is_async: bool,
    body: Callable[..., Any],
    *,
    domain: str,
    max_attempts: int,
    retryable_exceptions: tuple[type[Exception], ...] | None = None,
) -> Callable[..., Any]:
    """Wrap ``body`` in ``@retry`` on either the sync or the async branch.

    ``body`` carries synchronous semantics (returns a value or raises); the
    async branch simply awaits a coroutine that runs ``body`` inline, so both
    branches exercise the SAME retry logic through their respective policies.
    """
    if is_async:

        @retry(
            domain=domain,
            max_attempts=max_attempts,
            retryable_exceptions=retryable_exceptions,
            backoff=_NO_DELAY,
        )
        async def decorated(*args: Any, **kwargs: Any) -> Any:
            return body(*args, **kwargs)

        return decorated

    @retry(
        domain=domain,
        max_attempts=max_attempts,
        retryable_exceptions=retryable_exceptions,
        backoff=_NO_DELAY,
    )
    def decorated(*args: Any, **kwargs: Any) -> Any:
        return body(*args, **kwargs)

    return decorated


def _invoke(decorated: Callable[..., Any], is_async: bool, *args: Any) -> Any:
    """Call the decorated function, driving the async branch to completion."""
    if is_async:
        return asyncio.run(decorated(*args))
    return decorated(*args)


# =============================================================================
# Contract — re-export wiring (D10)
# =============================================================================


class TestRetryDecoratorExportContract:
    """``retry`` is re-exported from the resilience.policies package (D10)."""

    def test_retry_reexported_from_package(self):
        """``from baldur.resilience.policies import retry`` binds the same object."""
        from baldur.resilience.policies import retry as pkg_retry

        assert pkg_retry is retry


# =============================================================================
# Behavior — dual-dispatch across {sync, async}
# =============================================================================


class TestRetryDecoratorBehavior:
    """Unified ``@retry`` behaves identically on sync and async functions."""

    @_DISPATCH
    def test_retry_returns_unwrapped_value_on_first_success(self, is_async):
        """Success on the first attempt returns the unwrapped value (not a result)."""
        calls = {"n": 0}

        def body():
            calls["n"] += 1
            return "ok"

        decorated = _make_decorated(
            is_async, body, domain="dec.success", max_attempts=3
        )
        result = _invoke(decorated, is_async)

        assert result == "ok"
        assert calls["n"] == 1

    @_DISPATCH
    def test_retry_succeeds_after_transient_failures(self, is_async):
        """Transient failures below the attempt cap retry, then return the value."""
        calls = {"n": 0}

        def body():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("transient")
            return "recovered"

        decorated = _make_decorated(
            is_async, body, domain="dec.transient", max_attempts=5
        )
        result = _invoke(decorated, is_async)

        assert result == "recovered"
        # succeeds on the 3rd attempt → exactly 3 body invocations
        assert calls["n"] == 3

    @_DISPATCH
    def test_retry_exhaustion_raises_max_retries_exceeded(self, is_async):
        """Exhausting all attempts raises MaxRetriesExceededError carrying last_error."""
        calls = {"n": 0}

        def body():
            calls["n"] += 1
            raise ConnectionError("down")

        decorated = _make_decorated(
            is_async, body, domain="dec.exhaust", max_attempts=2
        )

        with pytest.raises(MaxRetriesExceededError) as exc_info:
            _invoke(decorated, is_async)

        # max_attempts=2 → exactly 2 body invocations before exhaustion
        assert calls["n"] == 2
        assert isinstance(exc_info.value.last_error, ConnectionError)
        assert exc_info.value.retry_count == 2

    @_DISPATCH
    def test_retry_non_retryable_stops_after_one_attempt(self, is_async):
        """A non-retryable exception stops immediately (single attempt), then raises."""
        calls = {"n": 0}

        def body():
            calls["n"] += 1
            raise ValueError("bad input")

        # Only ConnectionError is retryable → ValueError is non-retryable.
        decorated = _make_decorated(
            is_async,
            body,
            domain="dec.nonretry",
            max_attempts=5,
            retryable_exceptions=(ConnectionError,),
        )

        with pytest.raises(MaxRetriesExceededError) as exc_info:
            _invoke(decorated, is_async)

        assert calls["n"] == 1
        assert isinstance(exc_info.value.last_error, ValueError)

    @_DISPATCH
    def test_retry_forwards_positional_args(self, is_async):
        """Positional args are forwarded through the wrapper to the wrapped function."""
        received = {}

        def body(a, b):
            received["a"] = a
            received["b"] = b
            return a + b

        decorated = _make_decorated(is_async, body, domain="dec.args", max_attempts=2)
        result = _invoke(decorated, is_async, 3, 4)

        assert received == {"a": 3, "b": 4}
        assert result == 7


# =============================================================================
# Behavior — functools.wraps metadata preservation
# =============================================================================


class TestRetryDecoratorWrapsBehavior:
    """``@retry`` preserves the wrapped function's identity (FastAPI DI / IDE)."""

    def test_sync_wrapper_preserves_name_and_doc(self):
        """A sync-wrapped function keeps its __name__ / __doc__ (functools.wraps)."""

        @retry(domain="dec.wraps_sync", max_attempts=2, backoff=_NO_DELAY)
        def charge_card():
            """Charge the customer's card."""
            return "charged"

        assert charge_card.__name__ == "charge_card"
        assert charge_card.__doc__ == "Charge the customer's card."

    def test_async_wrapper_preserves_name_and_doc(self):
        """An async-wrapped function keeps its __name__ / __doc__ (functools.wraps)."""

        @retry(domain="dec.wraps_async", max_attempts=2, backoff=_NO_DELAY)
        async def fetch_profile():
            """Fetch the user profile."""
            return "profile"

        assert fetch_profile.__name__ == "fetch_profile"
        assert fetch_profile.__doc__ == "Fetch the user profile."


# =============================================================================
# Behavior — a cooldown deferral reaches the caller as RateLimitDeferredError
# =============================================================================

_RATE_LIMIT_MESSAGE = "429 too many requests"


@pytest.fixture
def singleton_coordinator():
    """Stand a spec'd coordinator in for the singleton both branches resolve.

    ``@retry`` has no injection seam, so the process-wide singleton is the
    path. Both waits admit by default; a test sets the deferral it needs on
    the wait its branch uses (``wait_if_needed`` / ``await_if_needed``).
    """
    from unittest.mock import MagicMock, patch

    from baldur.services.rate_limit_coordinator import RateLimitCoordinator
    from baldur.services.rate_limit_coordinator.models import RateLimitResult

    coordinator = MagicMock(spec=RateLimitCoordinator)
    coordinator.wait_if_needed.return_value = RateLimitResult(waited=False)
    coordinator.await_if_needed.return_value = RateLimitResult(waited=False)
    coordinator.on_rate_limited.return_value = 0.0
    coordinator.aon_rate_limited.return_value = 0.0
    with patch.object(
        RateLimitCoordinator, "get_instance", autospec=True, return_value=coordinator
    ):
        yield coordinator


def _defer_from_attempt(coordinator, is_async: bool, results: list) -> None:
    """Script the wait of the branch under test, attempt by attempt."""
    wait = coordinator.await_if_needed if is_async else coordinator.wait_if_needed
    wait.side_effect = results


class TestRetryDecoratorDeferralBehavior:
    """A refused attempt raises ``RateLimitDeferredError`` — never an exhaustion.

    "Max retries exceeded" for a call that made zero attempts sent operators
    debugging the wrong thing and buried ``not_before`` one level down. Both
    branches share the unwrap, so both raise the deferral: the loop's own
    (by type), and — when a real 429 on an earlier attempt kept the loop's
    error slot — a deferral synthesised from the metadata with that 429 as
    its ``__cause__``.
    """

    @_DISPATCH
    def test_a_deferral_on_the_first_attempt_raises_the_deferral_error(
        self, singleton_coordinator, is_async
    ):
        """The function is never called and the caller can read ``not_before``."""
        from baldur.core.exceptions import RateLimitDeferredError
        from baldur.services.rate_limit_coordinator.models import RateLimitResult

        _defer_from_attempt(
            singleton_coordinator,
            is_async,
            [RateLimitResult(deferred=True, not_before=1_700_000_000.0)],
        )
        calls = {"n": 0}

        def body():
            calls["n"] += 1
            return "ok"

        decorated = _make_decorated(
            is_async, body, domain="dec.deferral", max_attempts=3
        )

        with pytest.raises(RateLimitDeferredError) as exc_info:
            _invoke(decorated, is_async)

        assert type(exc_info.value) is RateLimitDeferredError
        assert not isinstance(exc_info.value, MaxRetriesExceededError)
        assert exc_info.value.not_before == 1_700_000_000.0
        assert exc_info.value.key == "dec.deferral"
        assert calls["n"] == 0

    @_DISPATCH
    def test_a_deferral_after_a_real_429_raises_the_deferral_with_the_429_as_cause(
        self, singleton_coordinator, is_async
    ):
        """Attempt 1 took a provider 429; attempt 2 was refused by the cooldown.

        The loop keeps the 429 as its error (the breaker must keep counting
        it), so the deferral is synthesised from the metadata — and the 429
        rides along as ``__cause__`` rather than being lost.
        """
        from baldur.core.exceptions import RateLimitDeferredError
        from baldur.services.rate_limit_coordinator.models import RateLimitResult

        _defer_from_attempt(
            singleton_coordinator,
            is_async,
            [
                RateLimitResult(waited=False),
                RateLimitResult(deferred=True, not_before=1_700_000_000.0),
            ],
        )
        throttled = Exception(_RATE_LIMIT_MESSAGE)
        calls = {"n": 0}

        def body():
            calls["n"] += 1
            raise throttled

        decorated = _make_decorated(
            is_async, body, domain="dec.deferral_after_a_real_429", max_attempts=3
        )

        with pytest.raises(RateLimitDeferredError) as exc_info:
            _invoke(decorated, is_async)

        assert exc_info.value.not_before == 1_700_000_000.0
        assert exc_info.value.key == "dec.deferral_after_a_real_429"
        assert exc_info.value.__cause__ is throttled
        assert calls["n"] == 1

    @_DISPATCH
    def test_an_inner_deferral_is_not_retried_and_passes_through_by_type(
        self, singleton_coordinator, is_async
    ):
        """The default non-retryable set: a deferral from inside stops the loop.

        Retrying it would only sleep through attempts that cannot succeed
        before ``not_before``; the caller gets the inner error itself.
        """
        from baldur.core.exceptions import RateLimitDeferredError

        inner = RateLimitDeferredError(key="inner", not_before=1_700_000_000.0)
        calls = {"n": 0}

        def body():
            calls["n"] += 1
            raise inner

        decorated = _make_decorated(
            is_async, body, domain="dec.inner_deferral", max_attempts=3
        )

        with pytest.raises(RateLimitDeferredError) as exc_info:
            _invoke(decorated, is_async)

        assert exc_info.value is inner
        assert calls["n"] == 1

    @_DISPATCH
    def test_a_retry_disabled_stage_passes_an_inner_deferral_through_by_type(
        self, singleton_coordinator, monkeypatch, is_async
    ):
        """The single-attempt path returns a FAILURE with no metadata at all.

        The unwrap must therefore key on the error's type, not on metadata —
        otherwise a disabled stage would re-wrap the deferral as an exhaustion.
        """
        from baldur.core.exceptions import RateLimitDeferredError
        from baldur.settings.retry import reset_retry_settings

        inner = RateLimitDeferredError(key="inner", not_before=1_700_000_000.0)

        def body():
            raise inner

        monkeypatch.setenv("BALDUR_RETRY_ENABLED", "false")
        reset_retry_settings()
        try:
            decorated = _make_decorated(
                is_async, body, domain="dec.disabled_deferral", max_attempts=3
            )
            with pytest.raises(RateLimitDeferredError) as exc_info:
                _invoke(decorated, is_async)
        finally:
            reset_retry_settings()

        assert exc_info.value is inner

    @_DISPATCH
    def test_a_result_rejection_exhaustion_is_not_double_wrapped(
        self, singleton_coordinator, is_async
    ):
        """The synthesised ``MaxRetriesExceededError`` is re-raised as-is."""
        if is_async:

            @retry(
                domain="dec.rejection",
                max_attempts=2,
                backoff=_NO_DELAY,
                retry_on_result=lambda r: True,
            )
            async def decorated():
                return "rejected"

        else:

            @retry(
                domain="dec.rejection",
                max_attempts=2,
                backoff=_NO_DELAY,
                retry_on_result=lambda r: True,
            )
            def decorated():
                return "rejected"

        with pytest.raises(MaxRetriesExceededError) as exc_info:
            _invoke(decorated, is_async)

        assert exc_info.value.result_rejected is True
        assert exc_info.value.last_result == "rejected"
        assert exc_info.value.last_error is None

    @_DISPATCH
    def test_an_ordinary_exhaustion_still_raises_max_retries_exceeded(
        self, singleton_coordinator, is_async
    ):
        """Discriminator: the deferral rows change nothing for a plain exhaustion."""

        def body():
            raise ConnectionError("down")

        decorated = _make_decorated(
            is_async, body, domain="dec.plain_exhaustion", max_attempts=2
        )

        with pytest.raises(MaxRetriesExceededError) as exc_info:
            _invoke(decorated, is_async)

        assert isinstance(exc_info.value.last_error, ConnectionError)
