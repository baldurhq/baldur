"""
Timeout Policy — wall-clock execution bound for resilience pipelines.

Sync: TimeoutPolicy uses concurrent.futures.ThreadPoolExecutor with
contextvars.copy_context() so that structlog binding, deadline, and
cell/actor context propagate into the worker thread (matching
ThreadPoolBulkhead and HedgingExecutor conventions).

Async: AsyncTimeoutPolicy uses asyncio.wait_for() (Task context is
preserved automatically).

Outcome is wrapped in PolicyResult(outcome=TIMEOUT, error=...) per the
ResiliencePolicy Protocol (see interfaces/resilience_policy.py — same
pattern as BulkheadPolicy.BulkheadTimeoutError handling). Business
exceptions from the inner chain propagate unmodified. _FallbackApplied
(BaseException) propagates correctly through future.result() / await.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, TypeVar

from baldur.core.exceptions import TimeoutPolicyError
from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
)

T = TypeVar("T")


class TimeoutPolicy:
    """Sync timeout policy — bounds wall-clock execution time.

    Submits the inner function to a process-shared ``ThreadPoolExecutor``
    via ``contextvars.copy_context().run`` so the worker thread inherits
    structlog binding, deadline ContextVar, cell/actor context, etc.
    Waits with ``future.result(timeout=)``.

    On timeout, returns ``PolicyResult(outcome=TIMEOUT, error=...)``
    matching the BulkheadPolicy pattern. Composer also catches a stray
    TimeoutPolicyError as defense-in-depth so outcome is preserved
    through outer policy wrapping (e.g., CircuitBreaker).

    The background thread cannot be forcibly killed (Python limitation)
    but the caller gets a timely timeout result via ``future.cancel()``.

    The executor is process-shared (class-level DCL singleton mirroring
    ``baldur_pro.services.hedging.executor.HedgingExecutor._get_executor``)
    so per-call ``protect()`` does not pay ``ThreadPoolExecutor`` setup +
    ``Thread.start()`` + ``shutdown`` overhead (~50-150 μs on Windows).
    Subclasses get their own ``_executor`` slot via Python attribute
    lookup, matching the #479 ``b80ba463`` subclass-safety contract.
    """

    # Process-shared executor (DCL singleton — see _get_executor).
    _executor: ThreadPoolExecutor | None = None
    _executor_lock = threading.Lock()

    def __init__(self, timeout_seconds: float):
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be > 0, got {timeout_seconds}")
        self._timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "timeout"

    @classmethod
    def _get_executor(cls) -> ThreadPoolExecutor:
        """Return the process-shared executor, lazily constructing on first call.

        Double-checked locking — fast path is an unlocked classvar read.
        Concurrent first-call from N threads results in exactly one
        ``ThreadPoolExecutor`` constructor invocation; the late arrivals see
        the cached instance via the second check inside the lock.
        """
        if cls._executor is None:
            with cls._executor_lock:
                if cls._executor is None:
                    from baldur.settings.protect import get_protect_settings

                    max_workers = (
                        get_protect_settings().default_timeout_executor_workers
                    )
                    cls._executor = ThreadPoolExecutor(
                        max_workers=max_workers,
                        thread_name_prefix="baldur-timeout",
                    )
                    try:
                        from baldur.metrics.recorders.executor import (
                            register_executor,
                        )

                        register_executor("baldur-timeout", cls._executor)
                    except Exception:
                        pass
        return cls._executor

    @classmethod
    def shutdown_executor(cls) -> None:
        """Drain and clear the process-shared executor.

        Used by ``reset_protect_caches()`` for test isolation and may be
        called by graceful shutdown handlers. ``wait=True`` ensures
        in-flight tasks complete before the next test starts, preventing
        cross-test races on shared ContextVars / structlog binding.

        After shutdown, the next ``_get_executor()`` call will rebuild —
        callers do not need to track lifecycle manually.
        """
        with cls._executor_lock:
            if cls._executor is not None:
                cls._executor.shutdown(wait=True)
                try:
                    from baldur.metrics.recorders.executor import (
                        unregister_executor,
                    )

                    unregister_executor("baldur-timeout")
                except Exception:
                    pass
                cls._executor = None

    def execute(
        self,
        func: Callable[..., T],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        executor = self._get_executor()
        ctx = contextvars.copy_context()
        future = executor.submit(ctx.run, func, *args, **kwargs)
        try:
            value = future.result(timeout=self._timeout_seconds)
            return PolicyResult(
                value=value,
                outcome=PolicyOutcome.SUCCESS,
                executed_policies=["timeout"],
            )
        except FuturesTimeoutError as e:
            # On Python >= 3.11 ``concurrent.futures.TimeoutError`` is an
            # alias of builtin ``TimeoutError``, so ``func`` raising
            # ``TimeoutError`` itself also lands here. A completed future
            # whose stored exception is the caught object means the error
            # came FROM ``func`` — propagate it unmodified like any other
            # business exception instead of reporting a policy timeout.
            if future.done() and future.exception() is e:
                raise
            future.cancel()
            err = TimeoutPolicyError(self._timeout_seconds)
            err.__cause__ = e
            return PolicyResult(
                value=None,
                outcome=PolicyOutcome.TIMEOUT,
                error=err,
                executed_policies=["timeout"],
                metadata={"timeout_seconds": self._timeout_seconds},
            )


class AsyncTimeoutPolicy:
    """Async timeout policy — bounds wall-clock execution time.

    Uses ``asyncio.wait_for()`` which cancels the inner coroutine on
    timeout. ``asyncio.CancelledError`` from external cancellation
    propagates without conversion. On timeout, returns
    ``PolicyResult(outcome=TIMEOUT, error=...)`` for Protocol parity
    with the sync variant and BulkheadPolicy.
    """

    def __init__(self, timeout_seconds: float):
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be > 0, got {timeout_seconds}")
        self._timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "timeout"

    async def execute(
        self,
        func: Callable[..., T],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        task: asyncio.Task[T] | None = None
        try:
            coro = func(*args, **kwargs)
            task = asyncio.ensure_future(coro)  # type: ignore[arg-type]
            value = await asyncio.wait_for(task, timeout=self._timeout_seconds)
            return PolicyResult(
                value=value,
                outcome=PolicyOutcome.SUCCESS,
                executed_policies=["timeout"],
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError as e:
            # On Python >= 3.11 ``asyncio.TimeoutError`` is an alias of
            # builtin ``TimeoutError``, so the coroutine raising
            # ``TimeoutError`` itself also lands here. A finished,
            # non-cancelled task whose stored exception is the caught object
            # means the error came FROM the coroutine — propagate it
            # unmodified like any other business exception. A real policy
            # timeout leaves the task cancelled by ``asyncio.wait_for``.
            if (
                task is not None
                and task.done()
                and not task.cancelled()
                and task.exception() is e
            ):
                raise
            err = TimeoutPolicyError(self._timeout_seconds)
            err.__cause__ = e
            return PolicyResult(
                value=None,
                outcome=PolicyOutcome.TIMEOUT,
                error=err,
                executed_policies=["timeout"],
                metadata={"timeout_seconds": self._timeout_seconds},
            )


__all__ = [
    "AsyncTimeoutPolicy",
    "TimeoutPolicy",
]
