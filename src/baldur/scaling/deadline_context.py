"""
Deadline Context — gRPC deadline propagation pattern.

Propagates an upstream service's deadline to downstream services via a
ContextVar plus an HTTP header. When the remaining time is below the
estimated processing time, the request is rejected immediately (Fast-Fail)
to avoid pointless work.

In an MSA call chain A → B → C:
- A calls B with a 3s timeout
- B spends 2.5s, then calls C
- C has 0.5s left but an estimated 2s of work → Fast-Fail saves resources

ContextVar-based propagation:
- Guarantees a per-thread independent context under WSGI gthread
- Propagates into thread pools (Bulkhead, Hedging) automatically via
  copy_context().run()
- Not propagated into Celery tasks (independent lifecycle)

HTTP header convention:
- X-Deadline-Remaining: 2500ms (milliseconds)
- The external entry point (Nginx) strips the client-supplied header
  (DoS prevention)
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

# Whether the deadline feature is enabled (env var: BALDUR_DEADLINE_ENABLED)
DEADLINE_ENABLED: bool = os.environ.get("BALDUR_DEADLINE_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)

# HTTP header name
DEADLINE_HEADER = "X-Deadline-Remaining"

# Django META key (HTTP_X_DEADLINE_REMAINING)
DEADLINE_META_KEY = "HTTP_X_DEADLINE_REMAINING"

# ContextVar: request deadline (absolute instant on the monotonic clock)
_request_deadline: ContextVar[float | None] = ContextVar(
    "request_deadline", default=None
)

# Minimum useful time (ms) — below this, Fast-Fail
DEFAULT_MINIMUM_USEFUL_TIME_MS: float = float(
    os.environ.get("BALDUR_DEADLINE_MINIMUM_USEFUL_MS", "50")
)

# Network latency compensation buffer (ms)
# 1~5ms between pods in the same AZ, 10~30ms cross-AZ; safety margin is
# 2× cross-AZ = 50ms
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

# Header parsing regex: "2500ms", "2500", "1500.5ms", etc.
_DEADLINE_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:ms)?\s*$", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Prometheus metrics
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
    """Record a Fast-Fail rejection metric."""
    if _HAS_PROMETHEUS and _fast_fail_counter is not None:
        _fast_fail_counter.labels(tier=tier, path_prefix=path_prefix).inc()


def record_remaining_ms(remaining: float, tier: str = "unknown") -> None:
    """Record the remaining-time histogram at reception."""
    if _HAS_PROMETHEUS and _remaining_histogram is not None:
        _remaining_histogram.labels(tier=tier).observe(remaining)


def record_exhausted_on_arrival(path_prefix: str = "unknown") -> None:
    """Record the counter for requests already expired on arrival."""
    if _HAS_PROMETHEUS and _exhausted_on_arrival_counter is not None:
        _exhausted_on_arrival_counter.labels(path_prefix=path_prefix).inc()


def parse_deadline_header(header_value: str) -> float | None:
    """
    Parse an X-Deadline-Remaining header value.

    Args:
        header_value: Header value (e.g. "2500ms", "2500", "1500.5ms")

    Returns:
        Remaining time (ms), or None if parsing fails
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
    Set the deadline on the current context.

    Subtracts the network latency buffer for a conservative estimate.

    Args:
        remaining_ms: Remaining time (milliseconds)
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
    Return the remaining time on the current context.

    Returns:
        Remaining time (ms), or None if no deadline is set
    """
    deadline = _request_deadline.get()
    if deadline is None:
        return None
    remaining = (deadline - time.monotonic()) * 1000.0
    return max(0.0, remaining)


def is_expired() -> bool:
    """
    Check whether the deadline has expired.

    Returns:
        True if expired; False if no deadline is set
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
    True when the remaining time is below the estimated processing time.

    A True result means Fast-Fail is recommended.

    Args:
        estimated_processing_ms: Estimated processing time (milliseconds)
        minimum_useful_ms: Minimum useful time (milliseconds)

    Returns:
        True if Fast-Fail is recommended
    """
    remaining = get_remaining_ms()
    if remaining is None:
        return False  # no deadline set → do not Fast-Fail

    if remaining < minimum_useful_ms:
        return True  # below the minimum useful time

    return remaining < estimated_processing_ms


def clear_deadline() -> None:
    """Remove the deadline from the current context."""
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
    Build the header value to propagate to downstream services.

    Returns:
        A "1234ms"-formatted value, or None if no deadline is set or it has
        expired
    """
    remaining = get_remaining_ms()
    if remaining is None or remaining <= 0:
        return None
    return f"{remaining:.0f}ms"


