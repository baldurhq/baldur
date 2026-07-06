"""Django utility functions for framework-agnostic infrastructure.

Provides wrapper functions that safely interact with Django internals.
Used as hooks/callbacks by core infrastructure (e.g., TimeoutExecutor).
"""

from __future__ import annotations

__all__ = ["close_django_connections"]


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
