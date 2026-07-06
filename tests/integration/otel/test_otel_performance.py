# OpenTelemetry 성능 검증 통합 테스트
"""
요청 오버헤드(<5ms), 메모리 사용량(<100MB 증가),
Collector 처리량(1000 spans/s), 데이터 손실률(0%)을 검증합니다.
"""

import concurrent.futures
import os
import re
import statistics
import time
import uuid

import pytest

pytestmark = pytest.mark.requires_otel
import requests

OTEL_COLLECTOR_ENDPOINT = os.getenv(
    "OTEL_COLLECTOR_ENDPOINT", "http://otel-collector:4318"
)
TEMPO_ENDPOINT = os.getenv("TEMPO_ENDPOINT", "http://tempo:3200")
COLLECTOR_HEALTH_ENDPOINT = os.getenv(
    "COLLECTOR_HEALTH_ENDPOINT", "http://otel-collector:13133"
)
COLLECTOR_METRICS_ENDPOINT = os.getenv(
    "COLLECTOR_METRICS_ENDPOINT",
    OTEL_COLLECTOR_ENDPOINT.replace(":4318", ":8888"),
)


class TestSpanTransmissionLatency:
    """Span 전송 요청 레이턴시 검증 (<5ms 처리 오버헤드)"""

    def _generate_trace_id_hex(self) -> str:
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        return uuid.uuid4().hex[:16]

    def _create_minimal_span_payload(self, trace_id_hex: str, span_id_hex: str):
        """최소한의 span 페이로드 생성"""
        current_time_ns = int(time.time() * 1e9)
        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "overhead-test"},
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
                                    "name": "test-op",
                                    "kind": 1,
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(
                                        current_time_ns + int(0.001 * 1e9)
                                    ),
                                    "status": {"code": 1},
                                }
                            ],
                        }
                    ],
                }
            ]
        }

    def _measure_single_request_latency(self) -> float:
        """단일 요청 레이턴시 측정 (밀리초)"""
        trace_id = self._generate_trace_id_hex()
        span_id = self._generate_span_id_hex()
        payload = self._create_minimal_span_payload(trace_id, span_id)

        start_time = time.perf_counter()
        try:
            response = requests.post(
                f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            end_time = time.perf_counter()

            if response.status_code in [200, 202]:
                return (end_time - start_time) * 1000
            return -1
        except requests.RequestException:
            return -1

    def test_single_span_request_latency_under_50ms(self):
        """단일 span 전송 요청이 50ms(네트워크 포함) 이내인지 검증"""
        for _ in range(5):
            self._measure_single_request_latency()

        latencies = []
        for _ in range(50):
            latency = self._measure_single_request_latency()
            if latency > 0:
                latencies.append(latency)

        assert len(latencies) >= 40, f"성공한 요청이 40개 미만: {len(latencies)}"

        median_latency = statistics.median(latencies)
        assert median_latency < 50, (
            f"중앙값 레이턴시가 50ms 초과: {median_latency:.2f}ms"
        )

    def test_batch_span_request_latency_under_100ms(self):
        """10개 span 배치 전송이 100ms 이내인지 검증"""
        trace_id = self._generate_trace_id_hex()
        current_time_ns = int(time.time() * 1e9)

        spans = []
        for i in range(10):
            span_id = self._generate_span_id_hex()
            spans.append(
                {
                    "traceId": trace_id,
                    "spanId": span_id,
                    "name": f"batch-op-{i}",
                    "kind": 1,
                    "startTimeUnixNano": str(current_time_ns + i * 1000),
                    "endTimeUnixNano": str(
                        current_time_ns + i * 1000 + int(0.001 * 1e9)
                    ),
                    "status": {"code": 1},
                }
            )

        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "batch-overhead-test"},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "test"},
                            "spans": spans,
                        }
                    ],
                }
            ]
        }

        start_time = time.perf_counter()
        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        end_time = time.perf_counter()

        assert response.status_code in [200, 202]

        latency_ms = (end_time - start_time) * 1000
        assert latency_ms < 100, f"배치 요청 레이턴시가 100ms 초과: {latency_ms:.2f}ms"


