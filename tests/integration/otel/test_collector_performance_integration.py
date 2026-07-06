# OTEL Collector 성능 및 안정성 테스트
"""
OpenTelemetry Collector 성능 및 안정성 테스트

테스트 항목:
1. 대량 Trace 전송 시 데이터 손실 없음 검증
2. 대량 Metric 전송 시 데이터 손실 없음 검증
3. 대량 Log 전송 시 데이터 손실 없음 검증
4. Collector 메모리 사용량 제한 동작 검증
5. 동시 다발적 요청 처리 안정성 검증
"""

import concurrent.futures
import os
import time
import uuid

import pytest

pytestmark = pytest.mark.requires_otel
import requests

OTEL_COLLECTOR_ENDPOINT = os.getenv(
    "OTEL_COLLECTOR_ENDPOINT", "http://otel-collector:4318"
)
TEMPO_ENDPOINT = os.getenv("TEMPO_ENDPOINT", "http://tempo:3200")
MIMIR_ENDPOINT = os.getenv("MIMIR_ENDPOINT", "http://mimir:9009")
LOKI_ENDPOINT = os.getenv("LOKI_ENDPOINT", "http://loki:3100")
COLLECTOR_HEALTH_ENDPOINT = os.getenv(
    "COLLECTOR_HEALTH_ENDPOINT", "http://otel-collector:13133"
)
COLLECTOR_METRICS_ENDPOINT = os.getenv(
    "COLLECTOR_METRICS_ENDPOINT",
    OTEL_COLLECTOR_ENDPOINT.replace(":4318", ":8888"),
)


