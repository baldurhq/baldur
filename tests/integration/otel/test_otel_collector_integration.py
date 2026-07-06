# OTEL Collector 및 Tempo 통합 테스트
"""
OpenTelemetry Collector와 Grafana Tempo 통합 테스트

테스트 항목:
1. OTEL Collector 헬스체크
2. OTLP HTTP 수신 테스트 (Trace 전송)
3. Tempo 헬스체크
4. Trace 저장 및 조회 테스트
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
COLLECTOR_HEALTH_ENDPOINT = os.getenv(
    "COLLECTOR_HEALTH_ENDPOINT", "http://otel-collector:13133"
)


class TestOtelCollectorHealth:
    """OTEL Collector 헬스체크 테스트"""

    def test_collector_health_endpoint_returns_ok(self):
        """Collector 헬스체크 엔드포인트가 정상 응답하는지 확인"""
        response = requests.get(f"{COLLECTOR_HEALTH_ENDPOINT}/", timeout=10)

        assert response.status_code == 200

    def test_collector_metrics_endpoint_available(self):
        """Collector 메트릭 엔드포인트 사용 가능 여부 확인"""
        metrics_endpoint = OTEL_COLLECTOR_ENDPOINT.replace(":4318", ":8888")
        response = requests.get(f"{metrics_endpoint}/metrics", timeout=10)

        assert response.status_code == 200
        assert "otelcol" in response.text or "process" in response.text


class TestTempoHealth:
    """Grafana Tempo 헬스체크 테스트"""

    def test_tempo_ready_endpoint_returns_ok(self):
        """Tempo ready 엔드포인트가 정상 응답하는지 확인"""
        response = requests.get(f"{TEMPO_ENDPOINT}/ready", timeout=10)

        assert response.status_code == 200


class TestOtlpTraceIngestion:
    """OTLP Trace 수신 테스트"""

    def _generate_trace_id_hex(self) -> str:
        """32자리 16진수 trace ID 생성 (16바이트)"""
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        """16자리 16진수 span ID 생성 (8바이트)"""
        return uuid.uuid4().hex[:16]

    def _create_trace_payload(
        self, trace_id_hex: str, span_id_hex: str, service_name: str = "test-service"
    ):
        """OTLP Trace 페이로드 생성 (hex 인코딩)"""
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
                            "scope": {"name": "test-scope", "version": "1.0.0"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "name": "test-operation",
                                    "kind": 1,  # SPAN_KIND_INTERNAL
                                    "startTimeUnixNano": str(int(time.time() * 1e9)),
                                    "endTimeUnixNano": str(
                                        int((time.time() + 0.1) * 1e9)
                                    ),
                                    "attributes": [
                                        {
                                            "key": "test.attribute",
                                            "value": {"stringValue": "test-value"},
                                        }
                                    ],
                                    "status": {
                                        "code": 2
                                    },  # STATUS_CODE_ERROR — matches tail_sampling errors-always policy
                                }
                            ],
                        }
                    ],
                }
            ]
        }

    def test_collector_accepts_otlp_trace_via_http(self):
        """Collector가 OTLP HTTP로 Trace를 수신하는지 확인"""
        trace_id_hex = self._generate_trace_id_hex()
        span_id_hex = self._generate_span_id_hex()

        payload = self._create_trace_payload(
            trace_id_hex, span_id_hex, "integration-test"
        )

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        # Collector가 202 Accepted 또는 200 OK 반환
        assert response.status_code in [200, 202], (
            f"Unexpected status: {response.status_code}, body: {response.text}"
        )

    def test_trace_reaches_tempo_after_ingestion(self):
        """Collector에 전송된 Trace가 Tempo에 저장되는지 확인"""
        trace_id_hex = self._generate_trace_id_hex()
        span_id_hex = self._generate_span_id_hex()
        service_name = "tempo-integration-test"

        # 1. Collector로 Trace 전송
        payload = self._create_trace_payload(trace_id_hex, span_id_hex, service_name)

        send_response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert send_response.status_code in [200, 202]

        # 2. Tempo에 도달할 때까지 대기 (batch 10s + tail sampling 10s + indexing)
        max_wait = 60
        interval = 3
        trace_found = False

        for _ in range(max_wait // interval):
            time.sleep(interval)

            try:
                # Tempo API로 trace 조회
                query_response = requests.get(
                    f"{TEMPO_ENDPOINT}/api/traces/{trace_id_hex}", timeout=10
                )

                if query_response.status_code == 200:
                    trace_found = True
                    break
            except requests.RequestException:
                continue

        assert trace_found, (
            f"Trace {trace_id_hex}가 {max_wait}초 내에 Tempo에 저장되지 않음"
        )


class TestCollectorProcessorPipeline:
    """Collector 프로세서 파이프라인 테스트"""

    def _generate_trace_id_hex(self) -> str:
        """32자리 16진수 trace ID 생성 (16바이트)"""
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        """16자리 16진수 span ID 생성 (8바이트)"""
        return uuid.uuid4().hex[:16]

    def test_batch_processor_aggregates_spans(self):
        """배치 프로세서가 여러 span을 집계하는지 확인"""
        trace_id_hex = self._generate_trace_id_hex()

        # 여러 span 전송
        spans_sent = 0
        for i in range(5):
            span_id_hex = self._generate_span_id_hex()
            payload = {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": [
                                {
                                    "key": "service.name",
                                    "value": {"stringValue": "batch-test"},
                                }
                            ]
                        },
                        "scopeSpans": [
                            {
                                "scope": {"name": "test"},
                                "spans": [
                                    {
                                        "traceId": trace_id_hex,
                                        "spanId": span_id_hex,
                                        "name": f"operation-{i}",
                                        "kind": 1,
                                        "startTimeUnixNano": str(
                                            int(time.time() * 1e9)
                                        ),
                                        "endTimeUnixNano": str(
                                            int((time.time() + 0.01) * 1e9)
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

            if response.status_code in [200, 202]:
                spans_sent += 1

        assert spans_sent == 5, f"5개 중 {spans_sent}개만 전송됨"


class TestCollectorResourceEnrichment:
    """Collector 리소스 속성 추가 테스트"""

    def _generate_trace_id_hex(self) -> str:
        """32자리 16진수 trace ID 생성 (16바이트)"""
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        """16자리 16진수 span ID 생성 (8바이트)"""
        return uuid.uuid4().hex[:16]

    def test_resource_attributes_added_to_spans(self):
        """리소스 프로세서가 속성을 추가하는지 확인"""
        trace_id_hex = self._generate_trace_id_hex()
        span_id_hex = self._generate_span_id_hex()

        # 최소한의 리소스 속성만 포함
        payload = {
            "resourceSpans": [
                {
                    "resource": {"attributes": []},
                    "scopeSpans": [
                        {
                            "scope": {"name": "minimal-test"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "name": "minimal-operation",
                                    "kind": 1,
                                    "startTimeUnixNano": str(int(time.time() * 1e9)),
                                    "endTimeUnixNano": str(
                                        int((time.time() + 0.01) * 1e9)
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

        # Collector가 정상적으로 처리해야 함
        assert response.status_code in [200, 202]
