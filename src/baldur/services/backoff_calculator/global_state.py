"""
Global Throttle State Manager

Redis-based global throttle state management for cluster-wide coordination.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from .models import GlobalThrottleState, ThrottleState

logger = structlog.get_logger()


# =============================================================================
# Global Throttle State Manager
# =============================================================================


class GlobalThrottleStateManager:
    """
    Redis-based global throttle state manager.

    On shared external-API calls it consults the cluster-wide average load to
    modulate retry aggressiveness.
    """

    REDIS_KEY = "baldur:throttle:global_state"
    STATE_TTL_SECONDS = 60

    def __init__(self, redis_client: Any = None):
        self._redis = redis_client

    @property
    def redis(self) -> Any | None:
        """Lazily initialize the Redis client."""
        if self._redis is None:
            try:
                # get_redis_client is a PRO cache extension; OSS falls open.
                from baldur.adapters import cache as _cache_module

                get_redis = getattr(_cache_module, "get_redis_client", None)
                self._redis = get_redis() if callable(get_redis) else None
            except Exception:
                return None
        return self._redis

    def report_local_state(self, local_state: ThrottleState, pod_id: str) -> None:
        """Report local state to the global store."""
        if not self.redis:
            return

        try:
            from baldur.utils.serialization import fast_dumps_str

            # Store this pod's state
            pod_key = f"{self.REDIS_KEY}:pod:{pod_id}"
            self.redis.setex(
                pod_key,
                self.STATE_TTL_SECONDS,
                fast_dumps_str(
                    {
                        "emergency_level": local_state.emergency_level,
                        "sla_warning": local_state.sla_warning_active,
                        "sla_critical": local_state.sla_critical_active,
                        "timestamp": time.time(),
                    }
                ),
            )
        except Exception as e:
            logger.debug(
                "global_throttle_state.report_failed",
                error=e,
            )

    def get_global_state(self) -> GlobalThrottleState | None:
        """Fetch the cluster-wide state."""
        if not self.redis:
            return None

        try:
            from baldur.utils.serialization import fast_loads

            # Read every pod's state
            pod_keys = self.redis.keys(f"{self.REDIS_KEY}:pod:*")
            if not pod_keys:
                return None

            total_emergency = 0
            warning_count = 0
            critical_count = 0

            for key in pod_keys:
                data = self.redis.get(key)
                if data:
                    pod_state = fast_loads(data)
                    total_emergency += pod_state.get("emergency_level", 0)
                    if pod_state.get("sla_warning"):
                        warning_count += 1
                    if pod_state.get("sla_critical"):
                        critical_count += 1

            pod_count = len(pod_keys)
            return GlobalThrottleState(
                cluster_emergency_level=(
                    total_emergency // pod_count if pod_count > 0 else 0
                ),
                cluster_sla_warning_count=warning_count,
                cluster_sla_critical_count=critical_count,
                reporting_pod_count=pod_count,
                last_updated=time.time(),
            )
        except Exception as e:
            logger.debug(
                "global_throttle_state.get_failed",
                error=e,
            )
            return None
