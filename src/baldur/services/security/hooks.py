"""
Security Hooks - security-event callback registry.

Lets the baldur package trigger the host app's token invalidation (etc.) on a
security violation, without depending on the host app's authentication system.

Usage (in the host app's AppConfig.ready()):
    from baldur.services.security.hooks import register_session_invalidation_hook

    def blacklist_user_jwt(user_id: int) -> str:
        from rest_framework_simplejwt.token_blacklist.models import (
            BlacklistedToken, OutstandingToken,
        )
        tokens = OutstandingToken.objects.filter(user_id=user_id)
        count = 0
        for token in tokens:
            _, created = BlacklistedToken.objects.get_or_create(token=token)
            if created:
                count += 1
        return f"jwt_blacklisted({count})"

    register_session_invalidation_hook(blacklist_user_jwt)
"""

from __future__ import annotations

from collections.abc import Callable

import structlog

logger = structlog.get_logger()

# Callback type: (user_id: int) -> str (result description)
# ──────────────────────────────────────────────────────────────────
# Why user_id is typed as int:
#   - Django AbstractUser has no custom PK → uses the default PK
#   - DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
#   - BigAutoField is a Python int type
# ──────────────────────────────────────────────────────────────────
SessionInvalidationHook = Callable[[int], str]

_hooks: list[SessionInvalidationHook] = []


def register_session_invalidation_hook(hook: SessionInvalidationHook) -> None:
    """
    Register a callback to run on session invalidation.

    Registered callbacks run in order when _invalidate_user_sessions(user_id)
    is called.

    Args:
        hook: A callback of the form (user_id: int) -> str
    """
    _hooks.append(hook)
    logger.info(
        "security_hooks.session_invalidation_hook_registered",
        getattr=getattr(hook, "__module__", "?"),
        hook_qualname=getattr(hook, "__qualname__", repr(hook)),
    )


def get_session_invalidation_hooks() -> list[SessionInvalidationHook]:
    """Return the list of registered session-invalidation callbacks."""
    return list(_hooks)


def clear_session_invalidation_hooks() -> None:
    """Remove all callbacks (for tests)."""
    _hooks.clear()
