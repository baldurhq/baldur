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

The same hook also waits, up to a second, for module imports other threads
have in progress. Python guards each module under import with a per-module
lock the importing thread owns until the module body has run; a fork taken
inside that window hands the child a lock owned by a thread it does not have,
and the child's first import of that module blocks forever. CPython repairs
only its global import lock in the child, not these. An import still running
when the second is up is inherited as it would be without the wait, and the
parent logs ``process_utils.fork_import_wait_timeout`` naming the module.
"""

from __future__ import annotations

import _imp
import functools
import importlib
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

logger = logging.getLogger(__name__)

# How long a fork waits, in total, for log records other threads are writing
# through stream handlers. A write normally finishes in microseconds; the bound
# only matters when a stream is blocked (a full output pipe), and after it the
# fork proceeds exactly as it would without the wait. Spent once per fork, however
# many times the before-step retries its hold.
_FORK_LOG_HANDLER_WAIT_SECONDS = 1.0

# How long a fork waits, in total, for module imports other threads have in
# progress. Counts only time the before-step spends waiting while it holds
# nothing, never time spent taking logging's locks. Boot-time imports finish in
# tens of milliseconds; an import that outlives the bound (network in a module
# body) is inherited as it would be without the wait. A named constant rather
# than a setting: loading settings inside an at-fork callback is unsafe.
_FORK_IMPORT_WAIT_SECONDS = 1.0

# Sleep between reads of the import state while that wait runs. Sleeping
# releases the GIL, which is what lets the importing thread finish.
_FORK_IMPORT_POLL_SECONDS = 0.001

# How a module whose import lock carries no readable name is logged.
_UNNAMED_MODULE = "<unnamed>"

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

# Module import locks this process inherited held: the thread that owned each
# one at the fork that created this process does not exist here, so the lock is
# never released and this process's own forks must not wait for it. Identity,
# not thread ident — a thread this process starts can be handed a dead thread's
# ident. Written only by the child step, before any other code runs; an entry
# leaves when its lock object dies, which is also when the import system drops
# the lock from its registry.
_fork_inherited_import_locks: weakref.WeakSet[Any] = weakref.WeakSet()

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
    """What one fork's before-step holds and saw, for its matching after-step."""

    __slots__ = (
        "handler_deadline",
        "handler_locks",
        "handler_locks_taken",
        "import_wait_seconds",
        "imports_in_progress",
        "module_locks",
    )

    def __init__(self) -> None:
        # A list rather than a slot so that recording it is one append (see
        # _take_recorded); it holds logging's module lock or nothing.
        self.module_locks: list[Any] = []
        self.handler_locks: list[Any] = []
        # Every handler lock any pass of this fork's step took. A retry
        # releases what it holds but keeps this record, so a later pass holds
        # those handlers again instead of skipping them.
        self.handler_locks_taken: list[Any] = []
        # The end of the stream-handler budget: set by the first pass, shared
        # by every retry of the same fork.
        self.handler_deadline: float | None = None
        # Filled only when the step gave up on module imports other threads
        # still had in progress: their names and how long it waited.
        self.imports_in_progress: list[str] = []
        self.import_wait_seconds = 0.0

    @property
    def module_lock(self) -> Any:
        return self.module_locks[0] if self.module_locks else None


def _is_recorded(lock: Any, locks: list[Any]) -> bool:
    """Is ``lock`` itself in ``locks``? Identity, never equality."""
    return any(entry is lock for entry in locks)


def _owned_by_this_thread(lock: Any) -> bool:
    """Return True if ``lock`` reports the calling thread as its owner.

    Only an RLock can say; a plain lock records no owner and reads as False.
    """
    is_owned = getattr(lock, "_is_owned", None)
    return is_owned is not None and is_owned() is True


