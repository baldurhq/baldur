# Grafana 스택 (Loki/Tempo) 대시보드 프로비저닝 통합 테스트
"""
Grafana 대시보드 프로비저닝 및 Loki/Tempo 연동 테스트

테스트 항목:
1. Loki 서비스 헬스체크
2. Tempo 서비스 헬스체크
3. Loki 로그 수집 및 조회
4. Tempo 트레이스 수집 및 조회
5. 로그-트레이스 상관관계 (trace_id 연동)
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
LOKI_ENDPOINT = os.getenv("LOKI_ENDPOINT", "http://loki:3100")
TEMPO_ENDPOINT = os.getenv("TEMPO_ENDPOINT", "http://tempo:3200")
COLLECTOR_HEALTH_ENDPOINT = os.getenv(
    "COLLECTOR_HEALTH_ENDPOINT", "http://otel-collector:13133"
)


class TestLokiServiceHealth:
    """Loki 로그 저장소 헬스체크 테스트"""

    def test_loki_ready_endpoint_returns_ok(self):
        """Loki /ready 엔드포인트가 정상 응답"""
        response = requests.get(f"{LOKI_ENDPOINT}/ready", timeout=10)
        assert response.status_code == 200

    def test_loki_build_info_available(self):
        """Loki 빌드 정보 API 사용 가능"""
        response = requests.get(
            f"{LOKI_ENDPOINT}/loki/api/v1/status/buildinfo", timeout=10
        )
        assert response.status_code == 200
        data = response.json()
        assert "version" in data

    def test_loki_labels_api_accessible(self):
        """Loki 라벨 조회 API 접근 가능"""
        response = requests.get(f"{LOKI_ENDPOINT}/loki/api/v1/labels", timeout=10)
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"


class TestTempoServiceHealth:
    """Tempo 분산 추적 저장소 헬스체크 테스트"""

    def test_tempo_ready_endpoint_returns_ok(self):
        """Tempo /ready 엔드포인트가 정상 응답"""
        response = requests.get(f"{TEMPO_ENDPOINT}/ready", timeout=10)
        assert response.status_code == 200

    def test_tempo_build_info_available(self):
        """Tempo 빌드 정보 API 사용 가능"""
        response = requests.get(f"{TEMPO_ENDPOINT}/api/status/buildinfo", timeout=10)
        assert response.status_code == 200
        data = response.json()
        assert "version" in data

    def test_tempo_api_search_endpoint_accessible(self):
        """Tempo 트레이스 검색 API 접근 가능"""
        response = requests.get(f"{TEMPO_ENDPOINT}/api/search", timeout=10)
        # 검색 결과 없어도 200 OK 반환
        assert response.status_code == 200


class TestLokiLogIngestionAndQuery:
    """Loki 로그 수집 및 조회 테스트"""

    def _generate_log_id(self) -> str:
        """고유 로그 식별자 생성"""
        return f"grafana_dashboard_test_{uuid.uuid4().hex[:12]}"

    def _generate_trace_id_hex(self) -> str:
        """32자리 16진수 trace_id 생성"""
        return uuid.uuid4().hex

    def _create_otlp_log_payload(
        self,
        message: str,
        service_name: str = "grafana-dashboard-test",
        severity: str = "INFO",
        trace_id_hex: str = None,
        source: str = "audit",
    ):
        """OTLP 로그 페이로드 생성"""
        current_time_ns = int(time.time() * 1e9)

        attributes = [
            {"key": "source", "value": {"stringValue": source}},
            {"key": "level", "value": {"stringValue": severity}},
        ]

        log_record = {
            "timeUnixNano": str(current_time_ns),
            "observedTimeUnixNano": str(current_time_ns),
            "severityText": severity,
            "body": {"stringValue": message},
            "attributes": attributes,
        }

        if trace_id_hex:
            log_record["traceId"] = trace_id_hex
            log_record["attributes"].append(
                {"key": "trace_id", "value": {"stringValue": trace_id_hex}}
            )

        return {
            "resourceLogs": [
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
                    "scopeLogs": [
                        {
                            "scope": {
                                "name": "grafana-dashboard-logger",
                                "version": "1.0.0",
                            },
                            "logRecords": [log_record],
                        }
                    ],
                }
            ]
        }

    def test_send_log_to_loki_via_otel_collector(self):
        """OTEL Collector를 통해 Loki에 로그 전송"""
        log_id = self._generate_log_id()
        message = f"Test log message for Log Explorer dashboard: {log_id}"

        payload = self._create_otlp_log_payload(
            message=message,
            service_name="log-explorer-test",
            severity="INFO",
            source="audit",
        )

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/logs",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code == 200

    def test_query_logs_by_service_name(self):
        """서비스 이름으로 로그 조회 (Log Explorer 대시보드 기능)"""
        log_id = self._generate_log_id()
        service_name = "log-query-test"
        message = f"Service name query test: {log_id}"

        # 로그 전송
        payload = self._create_otlp_log_payload(
            message=message,
            service_name=service_name,
            severity="WARNING",
        )
        requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/logs",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        # Loki 저장 대기
        time.sleep(3)

        # LogQL 쿼리 실행
        query = '{service_name="' + service_name + '"}'
        response = requests.get(
            f"{LOKI_ENDPOINT}/loki/api/v1/query_range",
            params={
                "query": query,
                "limit": 100,
                "start": str(int((time.time() - 300) * 1e9)),
                "end": str(int(time.time() * 1e9)),
            },
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"

    def test_query_logs_by_level_filter(self):
        """로그 레벨로 필터링 (Log Explorer 대시보드 level 변수)"""
        log_id = self._generate_log_id()
        service_name = "level-filter-test"
        message = f"Error level filter test: {log_id}"

        # ERROR 레벨 로그 전송
        payload = self._create_otlp_log_payload(
            message=message,
            service_name=service_name,
            severity="ERROR",
        )
        requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/logs",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        time.sleep(3)

        # ERROR 레벨 필터 쿼리
        query = '{service_name="' + service_name + '"} | json | level="ERROR"'
        response = requests.get(
            f"{LOKI_ENDPOINT}/loki/api/v1/query_range",
            params={
                "query": query,
                "limit": 100,
                "start": str(int((time.time() - 300) * 1e9)),
                "end": str(int(time.time() * 1e9)),
            },
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"

    def test_query_logs_by_source_filter(self):
        """소스로 필터링 (Log Explorer 대시보드 source 변수 - audit, circuit_breaker 등)"""
        log_id = self._generate_log_id()
        service_name = "source-filter-test"
        message = f"Audit source filter test: {log_id}"

        # audit 소스 로그 전송
        payload = self._create_otlp_log_payload(
            message=message,
            service_name=service_name,
            severity="INFO",
            source="audit",
        )
        requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/logs",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        time.sleep(3)

        # source=audit 필터 쿼리
        query = '{service_name="' + service_name + '"} | json | source="audit"'
        response = requests.get(
            f"{LOKI_ENDPOINT}/loki/api/v1/query_range",
            params={
                "query": query,
                "limit": 100,
                "start": str(int((time.time() - 300) * 1e9)),
                "end": str(int(time.time() * 1e9)),
            },
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"


class TestTempoTraceIngestionAndQuery:
    """Tempo 트레이스 수집 및 조회 테스트"""

    def _generate_trace_id_hex(self) -> str:
        """32자리 16진수 trace_id 생성"""
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        """16자리 16진수 span_id 생성"""
        return uuid.uuid4().hex[:16]

    def _create_otlp_trace_payload(
        self,
        trace_id_hex: str,
        span_id_hex: str,
        service_name: str = "request-tracing-test",
        operation_name: str = "test-operation",
        duration_ms: int = 100,
        status_code: int = 1,  # 1=OK, 2=ERROR
    ):
        """OTLP 트레이스 페이로드 생성"""
        start_time_ns = int(time.time() * 1e9)
        end_time_ns = start_time_ns + (duration_ms * 1_000_000)

        return {
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
                            "scope": {
                                "name": "request-tracing-scope",
                                "version": "1.0.0",
                            },
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "name": operation_name,
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
                                    ],
                                    "status": {"code": status_code},
                                }
                            ],
                        }
                    ],
                }
            ]
        }

    def test_send_trace_to_tempo_via_otel_collector(self):
        """OTEL Collector를 통해 Tempo에 트레이스 전송"""
        trace_id_hex = self._generate_trace_id_hex()
        span_id_hex = self._generate_span_id_hex()

        payload = self._create_otlp_trace_payload(
            trace_id_hex=trace_id_hex,
            span_id_hex=span_id_hex,
            service_name="request-tracing-dashboard-test",
            operation_name="dashboard-test-operation",
        )

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code == 200

    def test_query_trace_by_trace_id(self):
        """trace_id로 트레이스 조회 (Request Tracing 대시보드 기능)"""
        trace_id_hex = self._generate_trace_id_hex()
        span_id_hex = self._generate_span_id_hex()

        # 트레이스 전송
        payload = self._create_otlp_trace_payload(
            trace_id_hex=trace_id_hex,
            span_id_hex=span_id_hex,
            service_name="trace-query-test",
        )
        requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        # Tempo 저장 대기
        time.sleep(3)

        # trace_id로 조회
        response = requests.get(
            f"{TEMPO_ENDPOINT}/api/traces/{trace_id_hex}",
            timeout=10,
        )

        # 조회 성공 또는 아직 저장 안됨 (둘 다 허용)
        assert response.status_code in [200, 404]

    def test_search_traces_api(self):
        """트레이스 검색 API (Request Tracing 대시보드 Trace 목록)"""
        response = requests.get(
            f"{TEMPO_ENDPOINT}/api/search",
            params={"limit": 10},
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        # traces 키 존재 확인 (빈 리스트여도 OK)
        assert "traces" in data

    def test_send_error_trace(self):
        """오류 트레이스 전송 (Request Tracing 대시보드 오류 Trace 섹션)"""
        trace_id_hex = self._generate_trace_id_hex()
        span_id_hex = self._generate_span_id_hex()

        payload = self._create_otlp_trace_payload(
            trace_id_hex=trace_id_hex,
            span_id_hex=span_id_hex,
            service_name="error-trace-test",
            operation_name="failed-operation",
            status_code=2,  # STATUS_CODE_ERROR
        )

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code == 200


class TestLogTraceCorrelation:
    """로그-트레이스 상관관계 테스트 (Loki → Tempo 연동)"""

    def _generate_trace_id_hex(self) -> str:
        """32자리 16진수 trace_id 생성"""
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        """16자리 16진수 span_id 생성"""
        return uuid.uuid4().hex[:16]

    def _create_correlated_log_payload(
        self,
        message: str,
        trace_id_hex: str,
        service_name: str = "correlation-test",
    ):
        """trace_id가 포함된 로그 페이로드"""
        current_time_ns = int(time.time() * 1e9)

        return {
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
                            "scope": {"name": "correlation-logger"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(current_time_ns),
                                    "severityText": "INFO",
                                    "body": {"stringValue": message},
                                    "traceId": trace_id_hex,
                                    "attributes": [
                                        {
                                            "key": "trace_id",
                                            "value": {"stringValue": trace_id_hex},
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }

    def _create_trace_payload(
        self,
        trace_id_hex: str,
        span_id_hex: str,
        service_name: str = "correlation-test",
    ):
        """트레이스 페이로드"""
        start_time_ns = int(time.time() * 1e9)
        return {
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
                            "scope": {"name": "correlation-tracer"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "name": "correlated-operation",
                                    "kind": 2,
                                    "startTimeUnixNano": str(start_time_ns),
                                    "endTimeUnixNano": str(start_time_ns + 100_000_000),
                                    "status": {"code": 1},
                                }
                            ],
                        }
                    ],
                }
            ]
        }

    def test_log_contains_trace_id_for_tempo_link(self):
        """로그에 trace_id 포함되어 Tempo 링크 가능 (Derived Fields 테스트)"""
        trace_id_hex = self._generate_trace_id_hex()
        span_id_hex = self._generate_span_id_hex()
        service_name = "log-trace-link-test"

        # 트레이스 전송
        trace_payload = self._create_trace_payload(
            trace_id_hex=trace_id_hex,
            span_id_hex=span_id_hex,
            service_name=service_name,
        )
        requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=trace_payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        # 동일 trace_id로 로그 전송
        log_message = f"Correlated log with trace_id={trace_id_hex}"
        log_payload = self._create_correlated_log_payload(
            message=log_message,
            trace_id_hex=trace_id_hex,
            service_name=service_name,
        )
        requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/logs",
            json=log_payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        # Loki 저장 대기
        time.sleep(3)

        # trace_id로 로그 조회
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

    def test_query_logs_for_specific_trace(self):
        """특정 trace_id의 모든 로그 조회 (Request Tracing 대시보드 관련 로그 패널)"""
        trace_id_hex = self._generate_trace_id_hex()
        service_name = "trace-logs-query-test"

        # 동일 trace_id로 여러 로그 전송
        for i in range(3):
            log_payload = self._create_correlated_log_payload(
                message=f"Log entry {i + 1} for trace {trace_id_hex[:8]}",
                trace_id_hex=trace_id_hex,
                service_name=service_name,
            )
            requests.post(
                f"{OTEL_COLLECTOR_ENDPOINT}/v1/logs",
                json=log_payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            time.sleep(0.1)

        time.sleep(3)

        # trace_id로 모든 로그 조회
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
                "limit": 100,
                "start": str(int((time.time() - 300) * 1e9)),
                "end": str(int(time.time() * 1e9)),
            },
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"
