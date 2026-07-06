"""
Hybrid Rate Limiting for Baldur Control API.

Defense-in-Depth Strategy:
- L2 (Primary): Redis-based sliding window rate limit (configurable, default 100 req/min)
- L1 (Fallback): Local memory rate limit when Redis fails (configurable, default 10 req/min)

Features:
- Redis health checking with mini circuit breaker
- Jitter-based gradual recovery to prevent thundering herd
- Shadow audit logging for forensic analysis
- Prometheus metrics for observability
- Runtime-configurable via API (RateLimitConfig)

Package structure (split from rate_limit.py per 358_LARGE_SERVICE_IMPROVEMENT.md):
- config.py: Settings loader + Prometheus metrics
- redis_health_checker.py: Redis health checker (mini CB)
- shadow_audit.py: Shadow audit logger
- event_history.py: Rate limit event history ring buffer
- middleware.py: HybridRateLimitMiddleware (L1 uses services/rate_limit/SlidingWindowLimiter)
"""

from baldur.api.django.rate_limit.config import (
    FALLBACK_LOG_PATH,
    get_rate_limit_config,
)
from baldur.api.django.rate_limit.event_history import RateLimitEventHistory
from baldur.api.django.rate_limit.middleware import (
    HybridRateLimitMiddleware,
    get_client_stats,
    get_current_state,
    get_local_limiter,
    get_rate_limit_events,
    get_rate_limit_events_by_client,
    get_rate_limit_events_count,
    get_redis_health_checker,
    record_rate_limit_event,
    reset_rate_limit_events,
    reset_rate_limit_state,
)
from baldur.api.django.rate_limit.redis_health_checker import (
    RedisHealthChecker,
    RedisHealthState,
)
from baldur.api.django.rate_limit.shadow_audit import ShadowAuditLogger

__all__ = [
    # Core classes
    "HybridRateLimitMiddleware",
    "RedisHealthChecker",
    "RedisHealthState",
    "ShadowAuditLogger",
    "RateLimitEventHistory",
    # Config
    "get_rate_limit_config",
    "FALLBACK_LOG_PATH",
    # Singletons
    "get_redis_health_checker",
    "get_local_limiter",
    "reset_rate_limit_state",
    "get_current_state",
    # Event history
    "record_rate_limit_event",
    "get_rate_limit_events",
    "get_rate_limit_events_count",
    "get_rate_limit_events_by_client",
    "reset_rate_limit_events",
    "get_client_stats",
]
