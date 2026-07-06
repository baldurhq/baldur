"""
In-Memory Pool Recovery Handler.

Testing adapter for PoolRecoveryHandler ABC.
"""

from __future__ import annotations

import threading

from baldur.core.pool_watchdog import PoolRecoveryHandler


class InMemoryPoolRecoveryHandler(PoolRecoveryHandler):
    """
    In-memory PoolRecoveryHandler for testing.

    Records all recovery actions for test assertions.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._actions: list[dict] = []
        self._close_result = True
        self._expand_result = True
        self._shrink_result = True

    def close_connection(self, connection_id: str) -> bool:
        """Record and simulate connection close."""
        with self._lock:
            self._actions.append(
                {"action": "close_connection", "connection_id": connection_id}
            )
            return self._close_result

    def expand_pool(self, additional_connections: int) -> bool:
        """Record and simulate pool expansion."""
        with self._lock:
            self._actions.append(
                {
                    "action": "expand_pool",
                    "additional_connections": additional_connections,
                }
            )
            return self._expand_result

    def shrink_pool(self, target_size: int) -> bool:
        """Record and simulate pool shrink."""
        with self._lock:
            self._actions.append({"action": "shrink_pool", "target_size": target_size})
            return self._shrink_result

    def get_actions(self) -> list[dict]:
        """Get recorded actions for assertions."""
        with self._lock:
            return list(self._actions)

    def clear_actions(self) -> None:
        """Clear recorded actions."""
        with self._lock:
            self._actions.clear()


__all__ = ["InMemoryPoolRecoveryHandler"]
