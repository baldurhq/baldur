"""
Bulkhead Exceptions — resource-isolation exception classes.

Hierarchy:
- BulkheadFullError: concurrent-execution limit exceeded
- BulkheadTimeoutError: bulkhead-managed call exceeded its timeout budget
- BulkheadNotFoundError: requested domain has not been provisioned
"""

from __future__ import annotations

from baldur.core.exceptions import ResilienceError, TimeoutPolicyError
from baldur.interfaces.resilience_policy import PolicyRejectedException


class BulkheadError(ResilienceError):
    """Base exception for bulkhead-related failures."""

    def extra_context(self) -> dict:
        """Return structlog-bindable context for bulkhead errors."""
        return {}


class BulkheadFullError(PolicyRejectedException, BulkheadError):
    """
    Raised when the bulkhead has no permits available and a new call is rejected.

    Multi-inherits ``PolicyRejectedException`` so the outer
    ``PolicyComposer`` catch hierarchy classifies bulkhead rejection as
    ``PolicyOutcome.REJECTED`` rather than the generic ``except Exception``
    branch (which would mislabel them as FAILURE).
    """

    def __init__(
        self,
        bulkhead_name: str,
        max_concurrent: int,
        active_count: int,
    ):
        """
        Args:
            bulkhead_name: bulkhead identifier
            max_concurrent: maximum allowed concurrent executions
            active_count: number of executions currently active
        """
        self.bulkhead_name = bulkhead_name
        self.max_concurrent = max_concurrent
        self.active_count = active_count
        super().__init__(
            f"Bulkhead '{bulkhead_name}' is full: "
            f"{active_count}/{max_concurrent} active"
        )

    def extra_context(self) -> dict:
        """Return structlog-bindable context for bulkhead full errors."""
        return {
            "bulkhead_name": self.bulkhead_name,
            "max_concurrent": self.max_concurrent,
            "active_count": self.active_count,
        }


class BulkheadTimeoutError(TimeoutPolicyError, BulkheadError):
    """
    Raised when a bulkhead-managed call exceeds its timeout budget.

    Emitted by ThreadPoolBulkhead when the wrapped task does not complete
    within the configured timeout. Multi-inherits ``TimeoutPolicyError`` so
    the outer ``PolicyComposer`` catch hierarchy classifies the failure as
    ``PolicyOutcome.TIMEOUT`` (instead of funneling into the generic
    ``except Exception`` branch as FAILURE).
    """

    def __init__(self, bulkhead_name: str, timeout: float):
        """
        Args:
            bulkhead_name: bulkhead identifier
            timeout: configured timeout in seconds
        """
        self.bulkhead_name = bulkhead_name
        self.timeout = timeout
        super().__init__(
            timeout_seconds=timeout,
            message=f"Bulkhead '{bulkhead_name}' timed out after {timeout}s",
        )

    def extra_context(self) -> dict:
        """Return structlog-bindable context for bulkhead timeout errors."""
        return {
            "bulkhead_name": self.bulkhead_name,
            "timeout": self.timeout,
        }


class BulkheadNotFoundError(BulkheadError, KeyError):
    """
    Raised when a bulkhead is requested for a domain that has not been provisioned.

    Custom domains must be provisioned (via ``register()``, ``get_or_create()``,
    or a policy/settings helper) before a ``@bulkhead``-decorated function is
    first called, on both sync and async callees.

    Multi-inherits ``KeyError`` so the registry's long-standing not-found contract
    stays intact for existing ``except KeyError`` consumers (the traffic-gate
    skip-degradation path, the admin API 404 mapping), while ``except BulkheadError``
    / ``except BaldurError`` also classify it. Multi-inheritance precedent:
    ``BulkheadFullError(PolicyRejectedException, BulkheadError)``.

    ``__str__`` is overridden because ``KeyError.__str__`` would otherwise
    repr-quote the message (the base hierarchy defines no ``__str__``), mangling
    the actionable text into ``"Bulkhead not found: ..."`` with surrounding quotes.
    """

    def __init__(self, bulkhead_name: str, registered_names: list[str]):
        """
        Args:
            bulkhead_name: the requested, unregistered domain
            registered_names: currently registered compartment names
        """
        self.bulkhead_name = bulkhead_name
        self.registered_names = list(registered_names)
        registered_repr = ", ".join(sorted(self.registered_names)) or "(none)"
        super().__init__(
            f"Bulkhead not found: '{bulkhead_name}'. "
            f"Registered compartments: {registered_repr}. "
            f"Provision it via register() or get_or_create() before use."
        )

    def __str__(self) -> str:
        """Render the actionable message plainly (KeyError.__str__ repr-quotes)."""
        return str(self.args[0]) if self.args else ""

    def extra_context(self) -> dict:
        """Return structlog-bindable context for bulkhead not-found errors."""
        return {
            "bulkhead_name": self.bulkhead_name,
            "registered_names": self.registered_names,
        }
