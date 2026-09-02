"""Gunicorn worker-lifecycle hooks for baldur graceful shutdown.

These callables map to gunicorn server-config hook names and are
imported via the user's gunicorn config (``gunicorn -c``). They wire
baldur's :class:`GracefulShutdownCoordinator` into the worker
lifecycle so that registered handlers (Audit WAL flush, leader-election
release, bulkhead drain, etc.) actually run on SIGTERM.

This module is the single gunicorn hook surface baldur ships. Wire all
three hooks below; there is no separate post-fork hook to install.

Hook responsibilities
---------------------

``post_worker_init``
    Marks the process as a gunicorn worker by setting
    ``GUNICORN_WORKER=1``. Drops external-connection state the worker
    inherited across ``fork()`` — only under ``--preload``, since without
    it gunicorn loads the application inside the child and there is
    nothing inherited to drop. Initializes the shutdown coordinator with a
    ``RequestTracker`` so ``initiate_shutdown`` has state to drain.
    Installs a *chained* SIGTERM handler (``coordinator.initiate_shutdown``
    → original gunicorn handler), because gunicorn's ``worker_int``
    callback only fires for SIGINT/SIGQUIT — when the master forwards
    SIGTERM to workers (the normal graceful-shutdown path), gunicorn's
    own ``handle_exit`` runs without invoking any user hook. Chaining
    is the only way to plug ``coordinator.initiate_shutdown`` into the
    worker's SIGTERM lifecycle without breaking gunicorn's drain. The
    handler is fast: ``initiate_shutdown`` fires synchronous
    ``on_shutdown_start`` callbacks then spawns a daemon drain thread
    that runs in parallel with gunicorn's HTTP-drain. Then
    re-starts the ``init()``-started background daemon workers in the
    forked worker for **all** adapters via
    ``baldur.bootstrap.start_background_workers()`` (they die after fork
    if started in master, and ``init()`` is not re-run per worker), and
    additionally re-starts the Django-only extra threads (gauge
    hydration, correlation loop) when Django is present.

``worker_int``
    Invoked by gunicorn when SIGINT or SIGQUIT is forwarded to the
    worker. Calls ``coordinator.initiate_shutdown()`` for parity with
    the chained SIGTERM handler installed by ``post_worker_init``.

``worker_exit``
    Invoked by gunicorn after the worker stops accepting traffic, just
    before process termination — and, for a worker that died before the
    signal sent to it landed, also in the master, which is why the body
    starts by confirming it is running in the worker it was handed.
    Waits for the coordinator drain thread to complete, emits a reliable
    shutdown-complete log from the worker's main process (the coordinator's
    own terminal logs run in a signal frame or a daemon thread and can be
    dropped/killed), resets the Django background-thread start guards, tears
    the DLQ outbox down and flushes the audit system — the last two
    unconditionally, so a non-signal exit (``max_requests`` recycle,
    ``--reload``) still spills the buffered DLQ entries, closes the WAL and
    saves a checkpoint. ``shutdown.worker_exit_completed`` is the terminal
    marker: it is emitted exactly when the pipeline ran to the end, and
    carries ``process_role`` so the same event name answers that question on
    every adapter.
    The drain wait reads ``recovery_shutdown_settings``
    (``default_drain_timeout_seconds``, 30 s by default). Size gunicorn's
    ``--graceful-timeout`` ``>=`` that value (otherwise gunicorn SIGKILLs
    the worker before drain completes), and note that on the recycle path
    it is ``--timeout``, not ``--graceful-timeout``, that bounds how long
    this hook may run before the arbiter reports ``WORKER TIMEOUT``.

Doc reference: see Cat 1.8 scenario in
the perf-scenario plan for the canonical
end-to-end behavior contract.
"""

from __future__ import annotations

import os
import signal
from typing import Any

import structlog

logger = structlog.get_logger()

# Mirrors RecoveryShutdownSettings.default_drain_timeout_seconds' Field
# default. Used only when the settings read itself fails: a degenerate
# config must not skip the exit pipeline.
_DEFAULT_DRAIN_WAIT_SECONDS = 30.0


def _initiate_shutdown_safely() -> None:
    """Idempotent wrapper used by both the chained SIGTERM handler and
    the ``worker_int`` callback. ``initiate_shutdown`` itself is
    already idempotent (no-op when phase != RUNNING), so calling this
    twice is safe."""
    from baldur.core.shutdown_coordinator import get_shutdown_coordinator

    get_shutdown_coordinator().initiate_shutdown()


