"""
FastAPI adapter for Baldur.

Provides:
- ``init_fastapi(app, service_name=None)`` — app-construction-time hook. Calls
  ``baldur.init()`` exactly once and installs ``BaldurMiddleware`` via
  ``app.add_middleware``. Mirrors Flask's ``init_flask``; the auto-wiring path
  because an ASGI lifespan provably cannot add middleware after the stack is
  built.
- ``fastapi_lifespan`` — async context manager for FastAPI's ``lifespan=``
  parameter. Calls ``baldur.init()`` on startup; admin server auto-starts
  via the existing ``BALDUR_ADMIN_AUTOSTART`` flag in ``bootstrap.py``.
- ``BaldurMiddleware`` — ASGI middleware that composes the framework-free
  helpers from ``baldur.api.middleware`` (rate limit, admission / backpressure,
  CB pre-flight + observation) into a single mountable middleware.

Install: ``pip install baldur-framework[fastapi]``

Example:
    .. code-block:: python

        from fastapi import FastAPI
        from baldur.adapters.fastapi import fastapi_lifespan, init_fastapi

        app = FastAPI(lifespan=fastapi_lifespan)
        init_fastapi(app)

The adapter intentionally stays thin — every decision lives in
``baldur.api.middleware``. The wrapper only translates between FastAPI's
``Request`` / ``Response`` and Baldur's ``RequestContext`` /
``ResponseContext``.

Status: Public
"""

from __future__ import annotations

from baldur.adapters.fastapi.bootstrap import init_fastapi
from baldur.adapters.fastapi.lifespan import fastapi_lifespan
from baldur.adapters.fastapi.middleware import BaldurMiddleware

__all__ = [
    "init_fastapi",
    "fastapi_lifespan",
    "BaldurMiddleware",
]
