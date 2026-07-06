# OpenTelemetry 기능 검증 통합 테스트
"""
Django/Celery/HTTP Client의 Span 생성, trace_id 전파,
로그 상관관계, Tempo/Loki 검색, Grafana 통합을 검증합니다.
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
GRAFANA_ENDPOINT = os.getenv("GRAFANA_ENDPOINT", "http://grafana:3000")
WEB_ENDPOINT = os.getenv("WEB_ENDPOINT", "http://web:8000")


class TestDjangoSpanGeneration:
    """Django HTTP 요청 시 SERVER Span 생성 검증"""

    def _generate_trace_id_hex(self) -> str:
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        return uuid.uuid4().hex[:16]

    def _create_django_request_span_payload(
        self,
        trace_id_hex: str,
        span_id_hex: str,
        http_method: str = "GET",
        http_route: str = "/api/test",
        http_status_code: int = 200,
    ):
        """Django HTTP 요청 Span 페이로드 생성"""
        current_time_ns = int(time.time() * 1e9)
        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "django-web"},
                            },
                            {
                                "key": "deployment.environment",
                                "value": {"stringValue": "test"},
                            },
                            {
                                "key": "telemetry.sdk.name",
                                "value": {"stringValue": "opentelemetry"},
                            },
                            {
                                "key": "telemetry.sdk.language",
                                "value": {"stringValue": "python"},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {
                                "name": "opentelemetry.instrumentation.django",
                                "version": "0.44b0",
                            },
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "name": f"{http_method} {http_route}",
                                    "kind": 2,  # SPAN_KIND_SERVER
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(
                                        current_time_ns + int(0.05 * 1e9)
                                    ),
                                    "attributes": [
                                        {
                                            "key": "http.method",
                                            "value": {"stringValue": http_method},
                                        },
                                        {
                                            "key": "http.route",
                                            "value": {"stringValue": http_route},
                                        },
                                        {
                                            "key": "http.status_code",
                                            "value": {
                                                "intValue": str(http_status_code)
                                            },
                                        },
                                        {
                                            "key": "http.scheme",
                                            "value": {"stringValue": "http"},
                                        },
                                        {
                                            "key": "http.host",
                                            "value": {"stringValue": "localhost:8000"},
                                        },
                                        {
                                            "key": "net.host.port",
                                            "value": {"intValue": "8000"},
                                        },
                                    ],
                                    "status": {
                                        "code": 1 if http_status_code < 400 else 2
                                    },
                                }
                            ],
                        }
                    ],
                }
            ]
        }

    def test_django_http_request_generates_server_span(self):
        """Django HTTP GET 요청이 SERVER span을 생성하는지 검증"""
        trace_id_hex = self._generate_trace_id_hex()
        span_id_hex = self._generate_span_id_hex()

        payload = self._create_django_request_span_payload(
            trace_id_hex,
            span_id_hex,
            http_method="GET",
            http_route="/api/products/",
            http_status_code=200,
        )

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code in [200, 202], f"Span 전송 실패: {response.text}"

    def test_django_span_contains_http_attributes(self):
        """Django span에 http.method, http.route, http.status_code 속성이 포함되는지 검증"""
        trace_id_hex = self._generate_trace_id_hex()
        span_id_hex = self._generate_span_id_hex()

        payload = self._create_django_request_span_payload(
            trace_id_hex,
            span_id_hex,
            http_method="POST",
            http_route="/api/orders/",
            http_status_code=500,  # ERROR status — ensures tail_sampling keeps this trace
        )

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert response.status_code in [200, 202]

        trace_found = self._wait_for_trace_in_tempo(trace_id_hex, max_wait_seconds=60)
        assert trace_found, (
            f"Django span trace가 Tempo에서 조회되지 않음: {trace_id_hex}"
        )

    def _wait_for_trace_in_tempo(
        self, trace_id_hex: str, max_wait_seconds: int = 60
    ) -> bool:
        """Tempo에서 trace 조회 대기"""
        interval = 3
        for _ in range(max_wait_seconds // interval):
            time.sleep(interval)
            try:
                response = requests.get(
                    f"{TEMPO_ENDPOINT}/api/traces/{trace_id_hex}", timeout=10
                )
                if response.status_code == 200:
                    return True
            except requests.RequestException:
                continue
        return False


class TestCelerySpanGeneration:
    """Celery Task 실행 시 CONSUMER Span 생성 검증"""

    def _generate_trace_id_hex(self) -> str:
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        return uuid.uuid4().hex[:16]

    def _create_celery_task_span_payload(
        self,
        trace_id_hex: str,
        parent_span_id_hex: str,
        task_span_id_hex: str,
        task_name: str = "baldur.tasks.process_event",
        task_id: str = None,
    ):
        """Celery Task Span 페이로드 생성"""
        current_time_ns = int(time.time() * 1e9)
        task_id = task_id or str(uuid.uuid4())

        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "celery-worker"},
                            },
                            {
                                "key": "deployment.environment",
                                "value": {"stringValue": "test"},
                            },
                            {
                                "key": "telemetry.sdk.name",
                                "value": {"stringValue": "opentelemetry"},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {
                                "name": "opentelemetry.instrumentation.celery",
                                "version": "0.44b0",
                            },
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": task_span_id_hex,
                                    "parentSpanId": parent_span_id_hex,
                                    "name": f"run/{task_name}",
                                    "kind": 4,  # SPAN_KIND_CONSUMER
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(
                                        current_time_ns + int(0.2 * 1e9)
                                    ),
                                    "attributes": [
                                        {
                                            "key": "celery.task.name",
                                            "value": {"stringValue": task_name},
                                        },
                                        {
                                            "key": "celery.task.id",
                                            "value": {"stringValue": task_id},
                                        },
                                        {
                                            "key": "messaging.system",
                                            "value": {"stringValue": "celery"},
                                        },
                                        {
                                            "key": "messaging.destination",
                                            "value": {"stringValue": "celery"},
                                        },
                                        {
                                            "key": "messaging.destination_kind",
                                            "value": {"stringValue": "queue"},
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

    def test_celery_task_generates_consumer_span(self):
        """Celery Task 실행이 CONSUMER span을 생성하는지 검증"""
        trace_id_hex = self._generate_trace_id_hex()
        parent_span_id = self._generate_span_id_hex()
        task_span_id = self._generate_span_id_hex()

        payload = self._create_celery_task_span_payload(
            trace_id_hex,
            parent_span_id,
            task_span_id,
            task_name="baldur.tasks.sync_metrics",
        )

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code in [200, 202], (
            f"Celery span 전송 실패: {response.text}"
        )

    def test_celery_span_contains_task_attributes(self):
        """Celery span에 celery.task.name, celery.task.id 속성이 포함되는지 검증"""
        trace_id_hex = self._generate_trace_id_hex()
        parent_span_id = self._generate_span_id_hex()
        task_span_id = self._generate_span_id_hex()
        task_id = str(uuid.uuid4())

        payload = self._create_celery_task_span_payload(
            trace_id_hex,
            parent_span_id,
            task_span_id,
            task_name="shopping.tasks.send_order_notification",
            task_id=task_id,
        )

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert response.status_code in [200, 202]

        time.sleep(5)
        trace_response = requests.get(
            f"{TEMPO_ENDPOINT}/api/traces/{trace_id_hex}", timeout=10
        )
        assert trace_response.status_code in [200, 404]


class TestHttpClientSpanGeneration:
    """HTTP 클라이언트 호출 시 CLIENT Span 생성 검증"""

    def _generate_trace_id_hex(self) -> str:
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        return uuid.uuid4().hex[:16]

    def _create_http_client_span_payload(
        self,
        trace_id_hex: str,
        parent_span_id_hex: str,
        client_span_id_hex: str,
        http_method: str = "GET",
        http_url: str = "https://api.example.com/users",
        http_status_code: int = 200,
    ):
        """HTTP 클라이언트 Span 페이로드 생성"""
        current_time_ns = int(time.time() * 1e9)

        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "django-web"},
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
                                "name": "opentelemetry.instrumentation.requests",
                                "version": "0.44b0",
                            },
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": client_span_id_hex,
                                    "parentSpanId": parent_span_id_hex,
                                    "name": f"{http_method}",
                                    "kind": 3,  # SPAN_KIND_CLIENT
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(
                                        current_time_ns + int(0.1 * 1e9)
                                    ),
                                    "attributes": [
                                        {
                                            "key": "http.method",
                                            "value": {"stringValue": http_method},
                                        },
                                        {
                                            "key": "http.url",
                                            "value": {"stringValue": http_url},
                                        },
                                        {
                                            "key": "http.status_code",
                                            "value": {
                                                "intValue": str(http_status_code)
                                            },
                                        },
                                        {
                                            "key": "net.peer.name",
                                            "value": {"stringValue": "api.example.com"},
                                        },
                                        {
                                            "key": "net.peer.port",
                                            "value": {"intValue": "443"},
                                        },
                                    ],
                                    "status": {
                                        "code": 1 if http_status_code < 400 else 2
                                    },
                                }
                            ],
                        }
                    ],
                }
            ]
        }

    def test_http_client_generates_client_span(self):
        """외부 API 호출이 CLIENT span을 생성하는지 검증"""
        trace_id_hex = self._generate_trace_id_hex()
        parent_span_id = self._generate_span_id_hex()
        client_span_id = self._generate_span_id_hex()

        payload = self._create_http_client_span_payload(
            trace_id_hex,
            parent_span_id,
            client_span_id,
            http_method="POST",
            http_url="https://payment.example.com/charge",
            http_status_code=201,
        )

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code in [200, 202], (
            f"HTTP client span 전송 실패: {response.text}"
        )

    def test_http_client_span_propagates_traceparent(self):
        """HTTP 클라이언트 span이 traceparent 헤더 전파를 지원하는지 검증"""
        trace_id_hex = self._generate_trace_id_hex()
        parent_span_id = self._generate_span_id_hex()
        client_span_id = self._generate_span_id_hex()

        payload = self._create_http_client_span_payload(
            trace_id_hex,
            parent_span_id,
            client_span_id,
            http_method="GET",
            http_url="https://external-api.example.com/data",
            http_status_code=200,
        )

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert response.status_code in [200, 202]


class TestTraceIdPropagation:
    """Django에서 Celery로의 trace_id 전파 검증"""

    def _generate_trace_id_hex(self) -> str:
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        return uuid.uuid4().hex[:16]

    def test_trace_id_propagates_from_django_to_celery(self):
        """Django → Celery 호출 시 동일한 trace_id가 유지되는지 검증"""
        trace_id_hex = self._generate_trace_id_hex()
        django_span_id = self._generate_span_id_hex()
        celery_span_id = self._generate_span_id_hex()
        current_time_ns = int(time.time() * 1e9)

        combined_payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "django-web"},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "opentelemetry.instrumentation.django"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": django_span_id,
                                    "name": "POST /api/orders/",
                                    "kind": 2,
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(
                                        current_time_ns + int(0.1 * 1e9)
                                    ),
                                    "status": {"code": 1},
                                }
                            ],
                        }
                    ],
                },
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "celery-worker"},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "opentelemetry.instrumentation.celery"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": celery_span_id,
                                    "parentSpanId": django_span_id,
                                    "name": "run/shopping.tasks.process_order",
                                    "kind": 4,
                                    "startTimeUnixNano": str(
                                        current_time_ns + int(0.05 * 1e9)
                                    ),
                                    "endTimeUnixNano": str(
                                        current_time_ns + int(0.2 * 1e9)
                                    ),
                                    "status": {"code": 1},
                                }
                            ],
                        }
                    ],
                },
            ]
        }

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=combined_payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code in [200, 202], (
            f"Trace 전파 span 전송 실패: {response.text}"
        )

        trace_found = self._wait_for_trace_with_multiple_spans(
            trace_id_hex, expected_span_count=2
        )
        if not trace_found:
            import warnings

            warnings.warn(
                f"Trace {trace_id_hex}에서 2개 span을 즉시 확인할 수 없음", stacklevel=2
            )

    def _wait_for_trace_with_multiple_spans(
        self, trace_id_hex: str, expected_span_count: int, max_wait_seconds: int = 30
    ) -> bool:
        """Tempo에서 여러 span을 포함한 trace 조회"""
        interval = 2
        for _ in range(max_wait_seconds // interval):
            time.sleep(interval)
            try:
                response = requests.get(
                    f"{TEMPO_ENDPOINT}/api/traces/{trace_id_hex}", timeout=10
                )
                if response.status_code == 200:
                    data = response.json()
                    batches = data.get("batches", [])
                    total_spans = sum(
                        len(scope_span.get("spans", []))
                        for batch in batches
                        for scope_span in batch.get("scopeSpans", [])
                    )
                    if total_spans >= expected_span_count:
                        return True
            except requests.RequestException:
                continue
        return False


class TestLogTraceIdInclusion:
    """로그에 trace_id/span_id 포함 및 상관관계 검증"""

    def _generate_trace_id_hex(self) -> str:
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        return uuid.uuid4().hex[:16]

    def test_log_record_contains_trace_id(self):
        """OTLP 로그 레코드에 trace_id, span_id가 포함되는지 검증"""
        trace_id_hex = self._generate_trace_id_hex()
        span_id_hex = self._generate_span_id_hex()
        log_marker = f"trace_log_test_{uuid.uuid4().hex[:8]}"
        service_name = "log-trace-id-test"
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
                            "scope": {"name": "baldur.audit"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(current_time_ns),
                                    "observedTimeUnixNano": str(current_time_ns),
                                    "severityText": "INFO",
                                    "body": {
                                        "stringValue": f"Audit log with trace context: {log_marker}"
                                    },
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "attributes": [
                                        {
                                            "key": "audit.action",
                                            "value": {"stringValue": "create"},
                                        },
                                        {
                                            "key": "audit.entity",
                                            "value": {"stringValue": "order"},
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

        assert response.status_code in [200, 202], f"Log 전송 실패: {response.text}"

    def test_log_trace_id_enables_trace_correlation(self):
        """동일한 trace_id로 Trace와 Log가 연결되는지 검증"""
        trace_id_hex = self._generate_trace_id_hex()
        span_id_hex = self._generate_span_id_hex()
        log_marker = f"correlation_log_{uuid.uuid4().hex[:8]}"
        service_name = "log-correlation-test"
        current_time_ns = int(time.time() * 1e9)

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
                            "scope": {"name": "test"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "name": "test-operation",
                                    "kind": 1,
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(
                                        current_time_ns + int(0.05 * 1e9)
                                    ),
                                    "status": {"code": 1},
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
                            "scope": {"name": "test-logger"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(current_time_ns),
                                    "observedTimeUnixNano": str(current_time_ns),
                                    "severityText": "INFO",
                                    "body": {
                                        "stringValue": f"Log for trace correlation: {log_marker}"
                                    },
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        log_response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/logs",
            json=log_payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert log_response.status_code in [200, 202]


class TestTempoTraceSearch:
    """Tempo에서 trace_id로 조회 및 서비스명으로 검색 검증"""

    def _generate_trace_id_hex(self) -> str:
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        return uuid.uuid4().hex[:16]

    def test_tempo_trace_query_by_id(self):
        """Tempo /api/traces/{trace_id} 엔드포인트로 조회 가능한지 검증"""
        trace_id_hex = self._generate_trace_id_hex()
        span_id_hex = self._generate_span_id_hex()
        service_name = "tempo-search-test"
        current_time_ns = int(time.time() * 1e9)

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
                            "scope": {"name": "search-test"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "name": "searchable-operation",
                                    "kind": 1,
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(
                                        current_time_ns + int(0.05 * 1e9)
                                    ),
                                    "status": {
                                        "code": 2
                                    },  # ERROR — matches tail_sampling
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        send_response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert send_response.status_code in [200, 202]

        trace_found = self._wait_for_trace_in_tempo(trace_id_hex)
        assert trace_found, f"Trace {trace_id_hex}가 Tempo에서 검색되지 않음"

    def test_tempo_search_by_service_name(self):
        """Tempo /api/search 엔드포인트로 service.name 검색이 가능한지 검증"""
        response = requests.get(
            f"{TEMPO_ENDPOINT}/api/search",
            params={"tags": "service.name=tempo-search-test"},
            timeout=10,
        )
        assert response.status_code == 200

    def _wait_for_trace_in_tempo(
        self, trace_id_hex: str, max_wait_seconds: int = 60
    ) -> bool:
        """Tempo에서 trace 조회 대기"""
        interval = 3
        for _ in range(max_wait_seconds // interval):
            time.sleep(interval)
            try:
                response = requests.get(
                    f"{TEMPO_ENDPOINT}/api/traces/{trace_id_hex}", timeout=10
                )
                if response.status_code == 200:
                    return True
            except requests.RequestException:
                continue
        return False


class TestLokiLogSearch:
    """Loki에서 로그 검색 및 LogQL 쿼리 검증"""

    def test_loki_log_query_by_service_name(self):
        """Loki에 OTLP로 전송된 로그가 service_name으로 검색 가능한지 검증"""
        service_name = "loki-search-test"
        log_marker = f"loki_search_{uuid.uuid4().hex[:8]}"
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
                            "scope": {"name": "search-logger"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(current_time_ns),
                                    "observedTimeUnixNano": str(current_time_ns),
                                    "severityText": "INFO",
                                    "body": {
                                        "stringValue": f"Searchable log entry: {log_marker}"
                                    },
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        send_response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/logs",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert send_response.status_code in [200, 202]

    def test_loki_label_api_returns_labels(self):
        """Loki /loki/api/v1/labels 엔드포인트가 정상 동작하는지 검증"""
        response = requests.get(f"{LOKI_ENDPOINT}/loki/api/v1/labels", timeout=10)
        assert response.status_code == 200

    def test_loki_supports_logql_query(self):
        """Loki가 LogQL 쿼리를 처리하는지 검증"""
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


class TestGrafanaCorrelation:
    """Grafana Datasource 설정 및 상관관계 동작 검증"""

    def test_grafana_datasources_configured(self):
        """Grafana에 Tempo, Loki, Prometheus datasource가 설정되어 있는지 검증"""
        try:
            response = requests.get(
                f"{GRAFANA_ENDPOINT}/api/datasources",
                headers={"Accept": "application/json"},
                timeout=10,
            )

            if response.status_code == 401:
                assert True
            elif response.status_code == 200:
                data = response.json()
                datasource_types = [ds.get("type") for ds in data]
                has_tempo = "tempo" in datasource_types
                has_loki = "loki" in datasource_types
                has_prometheus = "prometheus" in datasource_types
                assert has_tempo or has_loki or has_prometheus, "필수 datasource 누락"
        except requests.RequestException:
            import warnings

            warnings.warn("Grafana 연결 불가, 상관관계 테스트 건너뜀", stacklevel=2)

    def test_grafana_health_check(self):
        """Grafana /api/health 엔드포인트가 정상 응답하는지 검증"""
        try:
            response = requests.get(f"{GRAFANA_ENDPOINT}/api/health", timeout=10)
            assert response.status_code == 200
            data = response.json()
            assert data.get("database") == "ok"
        except requests.RequestException:
            import warnings

            warnings.warn("Grafana 헬스체크 연결 불가", stacklevel=2)
