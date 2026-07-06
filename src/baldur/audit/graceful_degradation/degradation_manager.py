"""
Hash Chain Degradation Manager.

Coordinates degradation levels across all hash chain components,
provides unified status, and triggers recovery when possible.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

from baldur.utils.serialization import fast_dumps_str
from baldur.utils.time import utc_now

from .enums import DegradationLevel, FallbackConfig
from .fallback import HashChainFallbackChain
from .marker import DegradedEntryMarker
from .wal_recovery import HashChainWALRecovery

logger = structlog.get_logger()


class HashChainDegradationManager:
    """
    Manages graceful degradation for hash chain operations.

    Coordinates degradation levels across all hash chain components,
    provides unified status, and triggers recovery when possible.

    Degradation Levels:
    - NORMAL: Full Redis functionality
    - DEGRADED: Local fallback active, entries marked for reconciliation
    - EMERGENCY: Memory-only, minimal functionality
    - READONLY: No writes, only cache reads

    Pattern source:
        services/emergency_mode/manager.py#L37

    Usage:
        manager = HashChainDegradationManager(redis_client)
        manager.on_redis_failure()  # Triggers degradation
        manager.on_redis_recovery()  # Triggers recovery
    """

    _instance: HashChainDegradationManager | None = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs) -> HashChainDegradationManager:
        """Singleton pattern."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        redis_client: Any | None = None,
        key_prefix: str = "baldur:",
        wal_dir: Path | None = None,
    ):
        """
        Initialize degradation manager.

        Args:
            redis_client: Redis client
            key_prefix: Prefix for Redis keys
            wal_dir: Directory for WAL files
        """
        if getattr(self, "_initialized", False):
            return

        self._redis = redis_client
        self._key_prefix = key_prefix
        self._wal_dir = Path(wal_dir) if wal_dir else Path("logs/audit/wal")
        self._state_lock = threading.RLock()

        # Current state
        self._level = (
            DegradationLevel.NORMAL if redis_client else DegradationLevel.DEGRADED
        )
        self._level_changed_at = utc_now().isoformat()
        self._failure_count = 0
        self._recovery_attempts = 0

        # Component references (lazy initialized)
        self._fallback_chain: HashChainFallbackChain | None = None
        self._degraded_marker: DegradedEntryMarker | None = None
        self._wal_recovery: HashChainWALRecovery | None = None

        # Callbacks
        self._on_degradation_callbacks: list[Callable[[DegradationLevel], None]] = []
        self._on_recovery_callbacks: list[Callable[[DegradationLevel], None]] = []

        self._initialized = True

    @property
    def level(self) -> DegradationLevel:
        """Get current degradation level."""
        return self._level

    @property
    def is_degraded(self) -> bool:
        """Check if operating in any degraded mode."""
        return self._level != DegradationLevel.NORMAL

    def set_level(self, level: DegradationLevel, reason: str = "") -> None:
        """
        Set degradation level.

        Args:
            level: New degradation level
            reason: Reason for level change
        """
        with self._state_lock:
            if level == self._level:
                return

            old_level = self._level
            self._level = level
            self._level_changed_at = utc_now().isoformat()

            logger.warning(
                "hash_chain_degradation.level_changed_reason_else",
                old_level=old_level.value,
                degradation_level=level.value,
                degradation_reason=f" ({reason})" if reason else "",
            )

            # Record in Redis if available
            self._record_level_change(old_level, level, reason)

            # Broadcast state change
            from baldur.audit.resilience.degradation_protocol import (
                DegradationBroadcaster,
            )

            DegradationBroadcaster.notify(
                "redis_hashchain",
                level != DegradationLevel.NORMAL,
                level.value,
                reason,
            )

            # Notify callbacks
            if level == DegradationLevel.NORMAL:
                for callback in self._on_recovery_callbacks:
                    try:
                        callback(level)
                    except Exception as e:
                        logger.exception(
                            "hash_chain_degradation.recovery_callback_failed",
                            error=e,
                        )
            else:
                for callback in self._on_degradation_callbacks:
                    try:
                        callback(level)
                    except Exception as e:
                        logger.exception(
                            "hash_chain_degradation.degradation_callback_failed",
                            error=e,
                        )

    def _record_level_change(
        self,
        old_level: DegradationLevel,
        new_level: DegradationLevel,
        reason: str,
    ) -> None:
        """Record level change in Redis."""
        if not self._redis:
            return

        try:
            key = f"{self._key_prefix}audit:hash_chain:degradation_history"
            timestamp = utc_now().isoformat()
            pod_id = os.environ.get("HOSTNAME", os.environ.get("POD_NAME", "unknown"))

            entry = fast_dumps_str(
                {
                    "timestamp": timestamp,
                    "old_level": old_level.value,
                    "new_level": new_level.value,
                    "reason": reason,
                    "pod_id": pod_id,
                }
            )

            self._redis.lpush(key, entry)
            # Keep only last 100 entries
            self._redis.ltrim(key, 0, 99)

        except Exception as e:
            logger.debug(
                "hash_chain_degradation.record_failed",
                error=e,
            )

    def on_redis_failure(self, error: Exception | None = None) -> None:
        """
        Handle Redis failure event.

        Triggers degradation and activates fallback mechanisms.

        Args:
            error: The exception that caused the failure
        """
        with self._state_lock:
            self._failure_count += 1

            reason = str(error) if error else "connection_failed"

            if self._level == DegradationLevel.NORMAL:
                self.set_level(DegradationLevel.DEGRADED, reason)
            elif self._level == DegradationLevel.DEGRADED and self._failure_count > 10:
                self.set_level(DegradationLevel.EMERGENCY, "repeated_failures")

    def on_redis_recovery(self) -> None:
        """
        Handle Redis recovery event.

        Triggers recovery process including:
        1. WAL replay
        2. Degraded entry reconciliation
        3. Level restoration
        """
        with self._state_lock:
            self._recovery_attempts += 1

            try:
                # Attempt WAL recovery if available
                if self._wal_recovery:
                    wal_result = self._wal_recovery.recover_on_startup()
                    logger.info(
                        "hash_chain_degradation.wal_recovery",
                        wal_result=wal_result,
                    )

                # Reset failure count on successful recovery
                self._failure_count = 0

                # Restore normal operation
                self.set_level(DegradationLevel.NORMAL, "redis_recovered")

            except Exception as e:
                logger.exception(
                    "watchdog.recovery_failed",
                    error=e,
                )
                # Stay in degraded mode

    def on_filesystem_failure(self) -> None:
        """Handle filesystem failure event."""
        self.set_level(DegradationLevel.EMERGENCY, "filesystem_failure")

    def register_on_degradation(
        self, callback: Callable[[DegradationLevel], None]
    ) -> None:
        """Register callback for degradation events."""
        self._on_degradation_callbacks.append(callback)

    def register_on_recovery(
        self, callback: Callable[[DegradationLevel], None]
    ) -> None:
        """Register callback for recovery events."""
        self._on_recovery_callbacks.append(callback)

    def get_fallback_chain(self) -> HashChainFallbackChain:
        """Get or create fallback chain instance."""
        if self._fallback_chain is None:
            self._fallback_chain = HashChainFallbackChain(
                redis_primary=self._redis,
                config=FallbackConfig(key_prefix=self._key_prefix),
            )
        return self._fallback_chain

    def get_degraded_marker(self) -> DegradedEntryMarker:
        """Get or create degraded marker instance."""
        if self._degraded_marker is None:
            self._degraded_marker = DegradedEntryMarker(
                redis_client=self._redis,
                key_prefix=self._key_prefix,
            )
        return self._degraded_marker

    def get_wal_recovery(self) -> HashChainWALRecovery:
        """Get or create WAL recovery instance."""
        if self._wal_recovery is None:
            self._wal_recovery = HashChainWALRecovery(
                wal_dir=self._wal_dir,
                redis_client=self._redis,
                key_prefix=self._key_prefix,
            )
        return self._wal_recovery

    def get_status(self) -> dict[str, Any]:
        """Get comprehensive degradation status."""
        status: dict[str, Any] = {
            "level": self._level.value,
            "is_degraded": self.is_degraded,
            "level_changed_at": self._level_changed_at,
            "failure_count": self._failure_count,
            "recovery_attempts": self._recovery_attempts,
            "redis_available": self._redis is not None,
        }

        if self._fallback_chain:
            status["fallback"] = self._fallback_chain.get_stats()

        if self._degraded_marker:
            status["degraded_marker"] = self._degraded_marker.get_stats()

        if self._wal_recovery:
            status["wal_recovery"] = self._wal_recovery.get_stats()

        return status

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance (for testing)."""
        with cls._lock:
            cls._instance = None


__all__ = ["HashChainDegradationManager"]
