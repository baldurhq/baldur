"""
Deadline Context — gRPC Deadline Propagation 패턴.

상위 서비스의 deadline을 하위 서비스에 ContextVar + HTTP 헤더로 전파한다.
남은 시간이 예상 처리시간 미만이면 즉시 거절(Fast-Fail)하여
무의미한 작업을 방지한다.

MSA 호출 체인 A → B → C 에서:
- A가 3초 타임아웃으로 B를 호출
- B가 2.5초 소요 후 C를 호출
- C는 남은 시간 0.5초인데 예상 처리시간 2초 → Fast-Fail로 자원 절약

ContextVar 기반 전파:
- WSGI gthread 환경에서 스레드별 독립 컨텍스트 보장
- ThreadPool(Bulkhead, Hedging)에서 copy_context().run()으로 자동 전파
- Celery Task에는 전파하지 않음 (독립 라이프사이클)

HTTP 헤더 규약:
- X-Deadline-Remaining: 2500ms (밀리초 단위)
- 외부 진입점(Nginx)에서 클라이언트 헤더 제거 (DoS 방지)
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

import structlog

logger = structlog.get_logger()

# Deadline 기능 활성화 여부 (환경변수: BALDUR_DEADLINE_ENABLED)
DEADLINE_ENABLED: bool = os.environ.get("BALDUR_DEADLINE_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)

# HTTP 헤더 이름
DEADLINE_HEADER = "X-Deadline-Remaining"

# Django META 키 (HTTP_X_DEADLINE_REMAINING)
DEADLINE_META_KEY = "HTTP_X_DEADLINE_REMAINING"

# ContextVar: 요청 deadline (monotonic clock 기준 절대 시각)
_request_deadline: ContextVar[float | None] = ContextVar(
    "request_deadline", default=None
)

# 최소 유효 시간 (ms) — 이보다 적으면 Fast-Fail
DEFAULT_MINIMUM_USEFUL_TIME_MS: float = float(
    os.environ.get("BALDUR_DEADLINE_MINIMUM_USEFUL_MS", "50")
)

# 네트워크 레이턴시 보정 버퍼 (ms)
# 같은 AZ 내 Pod 간 1~5ms, Cross-AZ 10~30ms, 안전 마진 2× Cross-AZ = 50ms
DEFAULT_NETWORK_LATENCY_BUFFER_MS: float = float(
    os.environ.get("BALDUR_DEADLINE_NETWORK_BUFFER_MS", "50")
)

# RTT sample collection — triple-filtering constants (units: milliseconds).
# Sub-threshold ultra-short requests (health checks, etc.) are treated as noise
# and excluded from collection. Consumed by the framework-free
# ``record_rtt_sample`` post-response helper (api/middleware/deadline.py).
_RTT_MIN_SAMPLE_MS: float = float(
    os.environ.get("BALDUR_DEADLINE_RTT_MIN_SAMPLE_MS", "5")
)
# Probabilistic sampling rate (0.1 = 10%) to reduce lock contention. The EMA
# nature of the gradient makes a 10% sample sufficient to track the trend.
_RTT_SAMPLE_RATE: float = float(
    os.environ.get("BALDUR_DEADLINE_RTT_SAMPLE_RATE", "0.1")
)

# 헤더 파싱용 정규식: "2500ms", "2500", "1500.5ms" 등
_DEADLINE_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:ms)?\s*$", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Prometheus 메트릭
# ---------------------------------------------------------------------------
try:
    from baldur.metrics.registry import (
        get_or_create_counter,
        get_or_create_gauge,
        get_or_create_histogram,
    )

    _HAS_PROMETHEUS = True
except ImportError:
    _HAS_PROMETHEUS = False

if _HAS_PROMETHEUS:
    _fast_fail_counter = get_or_create_counter(
        "baldur_deadline_fast_fail_total",
        "Fast-Fail rejection count",
        ["tier", "path_prefix"],
    )
    _remaining_histogram = get_or_create_histogram(
        "baldur_deadline_remaining_ms",
        "Remaining time distribution at reception (ms)",
        ["tier"],
        buckets=(10, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000),
    )
    _exhausted_on_arrival_counter = get_or_create_counter(
        "baldur_deadline_exhausted_on_arrival_total",
        "Requests already expired on arrival",
        ["path_prefix"],
    )
    _estimated_ms_histogram = get_or_create_histogram(
        "baldur_deadline_estimated_ms",
        "Estimated processing time distribution (ms)",
        ["calculator"],
        buckets=(10, 25, 50, 100, 200, 500, 1000, 2500, 5000),
    )
    _gradient_rtt_gauge = get_or_create_gauge(
        "baldur_gradient_rtt_ms",
        "Current smoothed RTT (ms)",
        ["calculator"],
    )
    _gradient_value_gauge = get_or_create_gauge(
        "baldur_gradient_value",
        "Current gradient value",
        ["calculator"],
    )
else:
    _fast_fail_counter = None  # type: ignore[assignment]
    _remaining_histogram = None  # type: ignore[assignment]
    _exhausted_on_arrival_counter = None  # type: ignore[assignment]
    _estimated_ms_histogram = None  # type: ignore[assignment]
    _gradient_rtt_gauge = None  # type: ignore[assignment]
    _gradient_value_gauge = None  # type: ignore[assignment]


def record_fast_fail(tier: str = "unknown", path_prefix: str = "unknown") -> None:
    """Fast-Fail 거절 메트릭 기록."""
    if _HAS_PROMETHEUS and _fast_fail_counter is not None:
        _fast_fail_counter.labels(tier=tier, path_prefix=path_prefix).inc()


def record_remaining_ms(remaining: float, tier: str = "unknown") -> None:
    """수신 시점의 남은 시간 히스토그램 기록."""
    if _HAS_PROMETHEUS and _remaining_histogram is not None:
        _remaining_histogram.labels(tier=tier).observe(remaining)


def record_exhausted_on_arrival(path_prefix: str = "unknown") -> None:
    """도착 시점에 이미 만료된 요청 카운터 기록."""
    if _HAS_PROMETHEUS and _exhausted_on_arrival_counter is not None:
        _exhausted_on_arrival_counter.labels(path_prefix=path_prefix).inc()


def parse_deadline_header(header_value: str) -> float | None:
    """
    X-Deadline-Remaining 헤더 값 파싱.

    Args:
        header_value: 헤더 값 (예: "2500ms", "2500", "1500.5ms")

    Returns:
        남은 시간(ms) 또는 파싱 실패 시 None
    """
    if not header_value:
        return None

    match = _DEADLINE_PATTERN.match(header_value)
    if match:
        return float(match.group(1))

    logger.debug(
        "deadline_context.parse_header_failed",
        header_value=header_value,
    )
    return None


def set_deadline(remaining_ms: float) -> None:
    """
    현재 컨텍스트에 deadline 설정.
    네트워크 레이턴시 Buffer를 차감하여 보수적으로 계산한다.

    Args:
        remaining_ms: 남은 시간 (밀리초)
    """
    adjusted = remaining_ms - DEFAULT_NETWORK_LATENCY_BUFFER_MS

    if adjusted <= 0:
        logger.warning(
            "deadline_context.deadline_exhausted_on_arrival",
            remaining_ms=remaining_ms,
            buffer_ms=DEFAULT_NETWORK_LATENCY_BUFFER_MS,
        )
        record_exhausted_on_arrival()
        adjusted = 0

    deadline = time.monotonic() + (adjusted / 1000.0)
    _request_deadline.set(deadline)


def get_remaining_ms() -> float | None:
    """
    현재 컨텍스트의 남은 시간 반환.

    Returns:
        남은 시간(ms) 또는 deadline 미설정 시 None
    """
    deadline = _request_deadline.get()
    if deadline is None:
        return None
    remaining = (deadline - time.monotonic()) * 1000.0
    return max(0.0, remaining)


def is_expired() -> bool:
    """
    deadline이 만료되었는지 확인.

    Returns:
        만료 시 True, deadline 미설정 시 False
    """
    remaining = get_remaining_ms()
    if remaining is None:
        return False
    return remaining <= 0.0


def should_fast_fail(
    estimated_processing_ms: float,
    minimum_useful_ms: float = DEFAULT_MINIMUM_USEFUL_TIME_MS,
) -> bool:
    """
    남은 시간이 예상 처리시간 미만이면 True (Fast-Fail 권장).

    Args:
        estimated_processing_ms: 예상 처리시간 (밀리초)
        minimum_useful_ms: 최소 유효 시간 (밀리초)

    Returns:
        True이면 Fast-Fail 권장
    """
    remaining = get_remaining_ms()
    if remaining is None:
        return False  # deadline 미설정 시 Fast-Fail 하지 않음

    if remaining < minimum_useful_ms:
        return True  # 최소 유효 시간 미만

    return remaining < estimated_processing_ms


def clear_deadline() -> None:
    """현재 컨텍스트의 deadline 제거."""
    _request_deadline.set(None)


# Outbound deadline propagation = this explicit-header helper. The user /
# service mesh injects the returned relative "NNNms" value live at outbound-call
# time (correct only when computed live, not frozen at request start). OTel-
# baggage AUTO-propagation of the deadline is deferred to #593: the relative
# wire form is freeze-incompatible with the request-start baggage snapshot, so
# the correct auto path is a live-at-inject DeadlinePropagator (or an absolute
# wall-clock wire form), plus wiring the dormant instrumentation.
def get_propagation_header_value() -> str | None:
    """
    하위 서비스로 전파할 헤더 값 생성.

    Returns:
        "1234ms" 형식 또는 deadline 미설정/만료 시 None
    """
    remaining = get_remaining_ms()
    if remaining is None or remaining <= 0:
        return None
    return f"{remaining:.0f}ms"


def get_deadline_aware_statement_timeout(
    default_db_timeout_ms: int = 30_000,
) -> int | None:
    """
    DeadlineContext 남은 시간과 기본 DB timeout 중 작은 값 반환.
    deadline 미설정이거나 기본 DB timeout보다 넉넉하면 None (SET 불필요).

    Args:
        default_db_timeout_ms: DB 기본 statement_timeout (production.py 설정과 동기화)

    Returns:
        설정할 timeout(ms) 또는 None(SET 불필요)
    """
    remaining = get_remaining_ms()
    if remaining is None:
        return None  # deadline 미설정

    if remaining >= default_db_timeout_ms:
        return None  # 넉넉하면 SET 스킵

    return max(1, int(remaining))  # 최소 1ms


@contextmanager
def deadline_scope(remaining_ms: float) -> Generator[None, None, None]:
    """
    deadline 범위 컨텍스트 매니저.

    블록 진입 시 deadline을 설정하고, 블록 종료 시 이전 값을 복원한다.

    Usage:
        with deadline_scope(3000):
            if should_fast_fail(estimated_ms=2000):
                raise TimeoutError("Fast-Fail")
            process_request()

    Args:
        remaining_ms: 남은 시간 (밀리초)
    """
    previous = _request_deadline.get()
    set_deadline(remaining_ms)
    try:
        yield
    finally:
        _request_deadline.set(previous)


# =============================================================================
# Tier별 Cold Start 기본 예상 처리시간 (ms)
# RTT 데이터가 충분히 쌓이기 전까지 사용하는 Conservative Estimate.
# critical: 빠른 경로 (인증, 결제 확인 등)
# standard: 일반 CRUD 작업
# non_essential: 무거운 쿼리 (통계, 리포트 등)
# =============================================================================

DEFAULT_ESTIMATED_MS_CRITICAL: float = float(
    os.environ.get("BALDUR_DEADLINE_DEFAULT_ESTIMATED_MS_CRITICAL", "50")
)
DEFAULT_ESTIMATED_MS_STANDARD: float = float(
    os.environ.get("BALDUR_DEADLINE_DEFAULT_ESTIMATED_MS_STANDARD", "200")
)
DEFAULT_ESTIMATED_MS_NON_ESSENTIAL: float = float(
    os.environ.get("BALDUR_DEADLINE_DEFAULT_ESTIMATED_MS_NON_ESSENTIAL", "500")
)

# 예상 처리시간 안전 계수 (기본 1.5 = 50% 여유)
DEFAULT_SAFETY_MARGIN: float = float(
    os.environ.get("BALDUR_DEADLINE_SAFETY_MARGIN", "1.5")
)

_TIER_DEFAULT_ESTIMATED_MS: dict[str, float] = {
    "critical": DEFAULT_ESTIMATED_MS_CRITICAL,
    "standard": DEFAULT_ESTIMATED_MS_STANDARD,
    "non_essential": DEFAULT_ESTIMATED_MS_NON_ESSENTIAL,
}


def get_tier_default_estimated_ms(tier_id: str = "standard") -> float:
    """
    Tier별 Cold Start 기본 예상 처리시간 반환.

    GradientCalculator에 RTT 데이터가 없을 때 (Cold Start) Fallback으로 사용.

    Args:
        tier_id: Tier 식별자 (critical, standard, non_essential)

    Returns:
        기본 예상 처리시간 (ms)
    """
    return _TIER_DEFAULT_ESTIMATED_MS.get(tier_id, DEFAULT_ESTIMATED_MS_STANDARD)


def get_estimated_processing_ms(
    calculator_name: str = "default",
    safety_margin: float = DEFAULT_SAFETY_MARGIN,
    tier_id: str = "standard",
) -> float:
    """
    GradientCalculator 기반 예상 처리시간 반환.

    현재 smoothed RTT × 안전 계수로 산출합니다.
    gradient가 양수(RTT 증가 추세)이면 안전 계수를 더 높입니다.
    RTT 데이터가 없으면(Cold Start) Tier별 기본값을 반환합니다.

    Args:
        calculator_name: GradientCalculator 이름
        safety_margin: 안전 계수 (기본값: BALDUR_DEADLINE_SAFETY_MARGIN 환경변수, 미설정 시 1.5)
        tier_id: Tier 식별자 (Cold Start fallback에 사용)

    Returns:
        예상 처리시간 (ms). Cold Start 시에도 기본값을 반환하므로
        None을 반환하지 않습니다.
    """
    try:
        from baldur_pro.services.throttle.gradient import get_gradient_calculator

        calc = get_gradient_calculator(calculator_name)
        rtt, gradient = calc.get_snapshot()

        if rtt is None:
            # Cold Start: Tier별 기본값 반환
            return get_tier_default_estimated_ms(tier_id)

        # RTT 증가 추세이면 안전 계수 상향
        effective_margin = safety_margin
        if gradient > 0.1:  # 10% 이상 증가
            effective_margin *= 1.0 + gradient  # gradient 비례 증가

        estimated = rtt * effective_margin

        # Prometheus 메트릭 기록
        if _HAS_PROMETHEUS:
            if _estimated_ms_histogram is not None:
                _estimated_ms_histogram.labels(calculator=calculator_name).observe(
                    estimated
                )
            if _gradient_rtt_gauge is not None:
                _gradient_rtt_gauge.labels(calculator=calculator_name).set(rtt)
            if _gradient_value_gauge is not None:
                _gradient_value_gauge.labels(calculator=calculator_name).set(gradient)

        return estimated
    except ImportError:
        return get_tier_default_estimated_ms(tier_id)
