"""
Regional Isolation Gate.

Region-level traffic blocking gate.

Blocks traffic to a region globally when that region
(a cluster group) becomes unstable.

Audit Integration:
- Region isolation: log_region_isolation_audit (action="isolate")
- Region restore: log_region_isolation_audit (action="restore")

Code basis:
- blast_radius.py: the REGION level already exists
- guard.py: the global blocking pattern already exists
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.helpers import log_region_isolation_audit
from baldur.core.serializable import SerializableMixin
from baldur.utils.serialization import fast_dumps_str, fast_loads
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.core.cluster_identity import ClusterIdentity

logger = structlog.get_logger()


@dataclass
class IsolationInfo(SerializableMixin):
    """Region isolation information."""

    region: str
    isolated: bool
    reason: str
    isolated_at: datetime | None = None
    isolated_by: str | None = None
    expires_at: datetime | None = None


class RegionalIsolationGate:
    """
    Region-level traffic blocking gate.

    Blocks traffic to a region globally when that region
    (a cluster group) becomes unstable.

    Usage:
        gate = get_regional_isolation_gate()

        # Isolate a region
        gate.isolate_region("tokyo", reason="High error rate", duration_seconds=300)

        # Check isolation state
        is_isolated, reason = gate.is_region_isolated("tokyo")
        if is_isolated:
            return redirect_to_fallback()

        # Lift the isolation
        gate.restore_region("tokyo")
    """

    # Redis key patterns
    GATE_KEY_TEMPLATE = "baldur:global:isolation:{region}"
    ISOLATION_LIST_KEY = "baldur:global:isolation:list"

    # Event channel
    ISOLATION_EVENT_CHANNEL = "baldur:global:isolation:events"

    def __init__(
        self,
        global_redis: Any | None = None,
        cluster_identity: ClusterIdentity | None = None,
    ):
        """
        Initialize RegionalIsolationGate.

        Args:
            global_redis: Global Redis client
            cluster_identity: Cluster identity information
        """
        self._redis = global_redis
        self._identity = cluster_identity
        self._initialized = False

    def _ensure_initialized(self) -> None:
        """Perform lazy initialization."""
        if self._initialized:
            return

        # Initialize ClusterIdentity
        if self._identity is None:
            try:
                from baldur.core.cluster_identity import get_cluster_identity

                self._identity = get_cluster_identity()
            except Exception as e:
                logger.warning(
                    "regional_isolation_gate.get_cluster_identity_failed",
                    error=e,
                )

        # Initialize the Redis client
        if self._redis is None:
            try:
                from baldur.core.tiered_redis import (
                    RedisScope,
                    TieredRedisProvider,
                )

                provider = TieredRedisProvider()
                self._redis = provider.get_redis(RedisScope.GLOBAL)
            except Exception as e:
                logger.warning(
                    "regional_isolation_gate.initialize_redis_failed",
                    error=e,
                )

        self._initialized = True

    def isolate_region(
        self,
        region: str,
        reason: str,
        duration_seconds: int = 300,
    ) -> bool:
        """
        Activate region isolation.

        Args:
            region: Region to isolate
            reason: Isolation reason
            duration_seconds: Isolation duration in seconds (default 5 minutes)

        Returns:
            Whether the isolation succeeded
        """
        self._ensure_initialized()

        if not self._redis:
            logger.warning("regional_isolation_gate.redis_available")
            return False

        try:
            key = self.GATE_KEY_TEMPLATE.format(region=region)
            now = utc_now()
            expires_at = datetime.fromtimestamp(
                now.timestamp() + duration_seconds, tz=UTC
            )

            operator = self._identity.cluster_id if self._identity else "unknown"

            isolation_info = IsolationInfo(
                region=region,
                isolated=True,
                reason=reason,
                isolated_at=now,
                isolated_by=operator,
                expires_at=expires_at,
            )

            # Store
            self._redis.set(
                key, fast_dumps_str(isolation_info.to_dict()), ex=duration_seconds
            )

            # Add to the list
            self._redis.sadd(self.ISOLATION_LIST_KEY, region)

            # Publish the event
            self._publish_event("isolated", isolation_info)

            logger.warning(
                "cell_evacuation.cell_isolated",
                target_region=region,
                reason=reason,
                duration_seconds=duration_seconds,
                operator=operator,
            )

            # === Audit record: region isolation ===
            log_region_isolation_audit(
                region=region,
                action="isolate",
                result="success",
                reason=reason,
                duration_seconds=duration_seconds,
                operator=operator,
            )

            return True

        except Exception as e:
            logger.exception(
                "regional_isolation_gate.isolate_region_failed",
                target_region=region,
                error=e,
            )

            # === Audit record: region isolation failed ===
            log_region_isolation_audit(
                region=region,
                action="isolate",
                result="failed",
                reason=reason,
                duration_seconds=duration_seconds,
                operator=self._identity.cluster_id if self._identity else "unknown",
                details={"error": str(e)},
            )

            return False

    def is_region_isolated(self, region: str) -> tuple[bool, str | None]:
        """
        Check the isolation state of a region.

        Args:
            region: Region to check

        Returns:
            Tuple of (is isolated, isolation reason)
        """
        self._ensure_initialized()

        if not self._redis:
            return False, None

        try:
            key = self.GATE_KEY_TEMPLATE.format(region=region)
            data = self._redis.get(key)

            if data:
                if isinstance(data, bytes):
                    data = data.decode()
                info = IsolationInfo.from_dict(fast_loads(data))
                return info.isolated, info.reason

            return False, None

        except Exception as e:
            logger.exception(
                "regional_isolation_gate.check_isolation_status_failed",
                error=e,
            )
            return False, None

    def get_isolation_info(self, region: str) -> IsolationInfo | None:
        """
        Query detailed isolation information for a region.

        Args:
            region: Region to query

        Returns:
            Isolation information, or None
        """
        self._ensure_initialized()

        if not self._redis:
            return None

        try:
            key = self.GATE_KEY_TEMPLATE.format(region=region)
            data = self._redis.get(key)

            if data:
                if isinstance(data, bytes):
                    data = data.decode()
                return IsolationInfo.from_dict(fast_loads(data))

            return None

        except Exception as e:
            logger.exception(
                "regional_isolation_gate.get_isolation_info_failed",
                error=e,
            )
            return None

    def restore_region(self, region: str) -> bool:
        """
        Lift the isolation of a region.

        Args:
            region: Region to restore

        Returns:
            Whether the restore succeeded
        """
        self._ensure_initialized()

        if not self._redis:
            return False

        operator = self._identity.cluster_id if self._identity else "unknown"

        try:
            key = self.GATE_KEY_TEMPLATE.format(region=region)

            # Query the existing information
            existing = self.get_isolation_info(region)

            # Delete
            deleted = self._redis.delete(key)
            self._redis.srem(self.ISOLATION_LIST_KEY, region)

            if deleted:
                # Publish the event
                restore_info = IsolationInfo(
                    region=region,
                    isolated=False,
                    reason="Manual restore",
                    isolated_by=operator,
                )
                self._publish_event("restored", restore_info)

                logger.info(
                    "regional_isolation_gate.region_restored",
                    target_region=region,
                )

                # === Audit record: region restore ===
                log_region_isolation_audit(
                    region=region,
                    action="restore",
                    result="success",
                    reason="Manual restore",
                    operator=operator,
                    details={
                        "previous_reason": existing.reason if existing else None,
                        "was_isolated_by": existing.isolated_by if existing else None,
                    },
                )

                return True

            return False

        except Exception as e:
            logger.exception(
                "regional_isolation_gate.restore_region_failed",
                target_region=region,
                error=e,
            )

            # === Audit record: region restore failed ===
            log_region_isolation_audit(
                region=region,
                action="restore",
                result="failed",
                reason="Manual restore",
                operator=operator,
                details={"error": str(e)},
            )

            return False

    def list_isolated_regions(self) -> dict[str, IsolationInfo]:
        """
        List every region currently isolated.

        Returns:
            {region: IsolationInfo} dictionary
        """
        self._ensure_initialized()

        if not self._redis:
            return {}

        try:
            regions = self._redis.smembers(self.ISOLATION_LIST_KEY)
            result = {}

            for region in regions:
                if isinstance(region, bytes):
                    region = region.decode()

                info = self.get_isolation_info(region)
                if info and info.isolated:
                    result[region] = info
                else:
                    # Clean up expired entries
                    self._redis.srem(self.ISOLATION_LIST_KEY, region)

            return result

        except Exception as e:
            logger.exception(
                "regional_isolation_gate.list_isolated_regions_failed",
                error=e,
            )
            return {}

    def _publish_event(self, event_type: str, info: IsolationInfo) -> None:
        """Publish an isolation event."""
        if not self._redis:
            return

        try:
            event = {
                "type": event_type,
                "info": info.to_dict(),
                "timestamp": utc_now().isoformat(),
            }
            self._redis.publish(self.ISOLATION_EVENT_CHANNEL, fast_dumps_str(event))
        except Exception as e:
            logger.exception(
                "regional_isolation_gate.publish_event_failed",
                error=e,
            )

    def is_current_region_isolated(self) -> tuple[bool, str | None]:
        """
        Check the isolation state of the region this cluster belongs to.

        Returns:
            Tuple of (is isolated, isolation reason)
        """
        self._ensure_initialized()

        if not self._identity or not self._identity.region:
            return False, None

        return self.is_region_isolated(self._identity.region)


# =============================================================================
# Singleton
# =============================================================================

_gate: RegionalIsolationGate | None = None
_gate_lock = threading.Lock()


def get_regional_isolation_gate() -> RegionalIsolationGate:
    """Return the RegionalIsolationGate singleton."""
    global _gate
    if _gate is None:
        with _gate_lock:
            if _gate is None:
                _gate = RegionalIsolationGate()
    return _gate


def reset_regional_isolation_gate() -> None:
    """Reset the singleton (test use)."""
    global _gate
    _gate = None
