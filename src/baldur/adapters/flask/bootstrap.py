"""
Flask integration entry point.

``init_flask(app)`` is called once from the application factory. It:
    1. Calls ``baldur.init()`` (idempotent via ``bootstrap._init_done``).
    2. Registers ``before_request`` / ``after_request`` hooks that compose
       the framework-free helpers in ``baldur.api.middleware``.

Admin server autostart, scheduler autostart, and audit pipeline startup are
handled by ``baldur.init()`` per the existing ``BALDUR_*`` env-var gates —
this module adds zero new behavior beyond plumbing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from baldur.adapters.flask.middleware import (
    install_baldur_request_hooks,
)

if TYPE_CHECKING:
    from flask import Flask

logger = structlog.get_logger()


__all__ = ["init_flask"]


def init_flask(
    app: Flask,
    service_name: str | None = None,
    rate_limit: int | None = None,
    window_seconds: int | None = None,
) -> None:
    """Initialize Baldur for a Flask app.

    Args:
        app: The Flask application instance.
        service_name: Optional upstream identity. When supplied,
            CB pre-flight + post-response observation are enabled. When
            ``None``, the CB hooks are no-ops (rate limit + backpressure
            still apply).
        rate_limit: Per-instance override for the middleware rate limit
            (requests per window). ``None`` falls back to
            ``RateLimitSettings.middleware_rate_limit`` (default ``0`` =
            disabled). Pass a positive integer to enable rate limiting
            only for this Flask app.
        window_seconds: Per-instance window size override. ``None`` falls
            back to ``RateLimitSettings.middleware_window_seconds``.
    """
    import baldur

    baldur.init()
    install_baldur_request_hooks(
        app,
        service_name=service_name,
        rate_limit=rate_limit,
        window_seconds=window_seconds,
    )
    logger.info(
        "baldur.flask_initialized",
        service_name=service_name,
    )
