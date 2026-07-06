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
    Django ORM 기반 메트릭 소스 어댑터.

    Django 모델을 사용하여 DLQ, Circuit Breaker 등의 메트릭을 조회합니다.

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
        도메인별 대기 중인 DLQ 항목 수 반환.

        Args:
            domain: 도메인 이름 (payment, point, inventory 등)

        Returns:
            대기 중인 DLQ 항목 수
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
        상태별 DLQ 항목 수 반환.

        Args:
            status: 상태 (pending, resolved, failed 등)

        Returns:
            해당 상태의 DLQ 항목 수
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
        서비스의 Circuit Breaker 상태 반환.

        Args:
            service: 서비스 이름

        Returns:
            상태 문자열 (closed, open, half_open)
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
        도메인별 재시도 성공률 반환 (최근 1시간 기준).

        Args:
            domain: 도메인 이름

        Returns:
            성공률 (0.0 ~ 100.0)
        """
        if self.dlq_model is None:
            logger.debug("django_adapter.dlq_model_configured")
            return 0.0

        try:
            from django.db.models import Avg

            one_hour_ago = utc_now() - timedelta(hours=1)

            # 최근 1시간 내 해결된 항목 기준 성공률 계산
            filter_kwargs = {
                self.domain_field: domain,
                f"{self.resolved_at_field}__gte": one_hour_ago,
            }

            # 성공률 계산 (is_success 필드가 boolean인 경우)
            result = self.dlq_model.objects.filter(**filter_kwargs).aggregate(
                success_rate=Avg(self.is_success_field)
            )

            rate = result.get("success_rate")
            if rate is not None:
                # boolean은 0/1로 평균 계산되므로 100을 곱함
                return float(rate) * 100
            return 0.0

        except Exception as e:
            logger.warning(
                "django_adapter.get_retry_success_failed",
                error=e,
            )
            return 0.0


__all__ = ["DjangoMetricSourceAdapter"]
