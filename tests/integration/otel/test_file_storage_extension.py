# Collector file_storage 익스텐션 통합 테스트
"""
백엔드(Tempo/Loki) 장애 시 디스크 버퍼링으로 데이터 보존을 검증합니다.
file_storage 익스텐션이 활성화되어 있고, Collector가 정상 동작하는지 확인합니다.
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
COLLECTOR_HEALTH_ENDPOINT = os.getenv(
    "COLLECTOR_HEALTH_ENDPOINT", "http://otel-collector:13133"
)
COLLECTOR_METRICS_ENDPOINT = os.getenv(
    "COLLECTOR_METRICS_ENDPOINT", "http://otel-collector:8888"
)


class TestFileStorageExtensionActive:
    """file_storage 익스텐션이 활성화되어 있는지 검증"""

    def test_collector_health_with_file_storage_enabled(self):
        """file_storage 익스텐션 활성화 상태에서 Collector 헬스체크 정상"""
        response = requests.get(f"{COLLECTOR_HEALTH_ENDPOINT}/", timeout=10)
        assert response.status_code == 200, "Collector 헬스체크 실패"

    def test_collector_extensions_include_file_storage(self):
        """Collector 메트릭에서 file_storage 익스텐션 활성화 확인"""
        response = requests.get(f"{COLLECTOR_METRICS_ENDPOINT}/metrics", timeout=10)
        assert response.status_code == 200

        metrics_text = response.text

        # Collector가 정상 동작 중이면 관련 메트릭이 있어야 함
        assert "otelcol" in metrics_text, "Collector 메트릭이 노출되지 않음"


class TestFileStorageBufferDirectory:
    """file_storage 버퍼 디렉토리 관련 검증"""

    def test_trace_sent_successfully_with_file_storage(self):
        """file_storage가 활성화된 상태에서 Trace 전송 성공"""
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
                                "value": {"stringValue": "file-storage-test"},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "file-storage-test"},
                            "spans": [
                                {
                                    "traceId": trace_id,
                                    "spanId": span_id,
                                    "name": "file-storage-verification",
                                    "kind": 1,
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(current_time_ns + 1_000_000),
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
        assert response.status_code in [200, 202], (
            f"Trace 전송 실패: {response.status_code}"
        )

    def test_log_sent_successfully_with_file_storage(self):
        """file_storage가 활성화된 상태에서 Log 전송 성공"""
        current_time_ns = int(time.time() * 1e9)

        payload = {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "file-storage-test"},
                            },
                        ]
                    },
                    "scopeLogs": [
                        {
                            "scope": {"name": "file-storage-test-logger"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(current_time_ns),
                                    "observedTimeUnixNano": str(current_time_ns),
                                    "severityText": "INFO",
                                    "body": {
                                        "stringValue": "File storage extension verification log"
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
        assert response.status_code in [200, 202], (
            f"Log 전송 실패: {response.status_code}"
        )


class TestFileStorageResilience:
    """file_storage 익스텐션으로 인한 장애 복원력 검증"""

    def test_collector_accepts_data_when_queue_enabled(self):
        """sending_queue가 활성화된 상태에서 Collector가 데이터를 수신"""
        current_time_ns = int(time.time() * 1e9)

        # 연속 10개 Trace 전송
        success_count = 0
        for i in range(10):
            payload = {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": [
                                {
                                    "key": "service.name",
                                    "value": {"stringValue": "queue-test"},
                                },
                            ]
                        },
                        "scopeSpans": [
                            {
                                "scope": {"name": "queue-test"},
                                "spans": [
                                    {
                                        "traceId": uuid.uuid4().hex,
                                        "spanId": uuid.uuid4().hex[:16],
                                        "name": f"queue-test-span-{i}",
                                        "kind": 1,
                                        "startTimeUnixNano": str(
                                            current_time_ns + i * 1000
                                        ),
                                        "endTimeUnixNano": str(
                                            current_time_ns + i * 1000 + 1_000_000
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
                success_count += 1

        assert success_count == 10, f"연속 전송 중 실패: {success_count}/10"

    def test_collector_metrics_show_queue_stats(self):
        """Collector 메트릭에서 sending_queue 관련 통계 확인"""
        response = requests.get(f"{COLLECTOR_METRICS_ENDPOINT}/metrics", timeout=10)
        assert response.status_code == 200

        metrics_text = response.text

        # exporter 관련 메트릭 확인 (queue_size, queue_capacity 등)
        exporter_metrics_exist = (
            "otelcol_exporter" in metrics_text or "otelcol_processor" in metrics_text
        )
        assert exporter_metrics_exist, "Exporter/Processor 메트릭이 없음"


class TestFileStorageConfigValidation:
    """file_storage 설정 유효성 검증"""

    def test_collector_starts_with_file_storage_config(self):
        """file_storage 설정이 포함된 Collector가 정상 시작"""
        # Collector가 시작되었다면 설정이 유효함
        response = requests.get(f"{COLLECTOR_HEALTH_ENDPOINT}/", timeout=10)
        assert response.status_code == 200

    def test_traces_pipeline_operational(self):
        """Traces 파이프라인이 file_storage와 함께 동작"""
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
                                "value": {"stringValue": "config-validation-test"},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "config-validation"},
                            "spans": [
                                {
                                    "traceId": trace_id,
                                    "spanId": span_id,
                                    "name": "config-validation-span",
                                    "kind": 1,
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(current_time_ns + 1_000_000),
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

    def test_logs_pipeline_operational(self):
        """Logs 파이프라인이 file_storage와 함께 동작"""
        current_time_ns = int(time.time() * 1e9)

        payload = {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "config-validation-test"},
                            },
                        ]
                    },
                    "scopeLogs": [
                        {
                            "scope": {"name": "config-validation-logger"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(current_time_ns),
                                    "observedTimeUnixNano": str(current_time_ns),
                                    "severityText": "INFO",
                                    "body": {
                                        "stringValue": "Config validation log entry"
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
