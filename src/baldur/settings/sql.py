"""
SQL Backend Settings - Pydantic v2.

Canonical DSN-based configuration for the framework-free SQL adapter
(GenericSQLRepository). DB-API 2.0 compatible; dialect inferred from
the DSN scheme.

Precedence rules (implemented by ``resolve_dsn``):

1. ``BALDUR_SQL_DSN`` (canonical) — ``postgresql://`` / ``mysql://`` / ``sqlite:///``.
2. ``BALDUR_POSTGRES_*`` component fields (fallback for users already
   configured with the legacy prefix; postgres-only).

Environment variables:
    BALDUR_SQL_DSN=postgresql://user:pass@host/db
    BALDUR_SQL_SCHEMA_MANAGED=1            # default 1 — run CREATE TABLE IF NOT EXISTS
    BALDUR_SQL_AUTOCOMMIT=0                # default 0 — Baldur-side commit/rollback
    BALDUR_SQL_DIALECT=postgresql          # optional override; auto-inferred from DSN

See 429 Part 4 (C11/C15/C16).
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.postgres import get_postgres_settings

__all__ = [
    "SQLDialect",
    "SQLSettings",
    "get_sql_settings",
    "reset_sql_settings",
    "resolve_dsn",
    "infer_dialect",
]


class SQLDialect(str, Enum):
    """DB-API 2.0 dialects supported by GenericSQLRepository."""

    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    SQLITE = "sqlite"


_DIALECT_SCHEME_MAP: dict[str, SQLDialect] = {
    "postgresql": SQLDialect.POSTGRESQL,
    "postgres": SQLDialect.POSTGRESQL,
    "mysql": SQLDialect.MYSQL,
    "mariadb": SQLDialect.MYSQL,
    "sqlite": SQLDialect.SQLITE,
}


def infer_dialect(dsn: str) -> SQLDialect:
    """Return the SQLDialect implied by the DSN scheme.

    Falls back to POSTGRESQL for empty/unknown schemes — postgres is the
    primary supported backend.
    """
    if not dsn:
        return SQLDialect.POSTGRESQL
    scheme = dsn.split("://", 1)[0].lower()
    return _DIALECT_SCHEME_MAP.get(scheme, SQLDialect.POSTGRESQL)


class SQLSettings(BaseSettings):
    """Framework-free SQL backend settings.

    Shipped by Baldur under the ``BALDUR_SQL_`` prefix. ``dsn`` is the
    canonical input; when unset, ``resolve_dsn`` falls back to the legacy
    ``BALDUR_POSTGRES_*`` component fields.
    """

    model_config = make_settings_config("BALDUR_SQL_")

    dsn: str = Field(
        default="",
        description=(
            "DB-API 2.0 DSN — postgresql://user:pass@host/db, mysql://..., "
            "sqlite:///path/to/db. Empty falls back to BALDUR_POSTGRES_* fields."
        ),
    )
    dialect: str = Field(
        default="",
        description=(
            "Override dialect (postgresql/mysql/sqlite). Empty auto-infers "
            "from the DSN scheme."
        ),
    )
    schema_managed: bool = Field(
        default=True,
        description=(
            "When True, baldur.init() issues CREATE TABLE IF NOT EXISTS and "
            "baldur_schema_version bookkeeping. Set to False when DDL is "
            "owned by the host application (escape hatch)."
        ),
    )
    autocommit: bool = Field(
        default=False,
        description=(
            "When True, Baldur delegates commit/rollback to the user's "
            "connection — required when fronting with PgBouncer in "
            "transaction-pooling mode. Default (False) performs repo-scoped "
            "commit after each write."
        ),
    )

    @field_validator("dialect")
    @classmethod
    def _validate_dialect(cls, v: str) -> str:
        if not v:
            return ""
        normalized = v.lower()
        if normalized not in {d.value for d in SQLDialect}:
            raise ValueError(
                f"invalid SQL dialect {v!r}; expected one of "
                f"{[d.value for d in SQLDialect]}"
            )
        return normalized

    def resolved_dialect(self) -> SQLDialect:
        """Return the effective dialect (explicit override or DSN inferred)."""
        if self.dialect:
            return SQLDialect(self.dialect)
        return infer_dialect(resolve_dsn(self))


def resolve_dsn(settings: SQLSettings | None = None) -> str:
    """Return the effective DSN, applying the documented precedence chain.

    When ``settings.dsn`` is set it wins. Otherwise components are drawn
    from ``PostgresSettings`` (``BALDUR_POSTGRES_HOST/PORT/DATABASE/USER``).
    """
    settings = settings or get_sql_settings()
    if settings.dsn:
        return settings.dsn
    pg = get_postgres_settings()
    return f"postgresql://{pg.user}@{pg.host}:{pg.port}/{pg.database}"


def get_sql_settings() -> SQLSettings:
    """Get singleton SQLSettings instance."""
    from baldur.runtime import get_runtime

    return get_runtime().get_settings(SQLSettings)


def reset_sql_settings() -> None:
    """Reset SQLSettings singleton (for testing)."""
    from baldur.runtime import get_runtime

    get_runtime().reset_settings(SQLSettings)
