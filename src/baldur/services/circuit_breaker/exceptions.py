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
    """

    def __init__(self, service_name: str, message: str | None = None):
        self.service_name = service_name
        super().__init__(message or f"Circuit breaker '{service_name}' is OPEN")

    def extra_context(self) -> dict:
        return {"service_name": self.service_name}
