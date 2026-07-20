"""
Cell Tagging & Baggage Sync Django Middlewares.

CellTaggingMiddleware:
    Adds a cell_id attribute to the HTTP request and sets it on a ContextVar
    so that the service layer can access it too.
    When an incoming cell_id propagation is accepted, performs Trust Boundary
    (CIDR) validation and Topology Mismatch (Registry validity) validation.

BaggageSyncMiddleware:
    Bidirectional ContextVar ↔ OTel Baggage sync.
    Inbound: Baggage → ContextVar restore
    Outbound: ContextVar → Baggage sync (auto-propagated to outgoing requests)

Enabling:
    BALDUR_CELL_TOPOLOGY_ENABLED=true
    BALDUR_CELL_TAGGING_ENABLED=true

MIDDLEWARE configuration:
    "baldur.api.django.cell.middleware.CellTaggingMiddleware"
    → place after AuthenticationMiddleware, before BaggageSyncMiddleware
    "baldur.api.django.cell.middleware.BaggageSyncMiddleware"
    → place directly after CellTaggingMiddleware
"""

from __future__ import annotations

import time
from ipaddress import ip_address, ip_network
from typing import Any, cast

import structlog
from django.http import HttpRequest, HttpResponse

logger = structlog.get_logger()

# CIDR cache refresh interval (seconds) — picks up Settings changes without
# restarting the WSGI process
_TRUSTED_CIDRS_CACHE_TTL_SECONDS = 300.0

# Topology Mismatch counter singleton — prevents duplicate registration
_topology_mismatch_counter = None


