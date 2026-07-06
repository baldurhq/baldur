"""
PostgreSQL Connection Settings - Pydantic v2.

Environment Variables:
    BALDUR_POSTGRES_HOST=localhost
    BALDUR_POSTGRES_PORT=5432
    BALDUR_POSTGRES_DATABASE=baldur
    BALDUR_POSTGRES_USER=baldur
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

__all__ = ["PostgresSettings", "get_postgres_settings", "reset_postgres_settings"]


class PostgresSettings(BaseSettings):
    """
    PostgreSQL connection settings.

    Provides host, port, database, and user configuration
    for PostgreSQL connections used by the baldur framework.
    """

    model_config = make_settings_config("BALDUR_POSTGRES_")

    # ==========================================================================
    # Connection Settings
    # ==========================================================================
    host: str = Field(
        default="localhost",
        description="PostgreSQL server hostname",
    )
    port: int = Field(
        default=5432,
        ge=1,
        le=65535,
        description="PostgreSQL server port",
    )
    database: str = Field(
        default="baldur",
        description="PostgreSQL database name",
    )
    user: str = Field(
        default="baldur",
        description="PostgreSQL connection username",
    )


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_postgres_settings() -> PostgresSettings:
    """Get singleton PostgresSettings instance."""
    from baldur.runtime import get_runtime

    return get_runtime().get_settings(PostgresSettings)


def reset_postgres_settings() -> None:
    """Reset PostgresSettings singleton (for testing)."""
    from baldur.runtime import get_runtime

    get_runtime().reset_settings(PostgresSettings)
