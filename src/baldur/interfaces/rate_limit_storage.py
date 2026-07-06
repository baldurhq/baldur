"""
Rate Limit Storage Interface for Baldur System

Abstract interface for distributed rate limit state management.
Enables 100% Self-DDoS prevention across multi-server environments.

Design Principles:
1. Pure Python - no framework dependencies
2. ABC for provider contracts
3. Thread-safe operations
4. Fallback chain: Redis -> Database -> InMemory

Key Insight:
    "Every application has a database" - DB as guaranteed fallback
    ensures 100% coverage regardless of customer infrastructure.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from baldur.core.exceptions import AdapterError


class RateLimitStorageType(str, Enum):
    """Type of rate limit storage backend."""

    REDIS = "redis"
    MEMCACHED = "memcached"
    DATABASE = "database"
    MEMORY = "memory"


@dataclass
class RateLimitState:
    """
    Rate limit state for a specific endpoint/service.

    Attributes:
        key: Unique identifier (e.g., "payment_api", "external_service")
        cooldown_until: Unix timestamp when cooldown ends (0 = no cooldown)
        consecutive_429s: Number of consecutive 429 responses
        last_updated: Unix timestamp of last state update
    """

    key: str
    cooldown_until: float = 0.0
    consecutive_429s: int = 0
    last_updated: float = 0.0

    @property
    def is_in_cooldown(self) -> bool:
        """Check if currently in cooldown period."""
        import time

        return time.time() < self.cooldown_until

    @property
    def remaining_cooldown(self) -> float:
        """Get remaining cooldown time in seconds."""
        import time

        return max(0.0, self.cooldown_until - time.time())


class RateLimitStorageInterface(ABC):
    """
    Abstract interface for distributed rate limit state storage.

    Implementations must be thread-safe and support atomic operations.

    Usage:
        storage = get_rate_limit_storage()

        # On 429 response
        storage.set_cooldown("payment_api", cooldown_until=time.time() + 60)
        storage.increment_consecutive_429s("payment_api")

        # Before making request
        state = storage.get_state("payment_api")
        if state.is_in_cooldown:
            time.sleep(state.remaining_cooldown)

    Implementations:
        - RedisRateLimitStorage (fastest, requires Redis)
        - DatabaseRateLimitStorage (100% compatible, slightly slower)
        - InMemoryRateLimitStorage (single process only, for testing)
    """

    @property
    @abstractmethod
    def storage_type(self) -> RateLimitStorageType:
        """Return the type of storage backend."""
        pass

    @abstractmethod
    def get_state(self, key: str) -> RateLimitState:
        """
        Get the current rate limit state for a key.

        Args:
            key: Unique identifier for the rate-limited resource

        Returns:
            RateLimitState with current cooldown info

        Note:
            Returns a default state (no cooldown) if key doesn't exist.
        """
        pass

    @abstractmethod
    def set_cooldown(
        self,
        key: str,
        cooldown_until: float,
        ttl: int | None = None,
    ) -> None:
        """
        Set the cooldown end time for a key.

        Args:
            key: Unique identifier for the rate-limited resource
            cooldown_until: Unix timestamp when cooldown should end
            ttl: Time-to-live in seconds (for cleanup)

        Note:
            This should be an atomic operation to prevent race conditions.
        """
        pass

    @abstractmethod
    def increment_consecutive_429s(self, key: str) -> int:
        """
        Atomically increment the consecutive 429 counter.

        Args:
            key: Unique identifier for the rate-limited resource

        Returns:
            New counter value after increment

        Note:
            Used for exponential backoff calculation.
        """
        pass

    @abstractmethod
    def reset_consecutive_429s(self, key: str) -> None:
        """
        Reset the consecutive 429 counter on successful request.

        Args:
            key: Unique identifier for the rate-limited resource
        """
        pass

    @abstractmethod
    def clear(self, key: str) -> None:
        """
        Clear all rate limit state for a key.

        Args:
            key: Unique identifier for the rate-limited resource
        """
        pass

    def is_available(self) -> bool:
        """
        Check if the storage backend is available.

        Returns:
            True if the storage is operational

        Note:
            Used for fallback detection. Default returns True.
        """
        return True


class RateLimitStorageError(AdapterError):
    """Base exception for rate limit storage errors."""

    pass


class RateLimitStorageUnavailableError(RateLimitStorageError):
    """Raised when storage backend is unavailable."""

    pass
