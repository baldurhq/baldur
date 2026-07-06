"""
structlog 프로세서 — 로그 볼륨 제어 및 이벤트명 검증.

Event Name Validation (Q5, 314 Audit):
    이벤트명이 ``{component}.{entity}_{action}`` 컨벤션을 따르는지 검증.
    DEV/TEST: BALDUR_LOGGING_SETTINGS_STRICT_LOG_VALIDATION=true → ValueError (fail-fast)
    Production: 위반을 Prometheus counter로 기록만.

Rate Limiter (De-dup):
    동일 이벤트가 윈도우 내에 max_count 이상 반복되면 묵음 처리하고,
    윈도우 종료 시 "suppressed N events" 요약 1건을 출력한다.
    ERROR/CRITICAL 레벨은 절대 suppress하지 않는다.

Sampling:
    Hot path 로그(INFO/DEBUG)를 확률적으로 샘플링하여 볼륨을 줄인다.
    WARNING 이상은 항상 통과한다.
    특정 이벤트 이름만 대상으로 하여 중요 로그를 보호한다.

설정:
    LoggingSettings 에서 환경변수로 제어:
    - BALDUR_LOGGING_SETTINGS_STRICT_LOG_VALIDATION=true/false
    - BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_WINDOW=10
    - BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_MAX=100
    - BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_RATE=1.0
    - BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_EVENTS=event1,event2

Reference:
    - docs/baldur/middleware_system/281_LOG_RATE_LIMITER.md
    - docs/baldur/middleware_system/282_LOG_SAMPLING.md
    - docs/baldur/middleware_system/312_EXCEPTION_HIERARCHY_LOGGING_STANDARDIZATION.md
"""

from __future__ import annotations

import random
import re
import threading
import time
from typing import Any

import structlog

# =============================================================================
# Event Name Validation 프로세서 (Q5)
# =============================================================================

_EVENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

_violation_counter_initialized = False
_violation_counter = None


def _get_violation_counter():
    """Prometheus counter를 lazy-init한다."""
    global _violation_counter_initialized, _violation_counter
    if _violation_counter_initialized:
        return _violation_counter
    _violation_counter_initialized = True
    try:
        from baldur.metrics.registry import get_or_create_counter

        _violation_counter = get_or_create_counter(
            "baldur_log_convention_violations_total",
            "Count of log events violating naming convention",
            ["event_name"],
        )
    except ImportError:
        _violation_counter = None
    return _violation_counter


_strict_validation_cached: bool | None = None


def _is_strict_validation() -> bool:
    """LoggingSettings의 strict_log_validation 설정을 캐싱하여 O(1) 조회한다."""
    global _strict_validation_cached
    if _strict_validation_cached is not None:
        return _strict_validation_cached
    try:
        from baldur.settings.logging_settings import get_logging_settings

        _strict_validation_cached = get_logging_settings().strict_log_validation
    except Exception:
        _strict_validation_cached = False
    return _strict_validation_cached


def reset_strict_validation_cache() -> None:
    """strict validation 캐시를 초기화한다. 테스트용."""
    global _strict_validation_cached
    _strict_validation_cached = None


