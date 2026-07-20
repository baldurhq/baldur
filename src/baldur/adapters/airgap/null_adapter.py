"""
Null Air-Gap Storage Adapter.

No-op implementation used when Air-Gap feature is disabled.
All operations are pass-through with no side effects.
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.adapters.airgap.base import BaseAirGapAdapter

logger = structlog.get_logger()


class NullAirGapAdapter(BaseAirGapAdapter):
    """
    No-op Air-Gap adapter for the disabled case.

    Used when the Air-Gap feature is turned off.
    All writes are ignored and all reads return None,
    so existing logic keeps working unchanged.

    Example:
        >>> adapter = NullAirGapAdapter()
        >>> adapter.write_summary("key", "value")  # ignored
        True
        >>> adapter.read_summary("key")  # returns None
        None
        >>> adapter.is_enabled()
        False
    """

    def __init__(self) -> None:
        """Initialize NullAirGapAdapter."""
        logger.debug("air_gap.nullairgapadapter_initialized_air_gap")

    def write_summary(self, key: str, value: Any, ttl: int | None = None) -> bool:
        """
        Ignore the write (no-op).

        Args:
            key: storage key (ignored)
            value: value to store (ignored)
            ttl: TTL (ignored)

        Returns:
            Always True (treated as success)
        """
        return True

    def read_summary(self, key: str) -> Any:
        """
        Always return None.

        Args:
            key: storage key

        Returns:
            Always None (no data in the Air-Gap storage)
        """
        return None

    def delete_summary(self, key: str) -> bool:
        """
        Ignore the delete (no-op).

        Args:
            key: storage key (ignored)

        Returns:
            Always True (treated as success)
        """
        return True

    def read_many(self, keys: list[str]) -> dict[str, Any]:
        """
        Return None for every key.

        Args:
            keys: keys to read

        Returns:
            Dictionary whose values are all None
        """
        return dict.fromkeys(keys)

    def increment(self, key: str, amount: int = 1) -> int:
        """
        Ignore the increment (no-op).

        Args:
            key: storage key (ignored)
            amount: increment amount (ignored)

        Returns:
            Always 0
        """
        return 0

    def decrement(self, key: str, amount: int = 1) -> int:
        """
        Ignore the decrement (no-op).

        Args:
            key: storage key (ignored)
            amount: decrement amount (ignored)

        Returns:
            Always 0
        """
        return 0

    def is_enabled(self) -> bool:
        """
        Air-Gap is disabled.

        Returns:
            Always False
        """
        return False


__all__ = ["NullAirGapAdapter"]
