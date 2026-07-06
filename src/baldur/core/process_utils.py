"""Process model detection utilities for fork-safety.

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

import os


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
