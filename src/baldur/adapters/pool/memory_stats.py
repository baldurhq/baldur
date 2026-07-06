"""
In-Memory Pool Stats Provider.

Testing adapter for PoolStatsProvider ABC.
"""

from __future__ import annotations

import threading

from baldur.interfaces.pool_monitor import PoolStats, PoolStatsProvider


class InMemoryPoolStatsProvider(PoolStatsProvider):
    """
    In-memory PoolStatsProvider for testing.

    Allows direct manipulation of pool stats for unit tests.
    """

    def __init__(
        self,
        pool_name: str = "test_pool",
        max_connections: int = 10,
        active_connections: int = 0,
        available_connections: int = 10,
        waiting_requests: int = 0,
    ):
        self._lock = threading.Lock()
        self._stats = PoolStats(
            pool_name=pool_name,
            max_connections=max_connections,
            active_connections=active_connections,
            available_connections=available_connections,
            waiting_requests=waiting_requests,
        )

    def get_stats(self) -> PoolStats:
        """Get current pool statistics."""
        with self._lock:
            return PoolStats(
                pool_name=self._stats.pool_name,
                max_connections=self._stats.max_connections,
                active_connections=self._stats.active_connections,
                available_connections=self._stats.available_connections,
                waiting_requests=self._stats.waiting_requests,
            )

    def set_stats(self, **kwargs) -> None:
        """Set pool stats for testing."""
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._stats, key):
                    setattr(self._stats, key, value)


__all__ = ["InMemoryPoolStatsProvider"]
