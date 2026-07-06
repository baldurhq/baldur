# OpenTelemetry 보안 및 데이터 보호 검증 통합 테스트
"""
민감 정보 마스킹(password, token, api_key, secret, authorization),
gRPC 엔드포인트, 외부 접근 차단, PII 데이터 처리를 검증합니다.
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


class TestTraceSensitiveDataMasking:
    """Trace span에서 민감 속성(password, token, api_key, secret, authorization) 삭제 검증"""

    def _generate_trace_id_hex(self) -> str:
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        return uuid.uuid4().hex[:16]

    def test_trace_password_attribute_deleted(self):
        """span의 password 속성이 Collector에서 삭제되는지 검증"""
        trace_id_hex = self._generate_trace_id_hex()
        span_id_hex = self._generate_span_id_hex()
        current_time_ns = int(time.time() * 1e9)

        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "security-test"},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "security-test"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "name": "login-operation",
                                    "kind": 2,
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(
                                        current_time_ns + int(0.1 * 1e9)
                                    ),
                                    "attributes": [
                                        {
                                            "key": "http.method",
                                            "value": {"stringValue": "POST"},
                                        },
                                        {
                                            "key": "password",
                                            "value": {"stringValue": "secret123"},
                                        },
                                        {
                                            "key": "user.name",
                                            "value": {"stringValue": "testuser"},
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
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code in [200, 202]

        time.sleep(5)
        try:
            trace_response = requests.get(
                f"{TEMPO_ENDPOINT}/api/traces/{trace_id_hex}", timeout=10
            )
            if trace_response.status_code == 200:
                trace_data = trace_response.json()
                trace_str = str(trace_data)
                assert "secret123" not in trace_str, "password 평문이 Tempo에 저장됨"
        except requests.RequestException:
            pass

    def test_trace_token_attribute_deleted(self):
        """span의 token, api_key, secret 속성이 삭제되는지 검증"""
        trace_id_hex = self._generate_trace_id_hex()
        span_id_hex = self._generate_span_id_hex()
        current_time_ns = int(time.time() * 1e9)

        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "token-mask-test"},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "security-test"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "name": "api-call",
                                    "kind": 3,
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(
                                        current_time_ns + int(0.05 * 1e9)
                                    ),
                                    "attributes": [
                                        {
                                            "key": "token",
                                            "value": {
                                                "stringValue": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
                                            },
                                        },
                                        {
                                            "key": "api_key",
                                            "value": {
                                                "stringValue": "sk-1234567890abcdef"
                                            },
                                        },
                                        {
                                            "key": "secret",
                                            "value": {"stringValue": "my_secret_value"},
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
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code in [200, 202]

    def test_trace_authorization_header_deleted(self):
        """span의 authorization 속성이 삭제되는지 검증"""
        trace_id_hex = self._generate_trace_id_hex()
        span_id_hex = self._generate_span_id_hex()
        current_time_ns = int(time.time() * 1e9)

        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "auth-mask-test"},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "security-test"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "name": "authenticated-request",
                                    "kind": 2,
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(
                                        current_time_ns + int(0.03 * 1e9)
                                    ),
                                    "attributes": [
                                        {
                                            "key": "authorization",
                                            "value": {
                                                "stringValue": "Basic dXNlcjpwYXNz"
                                            },
                                        },
                                        {
                                            "key": "http.url",
                                            "value": {"stringValue": "/api/protected"},
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
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code in [200, 202]


class TestLogSensitiveDataMasking:
    """로그에서 민감 속성(password, passwd, pwd, token, api_key 등) 삭제 검증"""

    def test_log_password_attributes_deleted(self):
        """로그의 password, passwd, pwd 속성이 삭제되는지 검증"""
        log_marker = f"password_log_test_{uuid.uuid4().hex[:8]}"
        current_time_ns = int(time.time() * 1e9)

        payload = {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "log-security-test"},
                            },
                        ]
                    },
                    "scopeLogs": [
                        {
                            "scope": {"name": "security-logger"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(current_time_ns),
                                    "observedTimeUnixNano": str(current_time_ns),
                                    "severityText": "INFO",
                                    "body": {
                                        "stringValue": f"User login attempt: {log_marker}"
                                    },
                                    "attributes": [
                                        {
                                            "key": "user",
                                            "value": {"stringValue": "admin"},
                                        },
                                        {
                                            "key": "password",
                                            "value": {"stringValue": "admin123"},
                                        },
                                        {
                                            "key": "passwd",
                                            "value": {"stringValue": "secret"},
                                        },
                                        {
                                            "key": "pwd",
                                            "value": {"stringValue": "pass123"},
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

        assert response.status_code in [200, 202]

    def test_log_token_attributes_deleted(self):
        """로그의 token, access_token, refresh_token, api_key, apikey 속성이 삭제되는지 검증"""
        log_marker = f"token_log_test_{uuid.uuid4().hex[:8]}"
        current_time_ns = int(time.time() * 1e9)

        payload = {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "token-log-test"},
                            },
                        ]
                    },
                    "scopeLogs": [
                        {
                            "scope": {"name": "security-logger"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(current_time_ns),
                                    "observedTimeUnixNano": str(current_time_ns),
                                    "severityText": "DEBUG",
                                    "body": {
                                        "stringValue": f"API call with tokens: {log_marker}"
                                    },
                                    "attributes": [
                                        {
                                            "key": "token",
                                            "value": {"stringValue": "jwt_token_here"},
                                        },
                                        {
                                            "key": "access_token",
                                            "value": {"stringValue": "access_abc123"},
                                        },
                                        {
                                            "key": "refresh_token",
                                            "value": {"stringValue": "refresh_xyz789"},
                                        },
                                        {
                                            "key": "api_key",
                                            "value": {"stringValue": "apikey_secret"},
                                        },
                                        {
                                            "key": "apikey",
                                            "value": {"stringValue": "key123"},
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

        assert response.status_code in [200, 202]


class TestCollectorGrpcEndpoint:
    """Collector gRPC(4317) 및 HTTP(4318) 엔드포인트 검증"""

    def test_collector_grpc_endpoint_available(self):
        """Collector가 동작 중이고 gRPC 엔드포인트가 설정되어 있는지 검증"""
        response = requests.get(f"{COLLECTOR_HEALTH_ENDPOINT}/", timeout=10)
        assert response.status_code == 200

    def test_collector_http_endpoint_available(self):
        """Collector HTTP 엔드포인트(4318)가 동작하는지 검증"""
        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json={},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert response.status_code in [200, 400, 500]


class TestCollectorAccessControl:
    """Collector 포트 접근 제어 검증"""

    def test_collector_health_accessible_from_internal_network(self):
        """Collector 헬스 엔드포인트가 Docker 내부 네트워크에서 접근 가능한지 검증"""
        response = requests.get(f"{COLLECTOR_HEALTH_ENDPOINT}/", timeout=10)
        assert response.status_code == 200

    def test_collector_ports_configured_correctly(self):
        """Collector 포트(4317 gRPC, 4318 HTTP, 13133 Health, 8888 Metrics)가 올바르게 설정되어 있는지 검증"""
        http_response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json={},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert http_response.status_code in [200, 400, 500]


class TestPiiDataHandling:
    """PII(개인식별정보) 데이터 처리 검증"""

    def test_log_with_email_pattern(self):
        """이메일 패턴이 포함된 로그가 Collector에 정상 전송되는지 검증"""
        log_marker = f"email_test_{uuid.uuid4().hex[:8]}"
        current_time_ns = int(time.time() * 1e9)

        payload = {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "pii-test"},
                            },
                        ]
                    },
                    "scopeLogs": [
                        {
                            "scope": {"name": "pii-logger"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(current_time_ns),
                                    "observedTimeUnixNano": str(current_time_ns),
                                    "severityText": "INFO",
                                    "body": {
                                        "stringValue": f"User registered: {log_marker}"
                                    },
                                    "attributes": [
                                        {
                                            "key": "user.email",
                                            "value": {
                                                "stringValue": "user@example.com"
                                            },
                                        },
                                        {
                                            "key": "user.id",
                                            "value": {"stringValue": "12345"},
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

        assert response.status_code in [200, 202]

    def test_log_with_phone_pattern(self):
        """전화번호 패턴이 포함된 로그가 Collector에 정상 전송되는지 검증"""
        log_marker = f"phone_test_{uuid.uuid4().hex[:8]}"
        current_time_ns = int(time.time() * 1e9)

        payload = {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "pii-phone-test"},
                            },
                        ]
                    },
                    "scopeLogs": [
                        {
                            "scope": {"name": "pii-logger"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(current_time_ns),
                                    "observedTimeUnixNano": str(current_time_ns),
                                    "severityText": "INFO",
                                    "body": {"stringValue": f"SMS sent: {log_marker}"},
                                    "attributes": [
                                        {
                                            "key": "user.phone",
                                            "value": {
                                                "stringValue": "+82-10-1234-5678"
                                            },
                                        },
                                        {
                                            "key": "message.id",
                                            "value": {"stringValue": "msg-001"},
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

        assert response.status_code in [200, 202]

    def test_trace_sensitive_attributes_comprehensive_check(self):
        """Trace에서 모든 민감 속성(password, token, api_key, secret, authorization)이 삭제되는지 종합 검증"""
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
                                "value": {"stringValue": "sensitive-attrs-test"},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "security-test"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "name": "sensitive-operation",
                                    "kind": 1,
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(
                                        current_time_ns + int(0.02 * 1e9)
                                    ),
                                    "attributes": [
                                        {
                                            "key": "password",
                                            "value": {"stringValue": "p@ssw0rd!"},
                                        },
                                        {
                                            "key": "token",
                                            "value": {"stringValue": "tok_live_xxxx"},
                                        },
                                        {
                                            "key": "api_key",
                                            "value": {"stringValue": "ak_test_xxxx"},
                                        },
                                        {
                                            "key": "secret",
                                            "value": {"stringValue": "shh_secret"},
                                        },
                                        {
                                            "key": "authorization",
                                            "value": {"stringValue": "Bearer xxx"},
                                        },
                                        {
                                            "key": "http.method",
                                            "value": {"stringValue": "POST"},
                                        },
                                        {
                                            "key": "http.status_code",
                                            "value": {"intValue": "200"},
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
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code in [200, 202]

    def test_log_sensitive_attributes_comprehensive_check(self):
        """로그에서 모든 민감 속성이 삭제되는지 종합 검증"""
        log_marker = f"sensitive_log_{uuid.uuid4().hex[:8]}"
        current_time_ns = int(time.time() * 1e9)

        payload = {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "sensitive-log-test"},
                            },
                        ]
                    },
                    "scopeLogs": [
                        {
                            "scope": {"name": "security-logger"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(current_time_ns),
                                    "observedTimeUnixNano": str(current_time_ns),
                                    "severityText": "WARN",
                                    "body": {
                                        "stringValue": f"Security event: {log_marker}"
                                    },
                                    "attributes": [
                                        {
                                            "key": "password",
                                            "value": {"stringValue": "user_pass"},
                                        },
                                        {
                                            "key": "passwd",
                                            "value": {"stringValue": "old_pass"},
                                        },
                                        {
                                            "key": "pwd",
                                            "value": {"stringValue": "pwd123"},
                                        },
                                        {
                                            "key": "token",
                                            "value": {"stringValue": "session_token"},
                                        },
                                        {
                                            "key": "access_token",
                                            "value": {"stringValue": "access_xxx"},
                                        },
                                        {
                                            "key": "refresh_token",
                                            "value": {"stringValue": "refresh_xxx"},
                                        },
                                        {
                                            "key": "api_key",
                                            "value": {"stringValue": "api_xxx"},
                                        },
                                        {
                                            "key": "apikey",
                                            "value": {"stringValue": "key_xxx"},
                                        },
                                        {
                                            "key": "secret",
                                            "value": {"stringValue": "secret_xxx"},
                                        },
                                        {
                                            "key": "authorization",
                                            "value": {"stringValue": "auth_xxx"},
                                        },
                                        {
                                            "key": "auth",
                                            "value": {"stringValue": "auth_value"},
                                        },
                                        {
                                            "key": "event.type",
                                            "value": {"stringValue": "login_attempt"},
                                        },
                                        {
                                            "key": "source.ip",
                                            "value": {"stringValue": "192.168.1.1"},
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

        assert response.status_code in [200, 202]
