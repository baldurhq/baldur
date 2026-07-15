"""Gunicorn worker-lifecycle hooks for baldur graceful shutdown.

These callables map to gunicorn server-config hook names and are
imported via the user's gunicorn config (``gunicorn -c``). They wire
baldur's :class:`GracefulShutdownCoordinator` into the worker
lifecycle so that registered handlers (Audit WAL flush, leader-election
release, bulkhead drain, etc.) actually run on SIGTERM.

Hook responsibilities
---------------------

``post_worker_init``
    Marks the process as a gunicorn worker by setting
    ``GUNICORN_WORKER=1``. Initializes the shutdown coordinator with a
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
    that runs in parallel with gunicorn's HTTP-drain. Then re-starts the
    ``init()``-started background daemon workers in the forked worker for
    **all** adapters via ``baldur.bootstrap.start_background_workers()``
    (they die after fork if started in master, and ``init()`` is not
    re-run per worker), and additionally re-starts the Django-only extra
    threads (gauge hydration, correlation loop) when Django is present.

``worker_int``
    Invoked by gunicorn when SIGINT or SIGQUIT is forwarded to the
    worker. Calls ``coordinator.initiate_shutdown()`` for parity with
    the chained SIGTERM handler installed by ``post_worker_init``.

``worker_exit``
    Invoked by gunicorn after the worker stops accepting traffic, just
    before process termination. Blocks (up to 30s) waiting for the
    coordinator drain thread to complete, emits a reliable
    shutdown-complete log from the worker's main process (the coordinator's
    own terminal logs run in a signal frame or a daemon thread and can be
    dropped/killed), then stops Django background daemon threads cleanly.
    The drain timeout defaults to 30s; to use
    a different value, tune ``recovery_shutdown_settings`` and ensure
    gunicorn's ``--graceful-timeout`` is ``>=`` the configured value
    (otherwise gunicorn SIGKILLs the worker before drain completes).

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


def post_worker_init(worker: Any) -> None:
    """Gunicorn post_worker_init hook.

    See module docstring for responsibilities.
    """
    os.environ["GUNICORN_WORKER"] = "1"

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


def worker_exit(worker: Any, server: Any) -> None:
    """Gunicorn worker_exit hook (worker about to terminate).

    See module docstring for responsibilities.
    """
    from baldur.core.shutdown_coordinator import (
        ShutdownPhase,
        get_shutdown_coordinator,
    )

    coordinator = get_shutdown_coordinator()
    drained = coordinator.wait_for_shutdown(timeout=30.0)

    # Reliable shutdown-complete signal. worker_exit runs in the worker's
    # main process — not the OS signal-handler frame (where
    # shutdown.graceful_initiated may be dropped) and not the daemon drain
    # thread (which can be killed before shutdown.in_flight_drained lands),
    # so this is the one terminal log that survives a real worker exit.
    if drained:
        logger.info("shutdown.worker_drained")
    elif coordinator.phase != ShutdownPhase.RUNNING:
        # Shutdown was initiated but drain did not reach TERMINATED within
        # the wait timeout (drain-timeout / forced termination).
        logger.warning(
            "shutdown.worker_drain_incomplete",
            phase=coordinator.phase.value,
        )
    # phase == RUNNING ⇒ no shutdown was initiated (normal worker recycle /
    # reload exit) — nothing to report.

    try:
        from baldur.adapters.django.apps import BaldurConfig

        BaldurConfig.stop_background_threads()
    except ImportError:
        pass
