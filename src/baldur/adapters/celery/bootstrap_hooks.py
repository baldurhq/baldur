"""Celery worker bootstrap receivers — the adapter's ``baldur.init()`` call.

Every framework adapter is responsible for initializing Baldur on its own
startup path: Django from ``AppConfig.ready()``, Flask from its bootstrap,
FastAPI from the lifespan. This module is Celery's. Without it a Celery-only
deployment runs on pre-init defaults — ``BALDUR_REDIS_URL`` unread, so circuit
breaker and idempotency state diverge per worker; no scheduler; no background
maintenance — until the user writes the receiver themselves.

Two receivers, because a Celery worker has two kinds of process:

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

Both are connected with an explicit ``dispatch_uid``, so the three sites that
arm them (the two adapter entry points and ``baldur.init()`` itself) register
one receiver per signal between them.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

import structlog
from celery.signals import worker_init, worker_process_init

from baldur.core.exceptions import ConfigurationError

logger = structlog.get_logger()

__all__ = [
    "connect_celery_bootstrap_receivers",
    "disconnect_celery_bootstrap_receivers",
    "is_celery_bootstrap_receivers_connected",
]

_WORKER_INIT_DISPATCH_UID = "baldur.celery.bootstrap.worker_init"
_WORKER_PROCESS_INIT_DISPATCH_UID = "baldur.celery.bootstrap.worker_process_init"

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


def connect_celery_bootstrap_receivers() -> None:
    """Connect both bootstrap receivers. Idempotent across all arming sites.

    ``sender=None`` because the senders differ per signal and per worker
    (``worker_init`` sends the ``WorkController``, ``worker_process_init``
    sends nothing useful), so a sender-bound connect would simply never match.
    The ``dispatch_uid`` is what makes repeat arming a no-op: celery keys its
    receiver table on ``(dispatch_uid, sender_id)``.
    """
    worker_init.connect(_on_worker_init, dispatch_uid=_WORKER_INIT_DISPATCH_UID)
    worker_process_init.connect(
        _on_worker_process_init,
        dispatch_uid=_WORKER_PROCESS_INIT_DISPATCH_UID,
    )


def disconnect_celery_bootstrap_receivers() -> None:
    """Disconnect both bootstrap receivers (test isolation, adapter teardown)."""
    worker_init.disconnect(_on_worker_init, dispatch_uid=_WORKER_INIT_DISPATCH_UID)
    worker_process_init.disconnect(
        _on_worker_process_init,
        dispatch_uid=_WORKER_PROCESS_INIT_DISPATCH_UID,
    )


def _is_connected(signal: Any, dispatch_uid: str) -> bool:
    """True when ``signal`` holds a receiver registered under ``dispatch_uid``."""
    return any(key[0] == dispatch_uid for key, _ in signal.receivers)


def is_celery_bootstrap_receivers_connected() -> bool:
    """True when both bootstrap receivers are registered on their signals."""
    return _is_connected(worker_init, _WORKER_INIT_DISPATCH_UID) and _is_connected(
        worker_process_init, _WORKER_PROCESS_INIT_DISPATCH_UID
    )