def event_name_validator(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """이벤트명이 ``component.entity_action`` 컨벤션을 따르는지 검증한다.

    Pipeline position: add_logger_name 직후 (무거운 처리 전).

    DEV/TEST (BALDUR_STRICT_LOG_VALIDATION=true):
        컨벤션 위반 시 ValueError 발생 (fail-fast).
    Production (기본값):
        위반을 Prometheus counter로 기록만 하고 로그를 통과시킨다.
    """
    event_name = event_dict.get("event", "")
    if not event_name or not isinstance(event_name, str):
        return event_dict

    if _EVENT_NAME_PATTERN.match(event_name):
        return event_dict

    # Convention violation detected
    if _is_strict_validation():
        raise ValueError(
            f"Log event name '{event_name}' violates naming convention. "
            f"Expected pattern: 'component.entity_action' (lowercase, dot-separated)"
        )

    counter = _get_violation_counter()
    if counter is not None:
        counter.labels(event_name=event_name).inc()

    return event_dict


# =============================================================================
# Rate Limiter (De-dup) 프로세서
# =============================================================================

# 이벤트별 카운터: {(logger_name, event): {"count": int, "window_start": float, "suppressed": int}}
_rate_limit_state: dict[tuple[str, str], dict[str, Any]] = {}
_rate_limit_lock = threading.Lock()

# suppress 하지 않는 레벨 (에러/장애 로그는 항상 통과)
_NEVER_SUPPRESS_LEVELS = frozenset({"error", "critical"})


def _get_rate_limit_settings() -> tuple[int, int]:
    """Rate limit 설정을 로드한다. 실패 시 안전한 기본값 반환."""
    try:
        from baldur.settings.logging_settings import get_logging_settings

        settings = get_logging_settings()
        return (
            int(getattr(settings, "log_rate_limit_window", 10)),
            int(getattr(settings, "log_rate_limit_max", 100)),
        )
    except Exception:
        return (10, 100)


def rate_limit_processor(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """동일 이벤트 반복 시 de-dup하는 structlog 프로세서.

    동작:
    1. (logger_name, event) 키로 윈도우 내 발생 횟수를 추적
    2. max_count 이하: 그대로 통과
    3. max_count 초과: suppress하고 카운터 증가
    4. 다음 윈도우 전환 시: "suppressed N similar events" 요약 로그 출력 후 카운터 리셋

    ERROR/CRITICAL 레벨은 suppress하지 않는다.
    """
    # ERROR/CRITICAL은 절대 suppress하지 않음
    if method_name in _NEVER_SUPPRESS_LEVELS:
        return event_dict

    window_seconds, max_count = _get_rate_limit_settings()

    # rate limiting이 비활성화된 경우 (max=0이면 무제한)
    if max_count <= 0 or window_seconds <= 0:
        return event_dict

    event_name = event_dict.get("event", "")
    logger_name = event_dict.get("logger", "unknown")
    key = (logger_name, event_name)
    now = time.monotonic()

    with _rate_limit_lock:
        state = _rate_limit_state.get(key)

        if state is None or (now - state["window_start"]) >= window_seconds:
            # 새 윈도우 시작 또는 윈도우 만료
            suppressed_count = state["suppressed"] if state else 0

            # 이전 윈도우에서 suppress된 이벤트가 있으면 요약 로그를 주입
            if suppressed_count > 0:
                event_dict["_rate_limit_suppressed_previous"] = suppressed_count

            _rate_limit_state[key] = {
                "count": 1,
                "window_start": now,
                "suppressed": 0,
            }
            return event_dict

        state["count"] += 1

        if state["count"] <= max_count:
            return event_dict

        # max_count 초과: suppress
        state["suppressed"] += 1
        raise structlog.DropEvent


def reset_rate_limit_state() -> None:
    """Rate limit 상태를 초기화한다. 테스트용."""
    with _rate_limit_lock:
        _rate_limit_state.clear()


# =============================================================================
# Sampling 프로세서
# =============================================================================

# DEBUG/INFO 레벨만 샘플링 대상 (WARNING 이상은 항상 통과)
_SAMPLING_TARGET_LEVELS = frozenset({"debug", "info"})


def _get_sampling_settings() -> tuple[float, frozenset[str]]:
    """샘플링 설정을 로드한다. 실패 시 안전한 기본값 반환."""
    try:
        from baldur.settings.logging_settings import get_logging_settings

        settings = get_logging_settings()
        rate = float(getattr(settings, "log_sampling_rate", 1.0))
        events_str = str(getattr(settings, "log_sampling_events", "") or "")
        if events_str:
            events = frozenset(e.strip() for e in events_str.split(",") if e.strip())
        else:
            events = frozenset()
        return (rate, events)
    except Exception:
        return (1.0, frozenset())


def sampling_processor(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Hot path 로그를 확률적으로 샘플링하는 structlog 프로세서.

    동작:
    1. WARNING 이상: 항상 통과
    2. log_sampling_events가 비어있으면: 모든 DEBUG/INFO에 sample_rate 적용
    3. log_sampling_events가 설정되어 있으면: 해당 이벤트에만 sample_rate 적용
    4. random() > sample_rate이면 DropEvent

    설정:
    - BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_RATE=0.1  (10%만 기록)
    - BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_EVENTS=circuit_breaker.checked,action_executor.execute
    """
    # WARNING 이상은 항상 통과
    if method_name not in _SAMPLING_TARGET_LEVELS:
        return event_dict

    sample_rate, target_events = _get_sampling_settings()

    # sample_rate == 1.0이면 샘플링 비활성화
    if sample_rate >= 1.0:
        return event_dict

    event_name = event_dict.get("event", "")

    # target_events가 설정되어 있으면 해당 이벤트만 샘플링
    if target_events and event_name not in target_events:
        return event_dict

    # 확률적 샘플링
    if random.random() > sample_rate:  # noqa: S311
        raise structlog.DropEvent

    # 샘플링된 로그에 표시
    event_dict["_sampled"] = True
    return event_dict
