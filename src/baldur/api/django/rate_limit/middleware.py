"""
Hybrid Rate Limit Middleware — L1(Memory) + L2(Redis) defense-in-depth.

Defense-in-Depth Strategy:
- L2 (Primary): Redis-based sliding window rate limit (configurable, default 100 req/min)
- L1 (Fallback): Local memory rate limit when Redis fails (configurable, default 10 req/min)

Features:
- Automatic failover to local memory on Redis failure
- Shadow audit logging for forensic analysis
- Prometheus metrics for observability
- Jitter-based recovery to prevent thundering herd
- Runtime-configurable via API (RateLimitConfig)

Extracted from api/django/rate_limit.py as part of 358 rate_limit package split.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import structlog
from django.http import JsonResponse

from baldur.api.django.rate_limit.config import (
    _FALLBACK_CONTROL_API_PATH_PREFIX,
    _get_metrics,
    _get_setting,
    get_rate_limit_config,
)
from baldur.api.django.rate_limit.event_history import RateLimitEventHistory
from baldur.api.django.rate_limit.redis_health_checker import RedisHealthChecker
from baldur.api.django.rate_limit.shadow_audit import ShadowAuditLogger
from baldur.api.middleware.rate_limit import build_rate_limit_headers
from baldur.services.rate_limit import RateLimitState, SlidingWindowLimiter
from baldur.utils.network import extract_client_ip
from baldur.utils.singleton import make_singleton_factory

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

    from baldur.core.hooks import BypassResult

logger = structlog.get_logger()

__all__ = [
    "HybridRateLimitMiddleware",
    "get_redis_health_checker",
    "get_local_limiter",
    "reset_rate_limit_state",
    "get_current_state",
]


# =============================================================================
# Singleton Instances
# =============================================================================

get_redis_health_checker, configure_redis_health_checker, reset_redis_health_checker = (
    make_singleton_factory("redis_health_checker", RedisHealthChecker)
)

get_local_limiter, configure_local_limiter, reset_local_limiter = (
    make_singleton_factory(
        "local_limiter",
        lambda: SlidingWindowLimiter(
            cleanup_interval=_get_setting("local_cleanup_interval", 60.0),
        ),
    )
)

get_shadow_audit, configure_shadow_audit, reset_shadow_audit = make_singleton_factory(
    "shadow_audit", ShadowAuditLogger
)


# =============================================================================
# Hybrid Rate Limit Middleware
# =============================================================================


class HybridRateLimitMiddleware:
    """
    Intelligent hybrid rate limit middleware.

    Defense-in-Depth Strategy:
    - L2 (Redis) healthy -> Redis-based Rate Limit (100 req/min)
    - L2 (Redis) failure -> L1 (local memory) emergency Rate Limit (10 req/min)

    Features:
    - Automatic failover to local memory on Redis failure
    - Shadow audit logging for forensic analysis
    - Prometheus metrics for observability
    - Jitter-based recovery to prevent thundering herd
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.redis_client = self._get_redis_client()
        self.local_limiter: SlidingWindowLimiter = get_local_limiter()
        self.health_checker = get_redis_health_checker()
        self._shadow_audit = get_shadow_audit()

    def _get_redis_client(self):
        """Get Redis client from Django cache."""
        try:
            from django.core.cache import caches

            # CacheHandler uses [] indexing, not .get()
            cache = caches["default"]

            if hasattr(cache, "client"):
                client = cache.client
                if hasattr(client, "get_client"):
                    return client.get_client()

            if hasattr(cache, "_cache"):
                return cache._cache.get_client()

            return None
        except Exception:
            return None

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # Only apply to Control API
        control_api_prefix = _get_setting(
            "control_api_path_prefix", _FALLBACK_CONTROL_API_PATH_PREFIX
        )
        if not request.path.startswith(control_api_prefix):
            return cast("HttpResponse", self.get_response(request))

        # Hook Registry bypass check (Domain-Free, Audit-Logged)
        bypass_result = self._check_bypass_registry(request)
        if bypass_result.bypassed:
            response: HttpResponse = self.get_response(request)
            response["X-RateLimit-Mode"] = "bypass"
            response["X-RateLimit-Remaining"] = "unlimited"
            response["X-RateLimit-Bypass-Reason"] = bypass_result.hook_name
            return response

        # Get runtime config (API Control)
        config = get_rate_limit_config()
        rate_limit = config["control_api_rate_limit"]
        window_seconds = config["control_api_window_seconds"]
        emergency_limit = config["emergency_rate_limit"]
        emergency_window = config["emergency_window_seconds"]

        # Health check
        redis_healthy = self.health_checker.check_health()

        if redis_healthy:
            # L2 (Redis) Rate Limit. Mode may downgrade to "emergency" inside
            # _check_redis_limit if a Redis exception routes to the L1 fallback.
            state, mode = self._check_redis_limit(request, rate_limit, window_seconds)
        else:
            # L1 (Local Memory) Emergency Rate Limit
            state = self._check_local_limit(request, emergency_limit, emergency_window)
            mode = "emergency"

            # Shadow Audit for forensic analysis
            self._shadow_audit.log_rate_limit_event(
                request, state.allowed, emergency_limit, self._get_client_ip(request)
            )

        if not state.allowed:
            # Record exceeded metric
            self._record_exceeded(mode)
            return self._rate_limit_response(state, mode)

        response = cast("HttpResponse", self.get_response(request))

        # Add rate limit headers. state.limit carries the limit actually
        # enforced (normal, emergency, or redis-exception fallback), so the
        # advertised limit always matches what this request was checked against.
        for header, value in build_rate_limit_headers(
            state.limit, state.remaining, state.reset_at, mode=mode
        ).items():
            response[header] = value

        return response

    def _check_bypass_registry(self, request: HttpRequest) -> BypassResult:
        """
        Check if request should bypass rate limiting via Hook Registry.

        Returns:
            BypassResult with bypass decision and audit information
        """
        from baldur.core.hooks import BypassRegistry

        return BypassRegistry.should_bypass(request)

    def _get_client_key(self, request: HttpRequest) -> str:
        """Generate rate limit key (IP + User)."""
        ip = self._get_client_ip(request)
        user_id = (
            getattr(request.user, "id", "anonymous")
            if hasattr(request, "user")
            else "anonymous"
        )
        return f"ratelimit:control_api:{ip}:{user_id}"

    def _get_client_ip(self, request: HttpRequest) -> str:
        """Extract client IP (canonical resolution: XFF -> X-Real-IP -> REMOTE_ADDR)."""
        return cast(str, extract_client_ip(request, default="unknown"))

    def _check_redis_limit(
        self,
        request: HttpRequest,
        rate_limit: int,
        window_seconds: int,
    ) -> tuple[RateLimitState, str]:
        """
        Check rate limit using Redis sliding window.

        Returns:
            Tuple of (state, mode). ``state`` carries the limit actually
            enforced (the normal L2 limit, or the emergency L1 limit on the
            Redis-exception fallback) so headers, the 429 body and the
            exceeded metric all report what was enforced — not a recomputed
            guess. ``mode`` is ``"normal"`` for the L2 path (including the
            fail-open Redis-unavailable branch) and ``"emergency"`` when a
            Redis exception routed enforcement to the L1 fallback.
        """
        if not self.redis_client:
            logger.warning("rate_limit.redis_unavailable")
            return (
                RateLimitState(
                    limit=rate_limit,
                    remaining=rate_limit,
                    reset_at=0,
                    allowed=True,
                ),
                "normal",
            )

        try:
            key = self._get_client_key(request)
            now = time.time()
            window_start = now - window_seconds
            reset_time = int(now + window_seconds)

            # Step 1: prune members outside the window, then read the count
            # WITHOUT this request. The count-then-add spans two pipeline
            # round-trips and is deliberately non-atomic: on the Control/admin
            # API a cross-worker race can overshoot the limit only by the
            # per-client in-flight concurrency (~1-5 for a single operator or
            # console), immaterial against a coarse 100/min bound. A hot
            # non-admin path re-entering this window must switch to a one-shot
            # Lua EVAL (scoring from Redis-side TIME) for cross-worker atomicity.
            prune_pipe = self.redis_client.pipeline()
            prune_pipe.zremrangebyscore(key, 0, window_start)
            prune_pipe.zcard(key)
            current_count = prune_pipe.execute()[1]

            if current_count >= rate_limit:
                logger.warning(
                    "rate_limit.exceeded",
                    rate_limit_key=key,
                    current_count=current_count,
                    rate_limit=rate_limit,
                )
                return (
                    RateLimitState(
                        limit=rate_limit,
                        remaining=0,
                        reset_at=reset_time,
                        allowed=False,
                    ),
                    "normal",
                )

            # Step 2: record only ALLOWED requests, one unique member each. This
            # mirrors the L1 SlidingWindowLimiter (over-limit requests are not
            # recorded) and bounds the ZSET to ~limit members per client key, so
            # a rejected flood cannot amplify memory.
            add_pipe = self.redis_client.pipeline()
            add_pipe.zadd(key, {uuid4().hex: now})
            add_pipe.expire(key, window_seconds + 10)
            add_pipe.execute()

            remaining = max(0, rate_limit - current_count - 1)
            return (
                RateLimitState(
                    limit=rate_limit,
                    remaining=remaining,
                    reset_at=reset_time,
                    allowed=True,
                ),
                "normal",
            )

        except Exception as e:
            # On Redis error, fall back to local limiter. The returned state
            # carries the emergency limit and mode="emergency" so the headers
            # and metric report the limit actually enforced on this request.
            logger.exception(
                "rate_limit.redis_error_falling_back",
                error=e,
            )
            config = get_rate_limit_config()
            state = self._check_local_limit(
                request,
                config["emergency_rate_limit"],
                config["emergency_window_seconds"],
            )

            # Log the fallback
            self._shadow_audit.log_rate_limit_event(
                request,
                state.allowed,
                config["emergency_rate_limit"],
                self._get_client_ip(request),
                reason=str(e),
            )

            return (state, "emergency")

    def _check_local_limit(
        self,
        request: HttpRequest,
        max_requests: int,
        window_seconds: int,
    ) -> RateLimitState:
        """Check rate limit using local memory with per-call params."""

        key = self._get_client_key(request)
        return self.local_limiter.check(key, max_requests, window_seconds)

    def _record_exceeded(self, mode: str):
        """Record rate limit exceeded metric."""
        try:
            exceeded_total, _, _ = _get_metrics()
            if exceeded_total:
                exceeded_total.labels(mode=mode).inc()
        except Exception:
            pass

    def _rate_limit_response(
        self,
        state: RateLimitState,
        mode: str,
    ) -> JsonResponse:
        """Generate 429 Too Many Requests response.

        Emits the canonical 429 header set via the shared builder, including
        ``X-RateLimit-Limit`` equal to the limit actually enforced on this
        request (``state.limit``) — the normal L2 limit, or the emergency L1
        limit on the Redis-exception fallback. ``X-RateLimit-Mode`` is the
        documented D-only extension.
        """
        retry_after = max(1, state.reset_at - int(time.time()))

        message = "Too many requests to Control API"
        if mode == "emergency":
            message += " (Emergency mode: stricter limits applied)"

        return JsonResponse(
            {
                "error": "rate_limit_exceeded",
                "message": message,
                "mode": mode,
                "retry_after": retry_after,
            },
            status=429,
            headers=build_rate_limit_headers(
                state.limit,
                state.remaining,
                state.reset_at,
                retry_after=retry_after,
                mode=mode,
            ),
        )


