"""ThreadPool + Lock Heartbeat timeout executor.

Shared infrastructure for Saga, Runbook, and RecoveryCoordinator.
Executes a callable in a dedicated thread with cooperative cancellation
and periodic lock TTL extension (heartbeat).
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import logging
import threading
from collections.abc import Callable
from typing import Protocol, TypeVar, runtime_checkable

from baldur.core.exceptions import StepTimeoutError

__all__ = [
    "TimeoutExecutor",
    "LockExtendable",
    "HEARTBEAT_INTERVAL_SECONDS",
    "LOCK_EXTEND_SECONDS",
]

T = TypeVar("T")

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS: int = 60
LOCK_EXTEND_SECONDS: int = 300


@runtime_checkable
class LockExtendable(Protocol):
    """Protocol for locks that support TTL extension.

    Satisfied by DistributedRecoveryLock.extend().
    """

    def extend(
        self,
        namespace: str,
        session_id: str,
        additional_seconds: int | None = None,
    ) -> bool: ...


class TimeoutExecutor:
    """ThreadPool + Lock Heartbeat timeout executor.

    Saga, Runbook, RecoveryCoordinator share this executor.

    Features:
    - Single-thread ThreadPoolExecutor per call (bulkhead isolation)
    - Heartbeat polling: extends lock TTL at regular intervals
    - Cooperative cancellation via threading.Event passed to fn
    - Optional pre/post hook for framework wrappers (e.g., Django close_old_connections)
    - ContextVar propagation: the worker thread inherits the caller's structlog
      binding, deadline, and cell/actor context via contextvars.copy_context().run
      (matching TimeoutPolicy / ThreadPoolBulkhead / HedgingExecutor conventions)
    """

    def execute(
        self,
        fn: Callable[[threading.Event], T],
        timeout_seconds: float,
        lock: LockExtendable | None = None,
        lock_namespace: str = "",
        session_id: str = "",
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
        extend_seconds: float = LOCK_EXTEND_SECONDS,
        pre_execute_hook: Callable[[], None] | None = None,
    ) -> T:
        """Execute fn within timeout. Extend lock TTL via heartbeat if provided.

        Args:
            fn: Callable receiving a threading.Event (stop_event) as first
                argument. Implementations should check stop_event.is_set()
                periodically for cooperative cancellation.
            timeout_seconds: Maximum execution time in seconds.
            lock: Optional lock supporting extend(namespace, session_id, additional_seconds).
            lock_namespace: Namespace for lock extension.
            session_id: Session ID for lock extension.
            heartbeat_interval: Seconds between heartbeat polls. Default 60s.
            extend_seconds: Seconds to extend lock TTL on each heartbeat. Default 300s.
            pre_execute_hook: Optional callable invoked before and after fn
                execution in the worker thread (e.g., close_old_connections).

        Returns:
            Result of fn(stop_event).

        Raises:
            StepTimeoutError: If fn does not complete within timeout_seconds.
        """
        stop_event = threading.Event()
        # Propagate the caller's ContextVars (structlog binding, deadline,
        # cell/actor context) into the worker thread. Matches TimeoutPolicy /
        # ThreadPoolBulkhead / HedgingExecutor conventions.
        ctx = contextvars.copy_context()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(
                ctx.run,
                self._wrapped_fn,
                fn,
                stop_event,
                pre_execute_hook,
            )
            elapsed = 0.0

            while elapsed < timeout_seconds:
                remaining = timeout_seconds - elapsed
                wait_time = min(heartbeat_interval, remaining)
                try:
                    return future.result(timeout=wait_time)
                except concurrent.futures.TimeoutError:
                    elapsed += wait_time
                    if elapsed >= timeout_seconds:
                        break
                    if lock:
                        self._try_extend_lock(
                            lock,
                            lock_namespace,
                            session_id,
                            extend_seconds,
                            elapsed,
                        )

            # Timeout exceeded — cooperative cancellation
            stop_event.set()
            raise StepTimeoutError(timeout_seconds=timeout_seconds)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _wrapped_fn(
        fn: Callable[[threading.Event], T],
        stop_event: threading.Event,
        pre_execute_hook: Callable[[], None] | None,
    ) -> T:
        """Wrapper that calls pre_execute_hook before and after fn."""
        if pre_execute_hook:
            pre_execute_hook()
        try:
            return fn(stop_event)
        finally:
            if pre_execute_hook:
                pre_execute_hook()

    @staticmethod
    def _try_extend_lock(
        lock: LockExtendable,
        namespace: str,
        session_id: str,
        extend_seconds: float,
        elapsed: float,
    ) -> None:
        """Attempt lock TTL extension. Fail-open on error."""
        try:
            lock.extend(namespace, session_id, additional_seconds=int(extend_seconds))
            logger.debug(
                "timeout_executor.lock_heartbeat",
                extra={
                    "namespace": namespace,
                    "session_id": session_id,
                    "elapsed": elapsed,
                },
            )
        except Exception as exc:
            logger.warning(
                "timeout_executor.lock_extend_failed",
                extra={
                    "namespace": namespace,
                    "session_id": session_id,
                    "elapsed": elapsed,
                    "error": str(exc),
                },
            )
