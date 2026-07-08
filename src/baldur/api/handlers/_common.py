"""Shared helpers for framework-agnostic API handlers.

Centralizes small utilities that multiple handler modules would otherwise
duplicate with drift (e.g., inconsistent audit-actor fallback strings).
"""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Callable, Collection, Mapping
from typing import Any

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()

__all__ = [
    "resolve_actor",
    "dataclass_field_names",
    "reject_unknown_config_keys",
    "reject_unknown_kwargs",
]


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


def dataclass_field_names(config: Any) -> set[str]:
    """Return the field names of a dataclass config instance.

    Used by config-write handlers to derive the accepted key set at runtime
    from the registry-resolved sink config, so the allowlist can never drift
    from the actual config schema.
    """
    return {f.name for f in dataclasses.fields(config)}


def reject_unknown_config_keys(
    body: Mapping[str, Any],
    allowed_fields: Collection[str],
    *,
    config_label: str,
) -> ResponseContext | None:
    """Strict all-or-nothing validation for a config-write body.

    Returns a ``400`` :class:`ResponseContext` when ``body`` carries any key
    outside ``allowed_fields`` — nothing is applied — or ``None`` when every
    key is valid, in which case the caller applies the whole body. Rejecting
    the entire body on any unknown key (rather than silently dropping it, which
    a downstream ``hasattr``/allowlist filter does) makes a typo riding a valid
    key visible instead of a silent no-op, and keeps the sink's key-by-key
    application atomic from the operator's view.

    The 400 body carries ``unknown_fields`` and ``allowed_fields`` so a client
    can self-correct. A rejection is logged at WARNING level with a
    ``*_blocked``-suffixed event name.
    """
    unknown = sorted(k for k in body if k not in allowed_fields)
    if not unknown:
        return None

    logger.warning(
        "api.config_update_blocked",
        config=config_label,
        unknown_fields=unknown,
        allowed_fields=sorted(allowed_fields),
    )
    return ResponseContext.json(
        {
            "status": "error",
            "error": "Unknown configuration field(s)",
            "config": config_label,
            "unknown_fields": unknown,
            "allowed_fields": sorted(allowed_fields),
        },
        status_code=400,
    )


def reject_unknown_kwargs(
    body: Mapping[str, Any],
    method: Callable[..., Any],
    *,
    config_label: str,
) -> ResponseContext | None:
    """Strict validation for a handler that splats ``body`` into a typed-kwargs sink.

    Derives the accepted key set from ``method``'s signature (a bound method,
    so ``self`` is already excluded) and delegates to
    :func:`reject_unknown_config_keys`. Without this a typo'd key reaches the
    sink and raises ``TypeError`` — surfaced as an opaque 500 on the plain admin
    server — instead of a clear 400. If the method accepts ``**kwargs`` no key
    can be unknown, so validation is skipped (returns ``None``).
    """
    sig = inspect.signature(method)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return None
    allowed = {
        name
        for name, p in sig.parameters.items()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    }
    return reject_unknown_config_keys(body, allowed, config_label=config_label)
