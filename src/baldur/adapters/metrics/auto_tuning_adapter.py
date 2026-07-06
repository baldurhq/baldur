"""
Auto Tuning Metrics Adapters - 자율 조정용 메트릭 어댑터

RuntimeFeedbackLoop에서 사용하는 메트릭 수집 어댑터들

제공 어댑터:
- InternalMetricsAdapter: DB/캐시 기반 내부 메트릭
- PrometheusMetricsAdapter: Prometheus 연동
- MockMetricsAdapter: 테스트용
"""

from __future__ import annotations

from typing import Protocol

import structlog

logger = structlog.get_logger()


class AutoTuningMetricsAdapter(Protocol):
    """자율 조정용 메트릭 어댑터 프로토콜"""

    def fetch_current_metrics(self) -> dict[str, float]:
        """
        현재 메트릭 수집

        Returns:
            메트릭 딕셔너리:
            - error_rate: 에러율 (0.0 ~ 1.0)
            - p99_latency_ms: P99 레이턴시 (ms)
            - retry_exhausted_rate: 재시도 소진율
            - retry_collision_rate: 재시도 충돌율
            - throttle_rate: 스로틀링 비율
            - throughput_rps: 처리량 (requests/sec)
            - sample_count: 샘플 수
        """
        ...


class InternalMetricsAdapter:
    """
    내부 메트릭 어댑터

    DB나 캐시에서 메트릭을 수집합니다.
    외부 시스템 의존 없이 동작 가능합니다.
    """

    def __init__(
        self,
        cache_provider=None,
        db_provider=None,
        metrics_prefix: str = "baldur",
    ):
        """
        Args:
            cache_provider: Redis 등 캐시 제공자
            db_provider: DB 접근 제공자
            metrics_prefix: 메트릭 키 접두사
        """
        self.cache_provider = cache_provider
        self.db_provider = db_provider
        self.metrics_prefix = metrics_prefix

        # 내부 메트릭 저장소 (캐시 없을 경우)
        self._internal_metrics: dict[str, float] = {}
        self._sample_counts: dict[str, int] = {}

    def fetch_current_metrics(self) -> dict[str, float]:
        """현재 메트릭 수집"""
        metrics = {
            "error_rate": self._get_error_rate(),
            "p99_latency_ms": self._get_p99_latency(),
            "retry_exhausted_rate": self._get_retry_exhausted_rate(),
            "retry_collision_rate": self._get_retry_collision_rate(),
            "throttle_rate": self._get_throttle_rate(),
            "throughput_rps": self._get_throughput(),
            "sample_count": self._get_sample_count(),
        }

        logger.debug(
            "internal_metrics.fetched",
            metrics=metrics,
        )
        return metrics

    def record_metric(self, name: str, value: float):
        """메트릭 기록 (외부에서 호출)"""
        self._internal_metrics[name] = value
        self._sample_counts[name] = self._sample_counts.get(name, 0) + 1

        if self.cache_provider:
            try:
                key = f"{self.metrics_prefix}:{name}"
                self.cache_provider.set(key, value)
            except Exception as e:
                logger.debug(
                    "internal_metrics.cache_set_failed",
                    error=e,
                )

    def _get_metric(self, name: str, default: float = 0.0) -> float:
        """메트릭 값 조회"""
        # 캐시에서 먼저 시도
        if self.cache_provider:
            try:
                key = f"{self.metrics_prefix}:{name}"
                value = self.cache_provider.get(key)
                if value is not None:
                    return float(value)
            except Exception:
                pass

        # 내부 저장소에서 조회
        return self._internal_metrics.get(name, default)

    def _get_error_rate(self) -> float:
        return self._get_metric("error_rate", 0.01)

    def _get_p99_latency(self) -> float:
        return self._get_metric("p99_latency_ms", 200.0)

    def _get_retry_exhausted_rate(self) -> float:
        return self._get_metric("retry_exhausted_rate", 0.02)

    def _get_retry_collision_rate(self) -> float:
        return self._get_metric("retry_collision_rate", 0.01)

    def _get_throttle_rate(self) -> float:
        return self._get_metric("throttle_rate", 0.005)

    def _get_throughput(self) -> float:
        return self._get_metric("throughput_rps", 100.0)

    def _get_sample_count(self) -> int:
        return sum(self._sample_counts.values()) or 10