def _take_recorded(lock: Any, held: list[Any], timeout: float) -> None:
    """Acquire ``lock`` and append it to ``held``, never leaving it taken unrecorded.

    ``timeout`` is the lock's own: ``-1`` waits without a deadline, ``0``
    tries once without waiting.

    A signal handler can raise between a successful acquire and the append —
    gunicorn's master raises ``HaltServer`` from its SIGCHLD handler while it
    forks workers, a command-line program gets ``KeyboardInterrupt`` — and the
    after-step releases only what was recorded, so an unrecorded lock would
    stay held by this thread for the rest of the parent's life. On any
    exception the lock is given back if it reports this thread as its owner,
    and the exception propagates, as logging's own before-fork step does. The
    caller passes only locks this thread did not already hold, so ownership
    here means this acquire took it.
    """
    try:
        if lock.acquire(timeout=timeout):
            held.append(lock)
    except BaseException:
        if not _is_recorded(lock, held) and _owned_by_this_thread(lock):
            lock.release()
        raise


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


def _module_locks_held_elsewhere() -> list[Any]:
    """Return the module import locks other threads own right now.

    Reads the registry CPython's import system keeps of its per-module locks
    (``importlib._bootstrap._module_locks``, name to weak reference). A lock
    counts when a thread other than the caller owns it and it was not
    inherited held at this process's creation. A thread waiting for another
    thread's import owns nothing; the owner does, and the owner's import is
    what gets waited for. The importing thread takes the lock before the
    finder search starts, so an import still looking for its module counts.

    Every read is private and degrades toward "none": an absent attribute, a
    dead reference or any exception answers that no import is in progress,
    which is exactly how the fork behaves without the wait.
    """
    try:
        bootstrap = getattr(importlib, "_bootstrap", None)
        registry = getattr(bootstrap, "_module_locks", None)
        if registry is None:
            return []
        # One C call: list() over a dict view allocates nothing per item, so no
        # other thread runs inside it. A Python-level loop over the live view
        # would raise when a concurrent import adds an entry, and the except
        # below would read that as "no import in progress" exactly while
        # imports run.
        refs = list(registry.values())
        me = threading.get_ident()
        held: list[Any] = []
        for ref in refs:
            lock = ref()
            if lock is None:
                continue
            owner = getattr(lock, "owner", None)
            if owner is None or owner == me:
                continue
            # An int on 3.11, a list from 3.12 on; falsy when free on both.
            if not getattr(lock, "count", None):
                continue
            if lock in _fork_inherited_import_locks:
                continue
            held.append(lock)
        return held
    except Exception:
        return []


def _imports_in_progress() -> bool:
    """Return True if another thread is inside a module import right now."""
    return bool(_module_locks_held_elsewhere())


def _import_lock_held() -> bool:
    """Return True if a thread holds the interpreter's global import lock.

    A thread holds it while it creates a module's lock and around each finder
    call — before it owns any module lock. ``_imp.lock_held()`` cannot say
    which thread holds it; absent, the lock reads as free.
    """
    try:
        lock_held = getattr(_imp, "lock_held", None)
        return lock_held is not None and lock_held() is True
    except Exception:
        return False


def _import_system_idle() -> bool:
    """Return True if no other thread is importing and the import lock is free."""
    return not _imports_in_progress() and not _import_lock_held()


def _wait_for_import_system_idle(budget: float) -> float:
    """Wait, holding nothing, until the import system is idle or ``budget`` is up.

    Returns the seconds it waited. A Python signal handler can run inside the
    sleep; an ``Exception`` it raises (a soft time limit, an alarm timeout)
    ends the wait, which then reports all of ``budget`` as spent so the caller
    goes on to its hold. The exception is not re-raised: CPython drops what an
    at-fork callback raises, and raising here would skip the hold. A
    ``BaseException`` propagates.
    """
    started = time.monotonic()
    try:
        while not _import_system_idle():
            if time.monotonic() - started >= budget:
                break
            time.sleep(_FORK_IMPORT_POLL_SECONDS)
    except Exception:
        return max(budget, 0.0)
    return time.monotonic() - started