def _install_chained_sigterm_handler() -> None:
    """Wrap gunicorn's worker SIGTERM handler so baldur's drain is
    initiated alongside gunicorn's own ``handle_exit`` (which sets
    ``alive=False`` so the worker stops accepting new connections).

    Pattern precedent: ``baldur/audit/persistence/disk_buffer_shutdown.py``.
    Trade-off: the original handler is captured at registration time
    (post_worker_init), so any later re-registration by gunicorn would
    bypass baldur. This is acceptable because gunicorn does not re-init
    worker signals after ``post_worker_init`` — see ``workers/base.py``
    ``init_signals`` which runs once during ``init_process``.
    """
    original_sigterm = signal.getsignal(signal.SIGTERM)

    def _chained_sigterm(signum: int, frame: Any) -> None:
        _initiate_shutdown_safely()
        if callable(original_sigterm):
            original_sigterm(signum, frame)

    signal.signal(signal.SIGTERM, _chained_sigterm)


def _reset_kafka_after_fork(worker: Any) -> None:
    """Drop the event-producer reference inherited from the preload master.

    The reset drops the reference without issuing ``close()``/``flush()``,
    so nothing calls into the producer's background threads — they did not
    survive ``fork()`` and a call into them would deadlock.

    That producer adapter is distributed separately from the open-source
    core, so this is a no-op on a stock install.
    """
    try:
        from baldur_dormant.adapters.kafka.config import get_kafka_settings
        from baldur_dormant.adapters.kafka.producer import reset_kafka_producer
    except ImportError:
        logger.debug(
            "worker.postfork_kafka_skipped_no_dormant",
            worker_id=worker.pid,
        )
        return

    settings = get_kafka_settings()
    if not settings.bootstrap_servers:
        logger.debug("worker.postfork_kafka_skipped", worker_id=worker.pid)
        return

    reset_kafka_producer(cleanup=False)
    logger.info("worker.postfork_kafka_reset", worker_id=worker.pid)


def post_worker_init(worker: Any) -> None:
    """Gunicorn post_worker_init hook.

    See module docstring for responsibilities.
    """
    os.environ["GUNICORN_WORKER"] = "1"

    # Inherited-resource resets. Gated on preload because that is the only
    # branch where "drop what the master left me" is a true description:
    # without --preload, gunicorn runs load_wsgi() in the child, so
    # baldur.init() built this process's own state moments ago. Isolated so
    # a reset failure cannot skip the coordinator wiring below.
    if getattr(getattr(worker, "cfg", None), "preload_app", True):
        try:
            _reset_kafka_after_fork(worker)
        except Exception as exc:
            logger.warning(
                "worker.postfork_reset_failed",
                worker_id=getattr(worker, "pid", None),
                error=exc,
            )

    from baldur.core.shutdown_coordinator import (
        RequestTracker,
        get_shutdown_coordinator,
    )

    get_shutdown_coordinator(request_tracker=RequestTracker())

    _install_chained_sigterm_handler()

    # Framework-agnostic per-worker re-start of the init()-started background
    # daemon workers. Runs for ALL adapters: GUNICORN_WORKER=1 is set above, so
    # the per-starter is_gunicorn_master() skip now passes and the workers
    # (which die after fork() and are never re-started by init() in the worker)
    # come back. Each starter is fail-soft, so this cannot break the hook.
    from baldur.bootstrap import start_background_workers

    start_background_workers()

    # Django-adapter-intrinsic extras (gauge hydration, correlation loop)
    # that are not init()-started. The PRO/scaling starts moved into
    # start_background_workers() (615 D1/D4), so this is no longer a superset.
    try:
        from baldur.adapters.django.apps import BaldurConfig

        BaldurConfig.start_background_threads()
    except ImportError:
        pass


def worker_int(worker: Any) -> None:
    """Gunicorn worker_int hook (SIGINT/SIGQUIT forwarded to worker).

    See module docstring for responsibilities.
    """
    _initiate_shutdown_safely()


