"""
Time Series Metrics Provider.

시뮬레이션 시 과거 Raw 데이터 조회를 위한 Protocol 및 Mock 구현.
기존 MetricsProvider(core/auto_rollback_guard.py)는 "현재값"만 반환하므로
과거 시간 범위의 시계열이 필요한 Config Shadow용으로 별도 정의.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class TimeSeriesMetricsProvider(Protocol):
    """시계열 메트릭 제공자.

    추세 데이터(시계열 List)와 판정용 집계값(Scalar)을 분리하여 제공한다.

    Implementations:
    - MockTimeSeriesProvider: 테스트 및 개발용
    - PrometheusTimeSeriesProvider: Prometheus PromQL 기반 (향후)
    - DatadogTimeSeriesProvider: Datadog Metrics API 기반 (향후)
    """

    # --- 시계열 메서드 (추세 분석 / UI 대시보드용) ---

    def query_error_rate(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        step_seconds: int = 60,
        labels: dict[str, str] | None = None,
    ) -> list[tuple[datetime, float]]:
        """시간 범위의 에러율 시계열을 반환한다.

        Args:
            service_name: 대상 서비스
            start: 조회 시작 시각 (UTC)
            end: 조회 종료 시각 (UTC)
            step_seconds: 시계열 간격 (기본 60초)
            labels: K8s 복합 레이블 (예: {"track": "canary", "namespace": "prod"})

        Returns:
            (timestamp, error_rate) 튜플 리스트. error_rate는 0.0 ~ 1.0.
        """
        ...

    def query_request_rate(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        step_seconds: int = 60,
        labels: dict[str, str] | None = None,
    ) -> list[tuple[datetime, float]]:
        """시간 범위의 요청률(RPS) 시계열을 반환한다."""
        ...

    # --- 스칼라 집계 메서드 (Evaluator 판정용) ---

    def query_error_rate_aggregated(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        labels: dict[str, str] | None = None,
    ) -> float:
        """윈도우 전체의 가중치 기반 에러율 스칼라.

        내부적으로 sum(rate(errors)) / sum(rate(requests)) 형태의
        PromQL/Datadog 쿼리를 실행한다.

        Returns:
            가중치 기반 에러율 (0.0 ~ 1.0)
        """
        ...

    def query_request_count(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        labels: dict[str, str] | None = None,
    ) -> int:
        """윈도우 전체의 총 요청 수."""
        ...

    def query_latency_aggregated(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        percentile: float = 0.99,
        labels: dict[str, str] | None = None,
    ) -> float:
        """윈도우 전체의 Latency Percentile 스칼라 (밀리초).

        내부적으로 histogram_quantile(percentile, ...) 형태의
        PromQL을 실행한다. Percentile은 평균할 수 없으므로
        반드시 Provider 수준에서 한 번에 계산해야 한다.

        Args:
            percentile: 0.95 (P95) 또는 0.99 (P99)

        Returns:
            해당 percentile의 latency (밀리초)
        """
        ...


class MockTimeSeriesProvider:
    """테스트용 시계열 메트릭 제공자.

    임의의 시계열 데이터를 주입하여 시뮬레이터 로직을 검증한다.
    프로덕션에서는 Prometheus/Datadog 어댑터로 교체.

    키 구성:
    - labels=None: "{service}:{metric}" (기존 호환)
    - labels 지정 시: "{service}:{metric}:{k}={v},..." (label-aware)
    """

    def __init__(self, data: dict[str, list[tuple[datetime, float]]] | None = None):
        self._data = data or {}
        self._scalars: dict[str, float] = {}

    @staticmethod
    def _label_suffix(labels: dict[str, str] | None) -> str:
        if not labels:
            return ""
        return ":" + ",".join(f"{k}={v}" for k, v in sorted(labels.items()))

    def _scalar_key(
        self,
        service_name: str,
        metric: str,
        labels: dict[str, str] | None = None,
    ) -> str:
        return f"{service_name}:{metric}{self._label_suffix(labels)}"

    def _resolve_scalar(
        self,
        service_name: str,
        metric: str,
        labels: dict[str, str] | None,
        default: float = 0.0,
    ) -> float:
        labeled_key = self._scalar_key(service_name, metric, labels)
        if labeled_key in self._scalars:
            return self._scalars[labeled_key]
        return self._scalars.get(f"{service_name}:{metric}", default)

    # --- 시계열 메서드 (labels 파라미터 추가, 기본값 None) ---

    def query_error_rate(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        step_seconds: int = 60,
        labels: dict[str, str] | None = None,
    ) -> list[tuple[datetime, float]]:
        key = f"{service_name}:error_rate{self._label_suffix(labels)}"
        if key not in self._data:
            key = f"{service_name}:error_rate"
        return [(ts, val) for ts, val in self._data.get(key, []) if start <= ts < end]

    def query_request_rate(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        step_seconds: int = 60,
        labels: dict[str, str] | None = None,
    ) -> list[tuple[datetime, float]]:
        key = f"{service_name}:request_rate{self._label_suffix(labels)}"
        if key not in self._data:
            key = f"{service_name}:request_rate"
        return [(ts, val) for ts, val in self._data.get(key, []) if start <= ts < end]

    # --- 스칼라 집계 메서드 ---

    def query_error_rate_aggregated(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        labels: dict[str, str] | None = None,
    ) -> float:
        return self._resolve_scalar(service_name, "error_rate_agg", labels)

    def query_request_count(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        labels: dict[str, str] | None = None,
    ) -> int:
        return int(self._resolve_scalar(service_name, "request_count", labels))

    def query_latency_aggregated(
        self,
        service_name: str,
        start: datetime,
        end: datetime,
        percentile: float = 0.99,
        labels: dict[str, str] | None = None,
    ) -> float:
        return self._resolve_scalar(
            service_name,
            f"latency_p{int(percentile * 100)}",
            labels,
        )


_metrics_provider: TimeSeriesMetricsProvider | None = None
_metrics_provider_lock = threading.Lock()


def get_metrics_provider() -> TimeSeriesMetricsProvider:
    """TimeSeriesMetricsProvider 싱글톤 반환.

    프로덕션에서는 PrometheusTimeSeriesProvider 등으로 교체.
    기본값은 MockTimeSeriesProvider.
    """
    global _metrics_provider
    if _metrics_provider is None:
        with _metrics_provider_lock:
            if _metrics_provider is None:
                _metrics_provider = MockTimeSeriesProvider()
    return _metrics_provider


def set_metrics_provider(provider: TimeSeriesMetricsProvider) -> None:
    """TimeSeriesMetricsProvider 등록 (DI용)."""
    global _metrics_provider
    _metrics_provider = provider


def reset_metrics_provider() -> None:
    """싱글톤 리셋 (테스트용)."""
    global _metrics_provider
    _metrics_provider = None
