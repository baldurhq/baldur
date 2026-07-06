"""Drain-aware middleware (impl 471).

Returns 503 + ``Retry-After`` + ``Connection: close`` to clients while the
:class:`GracefulShutdownCoordinator` is in the DRAINING phase, except for
liveness-probe paths that must continue to return 200 so k8s does not
SIGKILL the pod mid-drain.

Pattern source: ``baldur.api.django.middleware.backpressure``.
The drain extension adds ``Connection: close`` because L7 LB pools
(envoy/nginx/GCLB/ALB) keep dispatching to the same worker socket after
a 503 alone (RFC 7230 §6.6 keep-alive semantics).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = structlog.get_logger()

# Baldur-canonical liveness probe paths (urls/health.py: LivenessView,
# simple_health_ping). These stay 200 during DRAINING regardless of
# operator-supplied overrides. Drain is a normal lifecycle phase, not a
# liveness failure — converting it to 503 would cause k8s to SIGKILL the
# pod mid-drain.
_CANONICAL_LIVENESS_PATHS: tuple[str, ...] = (
    "/api/baldur/health/live/",
    "/api/baldur/health/ping/",
)


class DrainAwareMiddleware:
    """Reject new requests with 503 while the coordinator is DRAINING.

    Settings (BALDUR_RECOVERY_SHUTDOWN_*):
        BALDUR_RECOVERY_SHUTDOWN_DRAIN_LIVENESS_PATHS: extra liveness paths to exempt
        BALDUR_RECOVERY_SHUTDOWN_DRAIN_DEFAULT_RETRY_AFTER_SECONDS: fallback Retry-After
            value when the coordinator is in TERMINATING/TERMINATED (where
            ``remaining_drain_time`` is None).

    Order: must sit immediately after ``HealthBridgeMiddleware`` so the
    bridge endpoints can apply their own ``status: draining`` payload.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator

        self._coordinator = get_shutdown_coordinator()

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if self._coordinator.is_accepting_requests():
            return self.get_response(request)

        if self._is_liveness_path(request.path):
            return self.get_response(request)

        return self._build_drain_response()

    def _is_liveness_path(self, path: str) -> bool:
        from baldur.settings.recovery_shutdown import (
            get_recovery_shutdown_settings,
        )

        if path in _CANONICAL_LIVENESS_PATHS:
            return True
        overrides = get_recovery_shutdown_settings().drain_liveness_paths
        return path in overrides

    def _retry_after_seconds(self) -> int:
        from baldur.settings.recovery_shutdown import (
            get_recovery_shutdown_settings,
        )

        stats = self._coordinator.get_stats()
        remaining = stats.remaining_drain_time
        if remaining is None:
            remaining = (
                get_recovery_shutdown_settings().drain_default_retry_after_seconds
            )
        # Always clamp >= 1 so the LB never receives Retry-After: 0.
        return max(1, int(remaining))

    def _build_drain_response(self) -> HttpResponse:
        from django.http import HttpResponse

        retry_after = self._retry_after_seconds()
        logger.info(
            "drain_aware_middleware.request_rejected",
            phase=self._coordinator.phase.value,
            retry_after_seconds=retry_after,
        )
        return HttpResponse(
            content="Service draining for shutdown.",
            status=503,
            content_type="text/plain; charset=utf-8",
            headers={
                "Retry-After": str(retry_after),
                "Connection": "close",
            },
        )
