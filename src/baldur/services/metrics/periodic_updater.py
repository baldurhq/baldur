"""Periodic per-process collection of the repository-backed gauge families.

The Prometheus registry is process-local: each serving process exposes what it
itself has written, and the exposition endpoint renders the answering worker's
own registry. A refresh job that runs in exactly one process — a leader-gated
scheduler entry, or a Celery beat task in a worker nobody scrapes — therefore
leaves every other process's scrape surface frozen at whatever it held when it
started. That is why this updater is a *per-process* daemon thread registered
in the background-worker starters, started once per serving process (and
re-started per forked worker by the gunicorn post-fork hook), rather than a
scheduled job.

Each tick calls ``collect_all_metrics()``, which takes one repository snapshot
and refreshes the DLQ pending / status, circuit-breaker and retry gauge
families from it. Structurally a sibling of ``BulkheadMetricsUpdater``
(immediate-first-tick loop, ``DaemonWorkerHandle`` registration with
crash-capture restart, idempotent ``start()``, Event-based ``stop()``), with
one addition: an optional startup jitter, so a multi-server restart does not
stampede a shared repository.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from baldur.meta.daemon_worker import DaemonWorkerHandle

logger = structlog.get_logger()

__all__ = [
    "DomainGaugeUpdater",
    "get_domain_gauge_updater",
    "reset_domain_gauge_updater",
    "start_domain_gauge_updater",
    "stop_domain_gauge_updater",
]

#: Registered daemon-worker name — also the ``name`` label of the
#: ``baldur_daemon_worker_*`` liveness series for this thread.
DAEMON_WORKER_NAME = "domain_gauge_updater"

#: Fallback interval when no caller supplies one. Production always passes
#: ``MetricsSettings.collection_interval_seconds``; this keeps a bare
#: ``get_domain_gauge_updater()`` in a REPL or test from inventing a cadence.
_DEFAULT_INTERVAL_SECONDS = 60.0

#: Fixed join ceiling on ``stop()`` — deliberately NOT ``interval + 1`` like the
#: bulkhead sibling: at a 60 s default interval a 61 s join would exceed a
#: typical 30 s worker-drain budget. The stop Event interrupts the sleep in
#: milliseconds, so this only ever trips on a tick blocked inside a repository
#: read, where abandoning the daemon thread is the correct outcome.
_STOP_JOIN_TIMEOUT_SECONDS = 5.0


class DomainGaugeUpdater:
    """Background thread refreshing the repository-backed gauge families.

    Usage:
        updater = DomainGaugeUpdater(interval=60.0)
        updater.start()
        # ... application runs ...
        updater.stop()
    """

    def __init__(
        self,
        interval: float = _DEFAULT_INTERVAL_SECONDS,
        *,
        jitter_seconds: float = 0.0,
        collect: Callable[[], Any] | None = None,
    ) -> None:
        """
        Args:
            interval: collection interval (seconds)
            jitter_seconds: delay before the FIRST tick only, to spread a
                multi-server restart across the repository. A respawn after a
                crash skips it and collects immediately.
            collect: collection callable (defaults to ``collect_all_metrics``);
                injectable so tests can drive a tick without a repository.
        """
        self._interval = interval
        self._jitter_seconds = jitter_seconds
        self._collect = collect
        self._jitter_pending = jitter_seconds > 0
        self._running = False
        self._thread: threading.Thread | None = None
        self._handle: DaemonWorkerHandle | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the collector thread (idempotent)."""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker
        from baldur.services.metrics.recorders import emit_heartbeat
        from baldur.services.metrics.updaters import (
            METRIC_COLLECTION_HEARTBEAT_COMPONENT,
        )

        if self._running:
            return

        self._stop_event.clear()
        self._running = True

        # Seed the labelled heartbeat before the first tick. A labelled gauge
        # exports no sample until it is first touched, so without this a
        # process whose collection NEVER succeeds (repository unresolvable from
        # boot) would export no heartbeat series at all — the staleness rule
        # would evaluate an empty vector and the dead-man's switch would be
        # silent on exactly the never-worked case.
        emit_heartbeat(component=METRIC_COLLECTION_HEARTBEAT_COMPONENT)

        self._spawn_thread()
        assert self._thread is not None  # _spawn_thread() postcondition
        self._handle = DaemonWorkerHandle(
            thread=self._thread,
            tick_interval_seconds=self._interval,
            restart_callback=self._spawn_thread,
        )
        register_daemon_worker(DAEMON_WORKER_NAME, self._handle)
        logger.info(
            "domain_gauge_updater.started",
            interval=self._interval,
            jitter_seconds=self._jitter_seconds,
        )

    def _spawn_thread(self) -> None:
        """Construct + start a fresh collector thread."""
        self._thread = threading.Thread(
            target=self._update_loop_with_crash_capture,
            name=DAEMON_WORKER_NAME,
            daemon=True,
        )
        self._thread.start()
        if self._handle is not None:
            self._handle.thread = self._thread

    def _update_loop_with_crash_capture(self) -> None:
        try:
            self._update_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def stop(self) -> None:
        """Stop the collector thread.

        Test-facing, plus the shutdown-coordinator handler: no other production
        path stops it, since a daemon thread dies with the process.
        """
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        if self._handle is not None:
            self._handle.is_stopping = True
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=_STOP_JOIN_TIMEOUT_SECONDS)
            unregister_daemon_worker(DAEMON_WORKER_NAME)
            if self._thread.is_alive():
                logger.critical(
                    "daemon_worker.stop_join_timeout",
                    worker_name=DAEMON_WORKER_NAME,
                    join_timeout_seconds=_STOP_JOIN_TIMEOUT_SECONDS,
                )
        else:
            unregister_daemon_worker(DAEMON_WORKER_NAME)
        logger.info("domain_gauge_updater.stopped")

    def is_alive(self) -> bool:
        """Whether the collector thread is currently running."""
        return self._thread is not None and self._thread.is_alive()

    def _update_loop(self) -> None:
        """Collection loop."""
        if self._jitter_pending:
            self._jitter_pending = False
            if self._stop_event.wait(timeout=self._jitter_seconds):
                return

        while self._running:
            iter_start = time.monotonic()
            try:
                self._collect_once()
            except Exception as e:
                logger.warning(
                    "domain_gauge_updater.collection_failed",
                    error=e,
                )

            if self._handle is not None:
                self._handle.observe_iteration(time.monotonic() - iter_start)
                self._handle.heartbeat()

            # Event.wait sleeps for the interval but returns immediately when
            # stop() sets the event — shutdown takes ms, not up to interval.
            if self._stop_event.wait(timeout=self._interval):
                break

    def _collect_once(self) -> None:
        """Run one collection tick."""
        if self._collect is not None:
            self._collect()
            return

        from baldur.services.metrics.updaters import collect_all_metrics

        collect_all_metrics()


