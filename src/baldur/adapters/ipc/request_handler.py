"""
IPC request handler - wraps existing services.

Provides a request-processing and service-routing layer shared by the
UDS server and the gRPC server.

Features:
- Lazy loading of service instances
- Plugin-style handler registration
- Common error handling

Usage:
    from baldur.adapters.ipc.request_handler import RequestHandler

    handler = RequestHandler()

    # Circuit Breaker requests
    result = handler.handle(
        "circuit_breaker.should_allow",
        {"service_name": "payment_gateway"}
    )

    # DLQ store requests
    result = handler.handle(
        "dlq.store",
        {"domain": "order", "failure_type": "timeout", ...}
    )
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog

from baldur.adapters.ipc.exceptions import (
    IPCInvalidParamsError,
    IPCMethodNotFoundError,
    IPCServiceUnavailableError,
)
from baldur.utils.serialization import fast_loads

logger = structlog.get_logger()


class RequestHandler:
    """
    Routes IPC requests to existing baldur services.

    Shared by both UDS and gRPC; extensible by registering handlers
    plugin-style.
    """

    def __init__(self):
        """Initialize the handler."""
        # Service instances (lazy loading)
        self._cb_service: Any = None
        self._dlq_service: Any = None
        self._learning_service: Any = None

        # Handler registry
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        self._register_default_handlers()

    # =========================================================================
    # Service Lazy Loading
    # =========================================================================

    @property
    def cb_service(self) -> Any:
        """CircuitBreakerService instance."""
        if self._cb_service is None:
            try:
                from baldur.services import get_circuit_breaker_service

                self._cb_service = get_circuit_breaker_service()
            except ImportError as e:
                logger.warning(
                    "request_handler.circuitbreakerservice_available",
                    error=e,
                )
        return self._cb_service

    @property
    def dlq_service(self) -> Any:
        """DLQService instance."""
        if self._dlq_service is None:
            from baldur.factory.registry import ProviderRegistry

            self._dlq_service = ProviderRegistry.dlq_service.safe_get()
            if self._dlq_service is None:
                logger.warning(
                    "request_handler.dlqservice_available",
                    error="baldur_pro DLQService not registered",
                )
        return self._dlq_service

    @property
    def learning_service(self) -> Any:
        """LearningService instance (599 D7 — registry slot resolution)."""
        if self._learning_service is None:
            from baldur.factory.registry import ProviderRegistry

            self._learning_service = ProviderRegistry.learning_service.safe_get()
            if self._learning_service is None:
                logger.warning(
                    "request_handler.learningservice_available",
                    error="baldur_dormant LearningService not registered",
                )
        return self._learning_service

    # =========================================================================
    # Handler Registration
    # =========================================================================

    def _register_default_handlers(self) -> None:
        """Register the default handlers."""
        self._handlers = {
            # Circuit Breaker
            "circuit_breaker.should_allow": self._cb_should_allow,
            "circuit_breaker.should_allow_batch": self._cb_should_allow_batch,
            "circuit_breaker.get_state": self._cb_get_state,
            "circuit_breaker.get_all_states": self._cb_get_all_states,
            "circuit_breaker.force_open": self._cb_force_open,
            "circuit_breaker.force_close": self._cb_force_close,
            # DLQ
            "dlq.store": self._dlq_store,
            "dlq.is_enabled": self._dlq_is_enabled,
            "dlq.get_entry": self._dlq_get_entry,
            "dlq.list": self._dlq_list,
            # Learning
            "learning.get_suggestions": self._learning_get_suggestions,
            "learning.record_success": self._learning_record_success,
            "learning.record_failure": self._learning_record_failure,
            # Health
            "health.check": self._health_check,
        }

    def register_handler(
        self,
        method: str,
        handler: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        """
        Register a custom handler.

        Args:
            method: method name (e.g. "custom.my_method")
            handler: handler function
        """
        self._handlers[method] = handler
        logger.debug(
            "cell_registry.bulkheads_registered",
            method=method,
        )

    def get_registered_methods(self) -> list[str]:
        """Return the list of registered methods."""
        return list(self._handlers.keys())

    # =========================================================================
    # Request Handling
    # =========================================================================

    def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Process and route a request.

        Args:
            method: JSON-RPC method name
            params: request parameters

        Returns:
            result dictionary

        Raises:
            IPCMethodNotFoundError: when the method cannot be found
            IPCInvalidParamsError: when the parameters are invalid
        """
        handler = self._handlers.get(method)
        if handler is None:
            raise IPCMethodNotFoundError(method)

        try:
            return handler(params)
        except IPCMethodNotFoundError:
            raise
        except IPCInvalidParamsError:
            raise
        except KeyError as e:
            raise IPCInvalidParamsError(
                f"Missing required parameter: {e}", param_name=str(e)
            ) from e
        except ValueError as e:
            raise IPCInvalidParamsError(str(e)) from e

    # =========================================================================
    # Circuit Breaker Handlers
    # =========================================================================

    def _cb_should_allow(self, params: dict[str, Any]) -> dict[str, Any]:
        """Check whether a circuit breaker allows the request."""
        service_name = params.get("service_name")
        if not service_name:
            raise IPCInvalidParamsError(
                "service_name is required", param_name="service_name"
            )

        if self.cb_service is None:
            # Fail-open: allow when the service is unavailable
            return {"allowed": True, "state": "closed"}

        allowed = self.cb_service.should_allow(service_name)
        state = self.cb_service.get_state(service_name)

        return {
            "allowed": allowed,
            "state": state,
        }

    def _cb_should_allow_batch(self, params: dict[str, Any]) -> dict[str, Any]:
        """Batch-check request admission across multiple services."""
        service_names = params.get("service_names", [])
        if not service_names:
            return {"results": {}}

        # Cap at 100 entries
        service_names = service_names[:100]
        results = {}

        for name in service_names:
            results[name] = self._cb_should_allow({"service_name": name})

        return {"results": results}

    def _cb_get_state(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get circuit breaker state."""
        service_name = params.get("service_name")
        if not service_name:
            raise IPCInvalidParamsError(
                "service_name is required", param_name="service_name"
            )

        if self.cb_service is None:
            return {"service_name": service_name, "state": "closed"}

        state_data = self.cb_service.get_or_create_state(service_name)
        return {
            "service_name": service_name,
            "state": state_data.state,
            "failure_count": getattr(state_data, "failure_count", 0),
            "success_count": getattr(state_data, "success_count", 0),
            "opened_at": (
                state_data.opened_at.isoformat()
                if getattr(state_data, "opened_at", None)
                else None
            ),
            "controlled_by": getattr(state_data, "controlled_by", None),
            "reason": getattr(state_data, "reason", None),
        }

    def _cb_get_all_states(self, params: dict[str, Any]) -> dict[str, Any]:
        """List all circuit breaker states."""
        if self.cb_service is None:
            return {"states": []}

        states = self.cb_service.get_all_states()
        return {"states": states}

    def _cb_force_open(self, params: dict[str, Any]) -> dict[str, Any]:
        """Force-open a circuit breaker (block requests)."""
        service_name = params.get("service_name")
        if not service_name:
            raise IPCInvalidParamsError(
                "service_name is required", param_name="service_name"
            )

        requester = params.get("controlled_by", "sidecar")
        reason = params.get("reason", "IPC request")
        # Include requester in reason for audit trail (ActorContext is SYSTEM_ACTOR for IPC)
        full_reason = f"{reason} (via {requester})"

        if self.cb_service is None:
            raise IPCServiceUnavailableError("circuit_breaker")

        result = self.cb_service.force_open(
            service_name=service_name,
            reason=full_reason,
        )

        return {
            "success": result.success,
            "message": result.message,
            "state": result.new_state,
        }

    def _cb_force_close(self, params: dict[str, Any]) -> dict[str, Any]:
        """Force-close a circuit breaker (allow requests)."""
        service_name = params.get("service_name")
        if not service_name:
            raise IPCInvalidParamsError(
                "service_name is required", param_name="service_name"
            )

        requester = params.get("controlled_by", "sidecar")
        reason = params.get("reason", "IPC request")
        trigger_replay = params.get("trigger_replay", False)
        # Include requester in reason for audit trail (ActorContext is SYSTEM_ACTOR for IPC)
        full_reason = f"{reason} (via {requester})"

        if self.cb_service is None:
            raise IPCServiceUnavailableError("circuit_breaker")

        result = self.cb_service.force_close(
            service_name=service_name,
            reason=full_reason,
            trigger_replay=trigger_replay,
        )

        return {
            "success": result.success,
            "message": result.message,
            "state": result.new_state,
        }

    # =========================================================================
    # DLQ Handlers
    # =========================================================================

    def _dlq_store(self, params: dict[str, Any]) -> dict[str, Any]:
        """Store a failed operation in the DLQ."""
        domain = params.get("domain")
        failure_type = params.get("failure_type")
        error_message = params.get("error_message", "")

        if not domain:
            raise IPCInvalidParamsError("domain is required", param_name="domain")
        if not failure_type:
            raise IPCInvalidParamsError(
                "failure_type is required", param_name="failure_type"
            )

        if self.dlq_service is None:
            raise IPCServiceUnavailableError("dlq")

        # Handle JSON bytes
        snapshot_data = params.get("snapshot_data", {})
        if isinstance(snapshot_data, bytes):
            snapshot_data = fast_loads(snapshot_data)

        request_data = params.get("request_data")
        if isinstance(request_data, bytes):
            request_data = fast_loads(request_data)

        result = self.dlq_service.store_with_snapshot(
            domain=domain,
            failure_type=failure_type,
            error_code=params.get("error_code", ""),
            error_message=error_message,
            snapshot_data=snapshot_data,
            request_data=request_data,
            entity_type=params.get("entity_type"),
            entity_id=params.get("entity_id"),
            user_id=params.get("user_id"),
            max_retries=params.get("max_retries"),
        )

        return {
            "success": result.success,
            "dlq_id": result.entry_id,
            "message": result.message,
        }

    def _dlq_is_enabled(self, params: dict[str, Any]) -> dict[str, Any]:
        """Check whether the DLQ is enabled."""
        if self.dlq_service is None:
            return {"enabled": False}
        return {"enabled": self.dlq_service.is_enabled}

    def _dlq_get_entry(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get a DLQ entry."""
        entry_id = params.get("entry_id")
        if not entry_id:
            raise IPCInvalidParamsError("entry_id is required", param_name="entry_id")

        if self.dlq_service is None:
            raise IPCServiceUnavailableError("dlq")

        entry = self.dlq_service.get_entry(entry_id)
        if entry is None:
            return {"error": f"Entry not found: {entry_id}"}

        return {
            "id": entry.id,
            "domain": entry.domain,
            "failure_type": entry.failure_type,
            "error_message": entry.error_message,
            "status": entry.status,
            "retry_count": entry.retry_count,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
        }

    def _dlq_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """List DLQ entries."""
        if self.dlq_service is None:
            return {"entries": [], "total_count": 0}

        # Build filter parameters matching the real service interface
        filters: dict[str, Any] = {}
        if params.get("domain"):
            filters["domain"] = params["domain"]
        if params.get("status"):
            filters["status"] = params["status"]

        page = params.get("page", 1)
        page_size = params.get("page_size", 20)

        result = self.dlq_service.list_entries(
            filters=filters,
            page=page,
            page_size=page_size,
        )

        # Convert results
        entries = result.get("results", [])
        return {
            "entries": [
                {
                    "id": e.get("id")
                    if isinstance(e, dict)
                    else getattr(e, "id", None),
                    "domain": e.get("domain")
                    if isinstance(e, dict)
                    else getattr(e, "domain", None),
                    "failure_type": (
                        e.get("failure_type")
                        if isinstance(e, dict)
                        else getattr(e, "failure_type", None)
                    ),
                    "status": e.get("status")
                    if isinstance(e, dict)
                    else getattr(e, "status", None),
                    "created_at": (
                        e.get("created_at")
                        if isinstance(e, dict)
                        else (
                            e.created_at.isoformat()
                            if hasattr(e, "created_at") and e.created_at
                            else None
                        )
                    ),
                }
                for e in entries
            ],
            "total_count": result.get("total_count", len(entries)),
        }

    # =========================================================================
    # Learning Handlers
    # =========================================================================

    def _learning_get_suggestions(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get learning-based improvement suggestions."""
        if self.learning_service is None:
            return {"suggestions": []}

        # Real LearningService interface parameters
        service_name = params.get("service_name")
        unapplied_only = params.get("unapplied_only", False)

        suggestions = self.learning_service.get_suggestions(
            service_name=service_name,
            unapplied_only=unapplied_only,
        )
        return {
            "suggestions": [
                {
                    "id": getattr(s, "id", None),
                    "type": getattr(s, "suggestion_type", getattr(s, "type", None)),
                    "description": getattr(s, "description", ""),
                    "priority": (
                        getattr(s.priority, "value", s.priority)
                        if hasattr(s, "priority")
                        else None
                    ),
                    "confidence": getattr(s, "confidence", None),
                }
                for s in suggestions
            ]
        }

    def _learning_record_success(self, params: dict[str, Any]) -> dict[str, Any]:
        """Record a success pattern."""
        pattern_type = params.get("pattern_type")
        if not pattern_type:
            raise IPCInvalidParamsError(
                "pattern_type is required", param_name="pattern_type"
            )

        if self.learning_service is None:
            return {"recorded": False}

        context = params.get("context", {})
        if isinstance(context, bytes):
            context = fast_loads(context)

        self.learning_service.record_success(pattern_type, context)
        return {"recorded": True}

    def _learning_record_failure(self, params: dict[str, Any]) -> dict[str, Any]:
        """Record a failure pattern."""
        pattern_type = params.get("pattern_type")
        if not pattern_type:
            raise IPCInvalidParamsError(
                "pattern_type is required", param_name="pattern_type"
            )

        if self.learning_service is None:
            return {"recorded": False}

        context = params.get("context", {})
        if isinstance(context, bytes):
            context = fast_loads(context)

        error = params.get("error", "")
        self.learning_service.record_failure(pattern_type, context, error)
        return {"recorded": True}

    # =========================================================================
    # Health Handlers
    # =========================================================================

    def _health_check(self, params: dict[str, Any]) -> dict[str, Any]:
        """Check sidecar health."""
        components = {
            "circuit_breaker": self.cb_service is not None,
            "dlq": self.dlq_service is not None,
            "learning": self.learning_service is not None,
        }

        all_healthy = any(components.values())
        status = "SERVING" if all_healthy else "NOT_SERVING"

        return {
            "status": status,
            "components": components,
        }


from baldur.utils.singleton import make_singleton_factory

get_request_handler, configure_request_handler, reset_request_handler = (
    make_singleton_factory("request_handler", RequestHandler)
)
