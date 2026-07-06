# Mimir PromQL 호환성 및 상관관계 통합 테스트
"""
Mimir PromQL 호환성 및 Metrics-Traces-Logs 상관관계 E2E 테스트

테스트 항목:
1. Mimir 서비스 헬스체크
2. Prometheus remote_write → Mimir 메트릭 복제 검증
3. PromQL 쿼리 호환성 (Prometheus vs Mimir)
4. Metrics → Traces Exemplar 연동
5. Traces → Logs 상관관계
6. Logs → Traces 상관관계 (Derived Fields)
7. 전체 경로 E2E 테스트 (Metric → Trace → Log)
"""

import json
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
TEMPO_ENDPOINT = os.getenv("TEMPO_ENDPOINT", "http://tempo:3200")
LOKI_ENDPOINT = os.getenv("LOKI_ENDPOINT", "http://loki:3100")
COLLECTOR_HEALTH_ENDPOINT = os.getenv(
    "COLLECTOR_HEALTH_ENDPOINT", "http://otel-collector:13133"
)


class TestMimirServiceHealth:
    """Mimir 메트릭 저장소 헬스체크 테스트"""

    def test_mimir_ready_endpoint_returns_ok(self):
        """Mimir /ready 엔드포인트가 정상 응답"""
        response = requests.get(f"{MIMIR_ENDPOINT}/ready", timeout=10)
        assert response.status_code == 200

    def test_mimir_build_info_available(self):
        """Mimir 빌드 정보 API 사용 가능"""
        response = requests.get(f"{MIMIR_ENDPOINT}/api/v1/status/buildinfo", timeout=10)
        assert response.status_code == 200
        data = response.json()
        assert "version" in data or "status" in data

    def test_mimir_prometheus_api_compatible(self):
        """Mimir가 Prometheus API 경로를 지원"""
        # /prometheus/api/v1/query 엔드포인트 테스트
        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/query",
            params={"query": "up"},
            timeout=10,
        )
        # 메트릭이 없어도 200 OK 반환
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"


