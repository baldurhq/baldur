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
    "DAEMON_EXECUTION_THREAD_NAME",
]

T = TypeVar("T")

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS: int = 60
LOCK_EXTEND_SECONDS: int = 300

# Identifiable in a thread dump alongside baldur-timeout / baldur-watchdog-beacon.
DAEMON_EXECUTION_THREAD_NAME = "baldur-bounded-call"


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
    - Optional daemon-thread execution mode for callers that must not leave an
      abandoned worker behind at interpreter shutdown (see ``execute``)
    """

    def __init__(self) -> None:
        self.last_daemon_thread: threading.Thread | None = None
        """The thread spawned by the most recent daemon-mode call.

        Exposed so a caller (or a test) can observe that an abandoned call was
        left on a daemon thread rather than on a pool worker.
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
        use_daemon_thread: bool = False,
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
            use_daemon_thread: Run fn on a plain daemon thread instead of a
                ThreadPoolExecutor worker. ``concurrent.futures`` pool workers
                are non-daemon and are joined at interpreter exit regardless of
                any flag, so an abandoned hung call turns SIGTERM into a hang
                until SIGKILL. Callers that time out on blocking I/O and must
                stay shutdown-safe opt in here. Lock heartbeating is not
                supported in this mode (``lock`` is ignored).

        Returns:
            Result of fn(stop_event).

        Raises:
            StepTimeoutError: If fn does not complete within timeout_seconds.
        """
        if use_daemon_thread:
            return self._execute_on_daemon_thread(fn, timeout_seconds, pre_execute_hook)

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

    def _execute_on_daemon_thread(
        self,
        fn: Callable[[threading.Event], T],
        timeout_seconds: float,
        pre_execute_hook: Callable[[], None] | None,
    ) -> T:
        """Run fn on a daemon thread and wait for it up to timeout_seconds.

        A timed-out call is abandoned, not cancelled: the thread keeps running
        until its blocking call returns, but being a daemon it is never joined
        at interpreter exit, so an abandoned call cannot hold shutdown open.
        The result slot is written before the done event is set, so a reader
        that observes the event sees a complete slot.
        """
        stop_event = threading.Event()
        ctx = contextvars.copy_context()
        slot: dict[str, object] = {}
        done = threading.Event()

        def _run() -> None:
            try:
                slot["result"] = ctx.run(
                    self._wrapped_fn, fn, stop_event, pre_execute_hook
                )
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
                slot["error"] = exc
            finally:
                done.set()

        thread = threading.Thread(
            target=_run,
            name=DAEMON_EXECUTION_THREAD_NAME,
            daemon=True,
        )
        self.last_daemon_thread = thread
        thread.start()

        if not done.wait(timeout_seconds):
            stop_event.set()
            raise StepTimeoutError(timeout_seconds=timeout_seconds)

        error = slot.get("error")
        if error is not None:
            raise error  # type: ignore[misc]
        return slot["result"]  # type: ignore[return-value]

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
