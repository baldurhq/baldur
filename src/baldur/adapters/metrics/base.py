"""
Metric Source Adapter Base Interface.

Provides an abstract interface for collecting metrics from various sources
without direct dependency on user's database schema.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable


@runtime_checkable
class MetricSourceAdapter(Protocol):
    """
    Metric source adapter interface.

    Users implement this interface to supply metric values from their own data
    source. Any source works: DB, cache, external API, and so on.

    Design Principles:
    - Zero DB Dependency: no direct dependency on the user's DB schema
    - Plug & Play: works without Redis, minimal infrastructure dependency

    Example:
        >>> class MyAdapter:
        ...     def get_dlq_pending_count(self, domain: str) -> int:
        ...         return MyDLQModel.objects.filter(domain=domain, status='pending').count()
        ...
        ...     def get_dlq_count_by_status(self, status: str) -> int:
        ...         return MyDLQModel.objects.filter(status=status).count()
        ...
        ...     def get_circuit_breaker_state(self, service: str) -> str:
        ...         return "closed"
        ...
        ...     def get_retry_success_rate(self, domain: str) -> float:
        ...         return 95.0
    """

    def get_dlq_pending_count(self, domain: str) -> int:
        """
        Return the number of pending DLQ entries for a domain.

        Args:
            domain: domain name (payment, point, inventory, etc.)

        Returns:
            Number of pending DLQ entries
        """
        ...

    def get_dlq_count_by_status(self, status: str) -> int:
        """
        Return the number of DLQ entries in a given status.

        Args:
            status: status (pending, resolved, failed, etc.)

        Returns:
            Number of DLQ entries in that status
        """
        ...

    def get_circuit_breaker_state(self, service: str) -> str:
        """
        Return the Circuit Breaker state of a service.

        Args:
            service: service name

        Returns:
            State string (closed, open, half_open)
        """
        ...

    def get_retry_success_rate(self, domain: str) -> float:
        """
        Return the retry success rate for a domain.

        Args:
            domain: domain name

        Returns:
            Success rate (0.0 ~ 100.0)
        """
        ...


class BaseMetricSourceAdapter(ABC):
    """
    Base class for metric source adapters.

    Provides a default implementation or an exception for every method.
    Concrete adapters subclass this and override only what they need.
    """

    @abstractmethod
    def get_dlq_pending_count(self, domain: str) -> int:
        """Return the number of pending DLQ entries for a domain."""
        raise NotImplementedError

    @abstractmethod
    def get_dlq_count_by_status(self, status: str) -> int:
        """Return the number of DLQ entries in a given status."""
        raise NotImplementedError

    def get_circuit_breaker_state(self, service: str) -> str:
        """Return the Circuit Breaker state of a service. Default: closed."""
        return "closed"

    def get_retry_success_rate(self, domain: str) -> float:
        """Return the retry success rate for a domain. Default: 0.0."""
        return 0.0


class NullMetricSourceAdapter(BaseMetricSourceAdapter):
    """
    No-op metric source adapter.

    Used when no adapter has been configured.
    Every method returns a default value (0, "closed").
    """

    def get_dlq_pending_count(self, domain: str) -> int:
        """Always returns 0."""
        return 0

    def get_dlq_count_by_status(self, status: str) -> int:
        """Always returns 0."""
        return 0

    def get_circuit_breaker_state(self, service: str) -> str:
        """Always returns 'closed'."""
        return "closed"

    def get_retry_success_rate(self, domain: str) -> float:
        """Always returns 0.0."""
        return 0.0


__all__ = [
    "MetricSourceAdapter",
    "BaseMetricSourceAdapter",
    "NullMetricSourceAdapter",
]
