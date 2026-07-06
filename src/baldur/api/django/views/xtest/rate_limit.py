"""
X-Test-Mode Rate Limiter Views

API for observing Rate Limiter (L1/L2) behavior in X-Test-Mode.

Endpoints:
- GET  /api/baldur/xtest/rate-limit/status/ - Overall rate limit status
- GET  /api/baldur/xtest/rate-limit/client/ - Per-client status
- GET  /api/baldur/xtest/rate-limit/history/ - Rate limit event history
- GET  /api/baldur/xtest/rate-limit/config/ - Current configuration
- POST /api/baldur/xtest/rate-limit/reset/ - Reset counters (testing only)

Architecture:
- L2 (Primary): Redis-based distributed rate limit
- L1 (Fallback): SlidingWindowLimiter (local fallback on Redis failure)
- Middleware: HybridRateLimitMiddleware (unified)

Security:
- X-Test-Mode: chaos-monkey header required
- DEBUG or CHAOS_ENABLED env var required
- Completely blocked in production
"""

import structlog
from django.utils import timezone
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .base import XTestModeMixin, collect_system_snapshot

logger = structlog.get_logger()


# =============================================================================
# Overall Rate Limit Status View
# =============================================================================


class RateLimitStatusView(XTestModeMixin, APIView):
    """
    Overall rate limit status API.

    GET /api/baldur/xtest/rate-limit/status/

    Query Parameters:
        client_key: Query a specific client key (optional)

    Response:
        {
            "status": "success",
            "mode": "normal|emergency|degraded",
            "redis_healthy": true,
            "fallback_active": false,
            "current_config": {
                "control_api_rate_limit": 100,
                "control_api_window_seconds": 60,
                "emergency_rate_limit": 10,
                "emergency_window_seconds": 60
            },
            "global_stats": {
                "total_events": 10,
                "exceeded_count": 2,
                "active_clients": 3
            },
            "timestamp": "2026-01-26T12:00:00Z",
            "snapshot": {...}
        }
    """

    def get(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        from baldur.api.django.rate_limit import (
            RedisHealthState,
            get_client_stats,
            get_local_limiter,
            get_rate_limit_config,
            get_rate_limit_events_count,
            get_redis_health_checker,
        )

        # Check Redis health checker state
        health_checker = get_redis_health_checker()
        local_limiter = get_local_limiter()
        config = get_rate_limit_config()

        # Determine current mode
        if health_checker.state == RedisHealthState.HEALTHY:
            mode = "normal"
        elif health_checker.state == RedisHealthState.RECOVERING:
            mode = "degraded"
        else:
            mode = "emergency"

        # Global statistics
        total_events = get_rate_limit_events_count()
        client_stats = get_client_stats()
        exceeded_count = sum(
            stats.get("exceeded", 0) for stats in client_stats.values()
        )
        active_clients = len(local_limiter.get_all_clients())

        # Query specific client if requested
        client_key = request.query_params.get("client_key")
        client_status = None
        if client_key:
            client_status = local_limiter.get_client_status(
                client_key,
                config["emergency_rate_limit"],
                config["emergency_window_seconds"],
            )

        snapshot = collect_system_snapshot()

        logger.info(
            "test.mode_rate_limit",
            mode=mode,
            health_checker=health_checker.is_healthy,
            active_clients=active_clients,
        )

        response_data = {
            "status": "success",
            "mode": mode,
            "redis_healthy": health_checker.is_healthy,
            "fallback_active": not health_checker.is_healthy,
            "redis_state": health_checker.state.value,
            "current_config": {
                "control_api_rate_limit": config["control_api_rate_limit"],
                "control_api_window_seconds": config["control_api_window_seconds"],
                "emergency_rate_limit": config["emergency_rate_limit"],
                "emergency_window_seconds": config["emergency_window_seconds"],
            },
            "global_stats": {
                "total_events": total_events,
                "exceeded_count": exceeded_count,
                "active_clients": active_clients,
            },
            "timestamp": timezone.now().isoformat(),
            "snapshot": snapshot,
        }

        if client_status:
            response_data["client_status"] = client_status

        # WAL Audit record
        self.log_xtest_audit(
            request=request,
            action="query_status",
            component="rate_limit",
            details={"mode": mode, "active_clients": active_clients},
            result="success",
        )

        return Response(response_data)


# =============================================================================
# Per-Client Rate Limit Status View
# =============================================================================


class RateLimitClientView(XTestModeMixin, APIView):
    """
    Per-client rate limit status API.

    GET /api/baldur/xtest/rate-limit/client/

    Query Parameters:
        client_key: Client identifier (required)
        window: Window type - minute, hour (optional, default minute)

    Response:
        {
            "status": "success",
            "client_key": "192.168.1.1:user123",
            "current_count": 95,
            "limit": 100,
            "remaining": 5,
            "reset_at": 1706270400,
            "blocked": false,
            "window_seconds": 60
        }
    """

    def get(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        client_key = request.query_params.get("client_key")
        if not client_key:
            return Response(
                {
                    "status": "error",
                    "error": "missing_parameter",
                    "message": "client_key query parameter is required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        from baldur.api.django.rate_limit import (
            get_local_limiter,
            get_rate_limit_config,
            get_redis_health_checker,
        )

        local_limiter = get_local_limiter()
        health_checker = get_redis_health_checker()
        config = get_rate_limit_config()

        # Query status from local limiter
        client_status = local_limiter.get_client_status(
            client_key,
            config["emergency_rate_limit"],
            config["emergency_window_seconds"],
        )

        logger.info(
            "test.mode_rate_limit",
            client_key=client_key,
            client_status=client_status["current_count"],
            blocked=client_status["blocked"],
        )

        response_data = {
            "status": "success",
            "source": "local" if not health_checker.is_healthy else "redis_fallback",
            **client_status,
        }

        # WAL Audit record
        self.log_xtest_audit(
            request=request,
            action="query_client",
            component="rate_limit",
            details={"client_key": client_key, "blocked": client_status["blocked"]},
            result="success",
        )

        return Response(response_data)


# =============================================================================
# Rate Limit History View
# =============================================================================


class RateLimitHistoryView(XTestModeMixin, APIView):
    """
    Rate limit event history API.

    GET /api/baldur/xtest/rate-limit/history/

    Query Parameters:
        limit: Number of entries to retrieve (default 20, max 100)
        client_key: Filter by specific client (optional)

    Response:
        {
            "status": "success",
            "total_exceeded": 5,
            "total_events": 100,
            "recent_events": [
                {
                    "timestamp": "...",
                    "client_key": "...",
                    "allowed": false,
                    "mode": "emergency",
                    ...
                }
            ],
            "by_client": {
                "192.168.1.1:user1": {"total": 50, "exceeded": 3},
                ...
            }
        }
    """

    def get(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        # Parse query parameters
        try:
            limit = int(request.query_params.get("limit", 20))
            limit = min(max(limit, 1), 100)
        except (ValueError, TypeError):
            limit = 20

        client_key = request.query_params.get("client_key")

        from baldur.api.django.rate_limit import (
            get_client_stats,
            get_rate_limit_events,
            get_rate_limit_events_by_client,
            get_rate_limit_events_count,
        )

        # Retrieve events
        if client_key:
            events = get_rate_limit_events_by_client(client_key, limit)
        else:
            events = get_rate_limit_events(limit)

        # Calculate statistics
        total_events = get_rate_limit_events_count()
        client_stats = get_client_stats()
        total_exceeded = sum(
            stats.get("exceeded", 0) for stats in client_stats.values()
        )

        logger.info(
            "test.mode_rate_limit",
            events_count=len(events),
            total_events=total_events,
            total_exceeded=total_exceeded,
        )

        response_data = {
            "status": "success",
            "total_events": total_events,
            "total_exceeded": total_exceeded,
            "returned_count": len(events),
            "recent_events": events,
            "by_client": (
                client_stats
                if not client_key
                else {client_key: client_stats.get(client_key, {})}
            ),
        }

        # WAL Audit record
        self.log_xtest_audit(
            request=request,
            action="query_history",
            component="rate_limit",
            details={"total_events": total_events, "returned_count": len(events)},
            result="success",
        )

        return Response(response_data)


# =============================================================================
# Rate Limit Configuration View
# =============================================================================


class RateLimitConfigXTestView(XTestModeMixin, APIView):
    """
    Rate limit configuration API.

    GET /api/baldur/xtest/rate-limit/config/

    Response:
        {
            "status": "success",
            "source": "runtime|settings|fallback",
            "normal_config": {
                "rate_limit": 100,
                "window_seconds": 60
            },
            "emergency_config": {
                "rate_limit": 10,
                "window_seconds": 60
            },
            "path_prefix": "/api/baldur/",
            "excluded_paths": ["/health/", "/metrics/"],
            "redis_config": {
                "ping_interval": 5,
                "failure_threshold": 3,
                "recovery_jitter_max": 10
            }
        }
    """

    def get(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        from baldur.api.django.rate_limit import (
            get_rate_limit_config,
            get_redis_health_checker,
        )
        from baldur.api.django.rate_limit.config import (
            _FALLBACK_CONTROL_API_PATH_PREFIX,
            _get_setting,
        )

        config = get_rate_limit_config()
        health_checker = get_redis_health_checker()

        # Determine config source
        try:
            from baldur.factory.registry import ProviderRegistry

            manager = ProviderRegistry.runtime_config_manager.safe_get()
            source = "runtime" if manager is not None else "settings"
        except Exception:
            source = "settings"

        # Attempt to load API Rate Limit Settings
        try:
            from baldur.settings.api_rate_limit import get_api_rate_limit_settings

            api_settings = get_api_rate_limit_settings()
            settings_available = True
        except Exception:
            api_settings = None
            settings_available = False

        if not settings_available:
            source = "fallback"

        # Excluded paths list
        excluded_paths: list[str] = []
        if api_settings and hasattr(api_settings, "excluded_paths"):
            excluded_paths = list(getattr(api_settings, "excluded_paths", []))

        logger.info(
            "test.mode_rate_limit",
            source=source,
        )

        response_data = {
            "status": "success",
            "source": source,
            "normal_config": {
                "rate_limit": config["control_api_rate_limit"],
                "window_seconds": config["control_api_window_seconds"],
            },
            "emergency_config": {
                "rate_limit": config["emergency_rate_limit"],
                "window_seconds": config["emergency_window_seconds"],
            },
            "path_prefix": _get_setting(
                "control_api_path_prefix", _FALLBACK_CONTROL_API_PATH_PREFIX
            ),
            "excluded_paths": excluded_paths,
            "redis_config": {
                "ping_interval": health_checker.ping_interval,
                "failure_threshold": health_checker.failure_threshold,
                "recovery_jitter_max": health_checker.recovery_jitter_max,
            },
        }

        # WAL Audit record
        self.log_xtest_audit(
            request=request,
            action="query_config",
            component="rate_limit",
            details={"source": source},
            result="success",
        )

        return Response(response_data)


# =============================================================================
# Rate Limit Counter Reset View (testing only)
# =============================================================================


class RateLimitResetView(XTestModeMixin, APIView):
    """
    Rate limit counter reset API (testing only).

    POST /api/baldur/xtest/rate-limit/reset/

    Request Body:
        {
            "client_key": "192.168.1.1:user123",  // optional, specific client only
            "reset_all": false,  // reset all counters (default false)
            "reset_events": false  // also reset event history (default false)
        }

    Response:
        {
            "status": "success",
            "reset_count": 5,
            "clients_reset": ["client1", "client2"],
            "events_reset": 10
        }
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        client_key = request.data.get("client_key")
        reset_all = request.data.get("reset_all", False)
        reset_events = request.data.get("reset_events", False)

        from baldur.api.django.rate_limit import (
            get_local_limiter,
            reset_rate_limit_events,
            reset_rate_limit_state,
        )

        local_limiter = get_local_limiter()
        clients_reset = []
        reset_count = 0
        events_reset = 0

        if reset_all:
            # Full reset
            clients_before = local_limiter.get_all_clients()
            reset_count = len(clients_before)
            clients_reset = list(clients_before)
            reset_rate_limit_state()

            if reset_events:
                events_reset = reset_rate_limit_events()

            logger.warning(
                "test.mode_rate_limit",
                reset_count=reset_count,
                events_reset=events_reset,
            )
        elif client_key:
            # Reset specific client only
            if local_limiter.reset_client(client_key):
                reset_count = 1
                clients_reset = [client_key]

            if reset_events:
                events_reset = reset_rate_limit_events(client_key)

            logger.info(
                "test.mode_rate_limit",
                client_key=client_key,
                events_reset=events_reset,
            )
        else:
            return Response(
                {
                    "status": "error",
                    "error": "missing_parameter",
                    "message": "Either 'client_key' or 'reset_all=true' is required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        response_data = {
            "status": "success",
            "reset_count": reset_count,
            "clients_reset": clients_reset,
            "events_reset": events_reset,
            "timestamp": timezone.now().isoformat(),
        }

        # WAL Audit record
        self.log_xtest_cleanup(
            request=request,
            component="rate_limit",
            cleaned_count=reset_count,
            cleaned_ids=clients_reset,
        )

        return Response(response_data)
