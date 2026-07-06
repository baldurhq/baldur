"""
Unified Graceful Degradation Manager.

Provides coordinated access to all Phase 4 graceful degradation components:
- fallback_chain: Multi-tier fallback (Redis → Replica → Local → Memory)
- degraded_marker: Tracks degraded entries for reconciliation
- wal_recovery: WAL-based crash recovery
- degradation_manager: Level management and coordination
- circuit_breaker: Failure detection and prevention
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import structlog

from .circuit_breaker import HashChainCircuitBreaker
from .degradation_manager import HashChainDegradationManager
from .enums import DegradationLevel, FallbackConfig
from .fallback import HashChainFallbackChain
from .marker import DegradedEntryMarker
from .wal_recovery import HashChainWALRecovery

logger = structlog.get_logger()


class HashChainGracefulDegradationManager:
    """
    Unified manager for Phase 4 graceful degradation components.

    .. note::
        **Tier**: Dormant (compliance-grade enhancement, no standalone demand)
        **Status**: Not auto-wired. Available for custom integration engagements.
        PRO Audit (full) uses the basic file-based ``HashChainManager`` in
        ``audit/integrity/`` instead.

    Provides coordinated access to:
    - fallback_chain: Multi-tier fallback (Redis → Replica → Local → Memory)
    - degraded_marker: Tracks degraded entries for reconciliation
    - wal_recovery: WAL-based crash recovery
    - degradation_manager: Level management and coordination
    - circuit_breaker: Failure detection and prevention

    Pattern source:
        services/emergency_mode/manager.py (unified coordination)

    Usage:
        manager = HashChainGracefulDegradationManager(redis_client)
        manager.initialize()

        # During normal operation
        entry = manager.add_integrity_with_fallback(entry)

        # On startup
        manager.recover_on_startup()
    """

    def __init__(
        self,
        redis_client: Any | None = None,
        redis_replica: Any | None = None,
        key_prefix: str = "baldur:",
        wal_dir: Path | None = None,
        local_fallback_path: Path | None = None,
    ):
        """
        Initialize graceful degradation manager.

        Args:
            redis_client: Primary Redis client
            redis_replica: Replica Redis client (optional)
            key_prefix: Prefix for Redis keys
            wal_dir: Directory for WAL files
            local_fallback_path: Path for local fallback file
        """
        self._redis = redis_client
        self._redis_replica = redis_replica
        self._key_prefix = key_prefix
        self._wal_dir = Path(wal_dir) if wal_dir else Path("logs/audit/wal")
        self._local_fallback_path = (
            Path(local_fallback_path)
            if local_fallback_path
            else Path("logs/audit/fallback/degraded_entries.jsonl")
        )
        self._lock = threading.RLock()
        self._initialized = False

        # Components (lazy initialized)
        self._fallback_chain: HashChainFallbackChain | None = None
        self._degraded_marker: DegradedEntryMarker | None = None
        self._wal_recovery: HashChainWALRecovery | None = None
        self._degradation_manager: HashChainDegradationManager | None = None
        self._circuit_breaker: HashChainCircuitBreaker | None = None

    def initialize(self) -> None:
        """Initialize all components."""
        if self._initialized:
            return

        with self._lock:
            # Initialize degradation manager first (coordinates others)
            self._degradation_manager = HashChainDegradationManager(
                redis_client=self._redis,
                key_prefix=self._key_prefix,
                wal_dir=self._wal_dir,
            )

            # Initialize circuit breaker with degradation manager
            self._circuit_breaker = HashChainCircuitBreaker(
                name="hash_chain_redis",
                degradation_manager=self._degradation_manager,
            )

            # Initialize fallback chain
            self._fallback_chain = HashChainFallbackChain(
                redis_primary=self._redis,
                redis_replica=self._redis_replica,
                config=FallbackConfig(
                    key_prefix=self._key_prefix,
                    local_file_path=self._local_fallback_path,
                ),
            )

            # Initialize degraded marker
            self._degraded_marker = DegradedEntryMarker(
                redis_client=self._redis,
                key_prefix=self._key_prefix,
            )

            # Initialize WAL recovery
            self._wal_recovery = HashChainWALRecovery(
                wal_dir=self._wal_dir,
                redis_client=self._redis,
                key_prefix=self._key_prefix,
            )

            self._initialized = True
            logger.info("graceful_degradation.initialized_all_phase_components")

    def recover_on_startup(self) -> dict[str, Any]:
        """
        Perform recovery operations on startup.

        Should be called during application initialization.

        Returns:
            Recovery result dictionary
        """
        self.initialize()

        result = {
            "wal_recovery": {},
            "degraded_entries": 0,
            "status": "success",
        }

        try:
            # WAL recovery first
            if self._wal_recovery:
                result["wal_recovery"] = self._wal_recovery.recover_on_startup()

            # Check for unreconciled degraded entries
            if self._degraded_marker:
                result["degraded_entries"] = (
                    self._degraded_marker.get_unreconciled_count()
                )

            logger.info(
                "graceful_degradation.startup_recovery",
                recovery_result=result,
            )

        except Exception as e:
            result["status"] = "failed"
            result["error"] = str(e)
            logger.exception(
                "graceful_degradation.startup_recovery_failed",
                error=e,
            )

        return result

    def add_integrity_with_fallback(self, entry: dict[str, Any]) -> dict[str, Any]:
        """
        Add integrity with automatic fallback and circuit breaker.

        Args:
            entry: Log entry dictionary

        Returns:
            Entry with integrity fields
        """
        self.initialize()
        assert self._fallback_chain is not None  # initialize() populates
        assert self._degraded_marker is not None  # initialize() populates

        # Check circuit breaker
        if self._circuit_breaker and not self._circuit_breaker.can_execute():
            # Circuit open - use fallback directly
            result = self._fallback_chain.add_integrity(entry)
            if result.get("integrity", {}).get("degraded"):
                self._degraded_marker.mark_degraded(
                    result,
                    "circuit_open",
                    result.get("integrity", {}).get("tier", "unknown"),
                )
            return result

        # Try with circuit breaker
        try:
            result = self._fallback_chain.add_integrity(entry)

            # Record success if using primary
            if result.get("integrity", {}).get("tier") == "redis_primary":
                if self._circuit_breaker:
                    self._circuit_breaker.record_success()
            elif result.get("integrity", {}).get("degraded"):
                # Mark degraded entries
                self._degraded_marker.mark_degraded(
                    result,
                    result.get("integrity", {}).get("degraded_reason", "unknown"),
                    result.get("integrity", {}).get("tier", "unknown"),
                )

            return result

        except Exception as e:
            if self._circuit_breaker:
                self._circuit_breaker.record_failure(e)
            raise

    @property
    def degradation_level(self) -> DegradationLevel:
        """Get current degradation level."""
        if self._degradation_manager:
            return self._degradation_manager.level
        return DegradationLevel.NORMAL

    @property
    def is_degraded(self) -> bool:
        """Check if operating in degraded mode."""
        return self.degradation_level != DegradationLevel.NORMAL

    def get_status(self) -> dict[str, Any]:
        """Get comprehensive status of all components."""
        self.initialize()

        status: dict[str, Any] = {
            "degradation_level": self.degradation_level.value,
            "is_degraded": self.is_degraded,
            "initialized": self._initialized,
        }

        if self._circuit_breaker:
            status["circuit_breaker"] = self._circuit_breaker.get_stats()

        if self._fallback_chain:
            status["fallback_chain"] = self._fallback_chain.get_stats()

        if self._degraded_marker:
            status["degraded_marker"] = self._degraded_marker.get_stats()

        if self._wal_recovery:
            status["wal_recovery"] = self._wal_recovery.get_stats()

        if self._degradation_manager:
            status["degradation_manager"] = self._degradation_manager.get_status()

        return status

    def close(self) -> None:
        """Clean up resources."""
        if self._fallback_chain:
            self._fallback_chain.close()

        if self._wal_recovery:
            self._wal_recovery.close()


__all__ = ["HashChainGracefulDegradationManager"]
