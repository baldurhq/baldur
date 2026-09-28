"""Process model utilities for fork-safety.

Three concerns live here: detecting which process model the current process
is running under, deciding whether some *other* process is still alive
(``pid_alive``), and marking the entry points at which a component repairs
its own fork-inherited state (``fork_repaired``).

Governing principle: framework startup code must not clobber a host
server's signal handlers. Two mechanisms implement it —

- Under gunicorn, Baldur skips OS signal registration entirely (the
  helpers in this module detect gunicorn across its whole lifecycle)
  and plugs into gunicorn's worker hooks instead.
- Everywhere else, ``GracefulShutdownCoordinator.register_signals``
  captures the previously installed disposition per signal and
  classifies it: an explicit ignore is honored (registration skipped),
  a host server's handler (e.g. uvicorn) is chained behind the drain,
  and the default disposition is re-raised after the drain so a
  standalone process terminates instead of swallowing the signal.

Gunicorn Workers must not register their own SIGTERM/SIGINT handlers
because Gunicorn Master (Arbiter) manages process lifecycle via signals
and forwards them to workers via the ``worker_int`` callback.
Overwriting Gunicorn's worker SIGTERM handler suppresses ``worker_int``
entirely, breaking gunicorn's own in-flight HTTP drain.

Instead, cleanup logic runs via Gunicorn hooks (``worker_int``,
``worker_exit``) defined in gunicorn.conf.py — see
``baldur.adapters.gunicorn.hooks``.

The second thing the process model decides is where background daemon threads
may be started. ``is_fork_source_process()`` answers it for both supported
pre-fork servers — the gunicorn master and a Celery worker main process on a
forking pool — so the starters carry one predicate rather than one per server.

The third is what a fork child inherits from the threads it does not have.
``fork()`` copies memory but keeps only the forking thread, so a lock another
parent thread held at that instant arrives held with no thread left to release
it. Every ``threading.Lock`` / ``threading.RLock`` Baldur constructs comes from
``fork_safe_lock()`` / ``fork_safe_rlock()``, which register it for an
``os.register_at_fork`` child step that re-initializes it; the same hook waits
out a log record another thread is writing through a ``logging.StreamHandler``,
whose stream keeps a lock of its own that CPython does not repair.
"""

from __future__ import annotations

import functools
import logging
import os
import sys
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any, TypeVar, overload

__all__ = [
    "fork_repaired",
    "fork_safe_lock",
    "fork_safe_rlock",
    "is_celery_worker_main",
    "is_celery_worker_process",
    "is_celery_worker_serving",
    "is_fork_source_process",
    "is_gunicorn_master",
    "is_gunicorn_worker",
    "is_under_gunicorn",
    "mark_celery_worker_main",
    "mark_celery_worker_serving",
    "pid_alive",
    "register_fork_safe_lock",
]

_F = TypeVar("_F", bound=Callable[..., Any])

# How long a fork waits, in total, for log records other threads are writing
# through stream handlers. A write normally finishes in microseconds; the bound
# only matters when a stream is blocked (a full output pipe), and after it the
# fork proceeds exactly as it would without the wait.
_FORK_LOG_HANDLER_WAIT_SECONDS = 1.0

# The C lock types the fork repair can re-initialize. Built from the
# constructors themselves so a lock handed in from outside (a redis-py pool's)
# is accepted by type, and a test double that answers every attribute is not.
_REPAIRABLE_LOCK_TYPES = (type(threading.Lock()), type(threading.RLock()))

# Every lock registered for the child repair. Weak, so registration never keeps
# a lock alive, and taken without a lock of its own: a registration lock would
# itself be a lock a fork can inherit held. The add is one C-level set insert,
# and the fork happens while the forking thread holds the GIL.
_fork_safe_locks: weakref.WeakSet[Any] = weakref.WeakSet()

# Per forking thread, one frame per fork in progress (see _ForkLogHold).
_fork_log_holds = threading.local()

# Env-var marker for "this process serves work", mirroring the
# ``GUNICORN_WORKER=1`` precedent. An env var rather than a module global
# because the two carriers differ where it matters: billiard's spawn path
# re-imports the app module in the child (a module global would come back
# False there) while the child's ``os.environ`` is its own copy, so a prefork
# parent that never sets it hands every child an unset marker.
_CELERY_WORKER_SERVING_ENV_VAR = "BALDUR_CELERY_WORKER_SERVING"

