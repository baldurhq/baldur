"""
Metrics Hook — 파이프라인 실행 결과를 Prometheus 메트릭으로 기록.

PolicyComposer의 Hook으로 등록하여 성공/실패/거부 메트릭을 수집한다.
prometheus_client를 lazy import하여 미설치 환경에서도 오류 없이 동작한다.

Fail-Open 원칙: prometheus_client import 실패 시 메트릭 수집을 건너뛴다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from baldur.interfaces.resilience_policy import PolicyResult

if TYPE_CHECKING:
    from baldur.interfaces.resilience_policy import PolicyContext

logger = structlog.get_logger()

# Lazy-initialized Prometheus instruments
_metrics_initialized = False
_pipeline_success_total = None
_pipeline_failure_total = None
_pipeline_rejected_total = None
_pipeline_duration_seconds = None


def _ensure_metrics() -> bool:
    """Prometheus 메트릭 인스턴스를 lazy 초기화. 사용 불가 시 False 반환."""
    global _metrics_initialized, _pipeline_success_total, _pipeline_failure_total
    global _pipeline_rejected_total, _pipeline_duration_seconds

    if _metrics_initialized:
        return _pipeline_success_total is not None

    _metrics_initialized = True

    try:
        from baldur.metrics.registry import (
            get_or_create_counter,
            get_or_create_histogram,
        )

        _pipeline_success_total = get_or_create_counter(
            "baldur_pipeline_success_total",
            "Total successful pipeline executions",
            ["pipeline"],
        )
        _pipeline_failure_total = get_or_create_counter(
            "baldur_pipeline_failure_total",
            "Total failed pipeline executions",
            ["pipeline", "error_type"],
        )
        _pipeline_rejected_total = get_or_create_counter(
            "baldur_pipeline_rejected_total",
            "Total rejected pipeline executions",
            ["pipeline", "guard"],
        )
        _pipeline_duration_seconds = get_or_create_histogram(
            "baldur_pipeline_duration_seconds",
            "Pipeline execution duration in seconds",
            ["pipeline"],
        )
        return True
    except ImportError:
        logger.debug("metrics.collection_disabled")
        return False


class MetricsHook:
    """Prometheus 메트릭 훅.

    파이프라인 전체(End-to-End) 결과만 관찰한다.
    prometheus_client 미설치 시 메트릭 수집을 건너뛴다.
    """

    def on_execute(
        self, policy_name: str, attempt: int, context: PolicyContext | None = None
    ) -> None:
        """실행 시작 — 메트릭 없음."""

    def on_success(
        self,
        policy_name: str,
        result: PolicyResult,
        context: PolicyContext | None = None,
    ) -> None:
        """파이프라인 성공 시 메트릭 기록."""
        if not _ensure_metrics():
            return

        _pipeline_success_total.labels(pipeline=policy_name).inc()  # type: ignore[union-attr]
        _pipeline_duration_seconds.labels(pipeline=policy_name).observe(  # type: ignore[union-attr]
            result.total_duration_ms / 1000.0
        )

    def on_failure(
        self,
        policy_name: str,
        error: Exception,
        attempt: int,
        context: PolicyContext | None = None,
    ) -> None:
        """파이프라인 실패 시 메트릭 기록."""
        if not _ensure_metrics():
            return

        error_type = type(error).__name__
        if _pipeline_failure_total is not None:
            _pipeline_failure_total.labels(
                pipeline=policy_name, error_type=error_type
            ).inc()

    def on_retry(
        self,
        policy_name: str,
        attempt: int,
        delay: float,
        context: PolicyContext | None = None,
    ) -> None:
        """재시도 — Composer 레벨에서는 미사용."""

    def on_reject(
        self, guard_name: str, reason: str, context: PolicyContext | None = None
    ) -> None:
        """파이프라인 거부 시 메트릭 기록."""
        if not _ensure_metrics():
            return

        _pipeline_rejected_total.labels(pipeline="composer", guard=guard_name).inc()  # type: ignore[union-attr]
