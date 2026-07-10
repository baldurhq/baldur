"""
Cell Tagger — tag requests/tasks with a ``cell_id``.

Uses ``CellRegistry.get_cell_for_key()`` to perform consistent Cell
assignment.

Tagging key priority:
1. tenant_id (multi-tenant environments)
2. user_id (authenticated users)
3. session_id (session-based)
4. client_ip (last resort)
5. trace_id (fallback — even distribution via distributed hashing)
"""

from __future__ import annotations

import time
from typing import Any, cast

import structlog

from baldur.utils.network import extract_client_ip

logger = structlog.get_logger()


class CellTagger:
    """
    Cell Tagger — determine a ``cell_id`` from the request context.

    Works with ``CellRegistry`` to perform Consistent Hash based Cell
    assignment.
    """

    # Tagging key priority (highest first)
    TAG_KEY_PRIORITY = [
        "tenant_id",
        "user_id",
        "session_id",
        "client_ip",
    ]

    def __init__(self):
        self._cell_registry = None

    def _get_registry(self):
        """Lazily load the CellRegistry."""
        if self._cell_registry is None:
            from baldur.services.cell_topology import get_cell_registry

            self._cell_registry = get_cell_registry()
        return self._cell_registry

    def resolve_cell_id(self, context: dict[str, Any]) -> str:
        """
        Determine the cell_id from a context.

        Args:
            context: Tagging context
                - tenant_id: tenant identifier
                - user_id: user identifier
                - session_id: session identifier
                - client_ip: client IP
                - trace_id: distributed trace ID (fallback)

        Returns:
            cell_id (e.g. "cell-3")
        """
        registry = self._get_registry()
        settings = registry._settings

        if not settings.enabled or not settings.tagging_enabled:
            return f"{settings.cell_prefix}-0"

        # Search tagging keys in priority order
        for key_name in self.TAG_KEY_PRIORITY:
            value = context.get(key_name)
            if value:
                return registry.get_cell_for_key(f"{key_name}:{value}")

        # ── Fallback: even distribution via distributed hashing ──
        # Instead of pinning to cell-0, reuse the Hash Ring to spread evenly
        # across ACTIVE Cells. Use trace_id when present (guarantees the same
        # Cell on a retry of the same request); otherwise spread by monotonic_ns.
        trace_id = context.get("trace_id")
        fallback_key = f"fallback:{trace_id or time.monotonic_ns()}"
        return registry.get_cell_for_key(fallback_key)

    def resolve_cell_id_from_request(self, request: Any) -> str:
        """
        Determine the cell_id from a Django HttpRequest.

        Args:
            request: Django HttpRequest

        Returns:
            cell_id

        Note:
            Accessing request.user / request.session requires this to run
            after AuthenticationMiddleware and SessionMiddleware.
        """
        context: dict[str, Any] = {}

        # tenant_id (set by the multi-tenant middleware)
        tenant_id = getattr(request, "tenant_id", None)
        if tenant_id:
            context["tenant_id"] = str(tenant_id)

        # user_id (set by the authentication middleware)
        if hasattr(request, "user") and hasattr(request.user, "pk") and request.user.pk:
            context["user_id"] = str(request.user.pk)

        # session_id
        session = getattr(request, "session", None)
        if session and hasattr(session, "session_key") and session.session_key:
            context["session_id"] = session.session_key

        # client_ip
        context["client_ip"] = self._get_client_ip(request)

        # trace_id (fallback — set earlier by trace_id_middleware)
        trace_id = getattr(request, "trace_id", None)
        if trace_id:
            context["trace_id"] = trace_id

        return self.resolve_cell_id(context)

    @staticmethod
    def _get_client_ip(request: Any) -> str:
        """Extract client IP (canonical resolution: XFF -> X-Real-IP -> REMOTE_ADDR)."""
        return cast(str, extract_client_ip(request, default="unknown"))
