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
