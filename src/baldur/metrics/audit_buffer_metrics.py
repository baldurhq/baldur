"""
Audit Buffer Prometheus 메트릭.

Redis Audit 버퍼의 상태와 백프레셔 수준을 Prometheus로 노출.
"""

from __future__ import annotations

from typing import Any

from baldur.metrics._metric_protocol import CounterMetric, GaugeMetric

audit_buffer_size: GaugeMetric
audit_buffer_backpressure: GaugeMetric
audit_buffer_dropped_total: CounterMetric
audit_buffer_batch_writes_total: CounterMetric
audit_buffer_batch_errors_total: CounterMetric
audit_buffer_flush_total: CounterMetric
audit_buffer_orphan_recovery_total: CounterMetric
audit_buffer_safety_ltrim_total: CounterMetric
audit_buffer_fallback_size: GaugeMetric

try:
    from baldur.metrics.registry import get_or_create_counter, get_or_create_gauge

    # 현재 버퍼 크기 (도메인별)
    audit_buffer_size = get_or_create_gauge(
        "audit_buffer_size",
        "Current size of audit buffer by domain",
        ["domain"],
    )

    # 백프레셔 수준 (0.0 ~ 1.0, 도메인별)
    audit_buffer_backpressure = get_or_create_gauge(
        "audit_buffer_backpressure",
        "Backpressure level of audit buffer (0.0-1.0)",
        ["domain"],
    )

    # 드롭된 항목 수 (Safety LTRIM으로 인한)
    audit_buffer_dropped_total = get_or_create_counter(
        "audit_buffer_dropped_total",
        "Total dropped audit entries due to buffer overflow",
        ["domain"],
    )

    # 배치 쓰기 성공 수
    audit_buffer_batch_writes_total = get_or_create_counter(
        "audit_buffer_batch_writes_total",
        "Total successful batch writes to audit buffer",
        ["domain"],
    )

    # 배치 쓰기 실패 수
    audit_buffer_batch_errors_total = get_or_create_counter(
        "audit_buffer_batch_errors_total",
        "Total failed batch writes to audit buffer",
        ["domain"],
    )

    # 플러시 성공 수
    audit_buffer_flush_total = get_or_create_counter(
        "audit_buffer_flush_total",
        "Total flushed entries from audit buffer",
        ["domain"],
    )

    # 고아 큐 복구 수
    audit_buffer_orphan_recovery_total = get_or_create_counter(
        "audit_buffer_orphan_recovery_total",
        "Total recovered entries from orphaned processing queues",
        ["domain"],
    )

    # Safety LTRIM 발생 횟수
    audit_buffer_safety_ltrim_total = get_or_create_counter(
        "audit_buffer_safety_ltrim_total",
        "Total safety LTRIM operations performed",
        ["domain"],
    )

    # 폴백 버퍼 크기
    audit_buffer_fallback_size = get_or_create_gauge(
        "audit_buffer_fallback_size",
        "Current size of in-memory fallback buffer",
        [],
    )

    METRICS_AVAILABLE = True

except ImportError:
    # prometheus_client가 없으면 더미 메트릭 생성. _DummyMetric은 GaugeMetric의
    # 상위집합 (labels + set + inc) 이므로 GaugeMetric, CounterMetric 어디든 할당 가능.
    METRICS_AVAILABLE = False

    class _DummyMetric:
        """prometheus_client 없을 때 사용하는 더미 메트릭."""

        def labels(self, *args: Any, **kwargs: Any) -> _DummyMetric:
            return self

        def set(self, value: float) -> None:
            pass

        def inc(self, amount: float = 1) -> None:
            pass

    audit_buffer_size = _DummyMetric()
    audit_buffer_backpressure = _DummyMetric()
    audit_buffer_dropped_total = _DummyMetric()
    audit_buffer_batch_writes_total = _DummyMetric()
    audit_buffer_batch_errors_total = _DummyMetric()
    audit_buffer_flush_total = _DummyMetric()
    audit_buffer_orphan_recovery_total = _DummyMetric()
    audit_buffer_safety_ltrim_total = _DummyMetric()
    audit_buffer_fallback_size = _DummyMetric()


def update_buffer_metrics(
    domain: str,
    size: int,
    max_size: int,
) -> None:
    """
    버퍼 메트릭 업데이트 헬퍼.

    Args:
        domain: 도메인 이름
        size: 현재 버퍼 크기
        max_size: 최대 버퍼 크기
    """
    audit_buffer_size.labels(domain=domain).set(size)

    backpressure = min(1.0, size / max(1, max_size))
    audit_buffer_backpressure.labels(domain=domain).set(backpressure)


def record_batch_write(domain: str, success: bool) -> None:
    """배치 쓰기 결과 기록."""
    if success:
        audit_buffer_batch_writes_total.labels(domain=domain).inc()
    else:
        audit_buffer_batch_errors_total.labels(domain=domain).inc()


def record_flush(domain: str, count: int) -> None:
    """플러시 결과 기록."""
    audit_buffer_flush_total.labels(domain=domain).inc(count)


def record_orphan_recovery(domain: str, count: int) -> None:
    """고아 큐 복구 기록."""
    audit_buffer_orphan_recovery_total.labels(domain=domain).inc(count)


def record_safety_ltrim(domain: str, dropped_count: int) -> None:
    """Safety LTRIM 기록."""
    audit_buffer_safety_ltrim_total.labels(domain=domain).inc()
    audit_buffer_dropped_total.labels(domain=domain).inc(dropped_count)
