"""
Air-Gap Storage Adapter Base Interface.

Provides an abstract interface for Air-Gap storage between
Baldur engine and business database.

Design Principles:
- Complete DB isolation: Engine never touches business DB
- Plug & Play: Enable/disable via configuration
- Graceful Fallback: Works without Redis (uses NullAdapter)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AirGapStorageAdapter(Protocol):
    """
    Air-Gap storage adapter interface.

    The business layer records summary state through this adapter whenever the
    DB changes, and the Baldur engine reads state only through this adapter.

    Architecture:
        ┌──────────────┐
        │ Business DB  │  ← Baldur engine access forbidden
        └──────────────┘
               │
               │ (business layer writes the summary)
               ▼
        ┌──────────────┐
        │  Air-Gap     │  ← Redis or another cache
        │  Storage     │
        └──────────────┘
               │
               │ (Baldur engine reads only)
               ▼
        ┌──────────────┐
        │    Baldur    │
        │    Engine    │
        └──────────────┘

    Example:
        >>> class MyAirGapAdapter:
        ...     def write_summary(self, key: str, value: Any, ttl: int = None) -> bool:
        ...         redis.set(f"sh:airgap:{key}", value, ex=ttl)
        ...         return True
        ...
        ...     def read_summary(self, key: str) -> Any:
        ...         return redis.get(f"sh:airgap:{key}")
        ...
        ...     def is_enabled(self) -> bool:
        ...         return True
    """

    def write_summary(self, key: str, value: Any, ttl: int | None = None) -> bool:
        """
        Write summary state to the Air-Gap storage.

        Called by the business layer whenever the DB changes.

        Args:
            key: storage key (e.g. "dlq:payment:pending", "cb:toss:state")
            value: value to store (must be serializable)
            ttl: Time-to-live in seconds (optional)

        Returns:
            Whether the write succeeded
        """
        ...

    def read_summary(self, key: str) -> Any:
        """
        Read summary state from the Air-Gap storage.

        Called by the Baldur engine when reading metrics.

        Args:
            key: storage key

        Returns:
            The stored value, or None
        """
        ...

    def delete_summary(self, key: str) -> bool:
        """
        Delete summary state from the Air-Gap storage.

        Args:
            key: storage key

        Returns:
            Whether the delete succeeded
        """
        ...

    def read_many(self, keys: list[str]) -> dict[str, Any]:
        """
        Read the values of several keys at once.

        Args:
            keys: keys to read

        Returns:
            Key-value dictionary
        """
        ...

    def increment(self, key: str, amount: int = 1) -> int:
        """
        Increment a counter value.

        Args:
            key: storage key
            amount: increment amount

        Returns:
            The value after incrementing
        """
        ...

    def decrement(self, key: str, amount: int = 1) -> int:
        """
        Decrement a counter value (never goes negative).

        Args:
            key: storage key
            amount: decrement amount

        Returns:
            The value after decrementing (minimum 0)
        """
        ...

    def is_enabled(self) -> bool:
        """
        Whether the Air-Gap feature is enabled.

        Returns:
            True if enabled, False otherwise
        """
        ...


class BaseAirGapAdapter(ABC):
    """
    Base class for Air-Gap storage adapters.

    Concrete adapters subclass this and provide the implementation.
    """

    @abstractmethod
    def write_summary(self, key: str, value: Any, ttl: int | None = None) -> bool:
        """Write summary state to the Air-Gap storage."""
        raise NotImplementedError

    @abstractmethod
    def read_summary(self, key: str) -> Any:
        """Read summary state from the Air-Gap storage."""
        raise NotImplementedError

    @abstractmethod
    def delete_summary(self, key: str) -> bool:
        """Delete summary state from the Air-Gap storage."""
        raise NotImplementedError

    def read_many(self, keys: list[str]) -> dict[str, Any]:
        """Read several keys at once. Default implementation reads one by one."""
        return {key: self.read_summary(key) for key in keys}

    def increment(self, key: str, amount: int = 1) -> int:
        """Increment a counter. Default implementation is read-modify-write."""
        current = self.read_summary(key)
        new_value = (int(current) if current else 0) + amount
        self.write_summary(key, new_value)
        return new_value

    def decrement(self, key: str, amount: int = 1) -> int:
        """Decrement a counter (never negative). Default is read-modify-write."""
        current = self.read_summary(key)
        current_int = int(current) if current else 0
        new_value = max(0, current_int - amount)
        self.write_summary(key, new_value)
        return new_value

    @abstractmethod
    def is_enabled(self) -> bool:
        """Whether the Air-Gap feature is enabled."""
        raise NotImplementedError


# Key-building helper functions
class AirGapKeys:
    """Air-Gap storage key builder helpers."""

    PREFIX = "sh:airgap:"

    @classmethod
    def dlq_pending(cls, domain: str) -> str:
        """DLQ pending count key."""
        return f"{cls.PREFIX}dlq:{domain}:pending"

    @classmethod
    def dlq_status(cls, domain: str, status: str) -> str:
        """DLQ status count key."""
        return f"{cls.PREFIX}dlq:{domain}:{status}"

    @classmethod
    def circuit_breaker_state(cls, service: str) -> str:
        """Circuit breaker state key."""
        return f"{cls.PREFIX}cb:{service}:state"

    @classmethod
    def circuit_breaker_failure_count(cls, service: str) -> str:
        """Circuit breaker failure count key."""
        return f"{cls.PREFIX}cb:{service}:failures"

    @classmethod
    def retry_success_count(cls, domain: str) -> str:
        """Retry success count key."""
        return f"{cls.PREFIX}retry:{domain}:success"

    @classmethod
    def retry_failure_count(cls, domain: str) -> str:
        """Retry failure count key."""
        return f"{cls.PREFIX}retry:{domain}:failure"


__all__ = [
    "AirGapStorageAdapter",
    "BaseAirGapAdapter",
    "AirGapKeys",
]
