"""
No-op Session Invalidation Adapter.

Returns empty results for environments without a session store.
"""

from __future__ import annotations

from baldur.interfaces.session_provider import SessionInvalidationProvider

__all__ = ["NoopSessionAdapter"]


class NoopSessionAdapter(SessionInvalidationProvider):
    """No-op implementation for environments without a session store."""

    def invalidate_user_sessions(self, user_id: int | str) -> list[str]:
        return []

    def get_active_session_count(self, user_id: int | str) -> int:
        return 0