def _sleep_one_poll(budget: float) -> float:
    """Sleep one poll interval holding nothing; return the seconds to count.

    Counts at least the interval, so a loop of such sleeps always reaches its
    budget. An ``Exception`` raised inside the sleep spends all of ``budget``,
    as in the import wait.
    """
    started = time.monotonic()
    try:
        time.sleep(_FORK_IMPORT_POLL_SECONDS)
    except Exception:
        return max(budget, 0.0)
    return max(time.monotonic() - started, _FORK_IMPORT_POLL_SECONDS)


def _before_fork() -> None:
    """Before ``fork()``: wait out other threads' imports, then hold the log locks.

    ``fork()`` keeps only the forking thread. A fork taken while another thread
    is inside a module import hands the child that module's import lock owned
    by a thread it does not have, and the child's first import of the module
    blocks forever. So the step waits, holding nothing, until no other thread
    owns a module import lock and the global import lock is free; takes the
    stream-handler hold; and checks again. An import that began while the hold
    was being taken — or a module body blocked on logging's lock, which the
    hold owns — fails that check, and the step releases the hold and repeats.
    Waiting before the hold lets a module body that calls
    ``logging.getLogger()`` finish; checking after it catches an import that
    started in between.

    Once ``_FORK_IMPORT_WAIT_SECONDS`` of waiting has passed, the fork proceeds
    holding what the stream-handler hold took — an import still in progress is
    inherited as it would be without the step — and the modules other threads
    are still importing are recorded for the parent step to report. One frame
    per fork whatever the number of passes, popped by the after-step. Never
    raises an ``Exception``.
    """
    try:
        hold = _push_fork_log_hold()
    except Exception:
        return
    waited = 0.0
    while True:
        waited += _wait_for_import_system_idle(_FORK_IMPORT_WAIT_SECONDS - waited)
        complete = _hold_stream_handler_locks(hold)
        if complete and _import_system_idle():
            return
        if waited >= _FORK_IMPORT_WAIT_SECONDS:
            _record_imports_in_progress(hold, waited)
            return
        _release_held_locks(hold)
        if not complete:
            waited += _sleep_one_poll(_FORK_IMPORT_WAIT_SECONDS - waited)


