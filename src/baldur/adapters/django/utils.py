"""Django utility functions for framework-agnostic infrastructure.

Provides wrapper functions that safely interact with Django internals.
Used as hooks/callbacks by core infrastructure (e.g., TimeoutExecutor).
"""

from __future__ import annotations

__all__ = ["close_django_connections", "close_all_django_connections"]


def close_django_connections() -> None:
    """Django DB connection safety wrapper for ThreadPool execution.

    Closes stale database connections before/after executing work in a
    ThreadPool worker thread. This prevents "connection already closed"
    errors when Django's connection pool is shared across threads.

    Safe to call when Django is not installed — silently no-ops.
    """
    try:
        from django.db import close_old_connections

        close_old_connections()
    except ImportError:
        pass


def close_all_django_connections() -> None:
    """Release every thread-local Django DB connection unconditionally.

    Distinct from :func:`close_django_connections`, which wraps Django's
    ``close_old_connections()`` — a *conditional* close that fires only on an
    autocommit mismatch, after an error, or once ``CONN_MAX_AGE`` has elapsed.
    Under persistent connections (``CONN_MAX_AGE > 0``) that helper is a no-op,
    so it cannot be used to clean up a short-lived worker thread.

    This one means "this thread is finished, release everything". Use it at the
    end of a worker thread that opened connections outside the request cycle,
    where the ``request_finished`` signal never fires and the connection would
    otherwise be stranded in the dying thread.

    Safe to call when Django is not installed — silently no-ops.
    """
    try:
        from django.db import connections

        connections.close_all()
    except ImportError:
        pass
