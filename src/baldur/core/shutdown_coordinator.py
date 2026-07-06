"""
Graceful Shutdown Coordinator

Manages graceful shutdown with in-flight request handling:
- Signal handling (SIGTERM, SIGINT)
- Request tracking
- Drain period
- Forced shutdown timeout

Framework-agnostic design.
"""

import os
import signal
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

import structlog

from baldur.core.exceptions import BaldurError
from baldur.utils.time import utc_now

try:
    from baldur.metrics.recorders.shutdown import (
        record_aborted,
        record_drain_duration,
        record_drained,
        record_shutdown_initiated,
        set_shutdown_phase,
    )
except ImportError:

    def set_shutdown_phase(phase: str) -> None:
        return None

    def record_drain_duration(duration: float) -> None:
        return None

    def record_drained(count: int = 1) -> None:
        return None

    def record_aborted(count: int = 1) -> None:
        return None

    def record_shutdown_initiated() -> None:
        return None


logger = structlog.get_logger()

__all__ = [
    "ShutdownError",
    "ShutdownPhase",
    "RequestState",
    "TrackedRequest",
    "ShutdownStats",
    "ShutdownHandler",
    "RequestTracker",
    "GracefulShutdownCoordinator",
    "get_shutdown_coordinator",
    "configure_shutdown_coordinator",
    "reset_shutdown_coordinator",
]


class ShutdownError(BaldurError):
    """Raised when a shutdown-related operation is invalid."""

    def __init__(self, message: str, *, phase: str = "", detail: str = ""):
        super().__init__(message)
        self.phase = phase
        self.detail = detail

    def extra_context(self) -> dict:
        return {"phase": self.phase, "detail": self.detail}


class ShutdownPhase(str, Enum):
    """Shutdown process phases"""

    RUNNING = "running"  # Normal operation
    DRAINING = "draining"  # Rejecting new requests, completing in-flight ones
    TERMINATING = "terminating"  # Forced termination in progress
    TERMINATED = "terminated"  # Termination complete


class RequestState(str, Enum):
    """State of an in-flight request"""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABORTED = "aborted"
    TIMED_OUT = "timed_out"


@dataclass
class TrackedRequest:
    """Information about a tracked in-flight request"""

    request_id: str
    started_at: datetime
    endpoint: str = ""
    method: str = ""
    state: RequestState = RequestState.IN_PROGRESS
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        return (utc_now() - self.started_at).total_seconds()


@dataclass
class ShutdownStats:
    """Statistics about the shutdown process"""

    phase: ShutdownPhase
    shutdown_started_at: datetime | None
    in_flight_count: int
    completed_during_drain: int
    aborted_count: int
    drain_timeout_seconds: float
    remaining_drain_time: float | None


class ShutdownHandler(ABC):
    """Abstract handler for shutdown actions"""

    @abstractmethod
    def on_shutdown_start(self) -> None:
        """Called when shutdown process begins. MUST return quickly (non-blocking)."""
        pass

    def is_drain_complete(self) -> bool:
        """Called periodically during drain loop. True = this handler's drain is done.

        Default returns True for backward compatibility — existing handlers
        (LeaderElector, MultiRegion) have no drain wait and need no change.
        """
        return True

    @abstractmethod
    def on_drain_complete(self) -> None:
        """Called when all in-flight requests AND all handlers are drained."""
        pass

    @abstractmethod
    def on_force_shutdown(self, pending_requests: list[TrackedRequest]) -> None:
        """Called when forced shutdown with pending requests."""
        pass


# Grace period before the deadman fallback force-exits a defer-exit
# process whose main thread never delivered the trampolined signal
# (e.g. blocked in a GIL-holding C call). Deliberately a constant, not
# a settings flag: the signal disposition itself is the operator escape.
# Constant choice rationale: 597 D4/D11.
_SIGNAL_EXIT_DEADMAN_SECONDS = 5.0


class _SignalDispositionMode(str, Enum):
    """Verdict of the registration-time signal-disposition classification."""

    SKIP_IGNORED = "skip_ignored"  # SIG_IGN — explicit ignore intent preserved
    SKIP_UNKNOWN = "skip_unknown"  # None — C-level handler, unknowable owner
    CHAIN = "chain"  # callable tail — host owns exit, Baldur chains
    DEFER_EXIT = "defer_exit"  # SIG_DFL tail — Baldur re-raises after the drain


