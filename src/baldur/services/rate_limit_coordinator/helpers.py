"""
Rate Limit Coordinator - Helpers

EventBus integration and utility functions for rate limit coordination.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()


# =============================================================================
# EventBus Integration Helper (Fail-Open)
# =============================================================================


def _emit_rate_limit_event(
    event_type_name: str,
    data: dict,
    priority_name: str = "HIGH",
) -> None:
    """
    Rate Limit 관련 이벤트를 EventBus에 발행.

    EventBus import 실패 또는 발행 실패 시에도 주요 기능에 영향 없음 (Fail-Open).

    Args:
        event_type_name: EventType 이름 (예: "RATE_LIMIT_429")
        data: 이벤트 데이터
        priority_name: 우선순위 이름 (예: "HIGH", "CRITICAL")
    """
    try:
        from baldur.services.event_bus import (
            EventPriority,
            EventType,
            get_event_bus,
        )

        bus = get_event_bus()
        event_type = getattr(EventType, event_type_name, None)
        if event_type is None:
            logger.warning(
                "adaptive_throttle.unknown_event_type",
                event_type_name=event_type_name,
            )
            return

        priority = getattr(EventPriority, priority_name, EventPriority.HIGH)
        bus.emit(
            event_type=event_type,
            data=data,
            source="rate_limit_coordinator",
            priority=priority,
        )
        logger.debug(
            "rate_limit_coordinator.emitted",
            event_type_name=event_type_name,
        )
    except ImportError:
        logger.debug("rate_limit_coordinator.eventbus_available")
    except Exception as e:
        logger.warning(
            "rate_limit_coordinator.emit_event_failed",
            error=e,
        )


def _record_rate_limit_metrics(
    key: str,
    status_code: int = 429,
    cooldown_seconds: float | None = None,
    consecutive_429s: int | None = None,
) -> None:
    """
    Rate Limit 관련 Prometheus 메트릭 기록.

    메트릭 정의가 없거나 import 실패 시 무시 (Fail-Open).
    """
    try:
        from baldur.services.metrics.definitions import (
            rate_limit_429_total,
            rate_limit_consecutive_429s,
            rate_limit_cooldown_seconds,
        )

        rate_limit_429_total.labels(key=key, status_code=str(status_code)).inc()

        if cooldown_seconds is not None:
            rate_limit_cooldown_seconds.labels(key=key).observe(cooldown_seconds)

        if consecutive_429s is not None:
            rate_limit_consecutive_429s.labels(key=key).set(consecutive_429s)

    except ImportError:
        logger.debug("rate_limit_coordinator.metrics_module_available")
    except Exception as e:
        logger.debug(
            "adaptive_throttle.metrics_failed",
            error=e,
        )


def _default_is_429(response: Any) -> bool:
    """Default 429 detection."""
    if hasattr(response, "status_code"):
        return response.status_code == 429
    return False


def _default_get_retry_after(response: Any) -> float | None:
    """Default Retry-After extraction."""
    if hasattr(response, "headers"):
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
    return None
