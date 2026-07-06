# OTEL Collector Logs 파이프라인 통합 테스트
"""
OpenTelemetry Collector Logs 파이프라인 테스트

테스트 항목:
1. Loki 헬스체크
2. OTLP 로그 수신 테스트
3. 로그-트레이스 연동 (trace_id 추출) 테스트
4. 민감 정보 마스킹 테스트
5. 로그 조회 테스트
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
COLLECTOR_HEALTH_ENDPOINT = os.getenv(
    "COLLECTOR_HEALTH_ENDPOINT", "http://otel-collector:13133"
)


class TestLokiHealth:
    """Grafana Loki 헬스체크 테스트"""

    def test_loki_ready_endpoint_returns_ok(self):
        """Loki ready 엔드포인트가 정상 응답하는지 확인"""
        response = requests.get(f"{LOKI_ENDPOINT}/ready", timeout=10)

        assert response.status_code == 200

    def test_loki_api_endpoint_available(self):
        """Loki API 엔드포인트 사용 가능 여부 확인"""
        response = requests.get(
            f"{LOKI_ENDPOINT}/loki/api/v1/status/buildinfo", timeout=10
        )

        assert response.status_code == 200


class TestOtlpLogIngestion:
    """OTLP Log 수신 테스트"""

    def _generate_log_id(self) -> str:
        """고유한 로그 식별자 생성"""
        return f"log_{uuid.uuid4().hex[:12]}"

    def _generate_trace_id_hex(self) -> str:
        """32자리 16진수 trace ID 생성"""
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        """16자리 16진수 span ID 생성"""
        return uuid.uuid4().hex[:16]

    def _create_log_payload(
        self,
        message: str,
        service_name: str = "logs-integration-test",
        severity: str = "INFO",
        trace_id_hex: str = None,
        span_id_hex: str = None,
        attributes: list = None,
    ):
        """OTLP Log 페이로드 생성"""
        current_time_ns = int(time.time() * 1e9)

        log_record = {
            "timeUnixNano": str(current_time_ns),
            "observedTimeUnixNano": str(current_time_ns),
            "severityText": severity,
            "body": {"stringValue": message},
            "attributes": attributes or [],
        }

        # trace_id와 span_id 추가 (로그-트레이스 연동용)
        if trace_id_hex:
            log_record["traceId"] = trace_id_hex
        if span_id_hex:
            log_record["spanId"] = span_id_hex

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
                            "scope": {"name": "test-logger", "version": "1.0.0"},
                            "logRecords": [log_record],
                        }
                    ],
                }
            ]
        }

    def test_collector_accepts_otlp_log_via_http(self):
        """Collector가 OTLP HTTP로 Log를 수신하는지 확인"""
        log_id = self._generate_log_id()
        message = f"Test log message {log_id}"

        payload = self._create_log_payload(message)

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/logs",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code in [200, 202], (
            f"Unexpected status: {response.status_code}, body: {response.text}"
        )

    def test_collector_accepts_log_with_trace_context(self):
        """trace_id와 span_id가 포함된 로그를 수신하는지 확인"""
        log_id = self._generate_log_id()
        trace_id = self._generate_trace_id_hex()
        span_id = self._generate_span_id_hex()
        message = f"Test log with trace context {log_id}"

        payload = self._create_log_payload(
            message,
            trace_id_hex=trace_id,
            span_id_hex=span_id,
        )

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/logs",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code in [200, 202]

    def test_log_reaches_loki_after_ingestion(self):
        """Collector에 전송된 Log가 Loki에 저장되는지 확인"""
        log_id = self._generate_log_id()
        service_name = "loki-integration-test"
        message = f"Integration test log {log_id}"

        # 1. Collector로 로그 전송
        payload = self._create_log_payload(message, service_name=service_name)
        send_response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/logs",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert send_response.status_code in [200, 202]

        # 2. Loki에 로그가 도달할 때까지 대기 (최대 60초)
        max_wait = 60
        interval = 5
        log_found = False

        query = f'{{service_name="{service_name}"}} |= "{log_id}"'

        for _ in range(max_wait // interval):
            time.sleep(interval)

            try:
                query_response = requests.get(
                    f"{LOKI_ENDPOINT}/loki/api/v1/query_range",
                    params={
                        "query": query,
                        "start": str(int((time.time() - 300) * 1e9)),
                        "end": str(int(time.time() * 1e9)),
                        "limit": 10,
                    },
                    timeout=10,
                )

                if query_response.status_code == 200:
                    data = query_response.json()
                    result = data.get("data", {}).get("result", [])
                    if result:
                        log_found = True
                        break
            except requests.RequestException:
                continue

            if log_found:
                break

        assert log_found, f"Log {log_id}가 {max_wait}초 내에 Loki에 저장되지 않음"


class TestLogTraceCorrelation:
    """로그-트레이스 연동 테스트"""

    def _generate_trace_id_hex(self) -> str:
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        return uuid.uuid4().hex[:16]

    def _generate_log_id(self) -> str:
        return f"trace_log_{uuid.uuid4().hex[:8]}"

    def test_log_with_trace_id_is_ingested(self):
        """trace_id가 포함된 로그가 정상적으로 수신되는지 확인"""
        log_id = self._generate_log_id()
        trace_id = self._generate_trace_id_hex()
        span_id = self._generate_span_id_hex()

        payload = {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "trace-correlation-test"},
                            }
                        ]
                    },
                    "scopeLogs": [
                        {
                            "scope": {"name": "test-logger"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(int(time.time() * 1e9)),
                                    "severityText": "INFO",
                                    "body": {"stringValue": f"Correlated log {log_id}"},
                                    "traceId": trace_id,
                                    "spanId": span_id,
                                    "attributes": [
                                        {
                                            "key": "custom.trace_id",
                                            "value": {"stringValue": trace_id},
                                        }
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

        assert response.status_code in [200, 202]


class TestSensitiveDataMasking:
    """민감 정보 마스킹 테스트"""

    def _generate_log_id(self) -> str:
        return f"mask_log_{uuid.uuid4().hex[:8]}"

    def test_log_with_sensitive_attributes_is_accepted(self):
        """민감 정보가 포함된 로그가 수신되는지 확인 (마스킹은 Collector에서 처리)"""
        log_id = self._generate_log_id()

        # 민감 정보 속성이 포함된 로그 (Collector가 삭제해야 함)
        payload = {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "masking-test"},
                            }
                        ]
                    },
                    "scopeLogs": [
                        {
                            "scope": {"name": "test-logger"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(int(time.time() * 1e9)),
                                    "severityText": "WARN",
                                    "body": {
                                        "stringValue": f"Log with sensitive data {log_id}"
                                    },
                                    "attributes": [
                                        {
                                            "key": "user.id",
                                            "value": {"stringValue": "12345"},
                                        },
                                        # 아래 속성들은 Collector에서 삭제됨
                                        {
                                            "key": "password",
                                            "value": {"stringValue": "secret123"},
                                        },
                                        {
                                            "key": "token",
                                            "value": {"stringValue": "abc-token-xyz"},
                                        },
                                        {
                                            "key": "api_key",
                                            "value": {"stringValue": "key-12345"},
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

        # Collector가 정상적으로 수신해야 함
        assert response.status_code in [200, 202]


class TestLokiLogQLQueries:
    """Loki LogQL 쿼리 테스트"""

    def test_loki_supports_label_query(self):
        """Loki가 라벨 기반 쿼리를 지원하는지 확인"""
        response = requests.get(
            f"{LOKI_ENDPOINT}/loki/api/v1/labels",
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"

    def test_loki_supports_stream_query(self):
        """Loki가 스트림 쿼리를 지원하는지 확인"""
        response = requests.get(
            f"{LOKI_ENDPOINT}/loki/api/v1/query_range",
            params={
                "query": '{service_name=~".+"}',
                "limit": 1,
                "start": str(int((time.time() - 300) * 1e9)),
                "end": str(int(time.time() * 1e9)),
            },
            timeout=10,
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "success"


class TestCollectorLogsPipeline:
    """Collector Logs 파이프라인 테스트"""

    def test_multiple_logs_batch_processing(self):
        """여러 로그가 배치로 처리되는지 확인"""
        batch_id = uuid.uuid4().hex[:8]
        logs_sent = 0

        for i in range(5):
            payload = {
                "resourceLogs": [
                    {
                        "resource": {
                            "attributes": [
                                {
                                    "key": "service.name",
                                    "value": {"stringValue": "batch-log-test"},
                                }
                            ]
                        },
                        "scopeLogs": [
                            {
                                "scope": {"name": "test"},
                                "logRecords": [
                                    {
                                        "timeUnixNano": str(int(time.time() * 1e9)),
                                        "severityText": "INFO",
                                        "body": {
                                            "stringValue": f"Batch log {batch_id} - {i}"
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

            if response.status_code in [200, 202]:
                logs_sent += 1

        assert logs_sent == 5, f"5개 중 {logs_sent}개만 전송됨"

    def test_log_with_different_severity_levels(self):
        """다양한 심각도 레벨의 로그가 처리되는지 확인"""
        severity_levels = ["DEBUG", "INFO", "WARN", "ERROR", "FATAL"]
        success_count = 0

        for severity in severity_levels:
            payload = {
                "resourceLogs": [
                    {
                        "resource": {
                            "attributes": [
                                {
                                    "key": "service.name",
                                    "value": {"stringValue": "severity-test"},
                                }
                            ]
                        },
                        "scopeLogs": [
                            {
                                "scope": {"name": "test"},
                                "logRecords": [
                                    {
                                        "timeUnixNano": str(int(time.time() * 1e9)),
                                        "severityText": severity,
                                        "body": {"stringValue": f"Test {severity} log"},
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

            if response.status_code in [200, 202]:
                success_count += 1

        assert success_count == len(severity_levels)