# argv[0] shapes that identify the celery launcher. The console script on
# Windows keeps the ``.exe`` suffix; ``python -m celery`` sets argv[0] to the
# package's own ``__main__.py`` instead of any program name.
_CELERY_PROGRAM_NAMES = frozenset({"celery", "celery.exe"})
_CELERY_MAIN_MODULE_SUFFIX = "/celery/__main__.py"

# Celery's global options that consume the token after them, so the
# subcommand scan does not mistake an option's value for the subcommand
# (``celery -A worker worker`` names an app, then the subcommand). Flags that
# take no value (``-C``/``--no-color``, ``-q``/``--quiet``, ``--version``,
# ``--skip-checks``) need no entry — they are skipped as options either way.
_CELERY_GLOBAL_VALUE_OPTIONS = frozenset(
    {
        "-A",
        "--app",
        "-b",
        "--broker",
        "--result-backend",
        "--loader",
        "--config",
        "--workdir",
    }
)

_CELERY_WORKER_SUBCOMMAND = "worker"

# Set by the celery ``worker_init`` receiver. Signal-based truth about the
# worker main process, so the argv heuristic below is only ever the fallback
# for an init() that runs before any celery signal (the Django-fixup path).
_celery_worker_main: bool = False


def pid_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` currently exists.

    Delegates to ``psutil.pid_exists()`` rather than probing with
    ``os.kill(pid, 0)``: on Windows CPython's ``os.kill`` calls
    ``TerminateProcess`` for every signal value other than the two console
    control events, so the "harmless" null-signal probe **kills the process it
    is asking about**. ``psutil`` resolves the same question per platform
    without that side effect (POSIX ``os.kill`` with ``ESRCH``/``EPERM``
    discrimination, a C-extension query on Windows), and it is already a core
    dependency, so nothing is added to the install footprint. The import lives
    in the function body to keep this module import-light.

    Two rules of the wrapper's own:

    - ``pid <= 0`` is rejected **before** probing. ``0`` is the caller's own
      process group on POSIX and the Idle process on Windows (where the
      delegate answers True), and ``-1`` means every process; neither is a
      filename-derived owner.
    - An unexpected probe failure reports **live**. Callers use this to decide
      whether a file's owner may still be writing it, so the safe direction is
      to defer, never to act as if the owner were gone.
    """
    if pid <= 0:
        return False

    try:
        import psutil

        return bool(psutil.pid_exists(pid))
    except Exception:
        # Undecidable — report live so callers defer rather than reclaim.
        return True


@overload
def fork_repaired(method: _F) -> _F: ...


@overload
def fork_repaired(*, repair: Callable[[], None]) -> Callable[[_F], _F]: ...


def fork_repaired(
    method: _F | None = None,
    *,
    repair: Callable[[], None] | None = None,
) -> Any:
    """Run the owner's ``_repair_if_forked()`` before the wrapped entry point.

    Components whose state does not survive ``fork()`` (locks with a recorded
    owner that no longer exists, ``Event`` objects, OS handles, process-local
    latches) repair that state lazily, at the head of every public entry point
    that touches it — a fork child otherwise deadlocks on the *first*
    acquisition, which is not necessarily the start path.

    Applied as a decorator rather than a hand-written first line so the
    coverage is machine-checkable: the wrapper carries an explicit
    ``__fork_repaired__`` marker, and the repaired classes' introspection
    gates assert that every public callable reaching repaired state carries it
    or is a written-down exemption.

    Two forms, one marker:

    - ``@fork_repaired`` (owner form) — for instance and class methods. The
      repair is looked up on the first positional argument, which is ``self``
      or ``cls`` respectively. Decorator order is pinned: ``@classmethod``
      outermost, this decorator directly beneath it. The reverse hands this
      function a ``classmethod`` object, which is not callable on the
      supported interpreter range.
    - ``@fork_repaired(repair=...)`` (module form) — for a module-level entry
      point whose inherited state is module-scoped (a module singleton and the
      locks guarding it) and so has no owner to look the repair up on. The
      repair callable is named explicitly, which keeps the indirection
      readable at the call site instead of resolving it through the wrapped
      function's module at call time.
    """

    def _decorate(target: _F) -> _F:
        if repair is None:

            @functools.wraps(target)
            def wrapper(owner: Any, *args: Any, **kwargs: Any) -> Any:
                owner._repair_if_forked()
                return target(owner, *args, **kwargs)

        else:

            @functools.wraps(target)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                repair()
                return target(*args, **kwargs)

        wrapper.__fork_repaired__ = True  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    if method is not None:
        return _decorate(method)
    return _decorate


def fork_safe_lock() -> threading.Lock:
    """Return a new ``threading.Lock`` that a fork child receives unlocked.

    The object returned is the plain C lock — no wrapper, so acquiring it costs
    exactly what acquiring any lock costs. It is registered for a step that runs
    in the child right after ``fork()``: a thread that held the lock in the
    parent does not exist in the child, so the child gets the lock back free
    instead of blocking forever on its first acquisition. Construct every
    Baldur lock through this factory; a fitness gate fails on a bare
    ``threading.Lock()``.

    The repair takes the data the lock guards as the parent's thread left it,
    so a section that builds something under the lock should publish the result
    as its last step — an unfinished build then leaves an empty slot the child
    fills itself.

    Constraint on callers: a plain lock records no owner, so the repair cannot
    tell the forking thread's own hold from a dead thread's. Code that holds a
    lock from this factory, calls ``os.fork()`` and then continues in the child
    to its ``release()`` gets ``RuntimeError`` there — the child's copy was
    already freed. Use ``fork_safe_rlock()`` for a lock held across a fork; an
    RLock the forking thread owns is left as it is.

    Only POSIX has ``fork()``; elsewhere the lock is returned unregistered.
    """
    lock = threading.Lock()
    register_fork_safe_lock(lock)
    return lock


def fork_safe_rlock() -> threading.RLock:
    """Return a new ``threading.RLock`` that a fork child receives unlocked.

    Same contract as ``fork_safe_lock()``, except that an RLock owned by the
    thread that calls ``fork()`` is not touched — that thread survives into the
    child and releases it itself.
    """
    lock = threading.RLock()
    register_fork_safe_lock(lock)
    return lock


def register_fork_safe_lock(lock: object) -> None:
    """Register a lock this code did not construct for the fork repair.

    For locks a library creates on Baldur's behalf and does not repair after a
    fork — the connection-pool locks of a redis-py client Baldur built are the
    case. Anything that is not a C ``Lock`` / ``RLock`` with an at-fork
    re-initializer (a renamed attribute read as ``None``, a test double, any
    lock on a platform without ``fork()``) is ignored, which leaves that object
    exactly as it would be without this call.
    """
    if not isinstance(lock, _REPAIRABLE_LOCK_TYPES):
        return
    if not hasattr(lock, "_at_fork_reinit"):
        return
    try:
        _fork_safe_locks.add(lock)
    except TypeError:
        # Not weak-referenceable on this interpreter; stays unrepaired.
        return


def _reinit_fork_safe_locks() -> None:
    """Free every registered lock in a fork child.

    Runs in the child before any other code, with the forking thread as the
    only thread. An RLock that thread owns is skipped — it will release it.
    Each lock is repaired on its own: CPython reports a raising at-fork
    callback and moves on, which would leave every lock after it held.
    """
    try:
        locks = list(_fork_safe_locks)
    except Exception:
        return
    for lock in locks:
        try:
            is_owned = getattr(lock, "_is_owned", None)
            if is_owned is not None and is_owned():
                continue
            lock._at_fork_reinit()
        except Exception:
            # The lock stays as inherited — what it was without the repair.
            continue


class _ForkLogHold:
    """The locks one fork's before-step took, for its matching after-step."""

    __slots__ = ("handler_locks", "module_lock")

    def __init__(self) -> None:
        self.module_lock: Any = None
        self.handler_locks: list[Any] = []


def _push_fork_log_hold() -> _ForkLogHold:
    stack = getattr(_fork_log_holds, "stack", None)
    if stack is None:
        stack = []
        _fork_log_holds.stack = stack
    hold = _ForkLogHold()
    stack.append(hold)
    return hold


def _pop_fork_log_hold() -> _ForkLogHold | None:
    stack = getattr(_fork_log_holds, "stack", None)
    if not stack:
        return None
    hold: _ForkLogHold = stack.pop()
    return hold


def _hold_stream_handler_locks() -> None:
    """Before ``fork()``: let in-progress stream-handler writes finish first.

    A buffered stream (``sys.stdout``, a log file) has an internal lock that
    CPython does not re-initialize in a fork child. A parent thread inside
    ``StreamHandler.emit`` at the fork instant holds it, and the child then
    blocks forever on its first log line. Holding each stream handler's own
    lock across the fork means no parent thread is inside such a write when the
    child is created.

    Logging's module lock is taken first, with no deadline — the order
    ``logging.config`` itself takes them in (module lock, then each handler),
    and the same wait logging's own before-fork step makes. Handler locks are
    then taken against one shared deadline; a handler whose write does not
    finish in time is skipped, which leaves it as it would be without this
    step. Only stream handlers are held: they are the ones that write, and the
    stdlib never chains one into another, while holding a handler that forwards
    to another can deadlock against a thread inside the forwarding.

    Each fork pushes its own frame on a per-thread stack, so two threads
    forking at once, or a fork started by a signal handler inside this step,
    never release each other's locks. Never raises.
    """
    try:
        hold = _push_fork_log_hold()
    except Exception:
        return
    try:
        module_lock = getattr(logging, "_lock", None)
        if module_lock is not None:
            module_lock.acquire()
            hold.module_lock = module_lock
    except Exception:
        pass
    try:
        handler_refs = list(getattr(logging, "_handlerList", ()))
    except Exception:
        return
    deadline = time.monotonic() + _FORK_LOG_HANDLER_WAIT_SECONDS
    for handler_ref in handler_refs:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            handler = handler_ref()
            if not isinstance(handler, logging.StreamHandler):
                continue
            lock = handler.lock
            if lock is not None and lock.acquire(timeout=remaining):
                hold.handler_locks.append(lock)
        except Exception:
            continue


def _release_stream_handler_locks() -> None:
    """After ``fork()`` in the parent — CPython also runs it when fork fails."""
    try:
        hold = _pop_fork_log_hold()
    except Exception:
        return
    if hold is None:
        return
    for lock in reversed(hold.handler_locks):
        try:
            lock.release()
        except Exception:
            continue
    if hold.module_lock is not None:
        try:
            hold.module_lock.release()
        except Exception:
            pass


def _repair_after_fork_in_child() -> None:
    """After ``fork()`` in the child: free every lock the child inherited held.

    Logging's own child step runs before this one and has re-initialized its
    module lock and the handler locks it tracks, so the module-lock hold is not
    released here — releasing a re-initialized RLock raises. The held handler
    locks are re-initialized once more to cover a handler whose lock was not
    created through ``Handler.createLock``.
    """
    _reinit_fork_safe_locks()
    try:
        hold = _pop_fork_log_hold()
    except Exception:
        return
    if hold is None:
        return
    for lock in hold.handler_locks:
        try:
            lock._at_fork_reinit()
        except Exception:
            continue


def _install_fork_hook() -> None:
    """Register the fork steps, once, at import; a no-op without ``fork()``.

    ``logging`` is imported above, so its own at-fork hook is registered
    before this one. Child and parent steps run in registration order and
    before-steps in reverse: logging's child step re-initializes its locks
    before this module's runs, and this module's before-step runs ahead of
    logging's and takes the module lock first.
    """
    register_at_fork = getattr(os, "register_at_fork", None)
    if register_at_fork is None:
        return
    register_at_fork(
        before=_hold_stream_handler_locks,
        after_in_parent=_release_stream_handler_locks,
        after_in_child=_repair_after_fork_in_child,
    )


_install_fork_hook()


def is_gunicorn_worker() -> bool:
    """Return True if the current process is a Gunicorn Worker.

    Detection relies on the GUNICORN_WORKER environment variable,
    which is set by the ``post_worker_init`` hook in
    ``baldur.adapters.gunicorn.hooks``. Because the env var is set
    AFTER the worker imports the WSGI app and calls ``baldur.init()``,
    callers that gate signal-handler installation against this helper
    have a race window: in worker pre-post_worker_init, the helper
    returns False and the caller installs a handler that briefly
    clobbers gunicorn's own SIGTERM. Use ``is_under_gunicorn()``
    instead for signal-handler guards.
    """
    return os.environ.get("GUNICORN_WORKER") == "1"


def is_under_gunicorn() -> bool:
    """Return True if the current process is running under gunicorn
    (either master/arbiter or worker), even before the
    ``post_worker_init`` hook has had a chance to set
    ``GUNICORN_WORKER=1``.

    Gunicorn sets ``SERVER_SOFTWARE`` in the master process and the
    worker inherits it via ``fork()``. This is a phase-independent
    detector — it returns True throughout the entire gunicorn
    lifecycle, whereas ``is_gunicorn_worker()`` only returns True
    after ``post_worker_init`` has run.

    Use this when deciding whether to install OS signal handlers from
    framework startup code (``baldur.init()``) — overwriting gunicorn's
    handlers, even briefly, would suppress ``worker_int`` and break
    graceful drain.
    """
    return "gunicorn" in os.environ.get("SERVER_SOFTWARE", "")


def is_gunicorn_master() -> bool:
    """Return True if the current process is the Gunicorn Master/Arbiter
    (i.e., running under gunicorn AND not yet identified as a worker).

    Caveat — same env-var-late race as ``is_gunicorn_worker()``: in a
    worker process, this helper returns True between fork() and the
    moment ``post_worker_init`` sets ``GUNICORN_WORKER=1``. Callers
    using this for "skip in master" gating should be tolerant of being
    invoked in worker pre-post_worker_init context.
    """
    return is_under_gunicorn() and not is_gunicorn_worker()


def mark_celery_worker_main() -> None:
    """Record that this process is a Celery worker's main process.

    Set by the ``worker_init`` receiver, which Celery sends from the
    ``WorkController`` constructor in every worker main process — CLI and
    programmatic alike — before any pool exists. Signal-based, so it holds for
    launcher shapes the argv heuristic cannot recognize.
    """
    global _celery_worker_main
    _celery_worker_main = True


def is_celery_worker_main() -> bool:
    """Return True if a Celery ``worker_init`` signal was observed here.

    Also True in a fork child, which inherits the flag from the pool parent —
    which is why callers compose it with the serving marker rather than
    reading it alone.
    """
    return _celery_worker_main


def mark_celery_worker_serving() -> None:
    """Record that this process runs tasks itself, rather than forking workers.

    Set by the ``worker_process_init`` receiver (every prefork child, and the
    solo pool's own main process) and by the ``worker_init`` receiver on a
    non-forking pool. The prefork parent never sets it, so its children inherit
    the marker unset and each one marks itself.
    """
    os.environ[_CELERY_WORKER_SERVING_ENV_VAR] = "1"


def is_celery_worker_serving() -> bool:
    """Return True if this process was marked as serving Celery tasks."""
    return os.environ.get(_CELERY_WORKER_SERVING_ENV_VAR) == "1"


def _celery_subcommand(args: list[str]) -> str | None:
    """Return the celery subcommand in ``args``, or None if there is none.

    Scans past the global options rather than reading ``args[0]``: the
    subcommand follows them (``celery -A proj worker``), and it is the option
    *values* that would otherwise be mistaken for it.
    """
    skip_next = False
    for token in args:
        if skip_next:
            skip_next = False
            continue
        if token.startswith("-"):
            if "=" not in token and token in _CELERY_GLOBAL_VALUE_OPTIONS:
                skip_next = True
            continue
        return token
    return None


def is_celery_worker_process() -> bool:
    """Return True if this process was launched as a Celery worker.

    An argv heuristic, used only where signal-based truth is not yet
    available: an ``init()`` that runs before any Celery signal — the
    Django-fixup path, where the fixup calls ``django.setup()`` at app-module
    import and the Django adapter's ``ready()`` initializes Baldur there.
    Once ``worker_init`` fires, ``is_celery_worker_main()`` is authoritative.

    Three conditions, all required: the launcher is celery (the console
    script, or the ``python -m celery`` form), the subcommand is ``worker``,
    and celery is actually imported here — which keeps a non-celery process
    that merely happens to carry those arguments out of the answer.

    Fails toward False. A launcher shape this does not recognize gets the
    behavior of a process that never mentioned celery, which is the status
    quo; a false positive would defer background workers in a process that
    never forks, so the deferral carries its own watchdog.
    """
    if "celery" not in sys.modules:
        return False

    argv = sys.argv
    if not argv:
        return False

    program = os.path.basename(argv[0])
    if program not in _CELERY_PROGRAM_NAMES:
        normalized = argv[0].replace("\\", "/")
        if not normalized.endswith(_CELERY_MAIN_MODULE_SUFFIX):
            return False

    return _celery_subcommand(list(argv[1:])) == _CELERY_WORKER_SUBCOMMAND


def is_fork_source_process() -> bool:
    """Return True if this process forks the workers that will serve.

    The single skip predicate the background-daemon starters consult. Threads
    do not survive ``fork()``, so a process that exists to fork must not start
    them: it would build state that is dead in every child while the child,
    which never re-runs ``init()``, has none of its own. The per-worker hooks
    (gunicorn ``post_worker_init``, the Celery ``worker_process_init``
    receiver) mark the serving process and re-run
    ``start_background_workers()`` there.

    Two fork sources are recognized:

    - the gunicorn master/arbiter;
    - a Celery worker main process on a forking pool, known by the
      ``worker_init`` signal or — before any signal has fired — by argv, and
      in both cases only while this process has not marked itself as serving.

    Everything else answers False, so outside celery this reduces exactly to
    ``is_gunicorn_master()``.
    """
    if is_gunicorn_master():
        return True
    if is_celery_worker_serving():
        return False
    return is_celery_worker_main() or is_celery_worker_process()