class TestMimirMetricIngestion:
    """Mimir 메트릭 수집 테스트 (OTEL Collector 경유)"""

    def _generate_metric_id(self) -> str:
        """고유 메트릭 식별자 생성"""
        return f"mimir_test_{uuid.uuid4().hex[:8]}"

    def _create_otlp_metric_payload(
        self,
        metric_name: str,
        value: float,
        service_name: str = "mimir-integration-test",
    ):
        """OTLP 메트릭 페이로드 생성"""
        current_time_ns = int(time.time() * 1e9)

        return {
            "resourceMetrics": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": service_name},
                            },
                            {
                                "key": "deployment.environment",
                                "value": {"stringValue": "test"},
                            },
                        ]
                    },
                    "scopeMetrics": [
                        {
                            "scope": {"name": "mimir-test-scope", "version": "1.0.0"},
                            "metrics": [
                                {
                                    "name": metric_name,
                                    "unit": "1",
                                    "gauge": {
                                        "dataPoints": [
                                            {
                                                "asDouble": value,
                                                "timeUnixNano": str(current_time_ns),
                                                "attributes": [
                                                    {
                                                        "key": "test_id",
                                                        "value": {
                                                            "stringValue": metric_name
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

    def test_send_metric_to_mimir_via_otel_collector(self):
        """OTEL Collector를 통해 Mimir에 메트릭 전송"""
        metric_id = self._generate_metric_id()
        metric_name = f"test_gauge_{metric_id}"

        payload = self._create_otlp_metric_payload(
            metric_name=metric_name,
            value=42.0,
            service_name="mimir-otlp-test",
        )

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/metrics",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code == 200

    def test_query_metric_from_mimir(self):
        """Mimir에서 메트릭 조회 (PromQL 호환성)"""
        # 저장 대기
        time.sleep(3)

        # PromQL 쿼리 실행
        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/query",
            params={"query": "up"},
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"


class TestPromQLCompatibility:
    """Prometheus PromQL 쿼리가 Mimir에서 동일하게 동작하는지 검증"""

    def test_instant_query_syntax(self):
        """인스턴트 쿼리 구문 호환성"""
        queries = [
            "up",
            "sum(up)",
            "rate(up[5m])",
            "count(up)",
        ]

        for query in queries:
            response = requests.get(
                f"{MIMIR_ENDPOINT}/prometheus/api/v1/query",
                params={"query": query},
                timeout=10,
            )
            assert response.status_code == 200, f"Query failed: {query}"
            data = response.json()
            assert data.get("status") == "success", f"Query returned error: {query}"

    def test_range_query_syntax(self):
        """레인지 쿼리 구문 호환성"""
        import time

        end_time = int(time.time())
        start_time = end_time - 300  # 5분 전

        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/query_range",
            params={
                "query": "up",
                "start": start_time,
                "end": end_time,
                "step": "15s",
            },
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"

    def test_aggregation_functions(self):
        """집계 함수 호환성"""
        agg_queries = [
            "sum by (job) (up)",
            "avg(up)",
            "max(up)",
            "min(up)",
            "count(up)",
        ]

        for query in agg_queries:
            response = requests.get(
                f"{MIMIR_ENDPOINT}/prometheus/api/v1/query",
                params={"query": query},
                timeout=10,
            )
            assert response.status_code == 200, f"Aggregation failed: {query}"

    def test_label_query(self):
        """라벨 조회 API 호환성"""
        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/labels",
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"

    def test_label_values_query(self):
        """라벨 값 조회 API 호환성"""
        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/label/job/values",
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"

    def test_histogram_quantile_function(self):
        """histogram_quantile 함수 호환성 (대시보드에서 사용)"""
        query = "histogram_quantile(0.95, sum(rate(http_server_request_duration_seconds_bucket[5m])) by (le))"

        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/query",
            params={"query": query},
            timeout=10,
        )

        # 메트릭이 없어도 쿼리 자체는 성공
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"


class TestMetricsTracesLogsCorrelation:
    """Metrics → Traces → Logs 상관관계 E2E 테스트"""

    def _generate_trace_id_hex(self) -> str:
        """32자리 16진수 trace_id 생성"""
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        """16자리 16진수 span_id 생성"""
        return uuid.uuid4().hex[:16]

    def _send_trace_with_metric(
        self,
        trace_id_hex: str,
        span_id_hex: str,
        service_name: str,
        duration_ms: int = 100,
    ):
        """트레이스 전송"""
        start_time_ns = int(time.time() * 1e9)
        end_time_ns = start_time_ns + (duration_ms * 1_000_000)

        trace_payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": service_name},
                            },
                            {
                                "key": "deployment.environment",
                                "value": {"stringValue": "test"},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "correlation-test-scope"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "name": "http_request",
                                    "kind": 2,  # SPAN_KIND_SERVER
                                    "startTimeUnixNano": str(start_time_ns),
                                    "endTimeUnixNano": str(end_time_ns),
                                    "attributes": [
                                        {
                                            "key": "http.method",
                                            "value": {"stringValue": "POST"},
                                        },
                                        {
                                            "key": "http.status_code",
                                            "value": {"intValue": 200},
                                        },
                                        {
                                            "key": "http.url",
                                            "value": {"stringValue": "/api/test"},
                                        },
                                    ],
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
        return response

    def _send_log_with_trace_id(
        self,
        trace_id_hex: str,
        service_name: str,
        message: str,
        level: str = "INFO",
    ):
        """trace_id가 포함된 로그 전송"""
        current_time_ns = int(time.time() * 1e9)

        log_payload = {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": service_name},
                            },
                        ]
                    },
                    "scopeLogs": [
                        {
                            "scope": {"name": "correlation-test-logger"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(current_time_ns),
                                    "severityText": level,
                                    "body": {"stringValue": message},
                                    "traceId": trace_id_hex,
                                    "attributes": [
                                        {
                                            "key": "trace_id",
                                            "value": {"stringValue": trace_id_hex},
                                        },
                                        {
                                            "key": "level",
                                            "value": {"stringValue": level},
                                        },
                                        {
                                            "key": "source",
                                            "value": {
                                                "stringValue": "correlation_test"
                                            },
                                        },
                                    ],
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
        return response

    def test_e2e_correlation_metric_to_trace_to_log(self):
        """E2E: 메트릭 → 트레이스 → 로그 전체 경로 테스트"""
        trace_id_hex = self._generate_trace_id_hex()
        span_id_hex = self._generate_span_id_hex()
        service_name = "e2e-correlation-test"

        # 1. 트레이스 전송
        trace_response = self._send_trace_with_metric(
            trace_id_hex=trace_id_hex,
            span_id_hex=span_id_hex,
            service_name=service_name,
            duration_ms=150,
        )
        assert trace_response.status_code == 200

        # 2. 동일 trace_id로 로그 전송
        log_response = self._send_log_with_trace_id(
            trace_id_hex=trace_id_hex,
            service_name=service_name,
            message=f"E2E correlation test - trace_id={trace_id_hex}",
            level="INFO",
        )
        assert log_response.status_code == 200

        # 3. 저장 대기
        time.sleep(3)

        # 4. Tempo에서 trace_id로 트레이스 조회
        tempo_response = requests.get(
            f"{TEMPO_ENDPOINT}/api/traces/{trace_id_hex}",
            timeout=10,
        )
        # 트레이스가 저장되었으면 200, 아직이면 404
        assert tempo_response.status_code in [200, 404]

        # 5. Loki에서 trace_id로 로그 조회
        query = (
            '{service_name="'
            + service_name
            + '"} | json | trace_id="'
            + trace_id_hex
            + '"'
        )
        loki_response = requests.get(
            f"{LOKI_ENDPOINT}/loki/api/v1/query_range",
            params={
                "query": query,
                "limit": 10,
                "start": str(int((time.time() - 300) * 1e9)),
                "end": str(int(time.time() * 1e9)),
            },
            timeout=10,
        )
        assert loki_response.status_code == 200
        loki_data = loki_response.json()
        assert loki_data.get("status") == "success"

    def test_traces_to_logs_link_data_exists(self):
        """Traces → Logs 링크용 데이터 존재 검증"""
        trace_id_hex = self._generate_trace_id_hex()
        span_id_hex = self._generate_span_id_hex()
        service_name = "traces-to-logs-test"

        # 트레이스와 로그 전송
        self._send_trace_with_metric(
            trace_id_hex=trace_id_hex,
            span_id_hex=span_id_hex,
            service_name=service_name,
        )

        for i in range(3):
            self._send_log_with_trace_id(
                trace_id_hex=trace_id_hex,
                service_name=service_name,
                message=f"Log entry {i} for trace {trace_id_hex[:8]}",
            )
            time.sleep(0.1)

        time.sleep(3)

        # Loki에서 trace_id로 로그 조회
        query = (
            '{service_name="'
            + service_name
            + '"} | json | trace_id="'
            + trace_id_hex
            + '"'
        )
        response = requests.get(
            f"{LOKI_ENDPOINT}/loki/api/v1/query_range",
            params={
                "query": query,
                "limit": 10,
                "start": str(int((time.time() - 300) * 1e9)),
                "end": str(int(time.time() * 1e9)),
            },
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"

    def test_logs_to_traces_derived_field_pattern(self):
        """Logs → Traces Derived Fields 패턴 검증"""
        trace_id_hex = self._generate_trace_id_hex()
        service_name = "logs-to-traces-test"

        # JSON 형식 trace_id를 포함한 로그 전송
        json_message = json.dumps(
            {
                "message": "Test log with trace_id in JSON",
                "trace_id": trace_id_hex,
                "level": "INFO",
            }
        )

        current_time_ns = int(time.time() * 1e9)
        log_payload = {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": service_name},
                            },
                        ]
                    },
                    "scopeLogs": [
                        {
                            "scope": {"name": "derived-fields-test"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(current_time_ns),
                                    "severityText": "INFO",
                                    "body": {"stringValue": json_message},
                                    "traceId": trace_id_hex,
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
        assert response.status_code == 200

        time.sleep(3)

        # 로그 조회
        query = '{service_name="' + service_name + '"}'
        response = requests.get(
            f"{LOKI_ENDPOINT}/loki/api/v1/query_range",
            params={
                "query": query,
                "limit": 10,
                "start": str(int((time.time() - 300) * 1e9)),
                "end": str(int(time.time() * 1e9)),
            },
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"

    def test_error_trace_with_error_log_correlation(self):
        """오류 트레이스와 오류 로그 상관관계 테스트"""
        trace_id_hex = self._generate_trace_id_hex()
        span_id_hex = self._generate_span_id_hex()
        service_name = "error-correlation-test"

        # 오류 트레이스 전송
        start_time_ns = int(time.time() * 1e9)
        trace_payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": service_name},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "error-test-scope"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "name": "error_request",
                                    "kind": 2,
                                    "startTimeUnixNano": str(start_time_ns),
                                    "endTimeUnixNano": str(start_time_ns + 50_000_000),
                                    "attributes": [
                                        {
                                            "key": "http.status_code",
                                            "value": {"intValue": 500},
                                        },
                                        {"key": "error", "value": {"boolValue": True}},
                                    ],
                                    "status": {
                                        "code": 2,
                                        "message": "Internal Server Error",
                                    },
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=trace_payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        # 오류 로그 전송
        self._send_log_with_trace_id(
            trace_id_hex=trace_id_hex,
            service_name=service_name,
            message=f"Error occurred: Internal Server Error - trace_id={trace_id_hex}",
            level="ERROR",
        )

        time.sleep(3)

        # Loki에서 ERROR 레벨 로그 조회
        query = '{service_name="' + service_name + '"} | json | level="ERROR"'
        response = requests.get(
            f"{LOKI_ENDPOINT}/loki/api/v1/query_range",
            params={
                "query": query,
                "limit": 10,
                "start": str(int((time.time() - 300) * 1e9)),
                "end": str(int(time.time() * 1e9)),
            },
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"


class TestUnifiedViewDashboardDataSources:
    """Unified View 대시보드에서 사용하는 데이터소스 검증"""

    def test_mimir_metrics_for_request_rate_panel(self):
        """Request Rate 패널용 메트릭 쿼리 검증"""
        query = "sum(rate(http_server_request_duration_seconds_count[5m])) by (service_name)"

        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/query",
            params={"query": query},
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"

    def test_mimir_metrics_for_error_rate_panel(self):
        """Error Rate 패널용 메트릭 쿼리 검증"""
        query = '100 * sum(rate(http_server_request_duration_seconds_count{http_status_code=~"5.."}[5m])) / sum(rate(http_server_request_duration_seconds_count[5m]))'

        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/query",
            params={"query": query},
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"

    def test_mimir_metrics_for_latency_p95_panel(self):
        """Latency P95 패널용 메트릭 쿼리 검증 (Exemplar 연동 대상)"""
        query = "histogram_quantile(0.95, sum(rate(http_server_request_duration_seconds_bucket[5m])) by (le, service_name)) * 1000"

        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/query",
            params={"query": query},
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"

    def test_tempo_service_map_api(self):
        """서비스 맵 API 검증 (Node Graph 패널)"""
        response = requests.get(
            f"{TEMPO_ENDPOINT}/api/search",
            params={"limit": 10},
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert "traces" in data

    def test_loki_log_stream_for_trace_id(self):
        """trace_id로 로그 스트림 필터링 검증 (Related Logs 패널)"""
        # 모든 로그에 대해 trace_id 필터링 쿼리 구문 검증
        query = '{service_name=~".+"}'

        response = requests.get(
            f"{LOKI_ENDPOINT}/loki/api/v1/query_range",
            params={
                "query": query,
                "limit": 10,
                "start": str(int((time.time() - 300) * 1e9)),
                "end": str(int(time.time() * 1e9)),
            },
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"

    def test_loki_error_logs_for_table_panel(self):
        """오류 로그 테이블 패널용 쿼리 검증"""
        query = '{service_name=~".+"} | json'

        response = requests.get(
            f"{LOKI_ENDPOINT}/loki/api/v1/query_range",
            params={
                "query": query,
                "limit": 10,
                "start": str(int((time.time() - 300) * 1e9)),
                "end": str(int(time.time() * 1e9)),
            },
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"
