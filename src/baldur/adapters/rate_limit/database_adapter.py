"""
Database Rate Limit Storage Adapter

Rate limit storage backed by any database, for deployments that share cooldown
state through a database rather than Redis.

Wiring:
    This is a bring-your-own extension point, not an automatic fallback.
    Backend auto-detection never selects it, because it needs a repository
    factory that nothing registers by default. Reach it by asking for it
    explicitly — ``get_rate_limit_storage("database")`` — and supply that
    factory.

Features:
    - Works with any database (PostgreSQL, MySQL, SQLite)
    - Framework-agnostic (uses repository pattern)
    - Slower than Redis (~1-5ms vs ~0.1ms)

Performance Note:
    The cost is per state read, not per 429: a coordinated retry consults the
    cooldown before every attempt, so a call path pays this latency on each
    one — which is why Redis is the recommended backend under load.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import structlog

from baldur.interfaces.rate_limit_storage import (
    RateLimitState,
    RateLimitStorageInterface,
    RateLimitStorageType,
)

logger = structlog.get_logger()


class DatabaseRateLimitStorage(RateLimitStorageInterface):
    """
    Database-based rate limit storage.

    Uses a simple key-value table for storing rate limit state.
    Works with any SQL database through a repository abstraction.

    Table schema (auto-created by migrations):
        CREATE TABLE baldur_ratelimitstate (
            id SERIAL PRIMARY KEY,
            key VARCHAR(255) UNIQUE NOT NULL,
            cooldown_until DOUBLE PRECISION DEFAULT 0,
            consecutive_429s INTEGER DEFAULT 0,
            last_updated DOUBLE PRECISION DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE INDEX idx_ratelimit_key ON baldur_ratelimitstate(key);

    Example:
        storage = DatabaseRateLimitStorage()
        storage.set_cooldown("payment_api", time.time() + 60)
    """

    def __init__(
        self,
        repository_factory: Callable | None = None,
    ) -> None:
        """
        Initialize database rate limit storage.

        Args:
            repository_factory: Factory function creating the rate-limit
                state repository. Required in practice — this package ships
                no Django repository, so an instance built without one can
                never serve a request and reports itself unavailable.
        """
        self._repository_factory = repository_factory
        self._lock = threading.Lock()
        self._available: bool | None = None

    @property
    def storage_type(self) -> RateLimitStorageType:
        return RateLimitStorageType.DATABASE

    def _get_repository(self):
        """Get the rate limit state repository."""
        if self._repository_factory:
            return self._repository_factory()

        # Django repository is not available in the package
        # Users must provide repository_factory for database storage
        raise RuntimeError(
            "No repository available. DatabaseRateLimitStorage requires "
            "repository_factory to be provided during initialization."
        )

    def is_available(self) -> bool:
        """Check if database is available.

        A bare instance — no ``repository_factory`` — is unconfigured, not
        broken: it was constructed by auto-detection probing every backend,
        and it can never work by construction. Reporting that at DEBUG
        without the doomed round-trip is behavior-preserving; a factory that
        was supplied and then fails keeps the WARNING.
        """
        if self._available is not None:
            return self._available

        if self._repository_factory is None:
            logger.debug("database_rate_limit_storage.not_configured")
            self._available = False
            return False

        try:
            repo = self._get_repository()
            # Simple query to check connectivity
            repo.get_or_create("__healthcheck__")
            self._available = True
            return True
        except Exception as e:
            logger.warning(
                "database_rate_limit_storage.database_unavailable",
                error=e,
            )
            self._available = False
            return False

    def get_state(self, key: str) -> RateLimitState:
        """Get rate limit state from database."""
        try:
            repo = self._get_repository()
            data = repo.get(key)

            if data is None:
                return RateLimitState(key=key)

            return RateLimitState(
                key=key,
                cooldown_until=data.get("cooldown_until", 0.0),
                consecutive_429s=data.get("consecutive_429s", 0),
                last_updated=data.get("last_updated", 0.0),
            )

        except Exception as e:
            logger.exception(
                "database_rate_limit_storage.get_state_failed",
                error=e,
            )
            return RateLimitState(key=key)

    def set_cooldown(
        self,
        key: str,
        cooldown_until: float,
        ttl: int | None = None,
    ) -> None:
        """Set cooldown in database."""
        try:
            with self._lock:
                repo = self._get_repository()
                now = time.time()

                repo.upsert(
                    rate_limit_key=key,
                    data={
                        "cooldown_until": cooldown_until,
                        "last_updated": now,
                    },
                )

                logger.debug(
                    "database_rate_limit_storage.set_cooldown",
                    rate_limit_key=key,
                    cooldown_until=cooldown_until,
                )

        except Exception as e:
            logger.exception(
                "database_rate_limit_storage.set_cooldown_failed",
                error=e,
            )
            raise

    def extend_cooldown(
        self,
        key: str,
        cooldown_until: float,
        ttl: int | None = None,
    ) -> float:
        """Move the cooldown end time later, atomically within this process.

        The read and the write are inlined under one lock hold rather than
        composing the public ``get_state``/``set_cooldown`` pair: ``_lock`` is a
        plain non-reentrant Lock that ``set_cooldown`` acquires itself, so the
        natural composition would deadlock the calling thread on its first 429.

        Cross-process atomicity would need a ``GREATEST``-style conditional
        update, i.e. a change to the repository contract this adapter is
        deliberately agnostic about. Two processes racing can therefore still
        lose the longer of two concurrent writes here — use the Redis adapter
        where that matters.

        Returns:
            The effective cooldown end time in force after this write.
        """
        try:
            with self._lock:
                repo = self._get_repository()
                now = time.time()

                data = repo.get(key)
                stored = data.get("cooldown_until", 0.0) if data else 0.0
                effective = max(stored, cooldown_until)

                repo.upsert(
                    rate_limit_key=key,
                    data={
                        "cooldown_until": effective,
                        "last_updated": now,
                    },
                )

                logger.debug(
                    "database_rate_limit_storage.extend_cooldown",
                    rate_limit_key=key,
                    cooldown_until=effective,
                )
                return effective

        except Exception as e:
            logger.exception(
                "database_rate_limit_storage.extend_cooldown_failed",
                error=e,
            )
            raise

    def get_state_strict(self, key: str) -> RateLimitState:
        """Get rate limit state from the database, raising instead of folding on failure."""
        try:
            repo = self._get_repository()
            data = repo.get(key)

            if data is None:
                return RateLimitState(key=key)

            return RateLimitState(
                key=key,
                cooldown_until=data.get("cooldown_until", 0.0),
                consecutive_429s=data.get("consecutive_429s", 0),
                last_updated=data.get("last_updated", 0.0),
            )

        except Exception as e:
            logger.exception(
                "database_rate_limit_storage.get_state_strict_failed",
                error=e,
            )
            raise

    def increment_consecutive_429s(self, key: str) -> int:
        """Increment 429 counter in database."""
        try:
            with self._lock:
                repo = self._get_repository()
                new_value = repo.increment(key, "consecutive_429s")

                logger.debug(
                    "database_rate_limit_storage.incremented_counter",
                    rate_limit_key=key,
                    new_value=new_value,
                )
                return new_value

        except Exception as e:
            logger.exception(
                "database_rate_limit_storage.increment_failed",
                error=e,
            )
            raise

    def reset_consecutive_429s(self, key: str) -> None:
        """Reset 429 counter in database."""
        try:
            with self._lock:
                repo = self._get_repository()
                repo.update(key, {"consecutive_429s": 0})

                logger.debug(
                    "database_rate_limit_storage.reset_counter",
                    rate_limit_key=key,
                )

        except Exception as e:
            logger.exception(
                "database_rate_limit_storage.reset_failed",
                error=e,
            )

    def clear(self, key: str) -> None:
        """Clear all rate limit state for a key."""
        try:
            with self._lock:
                repo = self._get_repository()
                repo.delete(key)

                logger.debug(
                    "database_rate_limit_storage.cleared_state",
                    rate_limit_key=key,
                )

        except Exception as e:
            logger.exception(
                "database_rate_limit_storage.clear_failed",
                error=e,
            )
