"""
Rate Limit Event History — In-memory ring buffer for X-Test-Mode.

Tracks rate limit events for debugging and forensic analysis.
Safe for production use with bounded memory.

Extracted from api/django/rate_limit.py as part of 358 rate_limit package split.
"""

from __future__ import annotations

import threading

from baldur.utils.time import utc_now

__all__ = ["RateLimitEventHistory"]


class RateLimitEventHistory:
    """In-memory ring buffer for rate limit event tracking.

    Thread-safe event recording with configurable max capacity.
    Used by X-Test-Mode to query rate limit history.
    """

    def __init__(self, max_events: int = 500):
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._max_events = max_events

    def record(self, event: dict) -> None:
        """
        Record a rate limit event.

        Args:
            event: Event dict to record (will have recorded_at added)
        """
        with self._lock:
            event["recorded_at"] = utc_now().isoformat()
            self._events.append(event)
            if len(self._events) > self._max_events:
                self._events = self._events[-self._max_events :]

    def get_events(self, limit: int = 20) -> list[dict]:
        """
        Get recent rate limit events.

        Args:
            limit: Maximum events to return (max 100)

        Returns:
            Recent events in reverse chronological order
        """
        limit = min(limit, 100)
        with self._lock:
            return list(reversed(self._events[-limit:]))

    def get_count(self) -> int:
        """Get total event count."""
        with self._lock:
            return len(self._events)

    def get_events_by_client(self, client_key: str, limit: int = 20) -> list[dict]:
        """
        Get events for a specific client.

        Args:
            client_key: Client identifier
            limit: Maximum events to return

        Returns:
            Client's events in reverse chronological order
        """
        limit = min(limit, 100)
        with self._lock:
            filtered = [e for e in self._events if e.get("client_key") == client_key]
            return list(reversed(filtered[-limit:]))

    def reset(self, client_key: str | None = None) -> int:
        """
        Reset event history.

        Args:
            client_key: Reset only this client. None = reset all.

        Returns:
            Number of events removed
        """
        with self._lock:
            if client_key is None:
                count = len(self._events)
                self._events = []
                return count
            original_count = len(self._events)
            self._events = [
                e for e in self._events if e.get("client_key") != client_key
            ]
            return original_count - len(self._events)

    def get_client_stats(self) -> dict:
        """
        Aggregate per-client rate limit statistics.

        Returns:
            {client_key: {"total": N, "exceeded": M}, ...}
        """
        with self._lock:
            stats: dict[str, dict] = {}
            for event in self._events:
                client_key = event.get("client_key", "unknown")
                if client_key not in stats:
                    stats[client_key] = {"total": 0, "exceeded": 0}
                stats[client_key]["total"] += 1
                if not event.get("allowed", True):
                    stats[client_key]["exceeded"] += 1
            return stats
