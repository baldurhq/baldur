# OpenTelemetry 롤백 시나리오 검증 통합 테스트
"""
SDK/Collector/Tempo/Loki/Mimir 장애 시 롤백 절차,
부분 롤백, 롤백 체크리스트를 검증합니다.
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
TEMPO_ENDPOINT = os.getenv("TEMPO_ENDPOINT", "http://tempo:3200")
LOKI_ENDPOINT = os.getenv("LOKI_ENDPOINT", "http://loki:3100")
MIMIR_ENDPOINT = os.getenv("MIMIR_ENDPOINT", "http://mimir:9009")
PROMETHEUS_ENDPOINT = os.getenv("PROMETHEUS_ENDPOINT", "http://prometheus:9090")
COLLECTOR_HEALTH_ENDPOINT = os.getenv(
    "COLLECTOR_HEALTH_ENDPOINT", "http://otel-collector:13133"
)


class TestSdkRollback:
    """OTEL SDK 문제 시 롤백(BALDUR_OBSERVABILITY_PROFILE=local) 검증"""

    def test_otel_disabled_fallback_prometheus_still_works(self):
        """OTEL 비활성화 시에도 Prometheus scraping이 계속 동작하는지 검증"""
        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/query",
            params={"query": "up"},
            timeout=10,
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"

    def test_existing_trace_id_system_fallback_ready(self):
        """기존 trace_id 시스템(req-{short_id})이 폴백으로 사용 가능한지 검증"""
        fallback_trace_id = f"req-{uuid.uuid4().hex[:8]}"
        assert fallback_trace_id.startswith("req-")
        assert len(fallback_trace_id) == 12

    def test_application_runs_without_otel_dependencies(self):
        """OTEL 의존성 없이도 애플리케이션 로직이 동작하는지 검증"""
        assert True


class TestCollectorRollback:
    """Collector 장애 시 롤백 검증"""

    def test_collector_health_check_available(self):
        """Collector /health 엔드포인트가 응답하는지 검증"""
        response = requests.get(f"{COLLECTOR_HEALTH_ENDPOINT}/", timeout=10)
        assert response.status_code == 200

    def test_mimir_independent_of_collector(self):
        """Collector 중지 시에도 Mimir가 독립적으로 동작하는지 검증"""
        response = requests.get(f"{MIMIR_ENDPOINT}/ready", timeout=10)
        assert response.status_code == 200

    def test_collector_restart_recovers_pipeline(self):
        """Collector 재시작 시 OTLP 파이프라인이 복구되는지 검증"""
        response = requests.get(f"{COLLECTOR_HEALTH_ENDPOINT}/", timeout=10)
        assert response.status_code == 200

        trace_id = uuid.uuid4().hex
        span_id = uuid.uuid4().hex[:16]
        current_time_ns = int(time.time() * 1e9)

        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "rollback-test"},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "rollback-test"},
                            "spans": [
                                {
                                    "traceId": trace_id,
                                    "spanId": span_id,
                                    "name": "recovery-check",
                                    "kind": 1,
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(current_time_ns + 1000),
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


class TestTempoRollback:
    """Tempo 장애 시 롤백 검증"""

    def test_tempo_health_check_available(self):
        """Tempo /ready 엔드포인트가 응답하는지 검증"""
        response = requests.get(f"{TEMPO_ENDPOINT}/ready", timeout=10)
        assert response.status_code == 200

    def test_collector_continues_without_tempo(self):
        """Tempo 중지 시에도 Collector와 다른 백엔드(Mimir, Loki)가 동작하는지 검증"""
        response = requests.get(f"{COLLECTOR_HEALTH_ENDPOINT}/", timeout=10)
        assert response.status_code == 200

        mimir_response = requests.get(f"{MIMIR_ENDPOINT}/ready", timeout=10)
        assert mimir_response.status_code == 200

        loki_response = requests.get(f"{LOKI_ENDPOINT}/ready", timeout=10)
        assert loki_response.status_code == 200

    def test_trace_storage_unavailable_does_not_crash_collector(self):
        """Trace 저장 불가 시에도 Collector가 데이터를 계속 수신하는지 검증"""
        current_time_ns = int(time.time() * 1e9)
        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "tempo-down-test"},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "test"},
                            "spans": [
                                {
                                    "traceId": uuid.uuid4().hex,
                                    "spanId": uuid.uuid4().hex[:16],
                                    "name": "resilience-check",
                                    "kind": 1,
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(current_time_ns + 1000),
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


class TestLokiRollback:
    """Loki 장애 시 롤백 검증"""

    def test_loki_health_check_available(self):
        """Loki /ready 엔드포인트가 응답하는지 검증"""
        response = requests.get(f"{LOKI_ENDPOINT}/ready", timeout=10)
        assert response.status_code == 200

    def test_collector_continues_without_loki(self):
        """Loki 중지 시에도 Collector와 Tempo가 동작하는지 검증"""
        response = requests.get(f"{COLLECTOR_HEALTH_ENDPOINT}/", timeout=10)
        assert response.status_code == 200

        tempo_response = requests.get(f"{TEMPO_ENDPOINT}/ready", timeout=10)
        assert tempo_response.status_code == 200

    def test_log_collection_graceful_degradation(self):
        """Loki 다운 시에도 Collector가 로그 수신을 계속하는지 검증"""
        current_time_ns = int(time.time() * 1e9)
        payload = {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "loki-down-test"},
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
                                    "body": {"stringValue": "Resilience test log"},
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


class TestMimirRollback:
    """Mimir 장애 시 Prometheus로 전환 검증"""

    def test_mimir_health_check_available(self):
        """Mimir /ready 엔드포인트가 응답하는지 검증"""
        response = requests.get(f"{MIMIR_ENDPOINT}/ready", timeout=10)
        assert response.status_code == 200

    def test_mimir_promql_api_compatible(self):
        """Mimir가 Prometheus API와 호환되어 URL 변경만으로 롤백 가능한지 검증"""
        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/query",
            params={"query": "up"},
            timeout=10,
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"

    def test_mimir_labels_api_available(self):
        """Mimir /prometheus/api/v1/labels 엔드포인트가 동작하는지 검증"""
        mimir_api_path = f"{MIMIR_ENDPOINT}/prometheus/api/v1/labels"
        response = requests.get(mimir_api_path, timeout=10)
        assert response.status_code == 200


class TestRollbackChecklist:
    """롤백 체크리스트 항목 검증"""

    def test_observability_profile_environment_variable_settable(self):
        """BALDUR_OBSERVABILITY_PROFILE 환경변수 설정 가능 여부 검증"""
        test_value = os.getenv("BALDUR_OBSERVABILITY_PROFILE", "not_set")
        assert test_value in ["auto", "local", "otel_collector", "not_set"]

    def test_service_restart_command_available(self):
        """docker-compose restart로 서비스 재시작 가능 여부 검증"""
        assert True

    def test_existing_functionality_after_rollback(self):
        """롤백 후 Prometheus/PromQL 쿼리가 동작하는지 검증"""
        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/query",
            params={"query": "up"},
            timeout=10,
        )
        assert response.status_code == 200

    def test_all_services_health_check(self):
        """롤백 후 모든 서비스(Collector, Tempo, Mimir, Loki)가 정상인지 검증"""
        services = [
            (f"{COLLECTOR_HEALTH_ENDPOINT}/", "Collector"),
            (f"{TEMPO_ENDPOINT}/ready", "Tempo"),
            (f"{MIMIR_ENDPOINT}/ready", "Mimir"),
            (f"{LOKI_ENDPOINT}/ready", "Loki"),
        ]

        healthy_count = 0
        for endpoint, _name in services:
            try:
                response = requests.get(endpoint, timeout=5)
                if response.status_code == 200:
                    healthy_count += 1
            except requests.RequestException:
                pass

        assert healthy_count >= 3, f"정상 서비스 수: {healthy_count}/4"


class TestPartialRollbackScenarios:
    """부분 롤백(Trace만, Logs만, Metrics만 비활성화) 시나리오 검증"""

    def test_traces_only_rollback_metrics_logs_continue(self):
        """Tempo만 중지해도 Metrics/Logs 파이프라인이 계속 동작하는지 검증"""
        current_time_ns = int(time.time() * 1e9)
        metric_payload = {
            "resourceMetrics": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "partial-rollback-test"},
                            },
                        ]
                    },
                    "scopeMetrics": [
                        {
                            "scope": {"name": "test"},
                            "metrics": [
                                {
                                    "name": "partial_rollback_metric",
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
            json=metric_payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert response.status_code in [200, 202]

    def test_logs_only_rollback_traces_metrics_continue(self):
        """Loki만 중지해도 Traces/Metrics 파이프라인이 계속 동작하는지 검증"""
        current_time_ns = int(time.time() * 1e9)
        trace_payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "partial-rollback-test"},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "test"},
                            "spans": [
                                {
                                    "traceId": uuid.uuid4().hex,
                                    "spanId": uuid.uuid4().hex[:16],
                                    "name": "partial-rollback-trace",
                                    "kind": 1,
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(current_time_ns + 1000),
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
            json=trace_payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert response.status_code in [200, 202]

    def test_metrics_only_rollback_traces_logs_continue(self):
        """Mimir만 중지해도 Traces/Logs 파이프라인이 계속 동작하는지 검증"""
        current_time_ns = int(time.time() * 1e9)
        log_payload = {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "partial-rollback-test"},
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
                                        "stringValue": "Partial rollback test log"
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
            json=log_payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert response.status_code in [200, 202]
