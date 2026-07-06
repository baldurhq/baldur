"""
Session Invalidation Provider Interface.

Abstract interface for session invalidation across backends.

Supports multiple session backends:
    - Django DB sessions
    - Redis sessions
    - JWT token blacklisting
    - External IdP (Okta, Auth0) session revocation

Implementations:
    - DjangoSessionAdapter: wraps Django Session model
    - NoopSessionAdapter: returns [] (no session store)
"""

from __future__ import annotations

from abc import ABC, abstractmethod

__all__ = [
    "SessionInvalidationProvider",
]


class SessionInvalidationProvider(ABC):
    """
    Abstract interface for session invalidation across backends.

    Implementations should return description strings for audit logging.
    """

    @abstractmethod
    def invalidate_user_sessions(self, user_id: int | str) -> list[str]:
        """
        Invalidate all active sessions for a user.

        Returns:
            List of invalidation description strings
            (e.g. ["django_sessions(2)", "redis_sessions(0:cleanup_failed)"]).
            Callers join these for audit logging and security event details.
        """
        ...

    @abstractmethod
    def get_active_session_count(self, user_id: int | str) -> int:
        """
        Count active sessions for a user.

        Useful for security audit compliance checks.
        """
        ...
