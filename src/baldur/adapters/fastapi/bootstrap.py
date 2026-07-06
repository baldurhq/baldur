"""
FastAPI integration entry point.

``init_fastapi(app)`` is called once at application-construction time. It:
    1. Calls ``baldur.init()`` (idempotent via ``bootstrap._init_done``).
    2. Installs ``BaldurMiddleware`` via ``app.add_middleware`` so the
       framework-free reject/observe helpers (rate limit, admission /
       backpressure, CB pre-flight + observation) wrap every request.

This mirrors Flask's ``init_flask`` and closes the auto-wiring asymmetry: an
ASGI lifespan provably cannot add middleware (Starlette freezes the middleware
stack at app-build time, before lifespan startup), so an app-construction-time
helper is the only auto-wiring path. ``fastapi_lifespan`` (startup/shutdown
drain) stays separate and also calls ``baldur.init()`` (idempotent); a user
wires both::

    app = FastAPI(lifespan=fastapi_lifespan)
    init_fastapi(app)

Admin server autostart, scheduler autostart, and audit pipeline startup are
handled by ``baldur.init()`` per the existing ``BALDUR_*`` env-var gates —
this module adds zero new behavior beyond plumbing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from baldur.adapters.fastapi.middleware import BaldurMiddleware

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = structlog.get_logger()


__all__ = ["init_fastapi"]


def init_fastapi(
    app: FastAPI,
    service_name: str | None = None,
    rate_limit: int | None = None,
    window_seconds: int | None = None,
) -> None:
    """Initialize Baldur for a FastAPI app.

    Args:
        app: The FastAPI application instance (must be created before this
            call — middleware cannot be added once the ASGI stack is built).
        service_name: Optional upstream identity. When supplied, CB pre-flight
            + post-response observation are enabled. When ``None``, the CB hooks
            are no-ops (rate limit + admission / backpressure still apply).
        rate_limit: Per-instance override for the middleware rate limit
            (requests per window). ``None`` falls back to
            ``RateLimitSettings.middleware_rate_limit`` (default ``0`` =
            disabled). Pass a positive integer to enable rate limiting only for
            this FastAPI app.
        window_seconds: Per-instance window size override. ``None`` falls back
            to ``RateLimitSettings.middleware_window_seconds``.
    """
    import baldur

    baldur.init()
    app.add_middleware(
        BaldurMiddleware,
        service_name=service_name,
        rate_limit=rate_limit,
        window_seconds=window_seconds,
    )
    logger.info(
        "baldur.fastapi_initialized",
        service_name=service_name,
    )