def _hold_stream_handler_locks(hold: _ForkLogHold) -> bool:
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
    then taken against the frame's handler budget, which the first pass of a
    fork sets and every retry shares; once it is spent each handler is tried
    without waiting. A handler not obtained is skipped, which leaves it as it
    would be without this step — unless an earlier pass of the same fork took
    it: a thread may be inside its stream write right now, so the pass
    reports itself incomplete and the caller retries rather than fork without
    a handler a single pass would have held. Only stream handlers are held:
    they are the ones that write, and the stdlib never chains one into
    another, while holding a handler that forwards to another can deadlock
    against a thread inside the forwarding.

    Acts on ``hold``, the frame the caller pushed for this fork, so two threads
    forking at once, or a fork started by a signal handler inside this step,
    never release each other's locks. A lock the forking thread already holds
    is not taken again: no other thread can be inside that section. Returns
    False only for a handler an earlier pass took and this one did not. Never
    raises an ``Exception``; a ``BaseException`` a signal handler raises
    mid-step propagates once the lock it interrupted is given back.
    """
    try:
        module_lock = getattr(logging, "_lock", None)
        if module_lock is not None and not _owned_by_this_thread(module_lock):
            _take_recorded(module_lock, hold.module_locks, -1)
    except Exception:
        pass
    try:
        handler_refs = list(getattr(logging, "_handlerList", ()))
        if hold.handler_deadline is None:
            hold.handler_deadline = time.monotonic() + _FORK_LOG_HANDLER_WAIT_SECONDS
        deadline = hold.handler_deadline
    except Exception:
        return True
    complete = True
    for handler_ref in handler_refs:
        try:
            if not _hold_stream_handler(hold, handler_ref, deadline):
                complete = False
        except Exception:
            continue
    return complete


def _hold_stream_handler(hold: _ForkLogHold, handler_ref: Any, deadline: float) -> bool:
    """Take one stream handler's lock for ``hold``, waiting until ``deadline``.

    Returns False only when an earlier pass of the same fork took this
    handler and this pass could not; anything that is not a stream handler,
    has no lock, or whose lock the forking thread already holds is passed over.
    """
    handler = handler_ref()
    if not isinstance(handler, logging.StreamHandler):
        return True
    lock = handler.lock
    if lock is None or _owned_by_this_thread(lock):
        return True
    remaining = max(deadline - time.monotonic(), 0.0)
    _take_recorded(lock, hold.handler_locks, remaining)
    taken_before = _is_recorded(lock, hold.handler_locks_taken)
    if _is_recorded(lock, hold.handler_locks):
        if not taken_before:
            hold.handler_locks_taken.append(lock)
        return True
    return not taken_before


def _release_held_locks(hold: _ForkLogHold) -> None:
    """Release every lock ``hold`` holds, handlers first, and clear the record.

    An entry leaves the record only after its release returns, so an exception
    in between leaves a released lock on the record — whose second release an
    RLock refuses — rather than a held lock off it. The record of handlers
    taken is kept for the next pass.
    """
    while hold.handler_locks:
        try:
            hold.handler_locks[-1].release()
        except Exception:
            pass
        hold.handler_locks.pop()
    while hold.module_locks:
        try:
            hold.module_locks[-1].release()
        except Exception:
            pass
        hold.module_locks.pop()


def _record_imports_in_progress(hold: _ForkLogHold, waited: float) -> None:
    """Note on ``hold`` the modules other threads are still importing.

    Module imports only: a fork that gave up on the global import lock alone
    hands the child nothing held, since CPython re-initializes that lock there.
    """
    try:
        names: list[str] = []
        for lock in _module_locks_held_elsewhere():
            name = getattr(lock, "name", None)
            names.append(name if isinstance(name, str) else _UNNAMED_MODULE)
        hold.imports_in_progress = names
        hold.import_wait_seconds = waited
    except Exception:
        return


def _release_stream_handler_locks() -> None:
    """After ``fork()`` in the parent — CPython also runs it when fork fails.

    Releases what the before-step holds, then, only when that step gave up on
    module imports other threads still had in progress, logs one WARNING
    naming them: the child may hang on its first import of any of them. Logged
    here rather than before the fork, where the line would wait on the locks
    the step was about to take, and rather than in the child, whose log path
    could import one of those very modules.
    """
    try:
        hold = _pop_fork_log_hold()
    except Exception:
        return
    if hold is None:
        return
    _release_held_locks(hold)
    if hold.imports_in_progress:
        try:
            logger.warning(
                "process_utils.fork_import_wait_timeout",
                extra={
                    "modules": list(hold.imports_in_progress),
                    "waited_seconds": round(hold.import_wait_seconds, 3),
                },
            )
        except Exception:
            return


def _record_inherited_import_locks() -> None:
    """In a fork child: remember the module import locks it inherited held.

    Their owners did not survive the fork, so the locks are never released
    here, and without the record each fork this process makes would wait its
    whole import budget for them. Runs on every child step, whatever the
    before-step did.
    """
    try:
        for lock in _module_locks_held_elsewhere():
            _fork_inherited_import_locks.add(lock)
    except Exception:
        return


def _repair_after_fork_in_child() -> None:
    """After ``fork()`` in the child: free every lock the child inherited held.

    Logging's own child step runs before this one and has re-initialized its
    module lock and the handler locks it tracks, so the module-lock hold is not
    released here — releasing a re-initialized RLock raises. The held handler
    locks are re-initialized once more to cover a handler whose lock was not
    created through ``Handler.createLock``. Module import locks are not
    repaired — an import the before-step gave up on stays as inherited — only
    recorded, so this process's own forks do not wait for them.
    """
    _reinit_fork_safe_locks()
    _record_inherited_import_locks()
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
    logging's and takes the module lock first. The import wait shares this one
    registration with the stream-handler hold because it runs as one fixed
    sequence with it — wait, hold, re-check — which a second registration,
    ordered by import order, could not guarantee.
    """
    register_at_fork = getattr(os, "register_at_fork", None)
    if register_at_fork is None:
        return
    register_at_fork(
        before=_before_fork,
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
