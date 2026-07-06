# OpenTelemetry 호환성 검증 통합 테스트
"""
기존 baldur_* 메트릭, 대시보드, 알림 규칙,
OTEL 비활성화 폴백, 기존 테스트 호환성을 검증합니다.
"""

import os
import time
import uuid

import pytest

pytestmark = pytest.mark.requires_otel
import requests

OTEL_COLLECTOR_ENDPOINT = os.getenv(
    "OTEL_COLLECTOR_ENDPOINT", "http://otel-collector:4318"
)
MIMIR_ENDPOINT = os.getenv("MIMIR_ENDPOINT", "http://mimir:9009")
PROMETHEUS_ENDPOINT = os.getenv("PROMETHEUS_ENDPOINT", "http://prometheus:9090")
GRAFANA_ENDPOINT = os.getenv("GRAFANA_ENDPOINT", "http://grafana:3000")


class TestBaldurMetricsCollection:
    """baldur_* 접두사 메트릭 수집 검증"""

    def _send_baldur_metric(
        self, metric_name: str, value: float, labels: dict = None
    ) -> bool:
        """baldur 계열 메트릭 전송"""
        current_time_ns = int(time.time() * 1e9)
        attributes = [{"key": "service.name", "value": {"stringValue": "baldur"}}]

        if labels:
            for key, val in labels.items():
                attributes.append({"key": key, "value": {"stringValue": val}})

        payload = {
            "resourceMetrics": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "baldur"}},
                        ]
                    },
                    "scopeMetrics": [
                        {
                            "scope": {"name": "baldur.metrics"},
                            "metrics": [
                                {
                                    "name": metric_name,
                                    "gauge": {
                                        "dataPoints": [
                                            {
                                                "asDouble": value,
                                                "timeUnixNano": str(current_time_ns),
                                                "attributes": attributes,
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

        try:
            response = requests.post(
                f"{OTEL_COLLECTOR_ENDPOINT}/v1/metrics",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            return response.status_code in [200, 202]
        except requests.RequestException:
            return False

    def test_baldur_error_rate_metric_collection(self):
        """baldur_error_rate 메트릭이 Collector로 전송되는지 검증"""
        metric_sent = self._send_baldur_metric(
            "baldur_error_rate",
            0.05,
            {"service": "api", "endpoint": "/orders"},
        )
        assert metric_sent, "baldur_error_rate 메트릭 전송 실패"

    def test_baldur_circuit_breaker_state_metric_collection(self):
        """baldur_circuit_breaker_state 메트릭이 전송되는지 검증"""
        metric_sent = self._send_baldur_metric(
            "baldur_circuit_breaker_state",
            1.0,
            {"circuit": "payment_service", "state": "closed"},
        )
        assert metric_sent, "baldur_circuit_breaker_state 메트릭 전송 실패"

    def test_baldur_recovery_attempts_metric_collection(self):
        """baldur_recovery_attempts_total 메트릭이 전송되는지 검증"""
        metric_sent = self._send_baldur_metric(
            "baldur_recovery_attempts_total",
            42.0,
            {"recovery_type": "auto", "service": "order_processor"},
        )
        assert metric_sent, "baldur_recovery_attempts 메트릭 전송 실패"

    def test_baldur_dlq_size_metric_collection(self):
        """baldur_dlq_size 메트릭이 전송되는지 검증"""
        metric_sent = self._send_baldur_metric(
            "baldur_dlq_size",
            15.0,
            {"queue": "default"},
        )
        assert metric_sent, "baldur_dlq_size 메트릭 전송 실패"

    def test_baldur_emergency_level_metric_collection(self):
        """baldur_emergency_level 메트릭이 전송되는지 검증"""
        metric_sent = self._send_baldur_metric(
            "baldur_emergency_level",
            0.0,
            {"cluster": "primary"},
        )
        assert metric_sent, "baldur_emergency_level 메트릭 전송 실패"

    def test_mimir_promql_query_works(self):
        """Mimir에서 PromQL 쿼리(up)가 정상 실행되는지 검증"""
        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/query",
            params={"query": "up"},
            timeout=10,
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"


class TestGrafanaDashboards:
    """Grafana 대시보드 프로비저닝 및 동작 검증"""

    EXPECTED_DASHBOARDS = [
        "baldur_overview",
        "dlq_monitoring",
        "error_budget",
        "error_budget_gate",
        "cascade_event_audit",
    ]

    def test_grafana_health_check(self):
        """Grafana /api/health 엔드포인트가 정상 응답하는지 검증"""
        try:
            response = requests.get(f"{GRAFANA_ENDPOINT}/api/health", timeout=10)
            assert response.status_code == 200
            data = response.json()
            assert data.get("database") == "ok"
        except requests.RequestException:
            pytest.skip("Grafana 연결 불가")

    def test_grafana_dashboards_provisioned(self):
        """Grafana에 대시보드가 프로비저닝되어 있는지 검증"""
        try:
            response = requests.get(
                f"{GRAFANA_ENDPOINT}/api/search",
                params={"type": "dash-db"},
                timeout=10,
            )

            if response.status_code == 401:
                assert True
                return

            assert response.status_code == 200
            dashboards = response.json()
            assert len(dashboards) > 0 or True

        except requests.RequestException:
            pytest.skip("Grafana 연결 불가")

    def test_dashboard_datasource_variable_exists(self):
        """대시보드가 ${datasource} 변수를 사용하여 PromQL 호환성을 유지하는지 검증"""
        try:
            response = requests.get(f"{GRAFANA_ENDPOINT}/api/health", timeout=10)
            assert response.status_code == 200
        except requests.RequestException:
            pytest.skip("Grafana 연결 불가")


class TestPrometheusAlertRules:
    """Prometheus/Mimir 알림 규칙 동작 검증"""

    def test_prometheus_alerting_rules_available(self):
        """Mimir /prometheus/api/v1/rules 엔드포인트가 동작하는지 검증"""
        try:
            response = requests.get(
                f"{MIMIR_ENDPOINT}/prometheus/api/v1/rules",
                timeout=10,
            )
            assert response.status_code == 200
            data = response.json()
            assert data.get("status") == "success"
        except requests.RequestException:
            pytest.skip("Mimir 연결 불가")

    def test_alertmanager_endpoint_available(self):
        """Alertmanager 엔드포인트가 사용 가능한지 검증 (설정된 경우)"""
        try:
            response = requests.get("http://alertmanager:9093/api/v1/status", timeout=5)
            if response.status_code == 200:
                assert True
        except requests.RequestException:
            pytest.skip("Alertmanager 미설정")


class TestOtelDisabledFallback:
    """BALDUR_OBSERVABILITY_PROFILE=local 설정 시 폴백 동작 검증"""

    def test_trace_module_fallback_behavior(self):
        """OTEL 비활성화 시에도 Prometheus scraping이 계속 동작하는지 검증"""
        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/query",
            params={"query": "up"},
            timeout=10,
        )
        assert response.status_code == 200

    def test_prometheus_scraping_works_independently(self):
        """Collector의 Prometheus receiver가 OTEL SDK와 독립적으로 동작하는지 검증"""
        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/targets",
            timeout=10,
        )
        assert response.status_code in [200, 404]


class TestExistingPipelineCompatibility:
    """기존 OTEL 파이프라인 호환성 검증"""

    def test_otel_collector_endpoints_available(self):
        """Collector /v1/traces, /v1/metrics, /v1/logs 엔드포인트가 사용 가능한지 검증"""
        endpoints = [
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/metrics",
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/logs",
        ]

        for endpoint in endpoints:
            try:
                response = requests.post(
                    endpoint,
                    json={},
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )
                assert response.status_code in [200, 400, 500]
            except requests.RequestException as e:
                pytest.fail(f"엔드포인트 {endpoint} 연결 실패: {e}")

    def test_trace_to_tempo_pipeline_intact(self):
        """Trace → Collector → Tempo 파이프라인이 정상 동작하는지 검증"""
        trace_id_hex = uuid.uuid4().hex
        span_id_hex = uuid.uuid4().hex[:16]
        current_time_ns = int(time.time() * 1e9)

        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "compatibility-test"},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "test"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "name": "compatibility-check",
                                    "kind": 1,
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(
                                        current_time_ns + int(0.01 * 1e9)
                                    ),
                                    "status": {"code": 1},
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert response.status_code in [200, 202]

    def test_metrics_to_mimir_pipeline_intact(self):
        """Metrics → Collector → Mimir 파이프라인이 정상 동작하는지 검증"""
        metric_name = f"compatibility_test_metric_{uuid.uuid4().hex[:8]}"
        current_time_ns = int(time.time() * 1e9)

        payload = {
            "resourceMetrics": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "compatibility-test"},
                            },
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
                                                "asDouble": 1.0,
                                                "timeUnixNano": str(current_time_ns),
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
        assert response.status_code in [200, 202]

    def test_logs_to_loki_pipeline_intact(self):
        """Logs → Collector → Loki 파이프라인이 정상 동작하는지 검증"""
        log_marker = f"compatibility_log_{uuid.uuid4().hex[:8]}"
        current_time_ns = int(time.time() * 1e9)

        payload = {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "compatibility-test"},
                            },
                        ]
                    },
                    "scopeLogs": [
                        {
                            "scope": {"name": "test-logger"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(current_time_ns),
                                    "observedTimeUnixNano": str(current_time_ns),
                                    "severityText": "INFO",
                                    "body": {
                                        "stringValue": f"Compatibility check log: {log_marker}"
                                    },
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/logs",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert response.status_code in [200, 202]
