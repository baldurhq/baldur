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
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable
from typing import Any, TypeVar

__all__ = [
    "fork_repaired",
    "is_gunicorn_master",
    "is_gunicorn_worker",
    "is_under_gunicorn",
    "pid_alive",
]

_F = TypeVar("_F", bound=Callable[..., Any])


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


def fork_repaired(method: _F) -> _F:
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

    Decorator order is pinned — ``@classmethod`` outermost, this decorator
    directly beneath it. The reverse hands this function a ``classmethod``
    object, which is not callable on the supported interpreter range.

    Works for both instance and class methods: the repair is looked up on the
    first positional argument, which is ``self`` or ``cls`` respectively.
    """

    @functools.wraps(method)
    def wrapper(owner: Any, *args: Any, **kwargs: Any) -> Any:
        owner._repair_if_forked()
        return method(owner, *args, **kwargs)

    wrapper.__fork_repaired__ = True  # type: ignore[attr-defined]
    return wrapper  # type: ignore[return-value]


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
