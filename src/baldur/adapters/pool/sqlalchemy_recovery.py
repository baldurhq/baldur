"""
SQLAlchemy Pool Recovery Handler.

Best-effort implementation — SQLAlchemy QueuePool has no runtime expand/shrink API.
Overflow mechanism provides natural temporary expansion.
"""

from __future__ import annotations

import structlog

from baldur.core.pool_watchdog import PoolRecoveryHandler

logger = structlog.get_logger()


class SQLAlchemyPoolRecoveryHandler(PoolRecoveryHandler):
    """
    PoolRecoveryHandler implementation for SQLAlchemy connection pools.

    Best-effort: QueuePool has no runtime resize API.
    - close_connection: attempts connection invalidation
    - expand_pool / shrink_pool: log + return False (not supported at runtime)

    QueuePool's overflow mechanism provides natural temporary expansion
    up to max_overflow connections.
    """

    def close_connection(self, connection_id: str) -> bool:
        """Force close a connection. Best-effort via pool dispose."""
        logger.info(
            "pool_recovery.close_connection_attempted",
            connection_id=connection_id,
        )
        # SQLAlchemy doesn't expose per-connection invalidation by ID
        return False

    def expand_pool(self, additional_connections: int) -> bool:
        """
        Temporarily expand pool size.

        Not supported by SQLAlchemy QueuePool at runtime.
        QueuePool's overflow mechanism handles temporary expansion.
        """
        logger.info(
            "pool_recovery.expand_not_supported",
            additional_connections=additional_connections,
            reason="SQLAlchemy QueuePool has no runtime expand API; "
            "overflow mechanism provides natural temporary expansion",
        )
        return False

    def shrink_pool(self, target_size: int) -> bool:
        """
        Shrink pool back to normal size.

        Not supported by SQLAlchemy QueuePool at runtime.
        """
        logger.info(
            "pool_recovery.shrink_not_supported",
            target_size=target_size,
            reason="SQLAlchemy QueuePool has no runtime shrink API",
        )
        return False


__all__ = ["SQLAlchemyPoolRecoveryHandler"]
