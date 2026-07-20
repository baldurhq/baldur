"""
structlog processors — log volume control and event name validation.

Event Name Validation (Q5, 314 Audit):
    Validates that event names follow the ``{component}.{entity}_{action}``
    convention.
    DEV/TEST: BALDUR_LOGGING_SETTINGS_STRICT_LOG_VALIDATION=true -> ValueError
    (fail-fast)
    Production: violations are only recorded on a Prometheus counter.

Rate Limiter (De-dup):
    Silences an event that repeats more than max_count times within the
    window, and emits a single "suppressed N events" summary when the window
    ends. ERROR/CRITICAL levels are never suppressed.

Sampling:
    Reduces volume by probabilistically sampling hot path logs (INFO/DEBUG).
    WARNING and above always pass through.
    Targeting specific event names only protects the important logs.

Configuration:
    Controlled via environment variables on LoggingSettings
    (``baldur.settings.logging_settings``):
    - BALDUR_LOGGING_SETTINGS_STRICT_LOG_VALIDATION=true/false
    - BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_WINDOW=10
    - BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_MAX=100
    - BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_RATE=1.0
    - BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_EVENTS=event1,event2
"""

from __future__ import annotations

import random
import re
import threading
import time
from typing import Any

import structlog

# =============================================================================
# Event name validation processor (Q5)
# =============================================================================

_EVENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

_violation_counter_initialized = False
_violation_counter = None


def _get_violation_counter():
    """Lazy-init the Prometheus counter."""
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
    """Cache the LoggingSettings strict_log_validation flag for O(1) lookup."""
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
    """Reset the strict validation cache. For tests."""
    global _strict_validation_cached
    _strict_validation_cached = None


def event_name_validator(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Validate that the event name follows the ``component.entity_action``
    convention.

    Pipeline position: right after add_logger_name (before heavy processing).

    DEV/TEST (BALDUR_STRICT_LOG_VALIDATION=true):
        Raises ValueError on a convention violation (fail-fast).
    Production (default):
        Only records the violation on a Prometheus counter and lets the log
        through.
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
# Rate limiter (de-dup) processor
# =============================================================================

# Per-event counters:
# {(logger_name, event): {"count": int, "window_start": float,
#                         "suppressed": int}}
_rate_limit_state: dict[tuple[str, str], dict[str, Any]] = {}
_rate_limit_lock = threading.Lock()

# Levels that are never suppressed (error/failure logs always pass through)
_NEVER_SUPPRESS_LEVELS = frozenset({"error", "critical"})


def _get_rate_limit_settings() -> tuple[int, int]:
    """Load the rate limit settings. Returns safe defaults on failure."""
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
    """structlog processor that de-dups a repeating event.

    Behavior:
    1. Tracks the occurrence count within the window under the
       (logger_name, event) key
    2. At or below max_count: passes through unchanged
    3. Above max_count: suppresses and increments the counter
    4. On the next window transition: emits a "suppressed N similar events"
       summary log, then resets the counter

    ERROR/CRITICAL levels are not suppressed.
    """
    # ERROR/CRITICAL are never suppressed
    if method_name in _NEVER_SUPPRESS_LEVELS:
        return event_dict

    window_seconds, max_count = _get_rate_limit_settings()

    # Rate limiting disabled (max=0 means unlimited)
    if max_count <= 0 or window_seconds <= 0:
        return event_dict

    event_name = event_dict.get("event", "")
    logger_name = event_dict.get("logger", "unknown")
    key = (logger_name, event_name)
    now = time.monotonic()

    with _rate_limit_lock:
        state = _rate_limit_state.get(key)

        if state is None or (now - state["window_start"]) >= window_seconds:
            # New window starting, or the window expired
            suppressed_count = state["suppressed"] if state else 0

            # Inject the summary when the previous window suppressed events
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

        # Above max_count: suppress
        state["suppressed"] += 1
        raise structlog.DropEvent


def reset_rate_limit_state() -> None:
    """Reset the rate limit state. For tests."""
    with _rate_limit_lock:
        _rate_limit_state.clear()


# =============================================================================
# Sampling processor
# =============================================================================

# Only DEBUG/INFO are sampled (WARNING and above always pass through)
_SAMPLING_TARGET_LEVELS = frozenset({"debug", "info"})


def _get_sampling_settings() -> tuple[float, frozenset[str]]:
    """Load the sampling settings. Returns safe defaults on failure."""
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
    """structlog processor that probabilistically samples hot path logs.

    Behavior:
    1. WARNING and above: always passes through
    2. log_sampling_events empty: sample_rate applies to every DEBUG/INFO
    3. log_sampling_events set: sample_rate applies only to those events
    4. DropEvent when random() > sample_rate

    Configuration:
    - BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_RATE=0.1  (record only 10%)
    - BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_EVENTS=circuit_breaker.checked,action_executor.execute
    """
    # WARNING and above always pass through
    if method_name not in _SAMPLING_TARGET_LEVELS:
        return event_dict

    sample_rate, target_events = _get_sampling_settings()

    # sample_rate == 1.0 disables sampling
    if sample_rate >= 1.0:
        return event_dict

    event_name = event_dict.get("event", "")

    # When target_events is set, sample only those events
    if target_events and event_name not in target_events:
        return event_dict

    # Probabilistic sampling
    if random.random() > sample_rate:  # noqa: S311
        raise structlog.DropEvent

    # Mark the log as sampled
    event_dict["_sampled"] = True
    return event_dict