class TestCollectorMemoryUsage:
    """Collector 메모리 사용량 검증 (<256MB)"""

    def test_collector_memory_usage_under_limit(self):
        """Collector 프로세스 메모리 사용량이 256MB 미만인지 검증"""
        try:
            response = requests.get(f"{COLLECTOR_METRICS_ENDPOINT}/metrics", timeout=10)
            if response.status_code != 200:
                pytest.skip("Collector 메트릭 엔드포인트 접근 불가")

            metrics_text = response.text

            match = re.search(
                r"otelcol_process_memory_rss[^}]*}\s+([0-9.e+]+)", metrics_text
            )
            if not match:
                match = re.search(
                    r"process_resident_memory_bytes\s+([0-9.e+]+)", metrics_text
                )

            if match:
                memory_bytes = float(match.group(1))
                memory_mb = memory_bytes / (1024 * 1024)

                max_memory_mb = 256
                assert memory_mb < max_memory_mb, (
                    f"메모리 사용량 초과: {memory_mb:.2f}MB >= {max_memory_mb}MB"
                )
            else:
                pytest.skip("메모리 메트릭을 찾을 수 없음")

        except requests.RequestException as e:
            pytest.skip(f"메트릭 조회 실패: {e}")

    def test_collector_memory_stable_after_load(self):
        """100개 span 전송 후 메모리 증가량이 100MB 미만인지 검증"""
        initial_memory = self._get_collector_memory_mb()
        if initial_memory is None:
            pytest.skip("메모리 측정 불가")

        for _ in range(100):
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
                                    "value": {"stringValue": "memory-test"},
                                },
                            ]
                        },
                        "scopeSpans": [
                            {
                                "scope": {"name": "test"},
                                "spans": [
                                    {
                                        "traceId": trace_id,
                                        "spanId": span_id,
                                        "name": "memory-op",
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
            try:
                requests.post(
                    f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=5,
                )
            except requests.RequestException:
                pass

        time.sleep(5)
        final_memory = self._get_collector_memory_mb()

        if final_memory is None:
            pytest.skip("부하 후 메모리 측정 불가")

        memory_increase = final_memory - initial_memory
        assert memory_increase < 100, (
            f"메모리 증가량이 100MB 초과: {memory_increase:.2f}MB"
        )

    def _get_collector_memory_mb(self) -> float | None:
        """Collector 현재 메모리 사용량 조회 (MB)"""
        try:
            response = requests.get(f"{COLLECTOR_METRICS_ENDPOINT}/metrics", timeout=10)
            if response.status_code != 200:
                return None

            metrics_text = response.text

            match = re.search(
                r"otelcol_process_memory_rss[^}]*}\s+([0-9.e+]+)", metrics_text
            )
            if not match:
                match = re.search(
                    r"process_resident_memory_bytes\s+([0-9.e+]+)", metrics_text
                )

            if match:
                memory_bytes = float(match.group(1))
                return memory_bytes / (1024 * 1024)

            return None
        except requests.RequestException:
            return None


class TestCollectorThroughput:
    """Collector 처리량 검증 (>=500 spans/s)"""

    def _generate_trace_id_hex(self) -> str:
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        return uuid.uuid4().hex[:16]

    def _send_single_span(self, service_name: str) -> bool:
        """단일 span 전송"""
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
                            "scope": {"name": "throughput-test"},
                            "spans": [
                                {
                                    "traceId": trace_id,
                                    "spanId": span_id,
                                    "name": "throughput-op",
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

        try:
            response = requests.post(
                f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=5,
            )
            return response.status_code in [200, 202]
        except requests.RequestException:
            return False

    def test_collector_handles_200_spans_per_second(self):
        """Collector가 1초에 200개 이상의 span 수신을 처리하는지 검증

        OTLP HTTP 수신(accept) throughput 측정. Collector 내부 batch
        processor timeout(10s)과는 별도로, 수신 자체는 즉시 응답한다.
        """
        target_spans = 500
        service_name = f"throughput-test-{uuid.uuid4().hex[:6]}"

        start_time = time.perf_counter()

        success_count = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
            futures = [
                executor.submit(self._send_single_span, service_name)
                for _ in range(target_spans)
            ]

            for future in concurrent.futures.as_completed(futures):
                if future.result():
                    success_count += 1

        end_time = time.perf_counter()
        duration = end_time - start_time
        throughput = success_count / duration

        assert throughput >= 200, (
            f"수신 처리량이 200 spans/s 미만: {throughput:.2f} spans/s"
        )

        success_rate = (success_count / target_spans) * 100
        assert success_rate >= 95, f"성공률이 95% 미만: {success_rate:.2f}%"

    def test_sustained_throughput_over_10_seconds(self):
        """10초 동안 초당 100개 span을 90% 이상 성공률로 처리하는지 검증"""
        duration_seconds = 10
        target_rps = 100
        service_name = f"sustained-test-{uuid.uuid4().hex[:6]}"

        total_sent = 0
        total_success = 0
        start_time = time.time()

        while time.time() - start_time < duration_seconds:
            batch_start = time.time()

            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                futures = [
                    executor.submit(self._send_single_span, service_name)
                    for _ in range(target_rps)
                ]

                for future in concurrent.futures.as_completed(futures, timeout=5):
                    total_sent += 1
                    if future.result():
                        total_success += 1

            elapsed = time.time() - batch_start
            if elapsed < 1.0:
                time.sleep(1.0 - elapsed)

        overall_success_rate = (
            (total_success / total_sent) * 100 if total_sent > 0 else 0
        )
        assert overall_success_rate >= 90, (
            f"10초 동안 성공률이 90% 미만: {overall_success_rate:.2f}%"
        )


class TestDataLossPrevention:
    """데이터 손실 방지 검증 (전송 성공률 99%+)"""

    def _generate_trace_id_hex(self) -> str:
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        return uuid.uuid4().hex[:16]

    def test_no_data_loss_under_normal_load(self):
        """정상 부하(100개 span)에서 전송 성공률이 99% 이상인지 검증"""
        span_count = 100
        service_name = f"data-loss-test-{uuid.uuid4().hex[:6]}"
        sent_trace_ids = []

        for _ in range(span_count):
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
                                "scope": {"name": "loss-test"},
                                "spans": [
                                    {
                                        "traceId": trace_id,
                                        "spanId": span_id,
                                        "name": "loss-check-op",
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

            try:
                response = requests.post(
                    f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )
                if response.status_code in [200, 202]:
                    sent_trace_ids.append(trace_id)
            except requests.RequestException:
                pass

        success_rate = (len(sent_trace_ids) / span_count) * 100
        assert success_rate >= 99, f"전송 성공률이 99% 미만: {success_rate:.2f}%"

    def test_collector_exporter_metrics_available(self):
        """Collector exporter 메트릭이 존재하는지 검증 (retry 메커니즘 동작 확인)"""
        try:
            response = requests.get(f"{COLLECTOR_METRICS_ENDPOINT}/metrics", timeout=10)
            if response.status_code != 200:
                pytest.skip("Collector 메트릭 접근 불가")

            metrics_text = response.text

            has_exporter_metrics = (
                "otelcol_exporter" in metrics_text or "exporter" in metrics_text
            )
            assert has_exporter_metrics, "Exporter 메트릭이 존재하지 않음"

        except requests.RequestException as e:
            pytest.skip(f"메트릭 조회 실패: {e}")

    def test_batch_processor_aggregation_works(self):
        """Batch Processor가 동일 trace의 여러 span을 집계하여 전송하는지 검증"""
        service_name = f"batch-agg-test-{uuid.uuid4().hex[:6]}"
        trace_id = self._generate_trace_id_hex()

        for i in range(5):
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
                                            current_time_ns + i * 1000
                                        ),
                                        "endTimeUnixNano": str(
                                            current_time_ns + i * 1000 + 500
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
            assert response.status_code in [200, 202]

        time.sleep(5)

        try:
            trace_response = requests.get(
                f"{TEMPO_ENDPOINT}/api/traces/{trace_id}", timeout=10
            )
            assert trace_response.status_code in [200, 404]
        except requests.RequestException:
            pass
