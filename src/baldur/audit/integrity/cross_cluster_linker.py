"""
Cross-Cluster Audit Linker.

Links each cluster's local hash chain through a global anchor.

Design principles:
- Chains stay independent per cluster (Local Chain) - guarantees performance
- Only daily anchors are consolidated into the global store (Global Anchoring)
  - org-wide integrity
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from baldur.core.cluster_identity import ClusterIdentity

from baldur.settings.audit_integrity import get_audit_integrity_settings
from baldur.utils.serialization import fast_dumps_str, fast_loads
from baldur.utils.time import utc_now

logger = structlog.get_logger()


def _get_local_anchor_ttl_days() -> int:
    """Get local anchor TTL from settings."""
    return get_audit_integrity_settings().cross_cluster_local_ttl_days


def _get_global_anchor_ttl_days() -> int:
    """Get global anchor TTL from settings."""
    return get_audit_integrity_settings().cross_cluster_global_ttl_days


@dataclass
class ClusterDailyAnchor:
    """Per-cluster daily anchor."""

    cluster_id: str
    anchor_date: date
    final_sequence: int
    final_hash: str
    entry_count: int
    created_at: datetime = field(default_factory=lambda: utc_now())

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "date": self.anchor_date.isoformat(),
            "final_sequence": self.final_sequence,
            "final_hash": self.final_hash,
            "entry_count": self.entry_count,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClusterDailyAnchor:
        """Build a ClusterDailyAnchor from a dict."""
        return cls(
            cluster_id=data["cluster_id"],
            anchor_date=date.fromisoformat(data["date"]),
            final_sequence=data["final_sequence"],
            final_hash=data["final_hash"],
            entry_count=data["entry_count"],
            created_at=(
                datetime.fromisoformat(data["created_at"])
                if data.get("created_at")
                else utc_now()
            ),
        )

    def compute_anchor_hash(self) -> str:
        """Compute the anchor hash."""
        data = f"{self.cluster_id}:{self.anchor_date}:{self.final_sequence}:{self.final_hash}"
        return hashlib.sha256(data.encode()).hexdigest()


@dataclass
class GlobalDailyAnchor:
    """Global daily anchor (all clusters consolidated)."""

    anchor_date: date
    cluster_anchors: list[ClusterDailyAnchor]
    global_hash: str = ""
    created_at: datetime = field(default_factory=lambda: utc_now())

    def __post_init__(self) -> None:
        if not self.global_hash:
            self.global_hash = self._compute_global_hash()

    def _compute_global_hash(self) -> str:
        """Global hash combining every cluster anchor."""
        if not self.cluster_anchors:
            return hashlib.sha256(b"empty").hexdigest()

        # Sort by cluster ID to guarantee a deterministic hash
        sorted_anchors = sorted(self.cluster_anchors, key=lambda a: a.cluster_id)
        combined = ":".join(a.compute_anchor_hash() for a in sorted_anchors)
        return hashlib.sha256(combined.encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.anchor_date.isoformat(),
            "cluster_count": len(self.cluster_anchors),
            "cluster_anchors": [a.to_dict() for a in self.cluster_anchors],
            "global_hash": self.global_hash,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GlobalDailyAnchor:
        """Build a GlobalDailyAnchor from a dict."""
        cluster_anchors = [
            ClusterDailyAnchor.from_dict(ca) for ca in data.get("cluster_anchors", [])
        ]
        return cls(
            anchor_date=date.fromisoformat(data["date"]),
            cluster_anchors=cluster_anchors,
            global_hash=data.get("global_hash", ""),
            created_at=(
                datetime.fromisoformat(data["created_at"])
                if data.get("created_at")
                else utc_now()
            ),
        )


class CrossClusterAuditLinker:
    """
    Cross-cluster audit chain linker.

    Hybrid strategy:
    - Local: each cluster maintains an independent hash chain (performance)
    - Global: only daily anchors are consolidated into the global store
      (integrity proof)
    """

    # Redis key patterns
    LOCAL_ANCHOR_KEY_TEMPLATE = "{prefix}audit:anchor:{date}"
    GLOBAL_ANCHOR_KEY_TEMPLATE = "baldur:global:anchor:{date}"
    GLOBAL_ANCHOR_LIST_KEY = "baldur:global:anchor:list"

    # Legacy constants for backward compatibility
    LOCAL_ANCHOR_TTL_DAYS = 90
    GLOBAL_ANCHOR_TTL_DAYS = 365

    def __init__(
        self,
        local_redis: Any | None = None,
        global_redis: Any | None = None,
        cluster_identity: ClusterIdentity | None = None,
        key_prefix: str = "baldur:",
        local_anchor_ttl_days: int | None = None,
        global_anchor_ttl_days: int | None = None,
    ):
        """
        Initialize CrossClusterAuditLinker.

        Args:
            local_redis: Local Redis client
            global_redis: Global Redis client (falls back to local if unset)
            cluster_identity: Cluster identification info
            key_prefix: Redis key prefix
            local_anchor_ttl_days: Local anchor TTL
                (default from AuditIntegritySettings)
            global_anchor_ttl_days: Global anchor TTL
                (default from AuditIntegritySettings)
        """
        self._local_redis = local_redis
        self._global_redis = global_redis or local_redis
        self._identity = cluster_identity
        self._key_prefix = key_prefix
        self._local_anchor_ttl = (
            local_anchor_ttl_days
            if local_anchor_ttl_days is not None
            else _get_local_anchor_ttl_days()
        )
        self._global_anchor_ttl = (
            global_anchor_ttl_days
            if global_anchor_ttl_days is not None
            else _get_global_anchor_ttl_days()
        )
        self._initialized = False

    def _ensure_initialized(self) -> None:
        """Perform lazy initialization."""
        if self._initialized:
            return

        # ClusterIdentity initialization
        if self._identity is None:
            try:
                from baldur.core.cluster_identity import get_cluster_identity

                self._identity = get_cluster_identity()
            except Exception as e:
                logger.warning(
                    "cross_cluster_audit_linker.get_cluster_identity_failed",
                    error=e,
                )

        # Redis client initialization
        if self._local_redis is None:
            try:
                from baldur.core.tiered_redis import (
                    RedisScope,
                    TieredRedisProvider,
                )

                provider = TieredRedisProvider()
                self._local_redis = provider.get_redis(RedisScope.LOCAL)
                if self._global_redis is None:
                    self._global_redis = provider.get_redis(RedisScope.GLOBAL)
            except Exception as e:
                logger.warning(
                    "cross_cluster_audit_linker.initialize_redis_failed",
                    error=e,
                )

        self._initialized = True

    def create_local_anchor(
        self, target_date: date | None = None
    ) -> ClusterDailyAnchor | None:
        """
        Create the local cluster's daily anchor.

        Args:
            target_date: Target date (default: yesterday)

        Returns:
            The created anchor, or None
        """
        self._ensure_initialized()

        if target_date is None:
            target_date = (utc_now() - timedelta(days=1)).date()

        if not self._local_redis:
            logger.warning("cross_cluster_audit_linker.local_redis_available")
            return None

        try:
            # Look up that date's last entry in the local chain
            state_key = f"{self._key_prefix}audit:hash_chain:state"
            state = self._local_redis.hgetall(state_key)

            if not state:
                logger.warning("cross_cluster_audit_linker.no_hash_chain_state")
                return None

            # bytes to str conversion if needed
            def get_state_value(key: str) -> Any:
                value = state.get(key.encode(), state.get(key))
                if isinstance(value, bytes):
                    return value.decode()
                return value

            sequence = int(get_state_value("sequence") or 0)
            previous_hash = get_state_value("previous_hash") or ""

            # Create the anchor
            cluster_id = self._identity.cluster_id if self._identity else "unknown"
            anchor = ClusterDailyAnchor(
                cluster_id=cluster_id,
                anchor_date=target_date,
                final_sequence=sequence,
                final_hash=previous_hash,
                entry_count=sequence,  # use the full sequence as the entry count
            )

            # Store locally
            anchor_key = self.LOCAL_ANCHOR_KEY_TEMPLATE.format(
                prefix=self._key_prefix, date=target_date.isoformat()
            )
            self._local_redis.set(
                anchor_key,
                fast_dumps_str(anchor.to_dict()),
                ex=self._local_anchor_ttl * 86400,
            )

            logger.info(
                "cross_cluster_audit_linker.created_local_anchor",
                target_date=target_date,
                anchor=anchor.compute_anchor_hash()[:16],
            )
            return anchor

        except Exception as e:
            logger.exception(
                "cross_cluster_audit_linker.create_local_anchor_failed",
                error=e,
            )
            return None

    def get_local_anchor(self, target_date: date) -> ClusterDailyAnchor | None:
        """
        Look up a local anchor.

        Args:
            target_date: Target date

        Returns:
            The anchor, or None
        """
        self._ensure_initialized()

        if not self._local_redis:
            return None

        try:
            anchor_key = self.LOCAL_ANCHOR_KEY_TEMPLATE.format(
                prefix=self._key_prefix, date=target_date.isoformat()
            )
            data = self._local_redis.get(anchor_key)
            if data:
                if isinstance(data, bytes):
                    data = data.decode()
                return ClusterDailyAnchor.from_dict(fast_loads(data))
            return None
        except Exception as e:
            logger.exception(
                "cross_cluster_audit_linker.get_local_anchor_failed",
                error=e,
            )
            return None

    def submit_to_global(self, anchor: ClusterDailyAnchor) -> bool:
        """
        Submit a local anchor to the global store.

        Args:
            anchor: Local anchor

        Returns:
            Whether the submission succeeded
        """
        self._ensure_initialized()

        if not self._global_redis:
            logger.warning("cross_cluster_audit_linker.global_redis_available")
            return False

        try:
            # Global anchor key
            global_key = self.GLOBAL_ANCHOR_KEY_TEMPLATE.format(
                date=anchor.anchor_date.isoformat()
            )

            # Look up the existing global anchor
            existing = self._global_redis.get(global_key)
            if existing:
                if isinstance(existing, bytes):
                    existing = existing.decode()
                global_data = fast_loads(existing)
                global_anchor = GlobalDailyAnchor.from_dict(global_data)
                cluster_anchors = list(global_anchor.cluster_anchors)

                # Duplicate check
                if any(ca.cluster_id == anchor.cluster_id for ca in cluster_anchors):
                    logger.info(
                        "cross_cluster_audit_linker.anchor_already_submitted",
                        anchor=anchor.cluster_id,
                    )
                    return True
                cluster_anchors.append(anchor)
            else:
                cluster_anchors = [anchor]

            # Create/refresh the global anchor
            global_anchor = GlobalDailyAnchor(
                anchor_date=anchor.anchor_date,
                cluster_anchors=cluster_anchors,
            )

            self._global_redis.set(
                global_key,
                fast_dumps_str(global_anchor.to_dict()),
                ex=self._global_anchor_ttl * 86400,
            )

            # Add to the anchor list
            self._global_redis.zadd(
                self.GLOBAL_ANCHOR_LIST_KEY,
                {anchor.anchor_date.isoformat(): anchor.anchor_date.toordinal()},
            )

            logger.info(
                "cross_cluster_audit_linker.submitted_global_global_hash",
                anchor=anchor.cluster_id,
                anchor_date=anchor.anchor_date,
                global_anchor=global_anchor.global_hash[:16],
            )
            return True

        except Exception as e:
            logger.exception(
                "cross_cluster_audit_linker.submit_global_failed",
                error=e,
            )
            return False

    def get_global_anchor(self, target_date: date) -> GlobalDailyAnchor | None:
        """
        Look up a global anchor.

        Args:
            target_date: Target date

        Returns:
            The global anchor, or None
        """
        self._ensure_initialized()

        if not self._global_redis:
            return None

        try:
            global_key = self.GLOBAL_ANCHOR_KEY_TEMPLATE.format(
                date=target_date.isoformat()
            )
            data = self._global_redis.get(global_key)
            if data:
                if isinstance(data, bytes):
                    data = data.decode()
                return GlobalDailyAnchor.from_dict(fast_loads(data))
            return None
        except Exception as e:
            logger.exception(
                "cross_cluster_audit_linker.get_global_anchor_failed",
                error=e,
            )
            return None

    def verify_global_integrity(self, target_date: date) -> dict[str, Any]:
        """
        Verify global anchor integrity.

        Args:
            target_date: Date to verify

        Returns:
            Verification result
        """
        self._ensure_initialized()

        if not self._global_redis:
            return {"valid": False, "error": "Global Redis not available"}

        try:
            global_anchor = self.get_global_anchor(target_date)

            if not global_anchor:
                return {"valid": False, "error": "Global anchor not found"}

            # Recompute the global hash
            recomputed = GlobalDailyAnchor(
                anchor_date=target_date,
                cluster_anchors=global_anchor.cluster_anchors,
            )

            stored_hash = global_anchor.global_hash
            computed_hash = recomputed.global_hash

            return {
                "valid": stored_hash == computed_hash,
                "date": target_date.isoformat(),
                "cluster_count": len(global_anchor.cluster_anchors),
                "clusters": [ca.cluster_id for ca in global_anchor.cluster_anchors],
                "stored_hash": stored_hash[:16] + "..." if stored_hash else None,
                "computed_hash": computed_hash[:16] + "..." if computed_hash else None,
            }

        except Exception as e:
            return {"valid": False, "error": str(e)}

    def list_global_anchors(self, limit: int = 30) -> list[str]:
        """
        List global anchors.

        Args:
            limit: Maximum number of results

        Returns:
            List of date strings (newest first)
        """
        self._ensure_initialized()

        if not self._global_redis:
            return []

        try:
            # Query newest first
            dates = self._global_redis.zrevrange(
                self.GLOBAL_ANCHOR_LIST_KEY, 0, limit - 1
            )
            return [d.decode() if isinstance(d, bytes) else d for d in dates]
        except Exception as e:
            logger.exception(
                "cross_cluster_audit_linker.list_global_anchors_failed",
                error=e,
            )
            return []


# =============================================================================
# Singleton
# =============================================================================

_linker: CrossClusterAuditLinker | None = None
_linker_lock = threading.Lock()


def get_cross_cluster_audit_linker() -> CrossClusterAuditLinker:
    """Return the CrossClusterAuditLinker singleton."""
    global _linker
    if _linker is None:
        with _linker_lock:
            if _linker is None:
                _linker = CrossClusterAuditLinker()
    return _linker


def reset_cross_cluster_audit_linker() -> None:
    """Reset the singleton (for tests)."""
    global _linker
    _linker = None
