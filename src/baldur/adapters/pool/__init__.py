"""
Pool Monitor Adapters.

Concrete implementations of PoolStatsProvider and PoolRecoveryHandler ABCs,
plus the OSS dict-shape PoolInfoProvider implementations (515).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .django_info import DjangoPoolInfoProvider
    from .memory_recovery import InMemoryPoolRecoveryHandler
    from .memory_stats import InMemoryPoolStatsProvider
    from .noop_info import NoopPoolInfoProvider
    from .sqlalchemy_info import SQLAlchemyPoolInfoProvider
    from .sqlalchemy_recovery import SQLAlchemyPoolRecoveryHandler
    from .sqlalchemy_stats import SQLAlchemyPoolStatsProvider


def __getattr__(name: str):
    if name == "SQLAlchemyPoolStatsProvider":
        from .sqlalchemy_stats import SQLAlchemyPoolStatsProvider

        return SQLAlchemyPoolStatsProvider
    if name == "SQLAlchemyPoolRecoveryHandler":
        from .sqlalchemy_recovery import SQLAlchemyPoolRecoveryHandler

        return SQLAlchemyPoolRecoveryHandler
    if name == "InMemoryPoolStatsProvider":
        from .memory_stats import InMemoryPoolStatsProvider

        return InMemoryPoolStatsProvider
    if name == "InMemoryPoolRecoveryHandler":
        from .memory_recovery import InMemoryPoolRecoveryHandler

        return InMemoryPoolRecoveryHandler
    if name == "DjangoPoolInfoProvider":
        from .django_info import DjangoPoolInfoProvider

        return DjangoPoolInfoProvider
    if name == "SQLAlchemyPoolInfoProvider":
        from .sqlalchemy_info import SQLAlchemyPoolInfoProvider

        return SQLAlchemyPoolInfoProvider
    if name == "NoopPoolInfoProvider":
        from .noop_info import NoopPoolInfoProvider

        return NoopPoolInfoProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "SQLAlchemyPoolStatsProvider",
    "SQLAlchemyPoolRecoveryHandler",
    "InMemoryPoolStatsProvider",
    "InMemoryPoolRecoveryHandler",
    "DjangoPoolInfoProvider",
    "SQLAlchemyPoolInfoProvider",
    "NoopPoolInfoProvider",
]