def _classify_signal_disposition(original: Any) -> _SignalDispositionMode:
    """Classify a signal disposition captured at registration time.

    Walks Baldur chained-handler markers (``_baldur_chained_original``)
    to the effective tail so registration order cannot flip the verdict:
    a Baldur cleanup handler (disk/redis audit buffer) chained over
    ``SIG_DFL`` still classifies as defer-exit, and one chained over a
    host server's handler still classifies as chain.
    """
    # Chain-walk rationale: 597 D2.
    tail = original
    visited: set[int] = set()
    while (
        callable(tail)
        and hasattr(tail, "_baldur_chained_original")
        and id(tail) not in visited
    ):
        visited.add(id(tail))
        tail = tail._baldur_chained_original
    if tail is None:
        return _SignalDispositionMode.SKIP_UNKNOWN
    if tail is signal.SIG_IGN:
        return _SignalDispositionMode.SKIP_IGNORED
    if tail is signal.SIG_DFL:
        return _SignalDispositionMode.DEFER_EXIT
    if callable(tail):
        return _SignalDispositionMode.CHAIN
    return _SignalDispositionMode.SKIP_UNKNOWN


class RequestTracker:
    """
    Tracks in-flight requests for graceful shutdown.

    Usage:
        tracker = RequestTracker()

        # On request start
        tracker.start_request(request_id, endpoint="/api/payment")

        # On request end
        tracker.end_request(request_id, success=True)

        # During shutdown
        pending = tracker.get_pending_requests()
    """

    def __init__(self, max_request_age_seconds: float | None = None):
        from baldur.settings.recovery_shutdown import (
            get_recovery_shutdown_settings,
        )

        self._requests: dict[str, TrackedRequest] = {}
        self._lock = threading.Lock()
        self._max_age = (
            max_request_age_seconds
            if max_request_age_seconds is not None
            else get_recovery_shutdown_settings().max_request_age_seconds
        )
        self._completed_count = 0

    def start_request(
        self,
        request_id: str,
        endpoint: str = "",
        method: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> TrackedRequest:
        """Start tracking a request"""
        request = TrackedRequest(
            request_id=request_id,
            started_at=utc_now(),
            endpoint=endpoint,
            method=method,
            metadata=metadata or {},
        )

        with self._lock:
            self._requests[request_id] = request
            # Clean up old requests
            self._cleanup_old_requests()

        return request

    def end_request(
        self,
        request_id: str,
        success: bool = True,
    ) -> TrackedRequest | None:
        """End tracking a request"""
        with self._lock:
            request = self._requests.pop(request_id, None)
            if request:
                request.state = (
                    RequestState.COMPLETED if success else RequestState.ABORTED
                )
                self._completed_count += 1
            return request

    def get_pending_requests(self) -> list[TrackedRequest]:
        """Get all pending (in-progress) requests"""
        with self._lock:
            return [
                r
                for r in self._requests.values()
                if r.state == RequestState.IN_PROGRESS
            ]

    def get_pending_count(self) -> int:
        """Get count of pending requests"""
        with self._lock:
            return sum(
                1
                for r in self._requests.values()
                if r.state == RequestState.IN_PROGRESS
            )

    def abort_all(self) -> tuple[list[TrackedRequest], int]:
        """Abort all pending requests and snapshot the completed count.

        Returns ``(aborted_requests, completed_count)`` from a single lock
        acquisition. The combined snapshot is what makes the force-shutdown
        books reconcile exactly: ``end_request`` pops a request and bumps the
        completed count under this same lock, so a request finishing
        concurrently lands in exactly one of the two values — never both
        (double-counted) and never neither (dropped).
        """
        with self._lock:
            aborted = []
            for request in self._requests.values():
                if request.state == RequestState.IN_PROGRESS:
                    request.state = RequestState.ABORTED
                    aborted.append(request)
            return aborted, self._completed_count

    def _cleanup_old_requests(self) -> None:
        """Remove requests older than max age"""
        cutoff = utc_now() - timedelta(seconds=self._max_age)
        old_ids = [
            rid
            for rid, req in self._requests.items()
            if req.started_at < cutoff and req.state != RequestState.IN_PROGRESS
        ]
        for rid in old_ids:
            del self._requests[rid]

    @property
    def completed_count(self) -> int:
        return self._completed_count


class GracefulShutdownCoordinator:
    """
    Coordinates graceful shutdown process.

    Usage:
        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            drain_timeout=30.0,
            shutdown_handler=my_handler,
        )

        # Register signal handlers
        coordinator.register_signals()

        # Or manually trigger
        coordinator.initiate_shutdown()

        # Check if accepting requests
        if coordinator.is_accepting_requests():
            process_request()
    """

    def __init__(
        self,
        request_tracker: RequestTracker | None = None,
        drain_timeout: float | None = None,
        shutdown_handler: ShutdownHandler | None = None,
        check_interval: float | None = None,
    ):
        # The former on_shutdown_complete callback seam was removed (597
        # D5): it fired on manual initiate_shutdown() and gunicorn worker
        # drains where exiting would be wrong, and duplicated
        # ShutdownHandler.on_drain_complete/on_force_shutdown with zero
        # production consumers. Post-drain exit lives in the signal path.
        from baldur.settings.recovery_shutdown import (
            get_recovery_shutdown_settings,
        )

        settings = get_recovery_shutdown_settings()
        self._tracker = request_tracker
        self._drain_timeout = (
            drain_timeout
            if drain_timeout is not None
            else settings.default_drain_timeout_seconds
        )
        self._handlers: list[ShutdownHandler] = []
        if shutdown_handler:
            self._handlers.append(shutdown_handler)
        self._check_interval = (
            check_interval
            if check_interval is not None
            else settings.check_interval_seconds
        )

        self._phase = ShutdownPhase.RUNNING
        self._shutdown_started_at: datetime | None = None
        self._shutdown_thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # Signal-lifecycle state. _signal_once is the non-blocking
        # test-and-set the installed handlers dispatch on (first vs
        # subsequent delivery); _exit_signum is armed by the first
        # defer-exit delivery and consumed by the drain thread.
        self._signal_once = threading.Lock()
        self._exit_signum: int | None = None

        # Stats
        self._drained_count = 0
        self._aborted_count = 0
        # Lifetime completed-count baseline captured when the drain begins;
        # drained metrics report the delta past this, not the process-lifetime
        # total the tracker accumulates.
        self._completed_at_drain_start = 0

    @property
    def phase(self) -> ShutdownPhase:
        return self._phase

    def register_handler(self, handler: ShutdownHandler) -> None:
        """Register an additional shutdown handler.

        Must be called during bootstrap (before shutdown starts).
        Raises RuntimeError if called after shutdown has been initiated.
        """
        with self._lock:
            if self._phase != ShutdownPhase.RUNNING:
                raise ShutdownError(
                    "Cannot register shutdown handlers after shutdown initiated",
                    phase=self._phase.value,
                    detail="handler_registration_after_shutdown",
                )
            self._handlers.append(handler)

    def is_accepting_requests(self) -> bool:
        """Check if server should accept new requests"""
        return self._phase == ShutdownPhase.RUNNING

    def is_shutting_down(self) -> bool:
        """Check if shutdown is in progress"""
        return self._phase in (ShutdownPhase.DRAINING, ShutdownPhase.TERMINATING)

    def register_signals(self) -> None:
        """Register SIGTERM/SIGINT handlers for graceful shutdown.

        Disposition-sensitive: the previously installed handler is
        captured per signal and classified instead of being replaced
        blindly —

        - ``SIG_IGN``: registration is skipped for that signal; the
          explicit ignore intent is preserved.
        - ``None`` (handler installed at the C level by a non-Python
          owner): skipped — the owner is unknowable.
        - callable (e.g. a host server's handler such as uvicorn's
          ``handle_exit``): a chaining handler is installed — the drain
          is initiated first, then the captured handler runs, and the
          host keeps ownership of process exit.
        - ``SIG_DFL``: the handler is installed in defer-exit mode —
          after the drain completes the signal is re-raised with the
          default disposition restored, so the process terminates with
          conventional signal semantics instead of swallowing the stop
          signal.

        Classification walks Baldur chained-handler markers to the
        effective tail, so a Baldur cleanup handler registered earlier
        (disk/redis audit buffer) cannot flip the verdict.

        Skipped under Gunicorn (both master and worker) — signal
        lifecycle is managed by gunicorn's arbiter and forwarded to
        workers via the ``worker_int`` callback. Overwriting either
        process's SIGTERM handler would suppress ``worker_int`` and
        break gunicorn's own in-flight HTTP drain. The worker-side
        wire-up lives in ``baldur.adapters.gunicorn.hooks``.

        ``is_under_gunicorn()`` (SERVER_SOFTWARE-based) is used here
        instead of ``is_gunicorn_worker()`` (GUNICORN_WORKER env var)
        because the env var is only set by ``post_worker_init`` —
        which runs AFTER the worker has imported the WSGI app and
        called ``baldur.init()`` → ``register_signals()``. Relying on
        ``is_gunicorn_worker()`` here means the worker would briefly
        overwrite gunicorn's SIGTERM handler before the env var is
        set, causing a silent drain failure.
        """
        # Disposition rule: 597 D2; handler runtime behavior: 597 D3.
        from baldur.core.process_utils import is_under_gunicorn

        if is_under_gunicorn():
            logger.info("shutdown_coordinator.skipping_signal_registration_gunicorn")
            return

        for sig in (signal.SIGTERM, signal.SIGINT):
            self._register_signal(sig)

    def _register_signal(self, sig: int) -> None:
        """Classify the current disposition of ``sig`` and install the
        coordinator handler when the verdict allows it."""
        original = signal.getsignal(sig)

        # Re-registration guard: a repeated register_signals() finding the
        # coordinator's own handler anywhere in the chain is a no-op —
        # self-chaining would make the inner link's subsequent-delivery
        # branch fire during the first delivery.
        if self._chain_contains_own_handler(original):
            logger.debug(
                "shutdown_coordinator.signal_already_registered",
                signum=sig,
            )
            return

        mode = _classify_signal_disposition(original)
        if mode is _SignalDispositionMode.SKIP_IGNORED:
            logger.info(
                "shutdown_coordinator.signal_registration_skipped_ignored",
                signum=sig,
            )
            return
        if mode is _SignalDispositionMode.SKIP_UNKNOWN:
            logger.info(
                "shutdown_coordinator.signal_registration_skipped_unknown_owner",
                signum=sig,
            )
            return

        handler = self._make_signal_handler(
            original,
            defer_exit=(mode is _SignalDispositionMode.DEFER_EXIT),
        )
        signal.signal(sig, handler)

    def _chain_contains_own_handler(self, original: Any) -> bool:
        """Walk the chained-handler markers looking for this coordinator's
        own handler."""
        node = original
        visited: set[int] = set()
        while callable(node) and id(node) not in visited:
            visited.add(id(node))
            if getattr(node, "_baldur_coordinator", None) is self:
                return True
            node = getattr(node, "_baldur_chained_original", None)
        return False

    def _make_signal_handler(
        self, original: Any, defer_exit: bool
    ) -> Callable[[int, Any], None]:
        """Build the OS signal handler for one signal.

        ``original`` is the disposition captured at registration time;
        ``defer_exit`` selects whether this coordinator owns process
        exit (``SIG_DFL`` tail) or the captured handler's owner does
        (callable tail).
        """

        def _coordinator_signal_handler(signum: int, frame: Any) -> None:
            # Non-blocking test-and-set FIRST — before any logging or
            # phase work. CPython signal handlers can nest at bytecode
            # boundaries on the main thread; dispatching on anything that
            # flips later (e.g. the coordinator phase) leaves a window
            # where a nested second delivery re-enters initiate_shutdown()
            # and deadlocks on the non-reentrant coordinator lock.
            # TAS dispatch rationale: 597 D3 (external-review amendment).
            if self._signal_once.acquire(blocking=False):
                if defer_exit:
                    # Arm the post-drain exit; the drain thread re-raises
                    # this signum once the drain has finished (597 D4).
                    self._exit_signum = signum
                logger.info(
                    "shutdown_coordinator.signal_received",
                    signum=signum,
                )
                self.initiate_shutdown()
                if callable(original):
                    # Chain the captured handler AFTER the drain is
                    # initiated — same ordering as the gunicorn hooks.
                    original(signum, frame)
            elif defer_exit:
                # Subsequent delivery during a defer-mode drain: operator
                # force escape — die now with conventional signal
                # semantics (matches uvicorn's second-SIGINT force_exit).
                # Also the landing point of the drain thread's post-drain
                # re-raise (597 D4 trampoline).
                self._perform_exit_reraise(signum)
            elif callable(original):
                # Chain mode: forward subsequent deliveries so the host
                # owner keeps its own double-signal semantics.
                original(signum, frame)

        # Markers consumed by the disposition chain-walk and the
        # re-registration guard.
        _coordinator_signal_handler._baldur_chained_original = original  # type: ignore[attr-defined]
        _coordinator_signal_handler._baldur_coordinator = self  # type: ignore[attr-defined]
        return _coordinator_signal_handler

    def _perform_exit_reraise(self, signum: int) -> None:
        """Restore the default disposition for ``signum`` and re-deliver it.

        Runs in the main thread (signal-handler context) — the only
        thread allowed to call ``signal.signal``. On handler return the
        pending signal is delivered with the default action, producing
        true signal death that supervisors observe and application-level
        ``except`` blocks cannot swallow.
        """
        # Main-thread-side exit seam (597 D4) — unit tests MUST patch
        # this method; an unmocked invocation kills the test process.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    def _arm_deferred_exit(self) -> None:
        """Drain-thread side of the post-drain exit.

        No-op unless a defer-exit signal delivery armed an exit signum —
        manual ``initiate_shutdown()`` calls and chain-mode drains never
        exit here. Re-sends the armed signum so delivery lands in the
        main-thread handler, whose subsequent-delivery branch restores
        the default disposition and re-raises. If the main thread never
        reaches a bytecode boundary (blocked in a GIL-holding C call),
        the deadman fallback ends the process directly after a short
        grace period.
        """
        # Drain-thread-side exit seam (597 D4/D10) — unit tests MUST
        # patch this method (or os.kill/os._exit); an unmocked invocation
        # kills the test process.
        signum = self._exit_signum
        if signum is None:
            return
        # Two-hop trampoline: deliver to the main thread, which performs
        # the restore + re-raise (signal.signal is main-thread-only).
        os.kill(os.getpid(), signum)
        # Deadman fallback: exit-code death (128+n) when the trampoline
        # cannot land. Drain and signal-time flushes have already run.
        time.sleep(_SIGNAL_EXIT_DEADMAN_SECONDS)
        os._exit(128 + signum)

    def initiate_shutdown(self) -> None:
        """Start the graceful shutdown process."""
        with self._lock:
            if self._phase != ShutdownPhase.RUNNING:
                return

            # Capture the drain-start baseline BEFORE the phase store: any
            # reader observing DRAINING is then guaranteed to also observe
            # the baseline (GIL store ordering), so get_stats can read both
            # lock-free without a torn drained-delta.
            self._completed_at_drain_start = (
                self._tracker.completed_count if self._tracker is not None else 0
            )
            self._phase = ShutdownPhase.DRAINING
            self._shutdown_started_at = utc_now()
            set_shutdown_phase(self._phase)

        # NOTE: structlog's first emit from an OS signal-handler context
        # may be dropped (signal interrupting logging-internal lock).
        # The metric increment below is the canonical "shutdown initiated"
        # marker for operator dashboards — its critical section is far
        # shorter than logging's handler chain, so it survives the signal-
        # handler context where the log line below may not.
        record_shutdown_initiated()
        logger.info("shutdown.graceful_initiated")

        # Notify all handlers — each MUST return quickly
        for handler in self._handlers:
            try:
                handler.on_shutdown_start()
            except Exception as e:
                logger.exception(
                    "shutdown_coordinator.handler_start_error",
                    handler=type(handler).__name__,
                    error=e,
                )

        # Start unified drain process in background
        self._shutdown_thread = threading.Thread(target=self._drain_and_shutdown)
        self._shutdown_thread.daemon = True
        self._shutdown_thread.start()

    def _drain_and_shutdown(self) -> None:
        """Drain in-flight requests AND handler-specific resources concurrently."""
        deadline = utc_now() + timedelta(seconds=self._drain_timeout)

        while utc_now() < deadline:
            http_drained = (
                self._tracker.get_pending_count() == 0
                if self._tracker is not None
                else True
            )
            handlers_drained = all(
                self._safe_is_drain_complete(h) for h in self._handlers
            )

            if http_drained and handlers_drained:
                logger.info("shutdown.in_flight_drained")
                self._phase = ShutdownPhase.TERMINATED
                # Drain-window delta, not the tracker's process-lifetime total.
                self._drained_count = (
                    self._tracker.completed_count - self._completed_at_drain_start
                    if self._tracker is not None
                    else 0
                )
                set_shutdown_phase(ShutdownPhase.TERMINATED)
                record_drained(self._drained_count)
                # _shutdown_started_at is set in shutdown() before this method
                # is reachable; guard with assert for mypy narrowing.
                assert self._shutdown_started_at is not None
                record_drain_duration(
                    (utc_now() - self._shutdown_started_at).total_seconds()
                )

                for handler in self._handlers:
                    try:
                        handler.on_drain_complete()
                    except Exception as e:
                        logger.exception(
                            "shutdown_coordinator.handler_drain_complete_error",
                            handler=type(handler).__name__,
                            error=e,
                        )

                # Drain ends ⇒ process ends, on both TERMINATED paths
                # (597 D10) — no-op unless a defer-exit signal armed it.
                self._arm_deferred_exit()
                return

            logger.debug(
                "shutdown_coordinator.draining",
                pending_http=(
                    self._tracker.get_pending_count()
                    if self._tracker is not None
                    else 0
                ),
                handlers_drained=handlers_drained,
            )
            time.sleep(self._check_interval)

        # Timeout — force shutdown
        logger.warning("shutdown.drain_timeout_reached")
        self._phase = ShutdownPhase.TERMINATING
        set_shutdown_phase(ShutdownPhase.TERMINATING)

        pending_requests = (
            self._tracker.get_pending_requests() if self._tracker is not None else []
        )
        # Single-lock force-time snapshot: abort_all returns the aborted set
        # AND the completed count from one tracker-lock acquisition, so
        # drained + aborted reconciles exactly even if a request completes
        # while the force shutdown is in progress.
        if self._tracker is not None:
            aborted, completed_at_force = self._tracker.abort_all()
        else:
            aborted, completed_at_force = [], 0
        self._aborted_count = len(aborted)
        # A forced drain still drained everything that finished before the
        # timeout — record that delta alongside the aborted count.
        self._drained_count = completed_at_force - self._completed_at_drain_start
        record_aborted(len(aborted))
        record_drained(self._drained_count)

        for handler in self._handlers:
            try:
                handler.on_force_shutdown(pending_requests)
            except Exception as e:
                logger.exception(
                    "shutdown_coordinator.handler_force_shutdown_error",
                    handler=type(handler).__name__,
                    error=e,
                )

        self._phase = ShutdownPhase.TERMINATED
        set_shutdown_phase(ShutdownPhase.TERMINATED)
        assert self._shutdown_started_at is not None
        record_drain_duration((utc_now() - self._shutdown_started_at).total_seconds())

        # Forced-path exit parity (597 D10): same arming step as the clean
        # drain — no-op unless a defer-exit signal armed it.
        self._arm_deferred_exit()

    def _safe_is_drain_complete(self, handler: ShutdownHandler) -> bool:
        """Call handler.is_drain_complete() with exception safety."""
        try:
            return handler.is_drain_complete()
        except Exception as e:
            logger.warning(
                "shutdown_coordinator.handler_drain_check_failed",
                handler=type(handler).__name__,
                error=e,
            )
            return True  # On error, consider handler drained to avoid blocking shutdown

    def get_stats(self) -> ShutdownStats:
        """Get current shutdown statistics"""
        remaining = None
        if self._shutdown_started_at and self._phase == ShutdownPhase.DRAINING:
            elapsed = (utc_now() - self._shutdown_started_at).total_seconds()
            remaining = max(0, self._drain_timeout - elapsed)

        # Live drain-window delta while the shutdown is in flight, frozen
        # final count afterwards. Lock-free is safe: initiate_shutdown stores
        # the baseline before the DRAINING phase store, so observing an
        # in-flight phase guarantees the baseline is already visible.
        if (
            self._tracker is not None
            and self._shutdown_started_at is not None
            and self._phase in (ShutdownPhase.DRAINING, ShutdownPhase.TERMINATING)
        ):
            completed_during_drain = (
                self._tracker.completed_count - self._completed_at_drain_start
            )
        else:
            completed_during_drain = self._drained_count

        return ShutdownStats(
            phase=self._phase,
            shutdown_started_at=self._shutdown_started_at,
            in_flight_count=(
                self._tracker.get_pending_count() if self._tracker is not None else 0
            ),
            completed_during_drain=completed_during_drain,
            aborted_count=self._aborted_count,
            drain_timeout_seconds=self._drain_timeout,
            remaining_drain_time=remaining,
        )

    def wait_for_shutdown(self, timeout: float | None = None) -> bool:
        """Wait for shutdown to complete. Returns True if completed."""
        if self._shutdown_thread:
            self._shutdown_thread.join(timeout=timeout)
            return self._phase == ShutdownPhase.TERMINATED
        return False


# =============================================================================
# Module-level Singleton
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

_get_coordinator, configure_shutdown_coordinator, reset_shutdown_coordinator = (
    make_singleton_factory("shutdown_coordinator", GracefulShutdownCoordinator)
)


def get_shutdown_coordinator(
    request_tracker: RequestTracker | None = None,
) -> GracefulShutdownCoordinator:
    """Module-level singleton for centralized shutdown coordination."""
    coordinator = _get_coordinator()
    if request_tracker is not None and coordinator._tracker is None:
        coordinator._tracker = request_tracker
    return coordinator
