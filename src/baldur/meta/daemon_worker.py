"""DaemonWorkerHandle — observability + respawn handle for daemon worker threads.

Carrier dataclass that the cross-shape ``DaemonWorkerProbe`` (impl 489 D3) and
``DaemonWorkerMetricRecorder`` (D2) consume. Each long-lived
``threading.Thread(daemon=True)`` worker constructs one of these at start
time and registers it via ``register_daemon_worker(name, handle)``; the worker
loop calls ``heartbeat()`` once per iteration and ``observe_iteration(d)`` to
record sleep-excluded execution time.

Per impl 489 D4.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

__all__ = ["DaemonWorkerHandle"]


@dataclass
class DaemonWorkerHandle:
    """Per-worker observability + respawn handle (impl 489 D4).

    The ``thread`` reference is rebindable — a respawn callback that
    constructs a fresh ``threading.Thread`` MUST update ``handle.thread``
    so subsequent liveness checks observe the new thread.

    ``restart_callback`` is the per-thread spawn helper (e.g.
    ``self._spawn_thread``). It MUST construct a new ``Thread(...)`` and
    call ``.start()`` WITHOUT consulting any ``_running`` / ``_is_running``
    guard on the worker class; the standard public ``start()`` methods
    early-return on the running flag and would silently no-op when used as
    a restart callback.
    """

    thread: threading.Thread
    tick_interval_seconds: float | None = None
    staleness_threshold_seconds: float | None = None
    last_heartbeat_at: float = field(default_factory=time.monotonic)
    restart_count: int = 0
    restart_callback: Callable[[], None] | None = None
    processing_delay_provider: Callable[[], float] | None = None
    last_healthy_observed_at: float | None = None
    last_respawn_attempt_at: float | None = None
    last_crash_reason: str | None = None
    is_stopping: bool = False
    _iteration_duration_observer: Callable[[float], None] | None = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        if self.staleness_threshold_seconds is None:
            if self.tick_interval_seconds is None:
                raise ValueError(
                    "DaemonWorkerHandle requires tick_interval_seconds or "
                    "staleness_threshold_seconds"
                )
            from baldur.settings.daemon_worker import get_daemon_worker_settings

            self.staleness_threshold_seconds = (
                self.tick_interval_seconds
                * get_daemon_worker_settings().default_staleness_multiplier
            )

    def heartbeat(self) -> None:
        """Mark the worker alive at the current monotonic time.

        Cost is one atomic monotonic write (~10ns); intended to be called
        once per loop iteration after the work is done and before the
        ``stop_event.wait(...)`` sleep.
        """
        self.last_heartbeat_at = time.monotonic()

    def observe_iteration(self, duration_seconds: float) -> None:
        """Record execution time of one loop iteration (sleep excluded).

        No-op when no observer is wired (handle constructed standalone or
        before ``register_daemon_worker`` has injected the histogram label
        binding).
        """
        if self._iteration_duration_observer is not None:
            self._iteration_duration_observer(duration_seconds)

    def record_crash(self, exc: BaseException) -> None:
        """Capture the last uncaught exception that escaped the loop target.

        Read by the ``DAEMON_WORKER_DIED`` event payload (impl 489 D12) so
        operators see the crash reason without grepping logs across pods.
        """
        self.last_crash_reason = f"{type(exc).__name__}: {exc}"
