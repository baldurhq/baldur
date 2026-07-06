"""Framework-free admin HTTP server.

Stdlib ``http.server.ThreadingHTTPServer`` based management API. Runs in a
daemon thread, dispatches to framework-agnostic handler functions, and
integrates with :class:`baldur.core.shutdown_coordinator.ShutdownCoordinator`
for graceful drain.

Public entry point::

    import baldur

    baldur.init()
    baldur.start_admin_server(port=9090)   # manual start

Or auto-start via :func:`baldur.init` when ``BALDUR_ADMIN_AUTOSTART=1`` (the
default).

Status: Public
"""

# Reference: 429 Part 2 (PR3).

from __future__ import annotations

from baldur.api.admin.registry import (
    AdminRoute,
    get_admin_registry,
    register_admin_route,
)
from baldur.api.admin.server import (
    AdminServer,
    get_admin_server,
    reset_admin_server,
    start_admin_server,
    stop_admin_server,
)

__all__ = [
    "AdminRoute",
    "AdminServer",
    "get_admin_registry",
    "get_admin_server",
    "register_admin_route",
    "reset_admin_server",
    "start_admin_server",
    "stop_admin_server",
]
