"""
Circuit Breaker Exceptions

Defines the general-purpose exception types used by the Circuit Breaker Policy.
"""

from __future__ import annotations

from baldur.core.exceptions import CircuitBreakerError
from baldur.interfaces.resilience_policy import PolicyRejectedException


class CircuitBreakerOpenError(PolicyRejectedException, CircuitBreakerError):
    """Raised when a request is rejected because the Circuit Breaker is OPEN.

    Multi-inherits ``PolicyRejectedException`` so the outer ``PolicyComposer``
    catch hierarchy classifies CB rejections as ``PolicyOutcome.REJECTED``
    rather than funneling into the generic ``except Exception`` branch (which
    would mislabel them as FAILURE). The MRO resolves ``__init__`` via
    ``BaldurError`` so ``self.code`` and the ``message`` argument behave
    unchanged.

    Attributes:
        service_name: Identifier of the service whose CB is OPEN.
        dlq_capture_dispatched: ``True`` once a layer has handed this rejection
            to the DLQ store. Later capture layers in the same process read it
            off the propagating instance and skip their own store, so one
            rejected call yields one entry.
        dlq_id: Identifier of that entry when the store returned one
            synchronously. ``None`` on the async outbox path, which acks before
            the id exists — so a dedup check must read
            ``dlq_capture_dispatched``, never the truthiness of this field.
    """

    def __init__(self, service_name: str, message: str | None = None):
        self.service_name = service_name
        self.dlq_capture_dispatched = False
        self.dlq_id: str | None = None
        super().__init__(message or f"Circuit breaker '{service_name}' is OPEN")

    def mark_dlq_capture_dispatched(self, dlq_id: str | None = None) -> None:
        """Record that this rejection's DLQ store has been dispatched."""
        self.dlq_capture_dispatched = True
        if dlq_id is not None:
            self.dlq_id = dlq_id

    def extra_context(self) -> dict:
        return {"service_name": self.service_name}