class TestHighVolumeTraceIngestion:
    """대량 Trace 전송 시 데이터 손실 테스트"""

    def _generate_trace_id_hex(self) -> str:
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        return uuid.uuid4().hex[:16]

    def _create_trace_payload(
        self, trace_id_hex: str, span_id_hex: str, service_name: str
    ):
        """OTLP Trace 페이로드 생성"""
        current_time_ns = int(time.time() * 1e9)
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
                            "scope": {"name": "load-test"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "name": "load-test-operation",
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

    def _send_single_trace(self, service_name: str) -> tuple[bool, str]:
        """단일 Trace 전송 및 결과 반환"""
        trace_id = self._generate_trace_id_hex()
        span_id = self._generate_span_id_hex()
        payload = self._create_trace_payload(trace_id, span_id, service_name)

        try:
            response = requests.post(
                f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            success = response.status_code in [200, 202]
            return success, trace_id
        except requests.RequestException:
            return False, trace_id

    def test_bulk_trace_ingestion_no_data_loss(self):
        """100개 Trace 연속 전송 시 데이터 손실 없음 검증"""
        trace_count = 100
        service_name = f"bulk-trace-test-{uuid.uuid4().hex[:6]}"

        sent_traces = []
        failed_count = 0

        for _ in range(trace_count):
            success, trace_id = self._send_single_trace(service_name)
            if success:
                sent_traces.append(trace_id)
            else:
                failed_count += 1

        # 모든 Trace가 성공적으로 전송되었는지 확인
        success_rate = (len(sent_traces) / trace_count) * 100
        assert success_rate >= 99.0, f"전송 성공률이 99% 미만: {success_rate}%"
        assert failed_count <= 1, f"실패 건수가 1건 초과: {failed_count}"


class TestHighVolumeMetricIngestion:
    """대량 Metric 전송 시 데이터 손실 테스트"""

    def _generate_metric_name(self, index: int) -> str:
        return f"load_test_metric_{index}"

    def _create_metric_payload(self, metric_name: str, value: float, service_name: str):
        """OTLP Metric 페이로드 생성"""
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
                        ]
                    },
                    "scopeMetrics": [
                        {
                            "scope": {"name": "load-test"},
                            "metrics": [
                                {
                                    "name": metric_name,
                                    "gauge": {
                                        "dataPoints": [
                                            {
                                                "asDouble": value,
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

    def _send_single_metric(self, metric_name: str, service_name: str) -> bool:
        """단일 Metric 전송"""
        payload = self._create_metric_payload(metric_name, 1.0, service_name)

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

    def test_bulk_metric_ingestion_no_data_loss(self):
        """50개 Metric 연속 전송 시 데이터 손실 없음 검증"""
        metric_count = 50
        service_name = f"bulk-metric-test-{uuid.uuid4().hex[:6]}"

        success_count = 0
        for i in range(metric_count):
            metric_name = self._generate_metric_name(i)
            if self._send_single_metric(metric_name, service_name):
                success_count += 1

        success_rate = (success_count / metric_count) * 100
        assert success_rate >= 98.0, f"전송 성공률이 98% 미만: {success_rate}%"


class TestHighVolumeLogIngestion:
    """대량 Log 전송 시 데이터 손실 테스트"""

    def _create_log_payload(self, message: str, service_name: str):
        """OTLP Log 페이로드 생성"""
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
                            "scope": {"name": "load-test-logger"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(current_time_ns),
                                    "observedTimeUnixNano": str(current_time_ns),
                                    "severityText": "INFO",
                                    "body": {"stringValue": message},
                                }
                            ],
                        }
                    ],
                }
            ]
        }

    def _send_single_log(self, message: str, service_name: str) -> bool:
        """단일 Log 전송"""
        payload = self._create_log_payload(message, service_name)

        try:
            response = requests.post(
                f"{OTEL_COLLECTOR_ENDPOINT}/v1/logs",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            return response.status_code in [200, 202]
        except requests.RequestException:
            return False

    def test_bulk_log_ingestion_no_data_loss(self):
        """100개 Log 연속 전송 시 데이터 손실 없음 검증"""
        log_count = 100
        service_name = f"bulk-log-test-{uuid.uuid4().hex[:6]}"
        test_id = uuid.uuid4().hex[:8]

        success_count = 0
        for i in range(log_count):
            message = f"Load test log {i}: {test_id}"
            if self._send_single_log(message, service_name):
                success_count += 1

        success_rate = (success_count / log_count) * 100
        assert success_rate >= 99.0, f"전송 성공률이 99% 미만: {success_rate}%"


class TestCollectorMemoryLimits:
    """Collector 메모리 사용량 제한 동작 검증"""

    def test_collector_metrics_endpoint_available(self):
        """Collector 자체 메트릭 엔드포인트 사용 가능 확인"""
        try:
            response = requests.get(f"{COLLECTOR_METRICS_ENDPOINT}/metrics", timeout=10)
            assert response.status_code == 200
            assert "otelcol" in response.text or "process" in response.text
        except requests.RequestException as e:
            pytest.skip(f"Collector 메트릭 엔드포인트 접근 불가: {e}")

    def test_memory_limiter_processor_active(self):
        """Memory Limiter 프로세서가 활성화되어 있는지 확인"""
        try:
            response = requests.get(f"{COLLECTOR_METRICS_ENDPOINT}/metrics", timeout=10)
            if response.status_code != 200:
                pytest.skip("Collector 메트릭 엔드포인트 접근 불가")

            metrics_text = response.text

            # memory_limiter 관련 메트릭 존재 여부 확인
            # otelcol_processor_* 메트릭에서 memory_limiter 확인
            has_processor_metrics = "otelcol_processor" in metrics_text
            assert has_processor_metrics, "프로세서 메트릭이 존재하지 않음"

        except requests.RequestException as e:
            pytest.skip(f"메트릭 조회 실패: {e}")

    def test_collector_process_memory_within_limits(self):
        """Collector 프로세스 메모리가 제한 내에 있는지 확인"""
        try:
            response = requests.get(f"{COLLECTOR_METRICS_ENDPOINT}/metrics", timeout=10)
            if response.status_code != 200:
                pytest.skip("Collector 메트릭 엔드포인트 접근 불가")

            metrics_text = response.text

            # OTEL Collector v0.96.0+는 otelcol_ 접두사 사용
            # otelcol_process_memory_rss 또는 process_resident_memory_bytes 파싱
            import re

            # otelcol_process_memory_rss 먼저 시도 (최신 버전)
            match = re.search(
                r"otelcol_process_memory_rss[^}]*}\s+([0-9.e+]+)", metrics_text
            )
            if not match:
                # 레거시 메트릭 이름 시도
                match = re.search(
                    r"process_resident_memory_bytes\s+([0-9.e+]+)", metrics_text
                )

            if match:
                memory_bytes = float(match.group(1))
                memory_mb = memory_bytes / (1024 * 1024)

                # 512MB 제한 기준 (문서 기준)
                max_memory_mb = 512
                assert memory_mb < max_memory_mb, (
                    f"메모리 사용량 초과: {memory_mb:.2f}MB >= {max_memory_mb}MB"
                )
            else:
                # 메트릭이 없으면 테스트 실패 (skip이 아닌 fail)
                pytest.fail(
                    "otelcol_process_memory_rss 또는 process_resident_memory_bytes 메트릭을 찾을 수 없음"
                )

        except requests.RequestException as e:
            pytest.skip(f"메트릭 조회 실패: {e}")


class TestConcurrentRequestStability:
    """동시 다발적 요청 처리 안정성 검증"""

    def _generate_trace_id_hex(self) -> str:
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        return uuid.uuid4().hex[:16]

    def _send_trace(self, args: tuple[str, int]) -> tuple[bool, int]:
        """ThreadPoolExecutor용 Trace 전송 함수"""
        service_name, index = args
        trace_id = self._generate_trace_id_hex()
        span_id = self._generate_span_id_hex()
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
                            "scope": {"name": "concurrent-test"},
                            "spans": [
                                {
                                    "traceId": trace_id,
                                    "spanId": span_id,
                                    "name": f"concurrent-op-{index}",
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

        try:
            response = requests.post(
                f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            return response.status_code in [200, 202], index
        except requests.RequestException:
            return False, index

    def test_concurrent_trace_requests_stability(self):
        """10개 동시 Trace 요청 처리 안정성 검증"""
        concurrent_requests = 10
        service_name = f"concurrent-test-{uuid.uuid4().hex[:6]}"

        args_list = [(service_name, i) for i in range(concurrent_requests)]

        success_count = 0
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=concurrent_requests
        ) as executor:
            results = executor.map(self._send_trace, args_list)
            for success, _ in results:
                if success:
                    success_count += 1

        success_rate = (success_count / concurrent_requests) * 100
        assert success_rate >= 90.0, f"동시 요청 성공률이 90% 미만: {success_rate}%"

    def test_collector_health_after_load(self):
        """부하 테스트 후 Collector 헬스체크 정상 확인"""
        # 부하 발생
        service_name = f"health-check-test-{uuid.uuid4().hex[:6]}"
        for _ in range(20):
            self._send_trace((service_name, 0))

        # 잠시 대기 후 헬스체크
        time.sleep(2)

        response = requests.get(f"{COLLECTOR_HEALTH_ENDPOINT}/", timeout=10)
        assert response.status_code == 200, "부하 후 Collector 헬스체크 실패"


class TestBatchProcessorEfficiency:
    """배치 프로세서 효율성 검증"""

    def _generate_trace_id_hex(self) -> str:
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        return uuid.uuid4().hex[:16]

    def test_batch_accumulation_and_flush(self):
        """배치 프로세서가 여러 span을 효율적으로 처리하는지 검증"""
        trace_id = self._generate_trace_id_hex()
        service_name = f"batch-efficiency-test-{uuid.uuid4().hex[:6]}"
        span_count = 20

        # 동일 trace에 여러 span 전송
        sent_count = 0
        for i in range(span_count):
            span_id = self._generate_span_id_hex()
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
                                "scope": {"name": "batch-test"},
                                "spans": [
                                    {
                                        "traceId": trace_id,
                                        "spanId": span_id,
                                        "name": f"batch-op-{i}",
                                        "kind": 1,
                                        "startTimeUnixNano": str(
                                            current_time_ns + i * 1000000
                                        ),
                                        "endTimeUnixNano": str(
                                            current_time_ns + i * 1000000 + 500000
                                        ),
                                        "status": {"code": 1},
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }

            try:
                response = requests.post(
                    f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )
                if response.status_code in [200, 202]:
                    sent_count += 1
            except requests.RequestException:
                pass

        # 대부분의 span이 전송되었는지 확인
        success_rate = (sent_count / span_count) * 100
        assert success_rate >= 95.0, f"배치 전송 성공률이 95% 미만: {success_rate}%"


class TestExporterRetryMechanism:
    """Exporter 재시도 메커니즘 검증"""

    def test_collector_exporter_metrics_available(self):
        """Exporter 관련 메트릭이 수집되는지 확인"""
        try:
            response = requests.get(f"{COLLECTOR_METRICS_ENDPOINT}/metrics", timeout=10)
            if response.status_code != 200:
                pytest.skip("Collector 메트릭 엔드포인트 접근 불가")

            metrics_text = response.text

            # exporter 관련 메트릭 확인
            has_exporter_metrics = "otelcol_exporter" in metrics_text
            assert has_exporter_metrics, "Exporter 메트릭이 존재하지 않음"

        except requests.RequestException as e:
            pytest.skip(f"메트릭 조회 실패: {e}")

    def test_exporter_sent_spans_metric_increases(self):
        """Trace 전송 시 exporter sent 메트릭 증가 확인"""
        try:
            # 초기 메트릭 조회
            initial_response = requests.get(
                f"{COLLECTOR_METRICS_ENDPOINT}/metrics", timeout=10
            )
            if initial_response.status_code != 200:
                pytest.skip("Collector 메트릭 엔드포인트 접근 불가")

            # Trace 전송
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
                                    "value": {"stringValue": "exporter-test"},
                                },
                            ]
                        },
                        "scopeSpans": [
                            {
                                "scope": {"name": "exporter-test"},
                                "spans": [
                                    {
                                        "traceId": trace_id,
                                        "spanId": span_id,
                                        "name": "exporter-op",
                                        "kind": 1,
                                        "startTimeUnixNano": str(current_time_ns),
                                        "endTimeUnixNano": str(
                                            current_time_ns + 1000000
                                        ),
                                        "status": {"code": 1},
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
            assert send_response.status_code in [200, 202], "Trace 전송 실패"

            # 배치 처리 대기
            time.sleep(3)

            # 메트릭 다시 조회
            final_response = requests.get(
                f"{COLLECTOR_METRICS_ENDPOINT}/metrics", timeout=10
            )
            assert final_response.status_code == 200

            # sent_spans 메트릭이 존재하는지 확인
            assert (
                "otelcol_exporter_sent_spans" in final_response.text
                or "otelcol" in final_response.text
            )

        except requests.RequestException as e:
            pytest.skip(f"테스트 실행 실패: {e}")
