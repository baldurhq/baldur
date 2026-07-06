"""Shared helpers for framework-agnostic API handlers.

Centralizes small utilities that multiple handler modules would otherwise
duplicate with drift (e.g., inconsistent audit-actor fallback strings).
"""

from __future__ import annotations

from baldur.interfaces.web_framework import RequestContext

__all__ = ["resolve_actor"]


def resolve_actor(ctx: RequestContext) -> str:
    """Extract actor string for audit trails.

    Contract:
        - Returns ``user.username`` when the user object exposes a non-empty
          username attribute.
        - Returns ``"anonymous"`` for unauthenticated requests or user objects
          that lack a username attribute.
        - Never returns an empty string or framework-specific repr
          (e.g., Django ``AnonymousUser.__str__`` -> "AnonymousUser").

    All handler modules must use this helper to keep the ``actor`` field
    consistent across audit logs. Previously each handler had its own
    fallback ("api", "anonymous", ``str(user or "api")``), which produced
    inconsistent audit entries for the same unauthenticated request.
    """
    user = ctx.user
    if user is None:
        return "anonymous"
    username = getattr(user, "username", None)
    return username or "anonymous"
