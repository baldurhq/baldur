"""
Session factories for :class:`PgAdmin`.

Three small helpers produce the two callables (``get_session`` returning a
context-managed cursor, ``get_connection`` returning a raw DB-API connection)
that :class:`baldur.adapters.postgres.admin.PgAdmin` injects. Each backend
ships its own pair:

- Django:  ``django_session_factory`` + ``django_connection_factory``
- DB-API:  ``dbapi_session_factory`` + the same ``get_connection`` callable
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from typing import Any

__all__ = [
    "dbapi_session_factory",
    "django_connection_factory",
    "django_session_factory",
]


def django_session_factory(
    alias: str = "default",
) -> Callable[[], AbstractContextManager[Any]]:
    """Return a ``get_session`` callable that yields a Django-managed cursor.

    Each call lazy-imports ``django.db.connections`` so the helper is
    cheap to construct in non-Django runtimes (the import happens only
    when the callable is invoked). The cursor's lifecycle is governed
    by Django's connection-per-thread proxy.
    """

    def _get_session() -> AbstractContextManager[Any]:
        from django.db import connections

        return connections[alias].cursor()

    return _get_session


def django_connection_factory(alias: str = "default") -> Callable[[], Any]:
    """Return a ``get_connection`` callable yielding the Django thread-local connection.

    Cursors opened from the returned object follow Django's lifecycle —
    used by :meth:`PgAdmin.create_cursor`, whose returned cursor is held
    externally by the caller (pool-exhaustion path).
    """

    def _get_connection() -> Any:
        from django.db import connections

        return connections[alias]

    return _get_connection


def dbapi_session_factory(
    get_connection: Callable[[], Any],
) -> Callable[[], AbstractContextManager[Any]]:
    """Return a ``get_session`` callable for DB-API 2.0 ``get_connection``.

    Opens ``get_connection()``, yields its cursor, closes the cursor and
    the connection on context exit. Pool-fronted callables (SQLAlchemy
    ``engine.raw_connection``, PgBouncer ``getconn``) return the connection
    to the pool on close; the dev ``build_connection_factory`` simply
    closes the raw connection.
    """

    @contextmanager
    def _get_session() -> Any:
        conn = get_connection()
        cursor = conn.cursor()
        try:
            yield cursor
        finally:
            try:
                cursor.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    return _get_session
