# OTEL Collector Metrics 파이프라인 통합 테스트
"""
OpenTelemetry Collector Metrics 파이프라인 테스트

테스트 항목:
1. Mimir 헬스체크
2. OTLP 메트릭 수신 테스트
3. Prometheus Remote Write 전송 확인
4. 메트릭 조회 테스트
"""

import os
import time
import uuid

import pytest
import requests

pytestmark = pytest.mark.requires_otel

OTEL_COLLECTOR_ENDPOINT = os.getenv(
    "OTEL_COLLECTOR_ENDPOINT", "http://otel-collector:4318"
)
MIMIR_ENDPOINT = os.getenv("MIMIR_ENDPOINT", "http://mimir:9009")
COLLECTOR_HEALTH_ENDPOINT = os.getenv(
    "COLLECTOR_HEALTH_ENDPOINT", "http://otel-collector:13133"
)


class TestMimirHealth:
    """Grafana Mimir 헬스체크 테스트"""

    def test_mimir_ready_endpoint_returns_ok(self):
        """Mimir ready 엔드포인트가 정상 응답하는지 확인"""
        response = requests.get(f"{MIMIR_ENDPOINT}/ready", timeout=10)

        assert response.status_code == 200

    def test_mimir_api_endpoint_available(self):
        """Mimir API 엔드포인트 사용 가능 여부 확인"""
        response = requests.get(f"{MIMIR_ENDPOINT}/api/v1/status/buildinfo", timeout=10)

        assert response.status_code == 200


class TestOtlpMetricIngestion:
    """OTLP Metric 수신 테스트"""

    def _generate_metric_name(self) -> str:
        """고유한 메트릭 이름 생성"""
        return f"test_metric_{uuid.uuid4().hex[:8]}"

    def _create_gauge_metric_payload(self, metric_name: str, value: float = 42.0):
        """OTLP Gauge 메트릭 페이로드 생성"""
        current_time_ns = int(time.time() * 1e9)
        return {
            "resourceMetrics": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "metrics-integration-test"},
                            },
                            {
                                "key": "deployment.environment",
                                "value": {"stringValue": "test"},
                            },
                        ]
                    },
                    "scopeMetrics": [
                        {
                            "scope": {"name": "test-scope", "version": "1.0.0"},
                            "metrics": [
                                {
                                    "name": metric_name,
                                    "description": "Test gauge metric",
                                    "gauge": {
                                        "dataPoints": [
                                            {
                                                "asDouble": value,
                                                "timeUnixNano": str(current_time_ns),
                                                "attributes": [
                                                    {
                                                        "key": "test.label",
                                                        "value": {
                                                            "stringValue": "integration"
                                                        },
                                                    }
                                                ],
                                            }
                                        ]
                                    },
                                }
                            ],
                        }
                    ],
                }
            ]
        }

    def _create_counter_metric_payload(self, metric_name: str, value: int = 100):
        """OTLP Counter 메트릭 페이로드 생성"""
        current_time_ns = int(time.time() * 1e9)
        start_time_ns = current_time_ns - int(60 * 1e9)  # 1분 전
        return {
            "resourceMetrics": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "metrics-integration-test"},
                            },
                        ]
                    },
                    "scopeMetrics": [
                        {
                            "scope": {"name": "test-scope"},
                            "metrics": [
                                {
                                    "name": metric_name,
                                    "description": "Test counter metric",
                                    "unit": "1",
                                    "sum": {
                                        "dataPoints": [
                                            {
                                                "asInt": str(value),
                                                "startTimeUnixNano": str(start_time_ns),
                                                "timeUnixNano": str(current_time_ns),
                                                "attributes": [],
                                            }
                                        ],
                                        "aggregationTemporality": 2,  # CUMULATIVE
                                        "isMonotonic": True,
                                    },
                                }
                            ],
                        }
                    ],
                }
            ]
        }

    def test_collector_accepts_otlp_metric_via_http(self):
        """Collector가 OTLP HTTP로 Metric을 수신하는지 확인"""
        metric_name = self._generate_metric_name()
        payload = self._create_gauge_metric_payload(metric_name, 42.0)

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/metrics",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code in [200, 202], (
            f"Unexpected status: {response.status_code}, body: {response.text}"
        )

    def test_collector_accepts_counter_metric_via_http(self):
        """Collector가 Counter 메트릭을 수신하는지 확인"""
        metric_name = self._generate_metric_name()
        payload = self._create_counter_metric_payload(metric_name, 100)

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/metrics",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code in [200, 202]

    def test_metric_reaches_mimir_after_ingestion(self):
        """Collector에 OTLP로 전송한 Metric이 Mimir에 저장되는지 확인

        OTLP metric → Collector → prometheusremotewrite → Mimir 파이프라인 검증.
        """
        metric_name = f"mimir_pipeline_test_{uuid.uuid4().hex[:8]}"

        payload = self._create_gauge_metric_payload(metric_name, 99.0)

        send_response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/metrics",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert send_response.status_code in [200, 202]

        max_wait = 90
        interval = 5
        metric_found = False

        for _ in range(max_wait // interval):
            time.sleep(interval)

            try:
                query_response = requests.get(
                    f"{MIMIR_ENDPOINT}/prometheus/api/v1/query",
                    params={"query": metric_name},
                    timeout=10,
                )

                if query_response.status_code == 200:
                    data = query_response.json()
                    if data.get("status") == "success" and data.get("data", {}).get(
                        "result"
                    ):
                        metric_found = True
                        break
            except requests.RequestException:
                pass

        assert metric_found, (
            f"Metric {metric_name}이 {max_wait}초 내에 Mimir에 저장되지 않음 "
            "(OTLP → Collector → prometheusremotewrite → Mimir 파이프라인 오류)"
        )


class TestCollectorMetricsPipeline:
    """Collector Metrics 파이프라인 테스트"""

    def test_multiple_metrics_batch_processing(self):
        """여러 메트릭이 배치로 처리되는지 확인"""
        base_name = f"batch_test_{uuid.uuid4().hex[:8]}"
        metrics_sent = 0

        for i in range(5):
            metric_name = f"{base_name}_{i}"
            payload = {
                "resourceMetrics": [
                    {
                        "resource": {
                            "attributes": [
                                {
                                    "key": "service.name",
                                    "value": {"stringValue": "batch-test"},
                                }
                            ]
                        },
                        "scopeMetrics": [
                            {
                                "scope": {"name": "test"},
                                "metrics": [
                                    {
                                        "name": metric_name,
                                        "gauge": {
                                            "dataPoints": [
                                                {
                                                    "asDouble": float(i * 10),
                                                    "timeUnixNano": str(
                                                        int(time.time() * 1e9)
                                                    ),
                                                }
                                            ]
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }

            response = requests.post(
                f"{OTEL_COLLECTOR_ENDPOINT}/v1/metrics",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )

            if response.status_code in [200, 202]:
                metrics_sent += 1

        assert metrics_sent == 5, f"5개 중 {metrics_sent}개만 전송됨"


class TestMimirPrometheusCompatibility:
    """Mimir Prometheus API 호환성 테스트"""

    def test_mimir_supports_promql_query(self):
        """Mimir가 PromQL 쿼리를 지원하는지 확인"""
        # up 메트릭 조회 (기본 메트릭)
        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/query",
            params={"query": "up"},
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"

    def test_mimir_supports_label_values_query(self):
        """Mimir가 라벨 값 조회를 지원하는지 확인"""
        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/label/__name__/values",
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"
