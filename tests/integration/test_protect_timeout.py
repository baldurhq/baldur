"""Mock-based integration tests for protect() timeout flow (#449).

Verifies that TimeoutPolicy is correctly wired into the protect() facade:
- protect() / aprotect(): timeout triggers TimeoutPolicyError
- protect_with_meta() / aprotect_with_meta(): TIMEOUT outcome in ProtectResult
- @protected / @aprotected: decorator forms forward timeout kwarg
- Sentinel resolution crosses protect.py ↔ settings boundary
- timeout=None disables wrapping entirely

No Docker / Redis needed — uses threading.Event for deterministic sync
timeouts and asyncio.sleep for async timeouts.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import patch

import pytest

from baldur.core.exceptions import TimeoutPolicyError
from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.protect_facade import (
    ProtectResult,
    aprotect,
    aprotect_with_meta,
    aprotected,
    protect,
    protect_with_meta,
    protected,
)
from baldur.settings.protect import reset_protect_settings


@pytest.fixture(autouse=True)
def _isolated_settings():
    reset_protect_settings()
    yield
    reset_protect_settings()


# =============================================================================
# Sync — protect() timeout wiring
# =============================================================================


class TestProtectTimeoutIntegration:
    """protect() + TimeoutPolicy end-to-end (sync)."""

    def test_protect_raises_timeout_policy_error_on_slow_fn(self):
        """protect(timeout=0.1) raises TimeoutPolicyError when fn blocks."""
        blocker = threading.Event()

        def slow_fn():
            blocker.wait(timeout=5.0)
            return "never"

        with pytest.raises(TimeoutPolicyError):
            protect(
                name="int.timeout",
                fn=slow_fn,
                timeout=0.1,
                circuit_breaker=False,
            )

        blocker.set()

    def test_protect_succeeds_within_timeout(self):
        """protect(timeout=5.0) returns fn value when fn completes in time."""
        result = protect(
            name="int.fast",
            fn=lambda: "quick",
            timeout=5.0,
            circuit_breaker=False,
        )
        assert result == "quick"

    def test_protect_with_meta_timeout_returns_timeout_outcome(self):
        """protect_with_meta(timeout=0.1) returns ProtectResult with TIMEOUT outcome."""
        blocker = threading.Event()

        def slow_fn():
            blocker.wait(timeout=5.0)

        meta = protect_with_meta(
            name="int.meta_timeout",
            fn=slow_fn,
            timeout=0.1,
            circuit_breaker=False,
        )

        assert isinstance(meta, ProtectResult)
        assert meta.success is False
        assert meta.outcome == PolicyOutcome.TIMEOUT
        assert isinstance(meta.error, TimeoutPolicyError)

        blocker.set()

    def test_protect_timeout_none_disables_timeout(self):
        """protect(timeout=None) skips TimeoutPolicy entirely."""
        result = protect(
            name="int.no_timeout",
            fn=lambda: "no-timeout",
            timeout=None,
            circuit_breaker=False,
        )
        assert result == "no-timeout"

    def test_protect_default_timeout_from_settings(self, monkeypatch):
        """Omitting timeout= uses ProtectSettings.default_timeout_seconds."""
        monkeypatch.setenv("BALDUR_PROTECT_DEFAULT_TIMEOUT_SECONDS", "0.1")
        reset_protect_settings()

        blocker = threading.Event()

        def slow_fn():
            blocker.wait(timeout=5.0)

        with pytest.raises(TimeoutPolicyError) as exc_info:
            protect(
                name="int.settings_timeout",
                fn=slow_fn,
                circuit_breaker=False,
            )

        assert exc_info.value.timeout_seconds == 0.1
        blocker.set()


# =============================================================================
# Sync — @protected decorator timeout
# =============================================================================


class TestProtectedDecoratorTimeoutIntegration:
    """@protected(timeout=...) decorator form."""

    def test_protected_decorator_forwards_timeout(self):
        """@protected(timeout=0.1) triggers TimeoutPolicyError on slow fn."""
        blocker = threading.Event()

        @protected(name="dec.timeout", timeout=0.1, circuit_breaker=False)
        def slow():
            blocker.wait(timeout=5.0)
            return "never"

        with pytest.raises(TimeoutPolicyError):
            slow()

        blocker.set()

    def test_protected_decorator_success_within_timeout(self):
        """@protected(timeout=5.0) returns value when fn is fast."""

        @protected(name="dec.fast", timeout=5.0, circuit_breaker=False)
        def fast(x):
            return x * 2

        assert fast(21) == 42


# =============================================================================
# Async — aprotect() timeout wiring
# =============================================================================


class TestAprotectTimeoutIntegration:
    """aprotect() + AsyncTimeoutPolicy end-to-end."""

    @pytest.mark.asyncio
    async def test_aprotect_raises_timeout_policy_error_on_slow_coro(self):
        """aprotect(timeout=0.05) raises TimeoutPolicyError on slow coroutine."""

        async def slow_fn():
            await asyncio.sleep(10)
            return "never"

        with pytest.raises(TimeoutPolicyError):
            await aprotect(
                name="async.timeout",
                fn=slow_fn,
                timeout=0.05,
            )

    @pytest.mark.asyncio
    async def test_aprotect_succeeds_within_timeout(self):
        """aprotect(timeout=5.0) returns value when coroutine completes in time."""

        async def fast_fn():
            return "async-quick"

        result = await aprotect(
            name="async.fast",
            fn=fast_fn,
            timeout=5.0,
        )
        assert result == "async-quick"

    @pytest.mark.asyncio
    async def test_aprotect_with_meta_timeout_returns_timeout_outcome(self):
        """aprotect_with_meta(timeout=0.05) returns TIMEOUT outcome in ProtectResult."""

        async def slow_fn():
            await asyncio.sleep(10)

        meta = await aprotect_with_meta(
            name="async.meta_timeout",
            fn=slow_fn,
            timeout=0.05,
        )

        assert meta.success is False
        assert meta.outcome == PolicyOutcome.TIMEOUT
        assert isinstance(meta.error, TimeoutPolicyError)

    @pytest.mark.asyncio
    async def test_aprotect_timeout_none_disables_timeout(self):
        """aprotect(timeout=None) skips AsyncTimeoutPolicy."""

        async def fast_fn():
            return "no-timeout"

        result = await aprotect(
            name="async.no_timeout",
            fn=fast_fn,
            timeout=None,
        )
        assert result == "no-timeout"


# =============================================================================
# Async — @aprotected decorator timeout
# =============================================================================


class TestAprotectedDecoratorTimeoutIntegration:
    """@aprotected(timeout=...) decorator form."""

    @pytest.mark.asyncio
    async def test_aprotected_decorator_forwards_timeout(self):
        """@aprotected(timeout=0.05) triggers TimeoutPolicyError on slow coro."""

        @aprotected(name="adec.timeout", timeout=0.05)
        async def slow():
            await asyncio.sleep(10)
            return "never"

        with pytest.raises(TimeoutPolicyError):
            await slow()

    @pytest.mark.asyncio
    async def test_aprotected_decorator_success_within_timeout(self):
        """@aprotected(timeout=5.0) returns value when coro is fast."""

        @aprotected(name="adec.fast", timeout=5.0)
        async def fast(x):
            return x + 1

        assert await fast(41) == 42


# =============================================================================
# Metrics — timeout outcome label
# =============================================================================


class TestTimeoutMetricsIntegration:
    """protect() emits 'timeout' Prometheus label on TimeoutPolicyError."""

    def test_timeout_records_timeout_outcome_label(self):
        """Metric recorder receives outcome='timeout' when fn times out."""
        from unittest.mock import MagicMock

        mock_recorder = MagicMock()
        blocker = threading.Event()

        def slow_fn():
            blocker.wait(timeout=5.0)

        with patch(
            "baldur.metrics.recorders.protect.get_protect_recorder",
            return_value=mock_recorder,
        ):
            meta = protect_with_meta(
                name="metric.timeout",
                fn=slow_fn,
                timeout=0.1,
                circuit_breaker=False,
            )

        assert meta.outcome == PolicyOutcome.TIMEOUT
        mock_recorder.record.assert_called_once()
        kwargs = mock_recorder.record.call_args.kwargs
        assert kwargs["outcome"] == "timeout"

        blocker.set()


# =============================================================================
# ContextVar propagation through protect() — TimeoutPolicy active path
# =============================================================================


class TestProtectContextVarPropagationIntegration:
    """protect() with explicit timeout= must propagate ContextVar into the worker thread.

    The TimeoutPolicy spins fn on a separate ThreadPoolExecutor thread. Without
    contextvars.copy_context() the worker thread sees an empty ContextVar table,
    breaking structlog binding, deadline propagation, cell/actor context, etc.
    """

    def test_protect_propagates_contextvar_into_timed_call(self):
        """ContextVar bound before protect() is visible to fn running under TimeoutPolicy."""
        # Given — a ContextVar bound in the calling thread
        import contextvars

        var: contextvars.ContextVar[str] = contextvars.ContextVar(
            "protect_timeout_test_var", default="default"
        )
        var.set("propagated")

        def read_var() -> str:
            return var.get()

        # When — protect() runs fn with timeout (TimeoutPolicy active)
        result = protect(
            name="int.contextvar_propagation",
            fn=read_var,
            timeout=5.0,
            circuit_breaker=False,
        )

        # Then — fn observed the calling thread's ContextVar value
        assert result == "propagated"
