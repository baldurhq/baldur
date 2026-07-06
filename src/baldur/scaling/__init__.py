"""
Kubernetes Auto-Scaling & Backpressure Integration.

트래픽 폭증 시 자동으로 Pod 스케일 아웃,
과부하 시 Rate-aware Backpressure로 시스템 보호.

Components:
    - BackpressureSettings: Backpressure 설정
    - RateController: 동적 처리율 조절 (Token Bucket + AIMD)
    - TrafficGate: RateController + LoadShedding 통합 파이프라인
    - BackpressureMetrics: Prometheus 메트릭 노출
    - HPAMetricsExporter: HPA용 메트릭 내보내기
    - GracefulDegradation: 단계별 기능 축소
    - CachedQueueSizeProvider: 큐 크기 캐싱

Usage:
    from baldur.scaling import (
        get_traffic_gate,
        get_rate_controller,
        get_graceful_degradation,
        get_hpa_metrics_exporter,
    )

    # 트래픽 제어
    gate = get_traffic_gate()
    decision = gate.should_allow(priority=5)
    if decision.allowed:
        process_request()

    # Rate 제어
    controller = get_rate_controller()
    if controller.should_process():
        process_item()

    # Graceful Degradation
    degradation = get_graceful_degradation()
    if degradation.is_enabled("detailed_logging"):
        log_details()

    # HPA Metrics Exporter
    exporter = get_hpa_metrics_exporter()
    exporter.start()

Status: Internal
"""

from baldur.scaling.config import (
    LEVEL_RATE_MULTIPLIERS,
    BackpressureLevel,
    BackpressureSettings,
    BackpressureStrategy,
    get_backpressure_settings,
    reset_backpressure_settings,
)
from baldur.scaling.deadline_context import (
    DEADLINE_ENABLED,
    DEADLINE_HEADER,
    DEADLINE_META_KEY,
    DEFAULT_ESTIMATED_MS_CRITICAL,
    DEFAULT_ESTIMATED_MS_NON_ESSENTIAL,
    DEFAULT_ESTIMATED_MS_STANDARD,
    DEFAULT_MINIMUM_USEFUL_TIME_MS,
    DEFAULT_NETWORK_LATENCY_BUFFER_MS,
    clear_deadline,
    deadline_scope,
    get_deadline_aware_statement_timeout,
    get_estimated_processing_ms,
    get_propagation_header_value,
    get_remaining_ms,
    get_tier_default_estimated_ms,
    is_expired,
    parse_deadline_header,
    record_exhausted_on_arrival,
    record_fast_fail,
    record_remaining_ms,
    set_deadline,
    should_fast_fail,
)
from baldur.scaling.graceful_degradation import (
    Feature,
    FeaturePriority,
    GracefulDegradation,
    get_graceful_degradation,
)
from baldur.scaling.hpa_exporter import (
    LEVEL_TO_INT,
    HPAMetricsExporter,
    get_hpa_metrics_exporter,
    reset_hpa_metrics_exporter,
)
from baldur.scaling.metrics import (
    BackpressureMetrics,
    get_backpressure_metrics,
)
from baldur.scaling.queue_provider import CachedQueueSizeProvider
from baldur.scaling.rate_controller import (
    RateController,
    RateControllerState,
    TokenBucket,
    get_rate_controller,
    reset_rate_controller,
)
from baldur.scaling.traffic_gate import (
    TrafficDecision,
    TrafficGate,
    create_traffic_gate_with_cascade_load_shedding,
    get_traffic_gate,
    reset_traffic_gate,
    traffic_gate,
)

__all__ = [
    # Config
    "BackpressureLevel",
    "BackpressureSettings",
    "BackpressureStrategy",
    "LEVEL_RATE_MULTIPLIERS",
    "get_backpressure_settings",
    "reset_backpressure_settings",
    # Deadline Context
    "DEADLINE_ENABLED",
    "DEADLINE_HEADER",
    "DEADLINE_META_KEY",
    "DEFAULT_ESTIMATED_MS_CRITICAL",
    "DEFAULT_ESTIMATED_MS_NON_ESSENTIAL",
    "DEFAULT_ESTIMATED_MS_STANDARD",
    "DEFAULT_MINIMUM_USEFUL_TIME_MS",
    "DEFAULT_NETWORK_LATENCY_BUFFER_MS",
    "clear_deadline",
    "deadline_scope",
    "get_deadline_aware_statement_timeout",
    "get_estimated_processing_ms",
    "get_propagation_header_value",
    "get_remaining_ms",
    "get_tier_default_estimated_ms",
    "is_expired",
    "parse_deadline_header",
    "record_exhausted_on_arrival",
    "record_fast_fail",
    "record_remaining_ms",
    "set_deadline",
    "should_fast_fail",
    # Rate Controller
    "RateController",
    "RateControllerState",
    "TokenBucket",
    "get_rate_controller",
    "reset_rate_controller",
    # Traffic Gate
    "TrafficDecision",
    "TrafficGate",
    "get_traffic_gate",
    "reset_traffic_gate",
    "create_traffic_gate_with_cascade_load_shedding",
    "traffic_gate",
    # Queue Provider
    "CachedQueueSizeProvider",
    # Metrics
    "BackpressureMetrics",
    "get_backpressure_metrics",
    # HPA Exporter
    "HPAMetricsExporter",
    "LEVEL_TO_INT",
    "get_hpa_metrics_exporter",
    "reset_hpa_metrics_exporter",
    # Graceful Degradation
    "Feature",
    "FeaturePriority",
    "GracefulDegradation",
    "get_graceful_degradation",
]