def get_deadline_aware_statement_timeout(
    default_db_timeout_ms: int = 30_000,
) -> int | None:
    """
    Return the smaller of the DeadlineContext remaining time and the default
    DB timeout.

    Returns None (no SET needed) when no deadline is set, or when the
    remaining time is more generous than the default DB timeout.

    Args:
        default_db_timeout_ms: Default DB statement_timeout (kept in sync with
            the production settings)

    Returns:
        Timeout (ms) to set, or None if no SET is needed
    """
    remaining = get_remaining_ms()
    if remaining is None:
        return None  # no deadline set

    if remaining >= default_db_timeout_ms:
        return None  # generous enough → skip the SET

    return max(1, int(remaining))  # at least 1ms


@contextmanager
def deadline_scope(remaining_ms: float) -> Generator[None, None, None]:
    """
    Deadline scope context manager.

    Sets the deadline on block entry and restores the previous value on exit.

    Usage:
        with deadline_scope(3000):
            if should_fast_fail(estimated_ms=2000):
                raise TimeoutError("Fast-Fail")
            process_request()

    Args:
        remaining_ms: Remaining time (milliseconds)
    """
    previous = _request_deadline.get()
    set_deadline(remaining_ms)
    try:
        yield
    finally:
        _request_deadline.set(previous)


# =============================================================================
# Per-tier cold-start default estimated processing time (ms)
# A conservative estimate used until enough RTT data has accumulated.
# critical: fast paths (authentication, payment confirmation, etc.)
# standard: ordinary CRUD operations
# non_essential: heavy queries (statistics, reports, etc.)
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

# Estimated processing time safety factor (default 1.5 = 50% headroom)
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
    Return the per-tier cold-start default estimated processing time.

    Used as the fallback when the GradientCalculator has no RTT data
    (cold start).

    Args:
        tier_id: Tier identifier (critical, standard, non_essential)

    Returns:
        Default estimated processing time (ms)
    """
    return _TIER_DEFAULT_ESTIMATED_MS.get(tier_id, DEFAULT_ESTIMATED_MS_STANDARD)


def get_estimated_processing_ms(
    calculator_name: str = "default",
    safety_margin: float = DEFAULT_SAFETY_MARGIN,
    tier_id: str = "standard",
) -> float:
    """
    Return the estimated processing time based on the GradientCalculator.

    Computed as the current smoothed RTT × safety factor. A positive gradient
    (rising RTT trend) raises the safety factor further. When there is no RTT
    data (cold start), the per-tier default is returned.

    Args:
        calculator_name: GradientCalculator name
        safety_margin: Safety factor (default: the BALDUR_DEADLINE_SAFETY_MARGIN
            env var, or 1.5 when unset)
        tier_id: Tier identifier (used for the cold-start fallback)

    Returns:
        Estimated processing time (ms). Never None — a default is returned even
        on cold start.
    """
    try:
        from baldur_pro.services.throttle.gradient import get_gradient_calculator

        calc = get_gradient_calculator(calculator_name)
        rtt, gradient = calc.get_snapshot()

        if rtt is None:
            # Cold start: return the per-tier default
            return get_tier_default_estimated_ms(tier_id)

        # Rising RTT trend → raise the safety factor
        effective_margin = safety_margin
        if gradient > 0.1:  # increase of 10% or more
            effective_margin *= 1.0 + gradient  # scale with the gradient

        estimated = rtt * effective_margin

        # Record Prometheus metrics
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
