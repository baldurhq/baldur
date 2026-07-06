"""
RedisCrossClusterStore — Redis implementation of CrossClusterStore.

Preserves existing key patterns from CrossClusterPropagationRequest
and GovernancePolicySync.
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.interfaces.cross_cluster_store import CrossClusterStore
from baldur.settings.namespace import get_key_prefix
from baldur.utils.serialization import fast_dumps_str, fast_loads

logger = structlog.get_logger()

__all__ = ["RedisCrossClusterStore"]

_REQUEST_KEY = "{prefix}cross_cluster:request:{request_id}"
_PENDING_KEY = "{prefix}cross_cluster:pending:{cluster}"
_POLICY_KEY = "{prefix}governance:policy:{policy_id}"


class RedisCrossClusterStore(CrossClusterStore):
    """Redis-backed cross-cluster state store."""

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    # -- Propagation requests -------------------------------------------------

    def save_request(
        self,
        request_id: str,
        data: dict[str, Any],
        ttl_seconds: int,
    ) -> None:
        key = _REQUEST_KEY.format(prefix=get_key_prefix(), request_id=request_id)
        try:
            self._redis.setex(key, ttl_seconds, fast_dumps_str(data))
        except Exception as e:
            logger.warning(
                "redis_cross_cluster_store.save_request_failed",
                request_id=request_id,
                error=e,
            )

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        key = _REQUEST_KEY.format(prefix=get_key_prefix(), request_id=request_id)
        try:
            data = self._redis.get(key)
            if data is None:
                return None
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            return fast_loads(data)
        except Exception as e:
            logger.warning(
                "redis_cross_cluster_store.get_request_failed",
                request_id=request_id,
                error=e,
            )
            return None

    def add_pending(self, cluster: str, request_id: str) -> None:
        key = _PENDING_KEY.format(prefix=get_key_prefix(), cluster=cluster)
        try:
            self._redis.sadd(key, request_id)
        except Exception as e:
            logger.warning(
                "redis_cross_cluster_store.add_pending_failed",
                cluster=cluster,
                request_id=request_id,
                error=e,
            )

    def remove_pending(self, cluster: str, request_id: str) -> None:
        key = _PENDING_KEY.format(prefix=get_key_prefix(), cluster=cluster)
        try:
            self._redis.srem(key, request_id)
        except Exception as e:
            logger.warning(
                "redis_cross_cluster_store.remove_pending_failed",
                cluster=cluster,
                request_id=request_id,
                error=e,
            )

    # -- Governance policies --------------------------------------------------

    def save_policy(self, policy_id: str, data: dict[str, Any]) -> None:
        key = _POLICY_KEY.format(prefix=get_key_prefix(), policy_id=policy_id)
        try:
            self._redis.set(key, fast_dumps_str(data))
        except Exception as e:
            logger.warning(
                "redis_cross_cluster_store.save_policy_failed",
                policy_id=policy_id,
                error=e,
            )

    def get_policy(self, policy_id: str) -> dict[str, Any] | None:
        key = _POLICY_KEY.format(prefix=get_key_prefix(), policy_id=policy_id)
        try:
            data = self._redis.get(key)
            if data is None:
                return None
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            return fast_loads(data)
        except Exception as e:
            logger.warning(
                "redis_cross_cluster_store.get_policy_failed",
                policy_id=policy_id,
                error=e,
            )
            return None
