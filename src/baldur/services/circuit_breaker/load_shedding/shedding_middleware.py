"""
Load Shedding Middleware.

Applies the Load Shedding policy before request processing to limit traffic.

Usage:
    middleware = LoadSheddingMiddleware(manager)

    # Use it like a Django middleware
    def process_request(service_id, request):
        decision = middleware.process(service_id)
        if not decision.allow_request:
            return Response(status=503, detail=decision.reason)
        # normal processing
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from baldur.services.circuit_breaker.load_shedding.manager import (
        LoadSheddingManager,
    )
    from baldur.services.circuit_breaker.load_shedding.shedding_models import (
        SheddingDecision,
    )

logger = structlog.get_logger()


class LoadSheddingMiddleware:
    """
    Load Shedding Middleware.

    Applies the Load Shedding policy before request processing to limit traffic.
    """

    def __init__(
        self,
        manager: LoadSheddingManager | None = None,
        on_shed_callback: Callable[[str, SheddingDecision], None] | None = None,
    ):
        """
        Initialize.

        Args:
            manager: LoadSheddingManager instance (uses the singleton if absent)
            on_shed_callback: Callback when Shedding occurs (metrics, logging, etc.)
        """
        self._manager = manager
        self._on_shed_callback = on_shed_callback

    @property
    def manager(self) -> LoadSheddingManager:
        """Manager instance."""
        if self._manager is None:
            from . import (
                get_load_shedding_manager,
            )

            self._manager = get_load_shedding_manager()
        return self._manager

    def process(self, service_id: str) -> SheddingDecision:
        """
        Shedding decision for a request.

        Args:
            service_id: Service ID

        Returns:
            SheddingDecision: whether allowed and detailed info
        """
        decision = self.manager.should_allow_request(service_id)

        if decision.is_shed and self._on_shed_callback:
            try:
                self._on_shed_callback(service_id, decision)
            except Exception as e:
                logger.exception(
                    "load_shedding_middleware.callback_failed",
                    error=e,
                )

        return decision

    def record_result(self, service_id: str, success: bool) -> None:
        """
        Record the request result.

        Args:
            service_id: Service ID
            success: Whether it succeeded
        """
        if success:
            self.manager.record_success(service_id)
        else:
            self.manager.record_failure(service_id)
