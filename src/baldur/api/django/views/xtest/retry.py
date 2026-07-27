"""
X-Test-Mode Retry Handler Views

API for observing the Retry Handler's Exponential Backoff, DLQ routing,
and Rate Limit awareness behavior in an X-Test-Mode environment.

Endpoints:
- GET  /api/baldur/xtest/retry/rate-limit-status/ - Rate limit awareness status
- GET  /api/baldur/xtest/retry/config/ - Query the current retry configuration

Security:
- X-Test-Mode: chaos-monkey header required
- DEBUG or CHAOS_ENABLED environment variable required
- Completely blocked in production environments
"""

import time
from typing import Any

import structlog
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from baldur.core.backoff import ExponentialBackoff

from .base import XTestModeMixin, collect_system_snapshot

logger = structlog.get_logger()


# =============================================================================
# Rate Limit Awareness Status View
# =============================================================================


class RetryRateLimitStatusView(XTestModeMixin, APIView):
    """
    Rate limit awareness status query API.

    GET /api/baldur/xtest/retry/rate-limit-status/

    Query Parameters:
        domain: Domain (rate_limit_key) (optional)

    Response:
        {
            "status": "success",
            "rate_limit_aware": true,
            "storage_type": "redis",
            "domain": "payment",
            "state": {
                "consecutive_429s": 3,
                "is_in_cooldown": true,
                "cooldown_until": "2026-01-26T12:00:00Z",
                "remaining_cooldown": 15.5
            },
            "throttled": true,
            "recommended_delay": 30,
            "snapshot": {...}
        }
    """

    def get(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        domain = request.query_params.get("domain", "default")

        try:
            from baldur.services.rate_limit_coordinator import (
                RateLimitCoordinatorConfig,
                get_rate_limit_coordinator,
            )

            coordinator = get_rate_limit_coordinator()
            config = RateLimitCoordinatorConfig.from_settings()
            state = coordinator.get_state(domain)

            # Build state information
            state_info = {
                "consecutive_429s": state.consecutive_429s,
                "is_in_cooldown": state.is_in_cooldown,
                "cooldown_until": (
                    time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(state.cooldown_until)
                    )
                    if state.cooldown_until
                    else None
                ),
                "remaining_cooldown": (
                    state.remaining_cooldown if state.is_in_cooldown else 0
                ),
            }

            # Compute the recommended wait time
            recommended_delay: float = 0.0
            if state.is_in_cooldown:
                recommended_delay = state.remaining_cooldown
            elif state.consecutive_429s > 0:
                # With consecutive 429s, estimate the next expected backoff by
                # composing the canonical strategy jitterlessly. calculate() is
                # 1-indexed, so the delay after one more 429 is attempt
                # consecutive_429s + 1 (== base * multiplier**consecutive_429s,
                # capped at max_delay).
                backoff = ExponentialBackoff(
                    base_delay=config.base_delay,
                    multiplier=config.backoff_multiplier,
                    max_delay=config.max_delay,
                    jitter=False,
                )
                recommended_delay = backoff.calculate(state.consecutive_429s + 1)

            snapshot = collect_system_snapshot()

            logger.info(
                "test.mode_rate_limit",
                healing_domain=domain,
                state=state.is_in_cooldown,
                consecutive_429s=state.consecutive_429s,
            )

            response_data = {
                "status": "success",
                "rate_limit_aware": True,
                "storage_type": coordinator.storage_type,
                "domain": domain,
                "state": state_info,
                "throttled": state.is_in_cooldown,
                "recommended_delay": round(recommended_delay, 2),
                "config": {
                    "base_delay": config.base_delay,
                    "max_delay": config.max_delay,
                    "jitter_percent": config.jitter_percent,
                    "default_retry_after": config.default_retry_after,
                    "backoff_multiplier": config.backoff_multiplier,
                },
                "snapshot": snapshot,
            }

            # Record WAL audit entry
            self.log_xtest_audit(
                request=request,
                action="query_rate_limit_status",
                component="retry",
                details={"domain": domain, "throttled": state.is_in_cooldown},
                result="success",
            )

            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.warning(
                "test.mode_rate_limit",
                error=e,
            )
            snapshot = collect_system_snapshot()
            return Response(
                {
                    "status": "error",
                    "rate_limit_aware": False,
                    "storage_type": None,
                    "domain": domain,
                    "error": str(e),
                    "throttled": False,
                    "recommended_delay": 0,
                    "snapshot": snapshot,
                },
                status=status.HTTP_200_OK,
            )


# =============================================================================
# RetryConfig Query View
# =============================================================================


class XTestRetryConfigView(XTestModeMixin, APIView):
    """
    API for querying the currently applied retry configuration (X-Test-Mode).

    Renamed from RetryConfigView to XTestRetryConfigView to avoid
    name collision with views.config.RetryConfigView.

    GET /api/baldur/xtest/retry/config/

    Query Parameters:
        domain: Query per-domain configuration (optional)

    Response:
        {
            "status": "success",
            "source": "runtime",
            "domain": "payment",
            "config": {
                "max_attempts": 3,
                "backoff_base": 4,
                "backoff_max": 180,
                "jitter_percent": 25,
                "enable_dlq": true,
                "rate_limit_aware": true
            },
            "domain_overrides": {
                "payment": {"max_attempts": 5}
            },
            "snapshot": {...}
        }
    """

    def get(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        domain = request.query_params.get("domain", "default")

        # Check and load the configuration source
        source = "default"
        domain_overrides: dict[str, Any] = {}

        try:
            # Try RuntimeConfigManager
            from baldur.factory.registry import ProviderRegistry

            manager = ProviderRegistry.runtime_config_manager.safe_get()
            if manager is not None:
                retry_config = manager.get_retry_config()
                if retry_config:
                    source = "runtime"
        except Exception:
            pass

        if source == "default":
            try:
                # Try core config
                from baldur.settings import get_config

                core_config = get_config()
                if hasattr(core_config, "retry"):
                    source = "settings"

                # Check per-domain overrides
                if hasattr(core_config, "domain_configs"):
                    domain_overrides = core_config.domain_configs
            except Exception:
                pass

        # Load the retry policy configuration
        from baldur.services.retry_handler import RetryPolicyConfig

        config = RetryPolicyConfig.from_settings(domain)

        snapshot = collect_system_snapshot()

        logger.info(
            "test.mode_retry_config",
            healing_domain=domain,
            source=source,
            config=config.max_attempts,
        )

        response_data = {
            "status": "success",
            "source": source,
            "domain": domain,
            "config": {
                "max_attempts": config.max_attempts,
                "backoff_base": config.backoff_base,
                "backoff_max": config.backoff_max,
                "jitter_percent": config.jitter_percent,
                "enable_dlq": config.enable_dlq,
                "rate_limit_aware": config.rate_limit_aware,
                "rate_limit_key": config.rate_limit_key,
                "retryable_exceptions": [
                    exc.__name__ for exc in config.retryable_exceptions
                ],
                "non_retryable_exceptions": [
                    exc.__name__ for exc in config.non_retryable_exceptions
                ],
            },
            "domain_overrides": domain_overrides,
            "snapshot": snapshot,
        }

        # Record WAL audit entry
        self.log_xtest_audit(
            request=request,
            action="query_config",
            component="retry",
            details={"domain": domain, "source": source},
            result="success",
        )

        return Response(response_data, status=status.HTTP_200_OK)