# =============================================================================
# Singleton
# =============================================================================

_updater: DomainGaugeUpdater | None = None
_updater_lock = threading.Lock()


def get_domain_gauge_updater(
    interval: float = _DEFAULT_INTERVAL_SECONDS,
    *,
    jitter_seconds: float = 0.0,
) -> DomainGaugeUpdater:
    """Return the DomainGaugeUpdater singleton.

    ``interval`` / ``jitter_seconds`` are captured at first call (the bootstrap
    starter is the production first-caller and passes the settings values).
    """
    global _updater
    if _updater is None:
        with _updater_lock:
            if _updater is None:
                _updater = DomainGaugeUpdater(
                    interval=interval,
                    jitter_seconds=jitter_seconds,
                )
    return _updater


def start_domain_gauge_updater(
    interval: float = _DEFAULT_INTERVAL_SECONDS,
    *,
    jitter_seconds: float = 0.0,
) -> DomainGaugeUpdater:
    """Start the collector (convenience function)."""
    updater = get_domain_gauge_updater(interval, jitter_seconds=jitter_seconds)
    updater.start()
    return updater


def stop_domain_gauge_updater() -> None:
    """Stop the collector (convenience function)."""
    if _updater is not None:
        _updater.stop()


def reset_domain_gauge_updater() -> None:
    """Stop and drop the collector singleton (for testing)."""
    global _updater

    with _updater_lock:
        if _updater is not None:
            _updater.stop()
            _updater = None
