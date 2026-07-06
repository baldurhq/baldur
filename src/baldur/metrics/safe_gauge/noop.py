"""
No-op Gauge Implementation.

Used when Prometheus gauge is not available (e.g., metrics disabled).
"""

from __future__ import annotations

from typing import Any


class NoOpGaugeChild:
    """No-op implementation for when gauge is not available."""

    def inc(self, amount: float = 1) -> None:
        """No-op increment."""
        pass

    def dec(self, amount: float = 1) -> None:
        """No-op decrement."""
        pass

    def set(self, value: float) -> None:
        """No-op set."""
        pass

    def get_shadow_value(self) -> float:
        """Return 0 for no-op."""
        return 0.0

    def sync_from_source(self, actual_value: float, source: str = "noop") -> None:
        """No-op sync."""
        pass

    @property
    def is_synced(self) -> bool:
        """Always False for no-op."""
        return False

    @property
    def is_recovering(self) -> bool:
        """Always False for no-op."""
        return False

    @property
    def last_sync_time(self) -> None:
        """Always None for no-op."""
        return None

    @property
    def sync_age_seconds(self) -> None:
        """Always None for no-op."""
        return None

    def mark_stale(self, reason: str = "external") -> None:
        """No-op mark_stale."""
        pass

    def get_reliability_info(self) -> dict[str, Any]:
        """Return empty reliability info."""
        return {"is_synced": False, "status": "noop"}


__all__ = [
    "NoOpGaugeChild",
]