class PrometheusMetricsAdapter:
    """
    Prometheus 메트릭 어댑터

    Prometheus에서 메트릭을 쿼리하여 자율 조정에 사용합니다.
    """

    def __init__(
        self,
        prometheus_url: str = "http://localhost:9090",
        timeout_seconds: int = 5,
        job_name: str = "baldur",
    ):
        """
        Args:
            prometheus_url: Prometheus 서버 URL
            timeout_seconds: 요청 타임아웃
            job_name: 메트릭 job 라벨
        """
        self.prometheus_url = prometheus_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.job_name = job_name

    def fetch_current_metrics(self) -> dict[str, float]:
        """Prometheus에서 메트릭 쿼리"""
        metrics = {}

        # 에러율 쿼리
        metrics["error_rate"] = self._query_metric(
            f'sum(rate(http_requests_total{{job="{self.job_name}",status=~"5.."}}[5m])) / '
            f'sum(rate(http_requests_total{{job="{self.job_name}"}}[5m]))',
            default=0.01,
        )

        # P99 레이턴시 쿼리
        metrics["p99_latency_ms"] = self._query_metric(
            f'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{{job="{self.job_name}"}}[5m])) by (le)) * 1000',
            default=200.0,
        )

        # 재시도 소진율
        metrics["retry_exhausted_rate"] = self._query_metric(
            f'sum(rate(retry_exhausted_total{{job="{self.job_name}"}}[5m])) / '
            f'sum(rate(retry_attempts_total{{job="{self.job_name}"}}[5m]))',
            default=0.02,
        )

        # 재시도 충돌율
        metrics["retry_collision_rate"] = self._query_metric(
            f'sum(rate(retry_collision_total{{job="{self.job_name}"}}[5m])) / '
            f'sum(rate(retry_attempts_total{{job="{self.job_name}"}}[5m]))',
            default=0.01,
        )

        # 스로틀링 비율
        metrics["throttle_rate"] = self._query_metric(
            f'sum(rate(rate_limited_total{{job="{self.job_name}"}}[5m])) / '
            f'sum(rate(http_requests_total{{job="{self.job_name}"}}[5m]))',
            default=0.005,
        )

        # 처리량
        metrics["throughput_rps"] = self._query_metric(
            f'sum(rate(http_requests_total{{job="{self.job_name}"}}[5m]))',
            default=100.0,
        )

        # 샘플 수
        metrics["sample_count"] = self._query_metric(
            f'sum(http_requests_total{{job="{self.job_name}"}})', default=1000
        )

        logger.debug(
            "prometheus_metrics.fetched",
            metrics=metrics,
        )
        return metrics

    def _query_metric(self, query: str, default: float = 0.0) -> float:
        """Prometheus 쿼리 실행"""
        try:
            import urllib.parse
            import urllib.request

            from baldur.utils.http import safe_urlopen
            from baldur.utils.serialization import fast_loads

            url = f"{self.prometheus_url}/api/v1/query"
            params = urllib.parse.urlencode({"query": query})
            full_url = f"{url}?{params}"

            req = urllib.request.Request(full_url)
            with safe_urlopen(req, timeout=self.timeout_seconds) as response:
                data = fast_loads(response.read())

            if data.get("status") == "success":
                result = data.get("data", {}).get("result", [])
                if result:
                    value = result[0].get("value", [None, None])[1]
                    if value is not None and value != "NaN":
                        return float(value)

            return default
        except Exception as e:
            logger.debug(
                "prometheus_metrics.query_failed",
                error=e,
            )
            return default


class MockMetricsAdapter:
    """
    Mock 메트릭 어댑터 (테스트용)

    테스트에서 메트릭 값을 직접 설정할 수 있습니다.
    """

    def __init__(self, initial_metrics: dict[str, float] | None = None):
        self.metrics = initial_metrics or {
            "error_rate": 0.02,
            "p99_latency_ms": 150.0,
            "retry_exhausted_rate": 0.03,
            "retry_collision_rate": 0.01,
            "throttle_rate": 0.005,
            "throughput_rps": 500.0,
            "sample_count": 1000,
        }

    def fetch_current_metrics(self) -> dict[str, float]:
        """Mock 메트릭 반환"""
        return self.metrics.copy()

    def set_metrics(self, metrics: dict[str, float]):
        """메트릭 설정"""
        self.metrics.update(metrics)

    def set_metric(self, name: str, value: float):
        """단일 메트릭 설정"""
        self.metrics[name] = value

    def simulate_degradation(self, level: str = "minor"):
        """저하 상황 시뮬레이션"""
        if level == "minor":
            self.metrics["error_rate"] = 0.06
            self.metrics["p99_latency_ms"] = 3500
        elif level == "major":
            self.metrics["error_rate"] = 0.15
            self.metrics["p99_latency_ms"] = 6000
        elif level == "critical":
            self.metrics["error_rate"] = 0.35
            self.metrics["p99_latency_ms"] = 12000

    def reset(self):
        """기본값으로 리셋"""
        self.metrics = {
            "error_rate": 0.02,
            "p99_latency_ms": 150.0,
            "retry_exhausted_rate": 0.03,
            "retry_collision_rate": 0.01,
            "throttle_rate": 0.005,
            "throughput_rps": 500.0,
            "sample_count": 1000,
        }


__all__ = [
    "AutoTuningMetricsAdapter",
    "InternalMetricsAdapter",
    "PrometheusMetricsAdapter",
    "MockMetricsAdapter",
]
