"""
Event Journal Settings - Pydantic v2.

Single Source of Truth for Event Journal configuration.
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class EventJournalSettings(BaseSettings):
    """
    Event Journal settings.

    The storage backend is no longer a settings field — it is selected at
    ``init()`` by the ``event_journal_repo`` registry-default wiring row,
    which honors ``BALDUR_REDIS_URL`` and the ``BALDUR_EVENT_JOURNAL_BACKEND``
    operator override.

    Environment variables:
        BALDUR_EVENT_JOURNAL_ENABLED=true
        BALDUR_EVENT_JOURNAL_TTL_DAYS=30
        BALDUR_EVENT_JOURNAL_MAX_ENTRIES_MEMORY=10000
        ...
    """

    model_config = make_settings_config("BALDUR_EVENT_JOURNAL_")

    enabled: bool = Field(
        default=True,
        description="Enable EventJournal",
    )
    ttl_days: int = Field(
        default=30,
        ge=7,
        le=365,
        description="Redis storage TTL (days)",
    )
    max_entries_memory: int = Field(
        default=10000,
        ge=100,
        le=1000000,
        description="Maximum entries for InMemory adapter",
    )
    max_query_limit: int = Field(
        default=10000,
        ge=100,
        le=100000,
        description="Maximum return count limit for query()",
    )


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_event_journal_settings() -> "EventJournalSettings":
    """Get cached EventJournalSettings instance."""
    from baldur.settings.root import get_config

    return get_config().audit_group.event_journal


def reset_event_journal_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().audit_group.__dict__["event_journal"]
    except KeyError:
        pass