class CellTaggingMiddleware:
    """
    Django middleware that tags the request with a cell_id.

    Toggle pattern: same as TieringMiddleware
    - BALDUR_CELL_TOPOLOGY_ENABLED=false → immediate pass-through
    - BALDUR_CELL_TAGGING_ENABLED=false → immediate pass-through

    Accepting an incoming cell_id propagation:
    - Reads the incoming cell_id from the ContextVar restored by
      BaggageSyncMiddleware
    - Accepted after CIDR-based Trust validation and CellRegistry Topology
      validation pass
    - Falls back to local hashing when validation fails

    ContextVar propagation:
    - Sets both the request.cell_id attribute and the _current_cell_id ContextVar
    - ContextVar is automatically restored when the request ends (token.reset)
    """

    def __init__(self, get_response: Any):
        self.get_response = get_response
        self._tagger: Any = None
        self._trusted_cidrs: list[str] | None = None
        self._trusted_cidrs_loaded_at: float = 0.0

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not self._check_enabled():
            return cast(HttpResponse, self.get_response(request))

        # 317: Regional Isolation — block traffic from an isolated region
        isolation_response = self._check_regional_isolation(request)
        if isolation_response is not None:
            return isolation_response

        from baldur.context.cell_context import _current_cell_id

        tagger = self._get_tagger()

        # Accept incoming cell_id propagation (Trust + Topology validation)
        cell_id = self._accept_incoming_cell_id(request)

        if cell_id is None:
            # No incoming propagation, or validation failed → local hashing
            cell_id = tagger.resolve_cell_id_from_request(request)

        # Add the cell_id attribute to the request
        request.cell_id = cell_id  # type: ignore[attr-defined]

        # Set the ContextVar — accessible from the service layer and at Celery
        # publish time
        token = _current_cell_id.set(cell_id)

        try:
            response: HttpResponse = self.get_response(request)
        finally:
            # Restore the ContextVar when the request ends (actor_context pattern)
            _current_cell_id.reset(token)

        # Add Cell info to the response headers (for debugging)
        response["X-Cell-Id"] = cell_id

        return response

    # =========================================================================
    # Accepting an incoming cell_id propagation
    # =========================================================================

    def _accept_incoming_cell_id(self, request: HttpRequest) -> str | None:
        """
        Accept or reject the incoming cell_id after Trust and Topology validation.

        Returns:
            A valid cell_id, or None (fall back to local hashing)
        """
        from baldur.context.cell_context import get_current_cell_id

        incoming_cell_id = get_current_cell_id()

        if not incoming_cell_id:
            return None

        # CIDR Trust validation — check the source is a trusted internal network
        if not self._is_trusted_source(request):
            logger.debug(
                "cell_middleware.untrusted_source",
                incoming_cell_id=incoming_cell_id,
            )
            return None

        # Topology Mismatch validation — check the Cell is valid in the local
        # Registry
        return self._validate_cell_id(incoming_cell_id)

    def _is_trusted_source(self, request: HttpRequest) -> bool:
        """
        Validate via CIDR whether the request source is a trusted internal network.

        IP extraction uses extract_client_ip(), which resolves in the order
        X-Forwarded-For, X-Real-IP, REMOTE_ADDR.
        """
        from baldur.utils.network import extract_client_ip

        client_ip = extract_client_ip(request)
        if not client_ip:
            return False

        try:
            addr = ip_address(client_ip)
            return any(
                addr in ip_network(cidr, strict=False)
                for cidr in self._get_trusted_cidrs()
            )
        except ValueError:
            return False

    def _get_trusted_cidrs(self) -> list[str]:
        """Lazy-load trusted_source_cidrs (TTL-based refresh)."""
        now = time.monotonic()
        if (
            self._trusted_cidrs is None
            or now - self._trusted_cidrs_loaded_at > _TRUSTED_CIDRS_CACHE_TTL_SECONDS
        ):
            from baldur.settings.cell_topology import get_cell_topology_settings

            settings = get_cell_topology_settings()
            self._trusted_cidrs = settings.trusted_source_cidrs
            self._trusted_cidrs_loaded_at = now
        return self._trusted_cidrs

    def _validate_cell_id(self, incoming_cell_id: str) -> str | None:
        """
        Validate that the incoming cell_id is valid in the local CellRegistry.

        Validation criteria:
        1. The Cell exists in the Registry (get_cell_info != None)
        2. Its state is ACTIVE or WARMUP (DRAINING/ISOLATED are rejected to
           prevent bypassing the isolation policy)

        On validation failure, increments the topology_mismatch metric and logs.
        """
        from baldur.services.cell_topology.models import CellState
        from baldur.services.cell_topology.registry import get_cell_registry

        registry = get_cell_registry()
        cell_info = registry.get_cell_info(incoming_cell_id)

        if cell_info is None:
            self._record_topology_mismatch(incoming_cell_id, "cell_not_found")
            logger.warning(
                "cell_middleware.topology_mismatch_detected",
                incoming_cell_id=incoming_cell_id,
                reason="cell_not_found",
                cell_count=len(registry.get_all_cells()),
            )
            return None

        if cell_info.state not in (CellState.ACTIVE, CellState.WARMUP):
            self._record_topology_mismatch(incoming_cell_id, "cell_not_active")
            logger.warning(
                "cell_middleware.topology_mismatch_detected",
                incoming_cell_id=incoming_cell_id,
                reason="cell_not_active",
                cell_state=cell_info.state.value,
            )
            return None

        return incoming_cell_id

    @staticmethod
    def _record_topology_mismatch(incoming_cell_id: str, reason: str) -> None:
        """Record the Topology Mismatch Prometheus counter (module singleton)."""
        global _topology_mismatch_counter  # noqa: PLW0603
        try:
            if _topology_mismatch_counter is None:
                from baldur.metrics.drift_metrics import _get_or_create_counter

                _topology_mismatch_counter = _get_or_create_counter(
                    "baldur_cell_topology_mismatch_total",
                    "Cell topology mismatch between upstream and local registry",
                    ["incoming_cell_id", "reason"],
                )
            if _topology_mismatch_counter is not None:
                _topology_mismatch_counter.labels(
                    incoming_cell_id=incoming_cell_id,
                    reason=reason,
                ).inc()
        except Exception:
            pass  # A metric failure must not abort the request

    # =========================================================================
    # Existing helpers
    # =========================================================================

    def _check_enabled(self) -> bool:
        """Check the toggles."""
        try:
            from django.conf import settings as django_settings

            if not getattr(django_settings, "BALDUR_CELL_TOPOLOGY_ENABLED", False):
                return False
            return getattr(django_settings, "BALDUR_CELL_TAGGING_ENABLED", False)
        except Exception:
            return False

    def _get_tagger(self) -> Any:
        """Lazy-load the CellTagger."""
        if self._tagger is None:
            from baldur.services.cell_topology.tagger import CellTagger

            self._tagger = CellTagger()
        return self._tagger

    # =========================================================================
    # 317: Regional Isolation Gate
    # =========================================================================

    @staticmethod
    def _check_regional_isolation(request: HttpRequest) -> HttpResponse | None:
        """317: Return 503 if the current region is isolated."""
        try:
            from django.conf import settings as django_settings

            if not getattr(
                django_settings,
                "BALDUR_REGIONAL_ISOLATION_ENABLED",
                False,
            ):
                return None

            from baldur.services.isolation.regional_gate import (
                get_regional_isolation_gate,
            )

            gate = get_regional_isolation_gate()
            is_isolated, reason = gate.is_current_region_isolated()

            if is_isolated:
                from django.http import JsonResponse

                logger.warning(
                    "cell_middleware.regional_isolation_active",
                    reason=reason,
                )
                return JsonResponse(
                    {
                        "error": "service_unavailable",
                        "reason": "regional_isolation",
                        "detail": reason,
                    },
                    status=503,
                )
        except ImportError:
            pass
        except Exception as e:
            logger.debug(
                "cell_middleware.regional_isolation_check_failed",
                error=e,
            )

        return None


class BaggageSyncMiddleware:
    """
    Middleware for bidirectional ContextVar ↔ OTel Baggage sync.

    Execution order:
    1. Inbound: Baggage parsed by DjangoInstrumentor → ContextVar restore
    2. Outbound: ContextVar values → OTel Baggage sync

    Placement: directly after CellTaggingMiddleware
    - Must run after every ContextVar is set so Baggage reflects the latest values
    - try/finally guarantees isolation of the OTel Context token
    """

    def __init__(self, get_response: Any):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        from baldur.observability.baggage import (
            detach_baggage_token,
            restore_contextvars_from_baggage,
            sync_contextvars_to_baggage,
        )

        # Inbound: Baggage → ContextVar restore
        restore_contextvars_from_baggage()

        # Outbound: ContextVar → Baggage sync
        token = sync_contextvars_to_baggage()
        try:
            response: HttpResponse = self.get_response(request)
        finally:
            # Restore the OTel Context when the request ends — prevents leaks
            detach_baggage_token(token)
        return response
