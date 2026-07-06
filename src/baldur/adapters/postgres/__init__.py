"""
PostgreSQL Database Adapters.

PG-admin SQL primitives (``pg_stat_activity``, advisory locks, session
timeouts) routed through callable-injected session/connection factories
so Django, DB-API 2.0 (psycopg2), and noop runtimes all satisfy the same
:class:`PgAdminProvider` contract (515).
"""

from baldur.adapters.postgres.admin import PgAdmin
from baldur.adapters.postgres.noop_admin import NoopPgAdmin
from baldur.adapters.postgres.sessions import (
    dbapi_session_factory,
    django_connection_factory,
    django_session_factory,
)

__all__ = [
    "PgAdmin",
    "NoopPgAdmin",
    "django_session_factory",
    "django_connection_factory",
    "dbapi_session_factory",
]
