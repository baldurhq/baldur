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
        This interface stores cooldown state; it does not decide how long a
        caller may block on it. Prefer ``RateLimitCoordinator``, which owns the
        bounded serve-or-defer semantics:

            coordinator = get_rate_limit_coordinator()

            # Before making request — sleeps at most ``max_wait`` seconds, and
            # returns ``deferred=True`` having slept nothing when the remaining
            # cooldown does not fit within that bound.
            result = coordinator.wait_if_needed("payment_api", max_wait=5.0)
            if result.deferred:
                reschedule_at(result.not_before)
                return

            # On 429 response — computes and stores the cooldown.
            coordinator.on_rate_limited("payment_api", retry_after=retry_after)

        Direct storage access is for implementing a backend or inspecting
        state; never sleep ``remaining_cooldown`` unbounded:

            state = storage.get_state("payment_api")
            if state.is_in_cooldown:
                ...

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

    def extend_cooldown(
        self,
        key: str,
        cooldown_until: float,
        ttl: int | None = None,
    ) -> float:
        """Move the cooldown end time later, never earlier, and report the result.

        A shared cooldown is written by every worker that observes a 429, and the
        writes are not ordered: a worker whose provider sent no ``Retry-After``
        computes a short ladder delay, and under a last-writer-wins store that
        short write can replace an honored long one, resuming the whole fleet
        before the provider's stated earliest time. Merging by ``max`` makes the
        write commutative and idempotent, so the order stops mattering.

        Args:
            key: Unique identifier for the rate-limited resource
            cooldown_until: Candidate Unix timestamp for the end of the cooldown
            ttl: Time-to-live in seconds (for cleanup)

        Returns:
            The **effective** cooldown end time in force after this write —
            ``max(stored, cooldown_until)``. Callers must use this rather than
            their own candidate; the two differ whenever a peer's longer
            cooldown wins.

        Note:
            This default is best-effort, and the read-modify-write below takes no
            lock at all: any writer landing between its read and its write loses
            the longer of the two cooldowns — another thread of this process just
            as much as another process. Every adapter shipped with Baldur
            overrides it (memory and database under their own lock, Redis
            server-side), so a bring-your-own implementation is the only one that
            inherits this window, and it closes it by overriding too.
        """
        stored = self.get_state(key).cooldown_until
        effective = max(stored, cooldown_until)
        self.set_cooldown(key, effective, ttl)
        return effective

    def get_state_strict(self, key: str) -> RateLimitState:
        """Read the state like :meth:`get_state`, but raise on backend failure.

        :meth:`get_state` folds a backend failure into a clean default state —
        no cooldown — which is the right bias for a caller deciding whether to
        wait, and the wrong one for a caller deciding whether a cooldown has
        *ended*: an unreachable backend would read as "ended" and release the
        fleet. This variant lets such a caller tell "no cooldown" apart from
        "cannot tell".

        Args:
            key: Unique identifier for the rate-limited resource

        Returns:
            RateLimitState with current cooldown info

        Raises:
            RateLimitStorageError: The backend could not be read.

        Note:
            This default delegates to :meth:`get_state`, so a bring-your-own
            implementation keeps the folding behavior until it overrides this
            with a read that lets its backend errors propagate.
        """
        return self.get_state(key)

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
