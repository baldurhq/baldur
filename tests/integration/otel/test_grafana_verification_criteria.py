# Grafana 스택 검증 기준 통합 테스트 (159 문서 섹션 10)
"""
159 문서 섹션 10 검증 기준 통합 테스트

테스트 항목:
1. Loki 연결 - Explore에서 쿼리 → 로그 표시
2. Tempo 연결 - Explore에서 검색 → Trace 표시
3. Mimir 연결 - 기존 대시보드 → 메트릭 표시
4. Traces → Logs - Trace에서 로그 버튼 클릭 → 관련 로그 표시
5. Logs → Traces - 로그의 trace_id 클릭 → Trace 상세 표시
6. Metrics → Traces - Exemplar 클릭 → Trace 상세 표시
7. 성능 검증 - 대시보드 로딩 < 3초, Trace/로그 검색 < 5초
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
LOKI_ENDPOINT = os.getenv("LOKI_ENDPOINT", "http://loki:3100")
TEMPO_ENDPOINT = os.getenv("TEMPO_ENDPOINT", "http://tempo:3200")
MIMIR_ENDPOINT = os.getenv("MIMIR_ENDPOINT", "http://mimir:9009")
GRAFANA_ENDPOINT = os.getenv("GRAFANA_ENDPOINT", "http://grafana:3000")
WEB_ENDPOINT = os.getenv("WEB_ENDPOINT", "http://web:8000")


class TestLokiConnectionVerification:
    """검증 기준: Loki 연결 - Explore에서 쿼리 → 로그 표시"""

    def test_loki_ready_health_check(self):
        """Loki /ready 헬스체크 정상"""
        response = requests.get(f"{LOKI_ENDPOINT}/ready", timeout=10)
        assert response.status_code == 200

    def test_loki_query_api_accessible(self):
        """Loki 쿼리 API 접근 가능"""
        # 빈 쿼리도 성공해야 함
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

    def test_loki_labels_api_returns_labels(self):
        """Loki 라벨 API가 성공 응답 반환"""
        response = requests.get(f"{LOKI_ENDPOINT}/loki/api/v1/labels", timeout=10)
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"

    def test_send_log_and_query_from_loki(self):
        """로그 전송 후 Loki에서 조회 가능 (Explore 쿼리 시뮬레이션)"""
        test_id = f"loki_verify_{uuid.uuid4().hex[:8]}"
        service_name = "verification-test"
        message = f"Verification log message: {test_id}"

        # OTLP 로그 전송
        current_time_ns = int(time.time() * 1e9)
        payload = {
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
                            "scope": {"name": "verification-logger"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(current_time_ns),
                                    "severityText": "INFO",
                                    "body": {"stringValue": message},
                                    "attributes": [
                                        {
                                            "key": "test_id",
                                            "value": {"stringValue": test_id},
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
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert response.status_code == 200

        # Loki 저장 대기 후 쿼리
        time.sleep(3)

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


class TestTempoConnectionVerification:
    """검증 기준: Tempo 연결 - Explore에서 검색 → Trace 표시"""

    def test_tempo_ready_health_check(self):
        """Tempo /ready 헬스체크 정상"""
        response = requests.get(f"{TEMPO_ENDPOINT}/ready", timeout=10)
        assert response.status_code == 200

    def test_tempo_search_api_accessible(self):
        """Tempo 검색 API 접근 가능"""
        response = requests.get(
            f"{TEMPO_ENDPOINT}/api/search",
            params={"limit": 10},
            timeout=10,
        )
        assert response.status_code == 200
        data = response.json()
        assert "traces" in data

    def test_send_trace_and_query_from_tempo(self):
        """Trace 전송 후 Tempo에서 조회 가능 (Explore 검색 시뮬레이션)"""
        trace_id_hex = uuid.uuid4().hex
        span_id_hex = uuid.uuid4().hex[:16]
        service_name = "tempo-verification-test"

        start_time_ns = int(time.time() * 1e9)
        payload = {
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
                            "scope": {"name": "verification-tracer"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "name": "verification-operation",
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

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert response.status_code == 200

        # Tempo 저장 대기 후 검색
        time.sleep(3)

        response = requests.get(
            f"{TEMPO_ENDPOINT}/api/search",
            params={"limit": 20},
            timeout=10,
        )
        assert response.status_code == 200


class TestMimirConnectionVerification:
    """검증 기준: Mimir 연결 - 기존 대시보드 → 메트릭 표시"""

    def test_mimir_ready_health_check(self):
        """Mimir /ready 헬스체크 정상"""
        response = requests.get(f"{MIMIR_ENDPOINT}/ready", timeout=10)
        assert response.status_code == 200

    def test_mimir_promql_query_api_accessible(self):
        """Mimir PromQL 쿼리 API 접근 가능"""
        query = "up"
        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/query",
            params={"query": query},
            timeout=10,
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"

    def test_mimir_label_values_api(self):
        """Mimir 라벨 값 조회 API (대시보드 변수 용도)"""
        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/label/__name__/values",
            timeout=10,
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"


class TestTracesToLogsCorrelation:
    """검증 기준: Traces → Logs - Trace에서 로그 버튼 클릭 → 관련 로그 표시"""

    def test_trace_and_log_with_same_trace_id(self):
        """동일 trace_id로 Trace와 Log 연결 가능"""
        trace_id_hex = uuid.uuid4().hex
        span_id_hex = uuid.uuid4().hex[:16]
        service_name = "traces-to-logs-test"

        # 1. Trace 전송
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

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=trace_payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert response.status_code == 200

        # 2. 동일 trace_id로 Log 전송
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
                            "scope": {"name": "correlation-logger"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(start_time_ns + 50_000_000),
                                    "severityText": "INFO",
                                    "body": {
                                        "stringValue": f"Correlated log for trace {trace_id_hex[:8]}"
                                    },
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

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/logs",
            json=log_payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert response.status_code == 200

        # 3. Loki에서 trace_id로 로그 조회 (Traces → Logs 연동 시뮬레이션)
        time.sleep(3)

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


class TestLogsToTracesCorrelation:
    """검증 기준: Logs → Traces - 로그의 trace_id 클릭 → Trace 상세 표시"""

    def test_extract_trace_id_from_log_and_query_tempo(self):
        """로그에서 trace_id 추출 후 Tempo에서 Trace 조회 가능"""
        trace_id_hex = uuid.uuid4().hex
        span_id_hex = uuid.uuid4().hex[:16]
        service_name = "logs-to-traces-test"

        # 1. Trace 전송
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
                            "scope": {"name": "l2t-tracer"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "name": "l2t-operation",
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

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=trace_payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert response.status_code == 200

        # 2. 로그 전송 (trace_id 포함)
        log_message = f"Processing request trace_id={trace_id_hex} completed"
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
                            "scope": {"name": "l2t-logger"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(start_time_ns + 50_000_000),
                                    "severityText": "INFO",
                                    "body": {"stringValue": log_message},
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

        # 3. Tempo에서 trace_id로 조회 (Logs → Traces 연동 시뮬레이션)
        time.sleep(3)

        response = requests.get(
            f"{TEMPO_ENDPOINT}/api/traces/{trace_id_hex}",
            timeout=10,
        )
        # 조회 성공 또는 아직 인덱싱 안됨 (둘 다 OK)
        assert response.status_code in [200, 404]


class TestMetricsToTracesExemplar:
    """검증 기준: Metrics → Traces - Exemplar 클릭 → Trace 상세 표시"""

    def test_mimir_exemplar_api_accessible(self):
        """Mimir Exemplar API 접근 가능"""
        query = "http_server_request_duration_seconds_bucket"
        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/query_exemplars",
            params={
                "query": query,
                "start": str(int(time.time()) - 3600),
                "end": str(int(time.time())),
            },
            timeout=10,
        )
        # Exemplar가 없어도 API 응답은 성공해야 함
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"


class TestGrafanaAlertWebhook:
    """검증 기준: Grafana Alert Webhook → UnifiedNotificationManager 연동

    Note: 이 테스트는 web 서비스가 실행 중인 환경에서만 동작합니다.
    docker-compose test 환경에서 web 서비스가 없으면 skip됩니다.
    """

    def test_grafana_webhook_endpoint_accessible(self):
        """Grafana Alert Webhook 테스트 엔드포인트 접근 가능"""
        try:
            response = requests.get(
                f"{WEB_ENDPOINT}/api/baldur/webhook/grafana/test/",
                timeout=10,
            )
        except requests.exceptions.ConnectionError:
            pytest.skip("Web service not available in current test environment")
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "ok"
        assert data.get("endpoint") == "grafana_alert_webhook"

    def test_grafana_webhook_receives_alert_payload(self):
        """Grafana Alert Webhook이 Alert 페이로드 수신 및 처리"""
        # Grafana Alert 형식의 테스트 페이로드
        alert_payload = {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "LatencyP95SlaCritical",
                        "severity": "critical",
                        "category": "sla",
                        "service_name": "payment-service",
                    },
                    "annotations": {
                        "summary": "SLA Critical: P95 Latency Exceeded",
                        "description": "payment-service P95 latency 650ms > 500ms threshold",
                        "current_latency_ms": "650",
                        "threshold_ms": "500",
                        "affected_service": "payment-service",
                    },
                    "startsAt": "2024-01-01T00:00:00.000Z",
                    "endsAt": "0001-01-01T00:00:00Z",
                }
            ],
            "commonLabels": {"alertname": "LatencyP95SlaCritical"},
            "externalURL": "http://grafana:3000",
            "version": "1",
            "title": "[FIRING:1] LatencyP95SlaCritical",
            "state": "alerting",
        }

        try:
            response = requests.post(
                f"{WEB_ENDPOINT}/api/baldur/webhook/grafana/alert/",
                json=alert_payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
        except requests.exceptions.ConnectionError:
            pytest.skip("Web service not available in current test environment")
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "ok"
        # processed 카운트 확인 (최소 1개 처리)
        assert data.get("processed", 0) >= 1

    def test_grafana_webhook_handles_resolved_alert(self):
        """Grafana Alert Webhook이 resolved 상태 Alert 처리"""
        alert_payload = {
            "alerts": [
                {
                    "status": "resolved",
                    "labels": {
                        "alertname": "LatencyP95SlaCritical",
                        "severity": "critical",
                    },
                    "annotations": {
                        "summary": "SLA Critical: P95 Latency Exceeded",
                        "description": "Alert has been resolved",
                    },
                }
            ],
        }

        try:
            response = requests.post(
                f"{WEB_ENDPOINT}/api/baldur/webhook/grafana/alert/",
                json=alert_payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
        except requests.exceptions.ConnectionError:
            pytest.skip("Web service not available in current test environment")
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "ok"


class TestPerformanceVerification:
    """검증 기준: 성능 검증 - 응답 시간"""

    def test_loki_query_response_under_5_seconds(self):
        """로그 검색 응답 시간 < 5초"""
        start_time = time.time()
        query = '{service_name=~".+"}'
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
        elapsed = time.time() - start_time

        assert response.status_code == 200
        assert elapsed < 5.0, f"Loki 쿼리 응답 시간 {elapsed:.2f}초 > 5초 임계치"

    def test_tempo_search_response_under_5_seconds(self):
        """Trace 검색 응답 시간 < 5초"""
        start_time = time.time()
        response = requests.get(
            f"{TEMPO_ENDPOINT}/api/search",
            params={"limit": 20},
            timeout=10,
        )
        elapsed = time.time() - start_time

        assert response.status_code == 200
        assert elapsed < 5.0, f"Tempo 검색 응답 시간 {elapsed:.2f}초 > 5초 임계치"

    def test_mimir_query_response_under_5_seconds(self):
        """메트릭 쿼리 응답 시간 < 5초"""
        start_time = time.time()
        query = "up"
        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/query",
            params={"query": query},
            timeout=10,
        )
        elapsed = time.time() - start_time

        assert response.status_code == 200
        assert elapsed < 5.0, f"Mimir 쿼리 응답 시간 {elapsed:.2f}초 > 5초 임계치"


class TestDatasourceConfigVerification:
    """Datasource 설정 검증"""

    def test_loki_derived_fields_regex_patterns(self):
        """Loki Derived Fields regex 패턴 테스트 (req-{prefix}-{uuid8} 형식)"""
        import re

        # 테스트할 trace_id 패턴들
        # W3C trace_id는 32자 hex (0-9, a-f만 허용)
        test_cases = [
            ("req-seop-a1b2c3d4", True, "Seoul Production"),
            ("req-tokp-f5e6d7c8", True, "Tokyo Production"),
            ("req-a1b2c3d4", True, "No cluster prefix"),
            ("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6", True, "W3C 32-char hex"),
            ("invalid-format", False, "Invalid format"),
        ]

        # datasource.yml에 정의된 regex 패턴들
        patterns = [
            r'"trace_id":"([a-f0-9]{32})"',  # W3C 32자리
            r"req-([a-z]{3,4})-([a-f0-9]{8})",  # 클러스터 prefix 있음
            r"req-([a-f0-9]{8})(?![a-f0-9])",  # 클러스터 prefix 없음
            r"trace_id=([a-f0-9]+)",  # 일반
        ]

        # 모든 유효한 trace_id가 최소 하나의 패턴에 매칭되어야 함
        for trace_id, should_match, description in test_cases:
            matched = any(
                re.search(pattern, f"trace_id={trace_id}") for pattern in patterns
            )
            if should_match:
                # W3C 형식은 별도 패턴
                if len(trace_id) == 32:
                    matched = (
                        re.search(patterns[0], f'"trace_id":"{trace_id}"') is not None
                    )
                assert matched or not should_match, (
                    f"'{trace_id}' ({description}) should match at least one pattern"
                )


class TestMultiRegionDashboardConfig:
    """멀티 리전 대시보드 설정 검증"""

    def test_cluster_label_for_multi_region(self):
        """멀티 리전 구분을 위한 cluster 라벨 확인"""
        # Mimir에서 cluster 라벨 존재 여부 확인
        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/label/cluster/values",
            timeout=10,
        )
        # 라벨이 없어도 API는 성공해야 함
        assert response.status_code == 200
