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
"""

from __future__ import annotations

import functools
import os
import sys
from collections.abc import Callable
from typing import Any, TypeVar, overload

__all__ = [
    "fork_repaired",
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
]

_F = TypeVar("_F", bound=Callable[..., Any])

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
