# `dlq_model` / `cb_model` are user-provided concrete Django models. No
# abstract base is shipped for either, so django-stubs sees `type[Model]`
# (the framework abstract) and reports `.objects` as `[attr-defined]` —
# disable at the file level since every queryset call hits the same issue.
# mypy: disable-error-code="attr-defined"

"""
Django ORM-based Metric Source Adapter.

Provides metrics from Django models for the baldur system.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import structlog

from baldur.adapters.metrics.base import BaseMetricSourceAdapter
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from django.db.models import Model

logger = structlog.get_logger()


class DjangoMetricSourceAdapter(BaseMetricSourceAdapter):
    """
    Django ORM-based metric source adapter.

    Reads DLQ, circuit breaker, and related metrics from Django models.

    Example:
        >>> from myapp.models import DLQItem, CircuitBreakerState
        >>> adapter = DjangoMetricSourceAdapter(
        ...     dlq_model=DLQItem,
        ...     circuit_breaker_model=CircuitBreakerState,
        ... )
        >>> count = adapter.get_dlq_pending_count("payment")
    """

    def __init__(
        self,
        dlq_model: type[Model] | None = None,
        circuit_breaker_model: type[Model] | None = None,
        pending_status: str = "pending",
        domain_field: str = "domain",
        status_field: str = "status",
        service_name_field: str = "service_name",
        state_field: str = "state",
        resolved_at_field: str = "resolved_at",
        is_success_field: str = "is_success",
    ):
        """
        Initialize the Django adapter.

        Args:
            dlq_model: Django model class for DLQ items
            circuit_breaker_model: Django model class for circuit breaker states
            pending_status: Status value that indicates pending items
            domain_field: Field name for domain in DLQ model
            status_field: Field name for status in DLQ model
            service_name_field: Field name for service name in CB model
            state_field: Field name for state in CB model
            resolved_at_field: Field name for resolved timestamp
            is_success_field: Field name for success flag
        """
        self.dlq_model = dlq_model
        self.cb_model = circuit_breaker_model
        self.pending_status = pending_status
        self.domain_field = domain_field
        self.status_field = status_field
        self.service_name_field = service_name_field
        self.state_field = state_field
        self.resolved_at_field = resolved_at_field
        self.is_success_field = is_success_field

    def get_dlq_pending_count(self, domain: str) -> int:
        """
        Return the number of pending DLQ items for a domain.

        Args:
            domain: Domain name (payment, point, inventory, etc.)

        Returns:
            Number of pending DLQ items
        """
        if self.dlq_model is None:
            logger.debug("django_adapter.dlq_model_configured")
            return 0

        try:
            filter_kwargs = {
                self.domain_field: domain,
                self.status_field: self.pending_status,
            }
            return self.dlq_model.objects.filter(**filter_kwargs).count()
        except Exception as e:
            logger.warning(
                "django_adapter.get_dlq_pending_failed",
                error=e,
            )
            return 0

    def get_dlq_count_by_status(self, status: str) -> int:
        """
        Return the number of DLQ items in a given status.

        Args:
            status: Status (pending, resolved, failed, etc.)

        Returns:
            Number of DLQ items in that status
        """
        if self.dlq_model is None:
            logger.debug("django_adapter.dlq_model_configured")
            return 0

        try:
            filter_kwargs = {self.status_field: status}
            return self.dlq_model.objects.filter(**filter_kwargs).count()
        except Exception as e:
            logger.warning(
                "django_adapter.get_dlq_count_failed",
                error=e,
            )
            return 0

    def get_circuit_breaker_state(self, service: str) -> str:
        """
        Return the circuit breaker state for a service.

        Args:
            service: Service name

        Returns:
            State string (closed, open, half_open)
        """
        if self.cb_model is None:
            logger.debug("django_adapter.circuit_breaker_model_configured")
            return "closed"

        try:
            filter_kwargs = {self.service_name_field: service}
            cb = self.cb_model.objects.filter(**filter_kwargs).first()
            if cb:
                return getattr(cb, self.state_field, "closed")
            return "closed"
        except Exception as e:
            logger.warning(
                "django_adapter.get_cb_state_failed",
                error=e,
            )
            return "closed"

    def get_retry_success_rate(self, domain: str) -> float:
        """
        Return the retry success rate for a domain (over the last hour).

        Args:
            domain: Domain name

        Returns:
            Success rate (0.0 ~ 100.0)
        """
        if self.dlq_model is None:
            logger.debug("django_adapter.dlq_model_configured")
            return 0.0

        try:
            from django.db.models import Avg

            one_hour_ago = utc_now() - timedelta(hours=1)

            # Compute the rate over items resolved within the last hour
            filter_kwargs = {
                self.domain_field: domain,
                f"{self.resolved_at_field}__gte": one_hour_ago,
            }

            # Compute the success rate (when is_success is a boolean field)
            result = self.dlq_model.objects.filter(**filter_kwargs).aggregate(
                success_rate=Avg(self.is_success_field)
            )

            rate = result.get("success_rate")
            if rate is not None:
                # Booleans average as 0/1, so scale by 100
                return float(rate) * 100
            return 0.0

        except Exception as e:
            logger.warning(
                "django_adapter.get_retry_success_failed",
                error=e,
            )
            return 0.0


__all__ = ["DjangoMetricSourceAdapter"]
