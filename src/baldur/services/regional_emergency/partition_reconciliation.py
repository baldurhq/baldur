"""
Partition Reconciliation Service.

State reconciliation service for recovery from network partitions.

Key features:
- check_partition_status(): check the current region's partition state
- reconcile_after_recovery(): reconcile state after network recovery
- start_heartbeat_loop(): start background heartbeat monitoring
- stop_heartbeat_loop(): stop heartbeat monitoring

Design principles:
- A partitioned region keeps its own state (Safety-First)
- TTL-based auto expiry prevents permanently stale state
- On recovery, notify the operator instead of forcing a sync

Code reference:
    error_budget/reconciliation/service.py (ReconciliationService pattern)
    isolation/regional_gate.py (TTL-based expiry)
    core/tiered_redis.py (TieredRedisProvider)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.meta.daemon_worker import DaemonWorkerHandle

logger = structlog.get_logger()


# =============================================================================
# Constants
# =============================================================================

HEARTBEAT_INTERVAL_SECONDS = 10
"""Heartbeat send interval (seconds)."""

PARTITION_DETECTION_THRESHOLD_SECONDS = 30
"""Partition detection threshold (seconds). Failing heartbeats for this long
means the region is considered partitioned."""

MAX_RECONCILIATION_ACTIONS = 100
"""Maximum size of the reconciliation action history."""


@dataclass
class PartitionStatus(SerializableMixin):
    """
    Network partition state.

    Represents the current region's connectivity to the Global Redis.
    """

    is_partitioned: bool = False
    """Whether the region is partitioned."""

    last_heartbeat_at: str | None = None
    """Timestamp of the last successful heartbeat (ISO format)."""

    partition_duration_seconds: float = 0.0
    """Partition duration (seconds)."""

    error_message: str | None = None
    """Error message on connection failure."""

    global_redis_url: str | None = None
    """Global Redis URL (masked)."""

    checked_at: str = field(default_factory=lambda: utc_now().isoformat())
    """Check timestamp."""


@dataclass
class ReconciliationAction(SerializableMixin):
    """
    Reconciliation action.

    An action performed after network recovery.
    """

    action_type: str = ""
    """Action type (NOTIFICATION, STATE_SYNC, MANUAL_REVIEW)."""

    message: str = ""
    """Action detail message."""

    namespace: str = ""
    """Target namespace."""

    executed_at: str = field(default_factory=lambda: utc_now().isoformat())
    """Execution timestamp."""

    success: bool = True
    """Whether the execution succeeded."""


@dataclass
class ReconciliationResult(SerializableMixin):
    """
    Reconciliation result.

    Result of a reconcile_after_recovery() call.
    """

    reconciled: bool = False
    """Whether reconciliation was performed."""

    reason: str | None = None
    """Reason reconciliation failed or was skipped."""

    actions: list[ReconciliationAction] = field(default_factory=list)
    """Actions performed."""

    global_state_mode: str = "UNKNOWN"
    """Global Emergency mode."""

    regional_state_mode: str = "UNKNOWN"
    """Regional Emergency mode."""

    executed_at: str = field(default_factory=lambda: utc_now().isoformat())
    """Execution timestamp."""


class PartitionReconciliationService:
    """
    State reconciliation service for recovery from network partitions.

    Monitors connectivity to the Global Redis and reconciles state
    mismatches after the network recovers.

    Design principles:
    - Safety-First: a partitioned region keeps its own protection mode
    - No forced sync: on recovery, notify the operator instead
    - TTL-based expiry: prevents permanently stale state

    Usage:
        service = PartitionReconciliationService()

        # Check partition state
        status = service.check_partition_status()
        if status.is_partitioned:
            print(f"Partitioned for {status.partition_duration_seconds}s")

        # Reconcile after recovery
        result = service.reconcile_after_recovery()
        for action in result.actions:
            print(f"Action: {action.action_type} - {action.message}")
    """

    def __init__(
        self,
        tracker: Any | None = None,
        tiered_redis: Any | None = None,
        heartbeat_interval: int = HEARTBEAT_INTERVAL_SECONDS,
        partition_threshold: int = PARTITION_DETECTION_THRESHOLD_SECONDS,
    ):
        """
        Initialize PartitionReconciliationService.

        Args:
            tracker: NamespacedEmergencyTracker instance
            tiered_redis: TieredRedisProvider instance
            heartbeat_interval: heartbeat interval (seconds)
            partition_threshold: partition detection threshold (seconds)
        """
        self._tracker = tracker
        self._tiered_redis = tiered_redis
        self._heartbeat_interval = heartbeat_interval
        self._partition_threshold = partition_threshold
        self._lock = threading.Lock()

        # State
        self._last_global_heartbeat: datetime | None = None
        self._is_partitioned = False
        self._partition_start: datetime | None = None

        # Action history
        self._action_history: list[ReconciliationAction] = []

        # Heartbeat thread
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_running = False
        self._handle: DaemonWorkerHandle | None = None  # impl 489 D9

    # =========================================================================
    # Dependency Injection
    # =========================================================================

    def _get_tracker(self) -> Any:
        """Obtain the NamespacedEmergencyTracker instance."""
        if self._tracker is None:
            from baldur.services.regional_emergency.tracker import (
                get_namespaced_emergency_tracker,
            )

            self._tracker = get_namespaced_emergency_tracker()
        return self._tracker

    def _get_tiered_redis(self) -> Any:
        """Obtain the TieredRedisProvider instance."""
        if self._tiered_redis is None:
            try:
                from baldur.core.tiered_redis import TieredRedisProvider

                self._tiered_redis = TieredRedisProvider()
            except ImportError:
                logger.warning("partition_reconciliation.tieredredisprovider_available")
        return self._tiered_redis

    def _get_current_namespace(self) -> str:
        """Obtain the current region's namespace."""
        try:
            from baldur.core.cluster_identity import get_cluster_identity

            identity = get_cluster_identity()
            return identity.region or "global"
        except Exception:
            return "global"

    # =========================================================================
    # Partition Detection
    # =========================================================================

    def check_partition_status(self) -> PartitionStatus:
        """
        Check the current region's network partition state.

        Pings the Global Redis to determine connectivity.

        Returns:
            PartitionStatus instance
        """
        now = utc_now()

        try:
            # Global Redis ping
            success = self._ping_global_redis()

            if success:
                with self._lock:
                    self._last_global_heartbeat = now
                    self._is_partitioned = False
                    self._partition_start = None

                return PartitionStatus(
                    is_partitioned=False,
                    last_heartbeat_at=now.isoformat(),
                    partition_duration_seconds=0.0,
                    global_redis_url=self._get_masked_redis_url(),
                )
            raise ConnectionError("Ping returned False")

        except Exception as e:
            # Connection failure
            with self._lock:
                if self._last_global_heartbeat:
                    duration = (now - self._last_global_heartbeat).total_seconds()
                    self._is_partitioned = duration > self._partition_threshold

                    if self._is_partitioned and self._partition_start is None:
                        self._partition_start = now
                else:
                    self._is_partitioned = True
                    self._partition_start = self._partition_start or now
                    duration = float("inf")

                return PartitionStatus(
                    is_partitioned=self._is_partitioned,
                    last_heartbeat_at=(
                        self._last_global_heartbeat.isoformat()
                        if self._last_global_heartbeat
                        else None
                    ),
                    partition_duration_seconds=duration,
                    error_message=str(e),
                    global_redis_url=self._get_masked_redis_url(),
                )

    def _ping_global_redis(self) -> bool:
        """
        Global Redis ping.

        Returns:
            True if ping successful, False otherwise
        """
        tiered_redis = self._get_tiered_redis()

        if tiered_redis is None:
            # Without a TieredRedisProvider, fall back to a plain Redis ping
            try:
                from baldur.core.state_backend import get_state_backend

                backend = get_state_backend()
                # Use the StateBackend ping method if it exists
                if hasattr(backend, "ping"):
                    return backend.ping()
                # Otherwise try a simple get
                backend.get("__ping_test__")
                return True
            except Exception as e:
                logger.debug(
                    "partition_reconciliation.fallback_ping_failed",
                    error=e,
                )
                return False

        try:
            from baldur.core.tiered_redis import RedisScope

            client = tiered_redis.get_redis(RedisScope.GLOBAL)
            return client.ping()
        except Exception as e:
            logger.debug(
                "partition_reconciliation.global_redis_ping_failed",
                error=e,
            )
            return False

    def _get_masked_redis_url(self) -> str:
        """Mask the Redis URL (security)."""
        import os

        url = os.environ.get("REDIS_GLOBAL_URL") or os.environ.get("REDIS_URL", "")
        if "://" in url:
            # redis://user:password@host:port/db -> redis://***@host:port/db
            parts = url.split("@")
            if len(parts) > 1:
                return f"***@{parts[-1]}"
        return url[:20] + "..." if len(url) > 20 else url

    # =========================================================================
    # Reconciliation
    # =========================================================================

    def reconcile_after_recovery(self) -> ReconciliationResult:
        """
        Reconcile state after network recovery.

        Compares Global and Regional state and notifies the operator when
        needed. No forced synchronization is performed (Safety-First).

        Returns:
            ReconciliationResult instance
        """
        # Skip while still partitioned
        if self._is_partitioned:
            return ReconciliationResult(
                reconciled=False,
                reason="Still partitioned - cannot reconcile",
            )

        tracker = self._get_tracker()
        current_ns = self._get_current_namespace()
        actions: list[ReconciliationAction] = []

        try:
            # Read state
            global_state = tracker.get_state(namespace="global")
            regional_state = tracker.get_state(namespace=current_ns)

            global_mode = global_state.governance_mode
            regional_mode = regional_state.governance_mode

            # Detect state mismatch and build actions
            if not global_state.is_active and regional_state.is_active:
                # Global is NORMAL but Regional is still STRICT
                action = ReconciliationAction(
                    action_type="MANUAL_REVIEW",
                    message=(
                        f"Region {current_ns} is still in {regional_mode} mode "
                        f"while Global is {global_mode}. "
                        "Manual review recommended."
                    ),
                    namespace=current_ns,
                )
                actions.append(action)
                self._log_action(action)

            elif global_state.is_active and not regional_state.is_active:
                # Global is STRICT but Regional is NORMAL (missed during recovery?)
                action = ReconciliationAction(
                    action_type="NOTIFICATION",
                    message=(
                        f"Global is in {global_mode} mode but "
                        f"region {current_ns} is {regional_mode}. "
                        "State will be synchronized via normal propagation."
                    ),
                    namespace=current_ns,
                )
                actions.append(action)
                self._log_action(action)

            elif global_state.is_active and regional_state.is_active:
                # Both active - compare levels
                global_level = getattr(global_state.emergency_level, "value", 0)
                regional_level = getattr(regional_state.emergency_level, "value", 0)

                if global_level != regional_level:
                    action = ReconciliationAction(
                        action_type="NOTIFICATION",
                        message=(
                            f"Emergency level mismatch: "
                            f"Global={global_level}, Regional={regional_level} "
                            f"for namespace {current_ns}."
                        ),
                        namespace=current_ns,
                    )
                    actions.append(action)
                    self._log_action(action)

            # Clean recovery log
            if not actions:
                logger.info(
                    "partition_reconciliation.recovery_complete_no_actions",
                    current_ns=current_ns,
                    global_mode=global_mode,
                    regional_mode=regional_mode,
                )

            return ReconciliationResult(
                reconciled=True,
                actions=actions,
                global_state_mode=global_mode,
                regional_state_mode=regional_mode,
            )

        except Exception as e:
            logger.exception(
                "partition_reconciliation.reconciliation_failed",
                error=e,
            )
            return ReconciliationResult(
                reconciled=False,
                reason=f"Reconciliation error: {e}",
            )

    def _log_action(self, action: ReconciliationAction) -> None:
        """Record an action in the history."""
        with self._lock:
            self._action_history.append(action)
            # Cap the history size
            if len(self._action_history) > MAX_RECONCILIATION_ACTIONS:
                self._action_history = self._action_history[
                    -MAX_RECONCILIATION_ACTIONS:
                ]

        logger.warning(
            "partition_reconciliation.action",
            reconciliation_action_type=action.action_type,
            reconciliation_message=action.message,
        )

    # =========================================================================
    # Heartbeat Loop
    # =========================================================================

    def start_heartbeat_loop(self) -> None:
        """
        Start background heartbeat monitoring.
        """
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        if self._heartbeat_running:
            logger.warning("partition_reconciliation.heartbeat_loop_already_running")
            return

        self._heartbeat_running = True
        self._spawn_heartbeat_thread()
        assert self._heartbeat_thread is not None  # spawn always sets non-None
        self._handle = DaemonWorkerHandle(
            thread=self._heartbeat_thread,
            tick_interval_seconds=float(self._heartbeat_interval),
            restart_callback=self._spawn_heartbeat_thread,
        )
        register_daemon_worker("PartitionHeartbeat", self._handle)

        logger.info(
            "partition_reconciliation.heartbeat_loop_started",
            heartbeat_interval=self._heartbeat_interval,
            partition_threshold=self._partition_threshold,
        )

    def _spawn_heartbeat_thread(self) -> None:
        """Construct + start a fresh heartbeat thread (impl 489 D9)."""
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop_with_crash_capture,
            daemon=True,
            name="PartitionHeartbeat",
        )
        self._heartbeat_thread.start()
        if self._handle is not None:
            self._handle.thread = self._heartbeat_thread

    def _heartbeat_loop_with_crash_capture(self) -> None:
        try:
            self._heartbeat_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def stop_heartbeat_loop(self) -> None:
        """Stop heartbeat monitoring."""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        if self._handle is not None:
            self._handle.is_stopping = True
        self._heartbeat_running = False

        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            from baldur.settings.thread_management import (
                get_thread_management_settings,
            )

            timeout = get_thread_management_settings().join_timeout
            self._heartbeat_thread.join(timeout=timeout)
            unregister_daemon_worker("PartitionHeartbeat")
            if self._heartbeat_thread.is_alive():
                logger.critical(
                    "daemon_worker.stop_join_timeout",
                    worker_name="PartitionHeartbeat",
                    join_timeout_seconds=timeout,
                )
        else:
            unregister_daemon_worker("PartitionHeartbeat")

        logger.info("partition_reconciliation.heartbeat_loop_stopped")

    def _heartbeat_loop(self) -> None:
        """Heartbeat loop (background thread)."""
        was_partitioned = False

        while self._heartbeat_running:
            iter_start = time.monotonic()
            try:
                status = self.check_partition_status()

                # Detect partition state transitions
                if status.is_partitioned and not was_partitioned:
                    logger.warning(
                        "partition_reconciliation.partition_detected",
                        partition_duration_seconds=status.partition_duration_seconds,
                    )
                elif not status.is_partitioned and was_partitioned:
                    logger.info(
                        "partition_reconciliation.partition_recovered_triggering_reconciliation"
                    )
                    self.reconcile_after_recovery()

                was_partitioned = status.is_partitioned

            except Exception as e:
                logger.exception(
                    "partition_reconciliation.heartbeat_error",
                    error=e,
                )

            if self._handle is not None:
                self._handle.observe_iteration(time.monotonic() - iter_start)
                self._handle.heartbeat()

            time.sleep(self._heartbeat_interval)

    # =========================================================================
    # Query Methods
    # =========================================================================

    def get_recent_actions(self, limit: int = 10) -> list[dict[str, Any]]:
        """
        Read recent reconciliation actions.

        Args:
            limit: maximum number of entries to return

        Returns:
            Action list (newest first)
        """
        with self._lock:
            actions = self._action_history[-limit:]
            return [a.to_dict() for a in reversed(actions)]

    def is_partitioned(self) -> bool:
        """
        Whether the region is currently partitioned.

        Returns the last known state without calling check_partition_status().
        """
        return self._is_partitioned

    def get_partition_duration(self) -> float | None:
        """
        Current partition duration (seconds).

        Returns:
            Partition duration (None if not partitioned)
        """
        if not self._is_partitioned or self._partition_start is None:
            return None

        now = utc_now()
        return (now - self._partition_start).total_seconds()


# =============================================================================
# Singleton
# =============================================================================

_reconciliation_service: PartitionReconciliationService | None = None
_service_lock = threading.Lock()


def get_partition_reconciliation_service() -> PartitionReconciliationService:
    """Return the PartitionReconciliationService singleton."""
    global _reconciliation_service
    if _reconciliation_service is None:
        with _service_lock:
            if _reconciliation_service is None:
                _reconciliation_service = PartitionReconciliationService()
    return _reconciliation_service


def reset_partition_reconciliation_service() -> None:
    """
    Reset the singleton (for tests).

    Removes the singleton instance to isolate tests from each other.
    """
    global _reconciliation_service
    with _service_lock:
        if _reconciliation_service is not None:
            _reconciliation_service.stop_heartbeat_loop()
        _reconciliation_service = None
