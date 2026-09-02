"""Celery worker bootstrap receivers — the adapter's ``baldur.init()`` call.

Every framework adapter is responsible for initializing Baldur on its own
startup path: Django from ``AppConfig.ready()``, Flask from its bootstrap,
FastAPI from the lifespan. This module is Celery's. Without it a Celery-only
deployment runs on pre-init defaults — ``BALDUR_REDIS_URL`` unread, so circuit
breaker and idempotency state diverge per worker; no scheduler; no background
maintenance — until the user writes the receiver themselves.

Four receivers, two per side, because a Celery worker has two kinds of process
and each has to be both started and stopped:

``worker_init``
    The worker's main process, for every launcher shape. Celery sends it from
    the ``WorkController`` constructor, so it reaches programmatic workers as
    well as the CLI, and it fires before any pool exists — which is what makes
    it the right place to decide where background threads may live. The
    decision is the pool's: on a forking pool this process is about to fork,
    so its daemon threads would die in the children, and the starters are
    deferred to them; on a non-forking pool this process serves tasks itself,
    so they start here.

``worker_process_init``
    The process that runs tasks: every prefork child (including the
    replacements ``maxtasksperchild`` recycles in) and the solo pool's own main
    process. It marks itself as serving, initializes — a no-op when the fork
    handed it a completed ``init()`` — and starts the background workers the
    parent deferred.

``worker_shutdown``
    The worker main process's exit, sent by ``WorkController.stop()`` after the
    pool has stopped and the blueprint has joined — so no task is running and it
    is safe to initiate the coordinator drain here, and only here. It is also
    the only stop-side seam a non-forking pool has, since ``process_destructor``
    is never wired there. This is the gunicorn ``worker_exit`` parity: drain,
    tear the outbox down, flush audit, emit the terminal marker.

``worker_process_shutdown``
    The exit of a process that runs tasks: every prefork child, including the
    ones a ``maxtasksperchild`` recycle retires. Sent from billiard's
    ``on_process_exit`` inside the child, immediately before ``os._exit`` — its
    last executable frame, reached on every child-exit lane (the recycle return,
    the pool-shutdown sentinel, an uncaught task-loop exception, a TERMSIG).
    It deliberately does NOT touch the shutdown coordinator: the child inherited
    the parent's handler list and does not own that state, and a recycle is
    routine operation that must not pay a full coordinator drain.

No receiver is connected to ``worker_shutting_down`` and none installs an OS
signal handler of its own. (``baldur.init()``, which ``worker_init`` runs, does
register the coordinator's SIGTERM/SIGINT handlers in the worker main; celery's
``install_platform_tweaks`` overwrites them before the worker serves, so they
never take part in a real stop.) ``worker_shutting_down`` fires while tasks are still executing
and from inside celery's signal-handler frame; initiating the drain there would
close the audit WAL and stop the outbox underneath running tasks — a worse loss
than the one the stop side exists to prevent. Celery reaches ``worker_shutdown``
on its own through the warm-shutdown chain, so nothing needs chaining.

All four are connected with an explicit ``dispatch_uid``, so the three sites
that arm them (the two adapter entry points and ``baldur.init()`` itself)
register one receiver per signal between them.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Any

import structlog
from celery.signals import (
    worker_init,
    worker_process_init,
    worker_process_shutdown,
    worker_shutdown,
)

from baldur.core.exceptions import ConfigurationError

logger = structlog.get_logger()

__all__ = [
    "connect_celery_bootstrap_receivers",
    "disconnect_celery_bootstrap_receivers",
    "is_celery_bootstrap_receivers_connected",
]

_WORKER_INIT_DISPATCH_UID = "baldur.celery.bootstrap.worker_init"
_WORKER_PROCESS_INIT_DISPATCH_UID = "baldur.celery.bootstrap.worker_process_init"
_WORKER_SHUTDOWN_DISPATCH_UID = "baldur.celery.bootstrap.worker_shutdown"
_WORKER_PROCESS_SHUTDOWN_DISPATCH_UID = (
    "baldur.celery.bootstrap.worker_process_shutdown"
)

# ``process_role`` values on the shared terminal marker. One event name answers
# "did this process's exit pipeline run to the end" on every adapter; the role
# says which pipeline ran.
_ROLE_WORKER_MAIN = "celery_worker_main"
_ROLE_POOL_CHILD = "celery_pool_child"

# Used only when the drain-timeout settings read itself fails.
_FALLBACK_DRAIN_WAIT_SECONDS = 30.0

# Pool aliases resolved without importing anything. Celery's own alias table
# maps both of these to the prefork pool; the rest of the table is
# non-forking. Taking the answer from the string matters under ``-P gevent`` /
# ``-P eventlet``, where importing the prefork module (and through it asynpool
# and billiard) would pull a large import set into a monkey-patched
# interpreter that celery itself never touches.
_FORK_POOL_ALIASES = frozenset({"prefork", "processes"})
_NON_FORK_POOL_ALIASES = frozenset({"solo", "threads", "gevent", "eventlet"})

_PREFORK_POOL_MODULE = "celery.concurrency.prefork"
_CELERY_POOL_MODULE_PREFIX = "celery.concurrency."


class _PoolLane(str, Enum):
    """How the worker main process should treat its background starters."""

    #: The pool forks; the children serve and start their own.
    FORK = "fork"
    #: This process serves tasks itself and starts them here.
    NON_FORK = "non_fork"
    #: The pool's shape is not knowable; defer and say so.
    UNKNOWN = "unknown"


def _classify_pool(pool_cls: Any) -> _PoolLane:
    """Decide the lane from the pool the worker is about to build.

    ``worker_init`` fires before celery resolves ``pool_cls``, so this sees
    whatever the operator or the app config supplied: an alias string, a dotted
    path, or a class object.

    Aliases answer without importing. Anything else is resolved through
    ``get_implementation`` — the identical call celery makes on the very next
    line, so it costs the same import set celery is about to pay for anyway —
    and the resolved class is then classified by the *module names* on its MRO
    rather than by an ``issubclass`` check, which would require importing the
    prefork module to have something to compare against.

    An out-of-tree pool (``CELERY_CUSTOM_WORKER_POOL``) lands in UNKNOWN
    deliberately. Whether it forks is unknowable from here, and guessing
    "non-forking" would start services in a process that then forks children
    with none — the failure this whole module exists to remove.
    """
    if isinstance(pool_cls, str):
        alias = pool_cls.strip().lower()
        if alias in _FORK_POOL_ALIASES:
            return _PoolLane.FORK
        if alias in _NON_FORK_POOL_ALIASES:
            return _PoolLane.NON_FORK

    try:
        from celery.concurrency import get_implementation

        resolved = get_implementation(pool_cls)
    except Exception as exc:
        logger.debug("celery.pool_resolution_failed", pool=repr(pool_cls), error=exc)
        return _PoolLane.UNKNOWN

    mro_modules = {
        getattr(base, "__module__", "") for base in getattr(resolved, "__mro__", ())
    }
    if _PREFORK_POOL_MODULE in mro_modules:
        return _PoolLane.FORK
    if getattr(resolved, "__module__", "").startswith(_CELERY_POOL_MODULE_PREFIX):
        return _PoolLane.NON_FORK
    return _PoolLane.UNKNOWN


def _warn_pool_unknown(pool_cls: Any) -> None:
    """Report the deferral this process cannot resolve on its own.

    Emitted at classification time rather than left to the deferral watchdog:
    the watchdog only arms on an argv-based deferral, and a custom pool that
    does not fork never sends ``worker_process_init``, so the deferral here is
    permanent by design and nothing later would report it.
    """
    logger.warning(
        "celery.background_workers_not_started",
        pool=repr(pool_cls),
        hint=(
            "baldur could not tell whether this celery pool forks, so it left "
            "its background workers unstarted rather than start them in a "
            "process that may fork them away. Connect baldur.init() plus "
            "baldur.bootstrap.start_background_workers() to the signal your "
            "pool fires in the process that runs tasks."
        ),
    )


def _on_worker_init(**kwargs: Any) -> None:
    """``worker_init`` receiver — initialize the worker's main process.

    Order is pinned. The celery-worker-main flag is set first, so the
    ``init()`` below sees signal-based truth about this process instead of
    falling back to the argv heuristic. The posture reconciliation runs next,
    while ``init()``'s own state still describes any *earlier* initialization
    (the Django-fixup path initializes at app-module import, before this).
    Only then is the lane acted on.

    ``ConfigurationError`` is converted to ``SystemExit``. Celery's
    ``Signal.send`` catches ``Exception`` and logs it, so a receiver cannot
    fail a worker's boot by raising one — the misconfiguration would be logged
    once and the worker would come up on in-process defaults, which for a
    production deploy is the silent-wrong outcome ``init()`` raises to prevent.
    ``SystemExit`` is a ``BaseException``: it passes that handler, propagates
    through the send, and the celery CLI does not catch it either. This is the
    universal main-process lane and it fires before any fork or spawn, so one
    abort here covers every worker process.

    Anything else is left to ``Signal.send`` — the worker then boots on the
    documented pre-init semantics rather than not at all.
    """
    from baldur.bootstrap import (
        init,
        reconcile_celery_deferral_posture,
        start_background_workers,
    )
    from baldur.core.process_utils import (
        mark_celery_worker_main,
        mark_celery_worker_serving,
    )

    mark_celery_worker_main()

    pool_cls = getattr(kwargs.get("sender"), "pool_cls", None)
    lane = _classify_pool(pool_cls)

    reconcile_celery_deferral_posture(fork_lane=lane is _PoolLane.FORK)

    if lane is _PoolLane.NON_FORK:
        mark_celery_worker_serving()

    try:
        init()
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from exc

    if lane is _PoolLane.NON_FORK:
        # Explicit, not implied by init(): on the Django-fixup path init()
        # already ran with the starters deferred and is a no-op here, so this
        # call is what un-defers them. When init() just ran them, the
        # per-starter guards make it a no-op instead.
        start_background_workers()
        logger.info("celery.background_workers_started", pool=repr(pool_cls))
    elif lane is _PoolLane.FORK:
        logger.info(
            "celery.background_workers_delegated",
            pool=repr(pool_cls),
            hint=(
                "this worker forks its pool, so baldur's background workers "
                "start in each pool child rather than here."
            ),
        )
    else:
        _warn_pool_unknown(pool_cls)


def _on_worker_process_init(**kwargs: Any) -> None:
    """``worker_process_init`` receiver — initialize a process that runs tasks.

    Order is pinned, and the serving marker comes first for a reason that only
    shows on Windows: billiard's spawn path restores the *parent's*
    ``sys.argv`` into the child, so the argv heuristic reports "celery worker
    main" there. With the marker already set, the composed fork-source
    predicate answers False regardless; with it set afterwards, every starter
    in this child would skip and never be asked again.

    ``ConfigurationError`` is logged rather than converted to ``SystemExit``
    here. In a fork child ``init()`` is an inherited-``_init_done`` no-op and
    cannot raise it at all; a spawn child does re-run the whole of ``init()``,
    where a condition that appeared after the parent booted clean (a full disk
    tripping the production WAL-dir check, say) would turn an abort into a
    kill-and-respawn loop. The main process already had its chance to fail
    loudly, at ``worker_init``, before any child existed.

    Everything here must stay spawn-only and non-blocking. On the default pool
    this receiver runs inside billiard's child initializer, *before* the child
    reports itself up, on the ``worker_proc_alive_timeout`` clock whose expiry
    is a SIGKILL and a respawn.
    """
    from baldur.bootstrap import init, start_background_workers
    from baldur.core.process_utils import (
        is_celery_worker_serving,
        mark_celery_worker_serving,
    )

    # Read before marking: an already-set marker means this signal is firing in
    # a process that was serving before it — the solo pool, whose main process
    # is the same one ``worker_init`` just handled. Only a genuinely new
    # process (a prefork child, including a maxtasksperchild replacement) finds
    # it unset, because the pool parent never sets it.
    is_new_serving_process = not is_celery_worker_serving()
    mark_celery_worker_serving()

    try:
        init()
    except ConfigurationError as exc:
        logger.exception("celery.worker_process_init_error", error=str(exc))
        return

    start_background_workers()

    if is_new_serving_process:
        # Django-adapter-intrinsic extras (the correlation loop), which
        # start_background_workers() deliberately does not own. The same call
        # the gunicorn post_worker_init hook makes, for the same reason: on a
        # Django+Celery worker the fixup runs ready() in the main process, so
        # this thread exists there and is dead here. It resets its own
        # duplicate-start guards first — which is what a fresh child needs and
        # exactly why it must not run in the solo pool's already-serving main.
        try:
            from baldur.adapters.django.apps import BaldurConfig

            BaldurConfig.start_background_threads()
        except ImportError:
            pass


def _tear_down_outbox(process_role: str) -> dict[str, int]:
    """Run the idempotent outbox teardown and return its terminal counts.

    Shared by both stop-side receivers. The counts ride the terminal marker so
    one log line answers "how many buffered DLQ entries did this process still
    hold, and where did they go" — including the residual, the entries that are
    gone. Returns an empty mapping when the teardown itself failed, so the
    marker still reports that the pipeline ran.
    """
    from baldur.services.dlq_outbox.outbox import stop_outbox_for_shutdown

    result = stop_outbox_for_shutdown()
    logger.info(
        "dlq_outbox.shutdown_teardown_completed",
        process_role=process_role,
        pending_at_entry=result.pending_at_entry,
        dispatched=result.dispatched,
        soft_failed=result.soft_failed,
        failed=result.failed,
        emergency_dumped=result.emergency_dumped,
        residual=result.residual,
        duplicated=result.duplicated,
    )
    return {
        "outbox_pending_at_entry": result.pending_at_entry,
        "outbox_dispatched": result.dispatched,
        "outbox_soft_failed": result.soft_failed,
        "outbox_failed": result.failed,
        "outbox_emergency_dumped": result.emergency_dumped,
        "outbox_residual": result.residual,
        "outbox_duplicated": result.duplicated,
    }


def _on_worker_process_shutdown(**kwargs: Any) -> None:
    """``worker_process_shutdown`` receiver — a task-running process is exiting.

    Runs in the pool child's last executable frame, before ``os._exit``. Order
    is pinned: the outbox teardown first, so its final writes land while the
    audit WAL is still open, then the audit flush, then the terminal marker.

    Each step is isolated in its own ``try/except``. An audit-side failure must
    not cost the child its outbox teardown, and vice versa — this is the only
    exit pipeline the child ever gets, and a ``maxtasksperchild`` recycle runs
    it as routine operation.

    The shutdown coordinator is deliberately never touched here. The child
    inherited the parent's handler list — ``init()`` in a fork child is an
    ``_init_done`` no-op, so the handlers were never re-registered — and firing
    it would run leader-election release, exporter teardown and the private
    service handlers against state this process does not own. It would also cost
    at least one drain check interval on every recycle, which is the wrong trade
    for a routine operation.
    """
    pid = os.getpid()
    counts: dict[str, int] = {}

    try:
        counts = _tear_down_outbox(_ROLE_POOL_CHILD)
    except Exception as exc:
        logger.warning(
            "dlq_outbox.worker_process_shutdown_teardown_failed",
            worker_id=pid,
            error=exc,
        )

    try:
        from baldur.audit.async_audit_lifecycle import graceful_shutdown_audit_system

        graceful_shutdown_audit_system()
    except Exception as exc:
        logger.warning("shutdown.audit_flush_failed", worker_id=pid, error=exc)

    logger.info(
        "shutdown.worker_exit_completed",
        worker_id=pid,
        process_role=_ROLE_POOL_CHILD,
        **counts,
    )


def _on_worker_shutdown(**kwargs: Any) -> None:
    """``worker_shutdown`` receiver — the worker main process is exiting.

    The gunicorn ``worker_exit`` parity, in the same order and with the same
    per-step isolation. By the time celery sends this signal the pool has
    stopped and the blueprint has joined, so no task is running and initiating
    the coordinator drain is safe here — which is exactly why no receiver is
    connected to ``worker_shutting_down``, which fires while tasks still run.

    Steps 3 and 4 are unconditional. When the drain in step 2 converged, the
    coordinator's own handlers already ran both and their once-guards make these
    no-ops; when it did not — or when nothing initiated a shutdown at all — they
    are the only teardown this process gets.

    The drain wait subtracts the outbox teardown's own budget. Step 2 waits on
    *other* subsystems and step 3 is queued behind it, so an unreserved wait
    makes the teardown the first thing an external stop timeout cuts: unlike
    gunicorn there is no in-process watcher here — billiard joins its children
    with no timeout — so the only bound on a celery worker's stop is the
    platform's (Kubernetes ``terminationGracePeriodSeconds``, systemd
    ``TimeoutStopSec``). Subtracting costs a slow-but-progressing drain its last
    few seconds and guarantees the teardown runs; a drain that needed them says
    so in its own log line.

    Step 1 is load-bearing rather than defensive: resolving the coordinator
    *lazily constructs* it, and the constructor reads settings, so a degenerate
    config raises there. Unchained, that one raise would cost the worker both
    its outbox teardown and its audit flush.
    """
    pid = os.getpid()
    counts: dict[str, int] = {}

    try:
        from baldur.core.shutdown_coordinator import (
            ShutdownPhase,
            get_shutdown_coordinator,
        )
        from baldur.services.dlq_outbox.outbox import get_shutdown_reserve_seconds
        from baldur.settings.recovery_shutdown import get_recovery_shutdown_settings

        try:
            drain_timeout = (
                get_recovery_shutdown_settings().default_drain_timeout_seconds
            )
        except Exception as exc:
            logger.warning("shutdown.drain_timeout_read_failed", error=exc)
            drain_timeout = _FALLBACK_DRAIN_WAIT_SECONDS

        coordinator = get_shutdown_coordinator()
        coordinator.initiate_shutdown()

        drain_wait = max(0.0, drain_timeout - get_shutdown_reserve_seconds())
        drained = coordinator.wait_for_shutdown(timeout=drain_wait)

        if drained:
            logger.info("shutdown.worker_drained", worker_id=pid)
        elif coordinator.phase != ShutdownPhase.RUNNING:
            logger.warning(
                "shutdown.worker_drain_incomplete",
                worker_id=pid,
                phase=coordinator.phase.value,
            )
        # phase == RUNNING ⇒ nothing was ever initiated — nothing to report
        # about a drain that never started.
    except Exception as exc:
        logger.warning("shutdown.drain_wait_failed", worker_id=pid, error=exc)

    try:
        counts = _tear_down_outbox(_ROLE_WORKER_MAIN)
    except Exception as exc:
        logger.warning(
            "dlq_outbox.worker_shutdown_teardown_failed",
            worker_id=pid,
            error=exc,
        )

    try:
        from baldur.audit.async_audit_lifecycle import graceful_shutdown_audit_system

        graceful_shutdown_audit_system()
    except Exception as exc:
        logger.warning("shutdown.audit_flush_failed", worker_id=pid, error=exc)

    logger.info(
        "shutdown.worker_exit_completed",
        worker_id=pid,
        process_role=_ROLE_WORKER_MAIN,
        **counts,
    )


def connect_celery_bootstrap_receivers() -> None:
    """Connect all four bootstrap receivers. Idempotent across arming sites.

    ``sender=None`` because the senders differ per signal and per worker
    (``worker_init`` sends the ``WorkController``, ``worker_process_init``
    sends nothing useful), so a sender-bound connect would simply never match.
    The ``dispatch_uid`` is what makes repeat arming a no-op: celery keys its
    receiver table on ``(dispatch_uid, sender_id)``.

    The stop side rides this same function rather than a separate arming path,
    so there is no reachable state where a worker's start side is armed and its
    stop side is not.
    """
    worker_init.connect(_on_worker_init, dispatch_uid=_WORKER_INIT_DISPATCH_UID)
    worker_process_init.connect(
        _on_worker_process_init,
        dispatch_uid=_WORKER_PROCESS_INIT_DISPATCH_UID,
    )
    worker_shutdown.connect(
        _on_worker_shutdown,
        dispatch_uid=_WORKER_SHUTDOWN_DISPATCH_UID,
    )
    worker_process_shutdown.connect(
        _on_worker_process_shutdown,
        dispatch_uid=_WORKER_PROCESS_SHUTDOWN_DISPATCH_UID,
    )


def disconnect_celery_bootstrap_receivers() -> None:
    """Disconnect all four bootstrap receivers (test isolation, teardown)."""
    worker_init.disconnect(_on_worker_init, dispatch_uid=_WORKER_INIT_DISPATCH_UID)
    worker_process_init.disconnect(
        _on_worker_process_init,
        dispatch_uid=_WORKER_PROCESS_INIT_DISPATCH_UID,
    )
    worker_shutdown.disconnect(
        _on_worker_shutdown,
        dispatch_uid=_WORKER_SHUTDOWN_DISPATCH_UID,
    )
    worker_process_shutdown.disconnect(
        _on_worker_process_shutdown,
        dispatch_uid=_WORKER_PROCESS_SHUTDOWN_DISPATCH_UID,
    )


def _is_connected(signal: Any, dispatch_uid: str) -> bool:
    """True when ``signal`` holds a receiver registered under ``dispatch_uid``."""
    return any(key[0] == dispatch_uid for key, _ in signal.receivers)


def is_celery_bootstrap_receivers_connected() -> bool:
    """True when all four bootstrap receivers are registered on their signals."""
    return all(
        _is_connected(signal, dispatch_uid)
        for signal, dispatch_uid in (
            (worker_init, _WORKER_INIT_DISPATCH_UID),
            (worker_process_init, _WORKER_PROCESS_INIT_DISPATCH_UID),
            (worker_shutdown, _WORKER_SHUTDOWN_DISPATCH_UID),
            (worker_process_shutdown, _WORKER_PROCESS_SHUTDOWN_DISPATCH_UID),
        )
    )
