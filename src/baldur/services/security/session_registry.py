"""
UserSessionRegistry - manages the reverse user_id → session_key mapping.

Django sessions only support a one-way session_key → session_data store.
This module manages a reverse index using a Redis list structure so the
session_key(s) of a user can be found by user_id.

Redis key structure:
    security:user_sessions:{user_id} → list[session_key]

Multi-session support:
    A user may log in from multiple devices, so a list (not a single value) is used.

Signal wiring:
    BaldurConfig.ready() calls connect_session_signals() in
    adapters/django/signal_hooks.py, which automatically connects to the
    user_logged_in / user_logged_out signals. No extra code is needed in the host app.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from baldur.interfaces.cache_provider import CacheProviderInterface

logger = structlog.get_logger()

# Django default: SESSION_COOKIE_AGE = 1209600 (2 weeks)
_DEFAULT_SESSION_TTL_SECONDS = 1209600


class UserSessionRegistry:
    """
    Manages the reverse user_id → session_key list mapping.

    The Django cache session backend looks up session_data from a session_key,
    but the reverse lookup of all session_keys for a user_id is not possible.
    This class manages that reverse index on top of CacheProviderInterface.
    """

    KEY_PREFIX = "security:user_sessions:"

    def __init__(self, cache: CacheProviderInterface | None = None):
        self._cache = cache

    @property
    def cache(self) -> CacheProviderInterface:
        if self._cache is None:
            from baldur.factory import ProviderRegistry

            try:
                self._cache = ProviderRegistry.get_cache()
            except (ValueError, ImportError):
                from baldur.adapters.cache.memory_adapter import (
                    InMemoryCacheAdapter,
                )

                self._cache = InMemoryCacheAdapter()
        return self._cache

    def _key(self, user_id: int) -> str:
        return f"{self.KEY_PREFIX}{user_id}"

    def register(self, user_id: int, session_key: str) -> None:
        """
        Register the user_id → session_key mapping on login.

        Appends the session_key to the existing list (avoiding duplicates).
        The TTL is synced with Django SESSION_COOKIE_AGE.
        """
        key = self._key(user_id)
        try:
            existing: list[str] = self.cache.get(key) or []
            if session_key not in existing:
                existing.append(session_key)
            ttl = self._get_session_ttl()
            self.cache.set(key, existing, ttl=timedelta(seconds=ttl))
            logger.debug(
                "cell_registry.bulkheads_registered",
                user_id=user_id,
                session_key=session_key[:8],
                count=len(existing),
            )
        except Exception as e:
            logger.warning(
                "user_session_registry.register_session_failed",
                error=e,
            )

    def unregister(self, user_id: int, session_key: str) -> None:
        """
        Remove the user_id → session_key mapping on logout.

        Removes only that session_key from the list, and deletes the key itself
        if the list becomes empty.
        """
        key = self._key(user_id)
        try:
            existing: list[str] = self.cache.get(key) or []
            if session_key in existing:
                existing.remove(session_key)
            if existing:
                ttl = self._get_session_ttl()
                self.cache.set(key, existing, ttl=timedelta(seconds=ttl))
            else:
                self.cache.delete(key)
            logger.debug(
                "user_session_registry.unregistered_session_user",
                user_id=user_id,
                session_key=session_key[:8],
            )
        except Exception as e:
            logger.warning(
                "user_session_registry.unregister_session_failed",
                error=e,
            )

    def get_session_keys(self, user_id: int) -> list[str]:
        """Look up all session_keys associated with a user_id."""
        key = self._key(user_id)
        try:
            return self.cache.get(key) or []
        except Exception:
            return []

    def invalidate_all(self, user_id: int) -> int:
        """
        Invalidate all sessions of a user_id.

        1. Look up the session_key list from the registry
        2. Attempt to delete the Django cache session keys
        3. Delete the registry key itself

        Returns:
            Number of deleted sessions
        """
        session_keys = self.get_session_keys(user_id)
        deleted = 0
        for sk in session_keys:
            try:
                # Attempt deletion in the Django cache session-key format
                self.cache.delete(sk)
                self.cache.delete(f"django.contrib.sessions.cache{sk}")
                deleted += 1
            except Exception:
                pass
        # Delete the registry key
        self.cache.delete(self._key(user_id))
        logger.info(
            "user_session_registry.invalidated_sessions_user",
            deleted=deleted,
            user_id=user_id,
        )
        return deleted

    @staticmethod
    def _get_session_ttl() -> int:
        """Look up the Session cookie age setting value."""
        try:
            # Layered read (686 D1/D5) so a console edit of the security domain's
            # session_cookie_age takes effect; env base when no manager registered.
            from baldur.settings.layered_provider import get_layered_settings
            from baldur.settings.security import SecuritySettings

            return get_layered_settings(SecuritySettings, "security").session_cookie_age
        except Exception:
            return _DEFAULT_SESSION_TTL_SECONDS


# =============================================================================
# Singleton
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

(
    get_user_session_registry,
    configure_user_session_registry,
    reset_user_session_registry,
) = make_singleton_factory("user_session_registry", UserSessionRegistry)
