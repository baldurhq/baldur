"""
FastAPI lifespan integration.

Calls ``baldur.init()`` on application startup and synchronizes the ASGI
teardown with the ``ShutdownCoordinator`` drain — the ASGI counterpart of
the gunicorn ``worker_exit`` wait. ``baldur.init()`` already gates
admin-server autostart, scheduler autostart, and audit pipeline startup
behind the relevant ``BALDUR_*`` settings — this lifespan adds zero new
behavior beyond plumbing.

Idempotency is guaranteed by ``bootstrap._init_done`` so reload-aware
deployments (uvicorn ``--reload``) do not double-init.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = structlog.get_logger()


__all__ = ["fastapi_lifespan"]


@asynccontextmanager
async def fastapi_lifespan(app: FastAPI) -> AsyncIterator[dict[str, Any]]:
    """Initialize Baldur on app startup, drain on shutdown.

    Yields an empty mapping so callers can attach lifespan-scoped state via
    the standard ``lifespan_state`` pattern; the dict is reserved for
    Baldur-internal use and may carry diagnostic state in future versions.
    """
    import baldur

    baldur.init()
    logger.info("baldur.fastapi_lifespan_initialized")

    try:
        yield {}
    finally:
        # Teardown is the ASGI drain sync point (597 D9). Under a signal
        # the coordinator's chained handler has already initiated the
        # drain and this initiate is a phase-guarded no-op; calling it
        # here also covers non-signal teardown such as uvicorn --reload
        # and programmatic shutdown. Safe against uvicorn's ordering:
        # Server.shutdown runs lifespan shutdown AFTER the HTTP drain and
        # SKIPS it on force_exit, so this wait never blocks a force-quit.
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator

        coordinator = get_shutdown_coordinator()
        coordinator.initiate_shutdown()
        drain_timeout = coordinator.get_stats().drain_timeout_seconds
        logger.info("baldur.fastapi_lifespan_shutdown")
        try:
            completed = await asyncio.to_thread(
                coordinator.wait_for_shutdown, timeout=drain_timeout + 5.0
            )
            if not completed:
                logger.warning("baldur.fastapi_lifespan_drain_timeout")
        except (asyncio.CancelledError, GeneratorExit):  # noqa: TRY203
            # Loop teardown abandoned the lifespan mid-wait (e.g. uvicorn
            # force_exit). The daemon drain thread continues independently;
            # the cancellation must propagate — never swallow it (asyncio
            # contract; 597 D9 external-review amendment).
            raise
