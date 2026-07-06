"""
Rate limit middleware helpers — framework-free.

This module ships an L1-only (in-process memory) rate limiter that adapters
can compose into framework-native middleware. The full hybrid L1+L2 (Redis)
limiter under ``api/django/rate_limit/middleware.py`` stays Django-coupled
for now because it depends on Django's cache framework for Redis client
discovery; that hybrid lives behind the same ``RateLimitSettings`` env-var
surface and migrates incrementally per the Part 3 scope discipline note in
``429``.

L1-only is sufficient for FastAPI / Flask MVP. Multi-process deployments
get ``effective_limit = rate_limit * worker_count`` — the same fail-open
trade-off the existing Django L1 fallback documents.

Opt-in surface
--------------
The helpers read ``RateLimitSettings.middleware_rate_limit`` (separate from
``control_api_rate_limit`` which is reserved for the Django admin-path
hybrid). The default is ``0`` = disabled, so mounting ``BaldurMiddleware``
for CB / backpressure protection does not silently rate-limit traffic.
Operators enable it via ``BALDUR_RATE_LIMIT_MIDDLEWARE_RATE_LIMIT`` or via
per-instance kwargs on ``BaldurMiddleware(...)`` / ``init_flask(...)``.
"""

from __future__ import annotations

import time
from collections.abc import MutableMapping
from typing import TYPE_CHECKING

import structlog

from baldur.interfaces.web_framework import ResponseContext
from baldur.services.rate_limit import RateLimitState, SlidingWindowLimiter
from baldur.settings.rate_limit import get_rate_limit_settings

if TYPE_CHECKING:
    from baldur.interfaces.web_framework import RequestContext

logger = structlog.get_logger()


__all__ = [
    "check_rate_limit",
    "apply_rate_limit_headers",
    "RateLimitState",
    "reset_rate_limit_state",
]


# Module-level singleton backed by the unified SlidingWindowLimiter.
_limiter = SlidingWindowLimiter()


def reset_rate_limit_state() -> None:
    """Reset the in-process limiter (test helper)."""
    _limiter.reset()


# =============================================================================
# Canonical header builder
# =============================================================================


def build_rate_limit_headers(
    limit: int,
    remaining: int,
    reset_at: int,
    *,
    retry_after: int | None = None,
    mode: str | None = None,
) -> dict[str, str]:
    """Build the canonical rate-limit header set shared by every OSS emit site.

    Single definition point for the ``X-RateLimit-*`` surface so the header
    field set cannot drift between the framework-free helper (path B) and the
    Django Control-API hybrid (path D). All four emit sites — B's 429, B's
    success headers, D's 429, D's success headers — construct their headers
    through this function.

    The canonical set is always ``X-RateLimit-Limit`` / ``X-RateLimit-Remaining``
    / ``X-RateLimit-Reset``. Two optional additions:

    - ``retry_after`` present -> 429 shape: adds ``Retry-After``. ``remaining``
      is the caller-provided value (canonically ``0`` on a rejection).
    - ``mode`` present -> adds ``X-RateLimit-Mode``. This is a documented D-only
      extension (normal / emergency / bypass); path B is L1-only and has no
      mode concept, so it never sets it. A parity check therefore compares B's
      and D's 429 key sets modulo ``{"X-RateLimit-Mode"}``.

    Returns a plain ``dict[str, str]`` so each caller can adapt it to its own
    response type (``ResponseContext`` headers vs Django response items).
    """
    headers = {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(reset_at),
    }
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    if mode is not None:
        headers["X-RateLimit-Mode"] = mode
    return headers


# =============================================================================
# Client key derivation
# =============================================================================


def _client_key(request: RequestContext) -> str:
    """Compose the rate-limit bucket key for a request.

    Mirrors the Django middleware's ``ip:user_id`` composition
    (``api/django/rate_limit/middleware.py:248-256``) so traffic from the
    same authenticated user behind the same IP shares a bucket regardless
    of which framework served the request.
    """
    ip = request.client_ip or "unknown"
    user_id: object = "anonymous"
    if request.user is not None:
        uid = getattr(request.user, "id", None)
        if uid is None:
            uid = getattr(request.user, "pk", None)
        user_id = uid if uid is not None else "anonymous"
    return f"ratelimit:{ip}:{user_id}"


# =============================================================================
# Public helpers
# =============================================================================


def _resolve_limits(
    rate_limit: int | None,
    window_seconds: int | None,
) -> tuple[int, int]:
    """Resolve (limit, window) from explicit kwargs falling back to settings."""
    settings = get_rate_limit_settings()
    limit = rate_limit if rate_limit is not None else settings.middleware_rate_limit
    window = (
        window_seconds
        if window_seconds is not None
        else settings.middleware_window_seconds
    )
    return limit, window


def check_rate_limit(
    request: RequestContext,
    rate_limit: int | None = None,
    window_seconds: int | None = None,
) -> ResponseContext | None:
    """Reject the request with 429 when the per-client window is exhausted.

    Returns ``None`` to allow. On rejection, returns a 429 ``ResponseContext``
    with ``Retry-After`` / ``X-RateLimit-Limit`` / ``X-RateLimit-Remaining``
    / ``X-RateLimit-Reset`` headers — preserving the surface that
    ``api/django/rate_limit/middleware.py:362-389`` already emits.

    ``rate_limit`` / ``window_seconds`` override the values from
    ``RateLimitSettings.middleware_*`` for this call. A resolved limit of
    ``<= 0`` disables the check (returns ``None``) — the default setting is
    ``0`` so the helper is opt-in.
    """
    limit, window = _resolve_limits(rate_limit, window_seconds)
    if limit <= 0:
        return None

    state = _limiter.check(_client_key(request), limit, window)

    if state.allowed:
        return None

    retry_after = max(1, state.reset_at - int(time.time()))

    logger.info(
        "rate_limit.request_rejected",
        client_ip=request.client_ip,
        path=request.path,
        limit=state.limit,
    )

    return ResponseContext(
        status_code=429,
        body={
            "error": "rate_limit_exceeded",
            "message": "Too many requests",
            "retry_after": retry_after,
        },
        headers=build_rate_limit_headers(
            state.limit,
            state.remaining,
            state.reset_at,
            retry_after=retry_after,
        ),
    )


def apply_rate_limit_headers(
    headers: MutableMapping[str, str],
    request: RequestContext,
    rate_limit: int | None = None,
    window_seconds: int | None = None,
) -> None:
    """Add rate-limit observability headers to a successful response.

    Mutates ``headers`` to add ``X-RateLimit-Limit`` /
    ``X-RateLimit-Remaining`` / ``X-RateLimit-Reset`` based on the current
    bucket state for the request's client. Uses ``peek`` so the read does
    not count against the client's quota (idempotent observability).

    When the resolved limit is ``<= 0`` (the default), this is a no-op so
    clients do not see rate-limit headers on a middleware that is not
    actually rate-limiting.
    """
    limit, window = _resolve_limits(rate_limit, window_seconds)
    if limit <= 0:
        return

    state = _limiter.peek(_client_key(request), limit, window)
    headers.update(
        build_rate_limit_headers(state.limit, state.remaining, state.reset_at)
    )