# =============================================================================
# Utility Functions
# =============================================================================


def reset_rate_limit_state():
    """Reset all rate limit state (for testing)."""
    global _compat_history

    reset_redis_health_checker(cleanup=False)
    reset_local_limiter(cleanup=False)
    reset_shadow_audit(cleanup=False)
    if _compat_history:
        _compat_history.reset()
        _compat_history = None


def get_current_state() -> dict:
    """Get current rate limit state (for debugging/monitoring)."""
    health_checker = get_redis_health_checker()
    local_limiter = get_local_limiter()

    return {
        "redis_state": health_checker.state.value,
        "redis_healthy": health_checker.is_healthy,
        "redis_degraded": health_checker.is_degraded,
        "local_limiter_keys": len(local_limiter.get_all_clients()),
    }


# =============================================================================
# Module-level event history compatibility functions
# =============================================================================

_compat_history = None


def _get_compat_history() -> RateLimitEventHistory:
    global _compat_history
    if _compat_history is None:
        _compat_history = RateLimitEventHistory()
    return _compat_history


def record_rate_limit_event(event: dict) -> None:
    """Record a rate limit event (compatibility wrapper)."""
    _get_compat_history().record(event)


def get_rate_limit_events(limit: int = 20) -> list[dict]:
    """Get recent rate limit events (compatibility wrapper)."""
    return _get_compat_history().get_events(limit)


def get_rate_limit_events_count() -> int:
    """Get total event count (compatibility wrapper)."""
    return _get_compat_history().get_count()


def get_rate_limit_events_by_client(client_key: str, limit: int = 20) -> list[dict]:
    """Get events for a specific client (compatibility wrapper)."""
    return _get_compat_history().get_events_by_client(client_key, limit)


def reset_rate_limit_events(client_key: str | None = None) -> int:
    """Reset event history (compatibility wrapper)."""
    return _get_compat_history().reset(client_key)


def get_client_stats() -> dict:
    """Get per-client statistics (compatibility wrapper)."""
    return _get_compat_history().get_client_stats()