def worker_exit(server: Any, worker: Any) -> None:
    """Gunicorn worker_exit hook (worker about to terminate).

    Parameter order is gunicorn's own: the arbiter calls this positionally
    as ``worker_exit(arbiter, worker)``.

    See module docstring for responsibilities.
    """
    worker_id = getattr(worker, "pid", None)

    if worker_id != os.getpid():
        # The arbiter also reaches this hook, in the master, for a worker
        # that had already exited when it was signalled — a routine race
        # during scale-down and timeout replacement. Running the exit
        # pipeline there would tear down the master's audit system and set
        # a process-global once-flag that every later-forked worker
        # inherits, silently skipping its own flush forever.
        _log_worker_exit_skipped(worker_id)
        return

    try:
        from baldur.settings.recovery_shutdown import get_recovery_shutdown_settings

        drain_wait = get_recovery_shutdown_settings().default_drain_timeout_seconds
    except Exception as exc:
        logger.warning(
            "shutdown.drain_timeout_read_failed",
            worker_id=worker_id,
            error=exc,
        )
        drain_wait = _DEFAULT_DRAIN_WAIT_SECONDS

    # Isolated like the two steps below it. Resolving the coordinator can
    # itself fail on a degenerate config — a lazily constructed one reads the
    # same settings the wait timeout came from — and a failure here must not
    # cost the worker its audit flush.
    try:
        from baldur.core.shutdown_coordinator import (
            ShutdownPhase,
            get_shutdown_coordinator,
        )

        coordinator = get_shutdown_coordinator()
        drained = coordinator.wait_for_shutdown(timeout=drain_wait)

        # Reliable shutdown-complete signal. worker_exit runs in the worker's
        # main process — not the OS signal-handler frame (where
        # shutdown.graceful_initiated may be dropped) and not the daemon drain
        # thread (which can be killed before shutdown.in_flight_drained lands),
        # so this is the one terminal log that survives a real worker exit.
        # It reports the drain predicate, which the coordinator satisfies
        # before it runs handler teardown — the flush below may still be
        # ahead of us.
        if drained:
            logger.info("shutdown.worker_drained", worker_id=worker_id)
        elif coordinator.phase != ShutdownPhase.RUNNING:
            # Shutdown was initiated but drain did not reach TERMINATED within
            # the wait timeout (drain-timeout / forced termination).
            logger.warning(
                "shutdown.worker_drain_incomplete",
                worker_id=worker_id,
                phase=coordinator.phase.value,
            )
        # phase == RUNNING ⇒ no shutdown was initiated (normal worker recycle
        # / reload exit) — nothing to report about a drain that never started.
    except Exception as exc:
        logger.warning(
            "shutdown.drain_wait_failed",
            worker_id=worker_id,
            error=exc,
        )

    # Each remaining step is isolated: a Django-side failure must not skip
    # the audit flush, which is the one guarantee only this hook can offer
    # on a recycle exit.
    try:
        from baldur.adapters.django.apps import BaldurConfig

        BaldurConfig.stop_background_threads()
    except ImportError:
        pass
    except Exception as exc:
        logger.warning(
            "shutdown.django_thread_guards_reset_failed",
            worker_id=worker_id,
            error=exc,
        )

    # Unconditional for the same reason as the audit flush below, and ahead
    # of it so the outbox's final writes land while the WAL is still open. On
    # the SIGTERM path the coordinator's handler already ran this and the
    # once-guard makes it a no-op; on a max_requests / --reload recycle it is
    # the only teardown the outbox gets, and the buffered DLQ entries would
    # otherwise die with the daemon drain thread.
    #
    # Every log line it emits is in the module's own ``dlq_outbox.*``
    # namespace, never ``shutdown.*``: this hook's shutdown-event list is an
    # operator-facing contract.
    try:
        from baldur.services.dlq_outbox.outbox import stop_outbox_for_shutdown

        stop_outbox_for_shutdown()
    except Exception as exc:
        logger.warning(
            "dlq_outbox.worker_exit_teardown_failed",
            worker_id=worker_id,
            error=exc,
        )

    # Unconditional: on the SIGTERM path the coordinator's handler already
    # ran this and the once-guard makes it a no-op, but on a recycle exit
    # this is the only WAL flush / checkpoint save the worker gets.
    try:
        from baldur.audit.async_audit_lifecycle import graceful_shutdown_audit_system

        graceful_shutdown_audit_system()
    except Exception as exc:
        logger.warning(
            "shutdown.audit_flush_failed",
            worker_id=worker_id,
            error=exc,
        )

    # ``process_role`` distinguishes this from the celery receivers, which
    # emit the same terminal marker: one event name answers "did this worker's
    # exit pipeline run to the end" on any adapter.
    logger.info(
        "shutdown.worker_exit_completed",
        worker_id=worker_id,
        process_role="gunicorn_worker",
    )


def _log_worker_exit_skipped(worker_id: Any) -> None:
    """Report a ``worker_exit`` invocation this process must not act on.

    Level-branched so the guard cannot fail silently. ``GUNICORN_WORKER``
    is set by ``post_worker_init`` in the worker's own environ and never in
    the master, so:

    - unset ⇒ the master-side race, or a worker that died before
      ``post_worker_init`` ran ⇒ DEBUG, the expected frequent case;
    - set ⇒ a process that ran ``post_worker_init`` does not recognize its
      own pid ⇒ the hook contract does not hold and every worker is
      skipping its whole exit pipeline ⇒ WARNING.
    """
    if os.environ.get("GUNICORN_WORKER"):
        logger.warning(
            "shutdown.worker_exit_skipped",
            reason="pid_mismatch_inside_worker",
            worker_id=worker_id,
            process_id=os.getpid(),
        )
    else:
        logger.debug(
            "shutdown.worker_exit_skipped",
            reason="not_the_exiting_worker",
            worker_id=worker_id,
            process_id=os.getpid(),
        )
