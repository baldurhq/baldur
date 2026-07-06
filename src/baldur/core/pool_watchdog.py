"""
Connection Pool Watchdog

Automatic recovery actions for pool issues:
- Force-close leaked connections
- Expand pool temporarily
- Alert operators
- Circuit breaker for new connections
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

import structlog

from baldur.audit.helpers import log_pool_expand_audit, log_pool_leak_closed_audit
from baldur.interfaces.pool_monitor import (
    ConnectionPoolMonitor,
    PoolHealthStatus,
    PoolStats,
)
from baldur.utils.time import utc_now

logger = structlog.get_logger()


def _get_pool_recorder():
    """Lazy accessor for the PoolMetricRecorder via facade."""
    try:
        from baldur.metrics.prometheus import get_metrics

        metrics = get_metrics()
        if metrics._initialized:
            return metrics.pool_monitor
    except Exception:
        pass
    return None


class PoolRecoveryAction(str, Enum):
    """Types of recovery actions"""

    NONE = "none"
    ALERT_ONLY = "alert_only"
    CLOSE_LEAKED = "close_leaked"
    EXPAND_POOL = "expand_pool"
    CIRCUIT_BREAK = "circuit_break"


@dataclass
class PoolRecoveryResult:
    """Result of a recovery action"""

    action: PoolRecoveryAction
    success: bool
    message: str
    timestamp: datetime
    connections_closed: int = 0


class PoolRecoveryHandler(ABC):
    """Abstract handler for pool recovery actions"""

    @abstractmethod
    def close_connection(self, connection_id: str) -> bool:
        """Force close a connection"""
        pass

    @abstractmethod
    def expand_pool(self, additional_connections: int) -> bool:
        """Temporarily expand pool size"""
        pass

    @abstractmethod
    def shrink_pool(self, target_size: int) -> bool:
        """Shrink pool back to normal size"""
        pass


class PoolWatchdog:
    """
    Watches pool health and takes recovery actions.

    Usage:
        watchdog = PoolWatchdog(
            monitor=pool_monitor,
            recovery_handler=my_recovery_handler,
            alert_callback=send_alert,
        )

        # Run check (call periodically)
        result = watchdog.check_and_recover()
    """

    def __init__(
        self,
        monitor: ConnectionPoolMonitor,
        recovery_handler: PoolRecoveryHandler | None = None,
        alert_callback: Callable[[str, PoolHealthStatus], None] | None = None,
        auto_close_leaked: bool = True,
        auto_expand: bool = False,
        max_expansion: int = 10,
        shrink_guard: Callable[[], str | None] | None = None,
    ):
        """
        Args:
            shrink_guard: Optional guard for shrink suppression.
                Returns None to allow shrink, or a reason string to suppress.
                Contract: MUST be non-blocking (O(1), in-memory only, no I/O).
                Invoked on every check_and_recover() cycle when shrink is attempted.
        """
        self._monitor = monitor
        self._recovery_handler = recovery_handler
        self._alert_callback = alert_callback
        self._auto_close_leaked = auto_close_leaked
        self._auto_expand = auto_expand
        self._max_expansion = max_expansion
        self._expanded_by = 0
        self._shrink_guard = shrink_guard

    def check_and_recover(self) -> PoolRecoveryResult:
        """
        Check pool health and take recovery action if needed.
        Returns the action taken.
        """
        status, stats = self._monitor.check_health()

        if status == PoolHealthStatus.HEALTHY:
            # If we expanded before, consider shrinking
            if self._expanded_by > 0:
                return self._try_shrink(stats)
            return PoolRecoveryResult(
                action=PoolRecoveryAction.NONE,
                success=True,
                message="Pool is healthy",
                timestamp=utc_now(),
            )

        if status == PoolHealthStatus.LEAK_SUSPECTED:
            return self._handle_leak(stats)

        if status == PoolHealthStatus.EXHAUSTED:
            return self._handle_exhaustion(stats)

        if status in (PoolHealthStatus.WARNING, PoolHealthStatus.CRITICAL):
            return self._handle_high_usage(status, stats)

        return PoolRecoveryResult(
            action=PoolRecoveryAction.NONE,
            success=True,
            message=f"Status: {status.value}",
            timestamp=utc_now(),
        )

    def _handle_leak(self, stats: PoolStats) -> PoolRecoveryResult:
        """Handle suspected connection leak"""
        leak_report = self._monitor.detect_leaks()
        pool_name = stats.pool_name

        self._send_alert(
            f"Connection leak detected: {leak_report.leak_count} connections",
            PoolHealthStatus.LEAK_SUSPECTED,
        )

        if not self._auto_close_leaked or not self._recovery_handler:
            return PoolRecoveryResult(
                action=PoolRecoveryAction.ALERT_ONLY,
                success=True,
                message=f"Leak alert sent for {leak_report.leak_count} connections",
                timestamp=utc_now(),
            )

        # Force close leaked connections
        closed = 0
        for conn_info in leak_report.suspected_leaks:
            try:
                if self._recovery_handler.close_connection(conn_info.connection_id):
                    self._monitor.on_connection_released(conn_info.connection_id)
                    closed += 1
            except Exception as e:
                logger.exception(
                    "pool_watchdog.connection_close_failed",
                    conn_info=conn_info.connection_id,
                    error=e,
                )

        # Record metric for closed leaked connections
        if closed > 0:
            recorder = _get_pool_recorder()
            if recorder:
                recorder.record_close_leaked(pool_name)

            # Audit trail for leaked connections closure
            log_pool_leak_closed_audit(pool_name, closed)

        return PoolRecoveryResult(
            action=PoolRecoveryAction.CLOSE_LEAKED,
            success=closed > 0,
            message=f"Closed {closed}/{leak_report.leak_count} leaked connections",
            timestamp=utc_now(),
            connections_closed=closed,
        )

    def _handle_exhaustion(self, stats: PoolStats) -> PoolRecoveryResult:
        """Handle pool exhaustion"""
        self._send_alert(
            f"Connection pool exhausted: {stats.waiting_requests} requests waiting",
            PoolHealthStatus.EXHAUSTED,
        )

        if not self._auto_expand or not self._recovery_handler:
            return PoolRecoveryResult(
                action=PoolRecoveryAction.ALERT_ONLY,
                success=True,
                message="Pool exhausted, alert sent",
                timestamp=utc_now(),
            )

        # Try to expand pool
        if self._expanded_by >= self._max_expansion:
            recorder = _get_pool_recorder()
            if recorder:
                recorder.record_circuit_break(stats.pool_name)
            return PoolRecoveryResult(
                action=PoolRecoveryAction.CIRCUIT_BREAK,
                success=False,
                message="Max expansion reached, circuit breaking",
                timestamp=utc_now(),
            )

        # Governance check before pool expansion
        try:
            from baldur.factory.registry import ProviderRegistry

            gov = ProviderRegistry.governance.get().check_all_governance(
                check_kill_switch=True,
                check_emergency=True,
                emergency_min_level=3,
                check_error_budget=True,
                operation_name="pool_expansion",
                audit_on_block=True,
                resource_context={"pool_name": stats.pool_name},
            )
            if not gov.allowed:
                return PoolRecoveryResult(
                    action=PoolRecoveryAction.ALERT_ONLY,
                    success=False,
                    message=f"Pool expansion blocked by governance: {gov.block_reason}",
                    timestamp=utc_now(),
                )
        except Exception:
            pass  # Fail-open: governance lookup failed

        expand_by = min(5, self._max_expansion - self._expanded_by)
        success = self._recovery_handler.expand_pool(expand_by)

        if success:
            self._expanded_by += expand_by
            new_size = stats.max_connections + self._expanded_by
            recorder = _get_pool_recorder()
            if recorder:
                recorder.record_expand(stats.pool_name)

            # Audit trail for pool expansion
            log_pool_expand_audit(stats.pool_name, expand_by, new_size)

        return PoolRecoveryResult(
            action=PoolRecoveryAction.EXPAND_POOL,
            success=success,
            message=f"Expanded pool by {expand_by}" if success else "Failed to expand",
            timestamp=utc_now(),
        )

    def _handle_high_usage(
        self, status: PoolHealthStatus, stats: PoolStats
    ) -> PoolRecoveryResult:
        """Handle high usage warning/critical"""
        self._send_alert(
            f"Pool usage {status.value}: {stats.usage_percent:.1f}%", status
        )

        return PoolRecoveryResult(
            action=PoolRecoveryAction.ALERT_ONLY,
            success=True,
            message=f"Alert sent for {status.value} usage",
            timestamp=utc_now(),
        )

    def _try_shrink(self, stats: PoolStats) -> PoolRecoveryResult:
        """Try to shrink pool back to normal if healthy"""
        if self._shrink_guard:
            suppress_reason = self._shrink_guard()
            if suppress_reason:
                return PoolRecoveryResult(
                    action=PoolRecoveryAction.NONE,
                    success=True,
                    message=f"Shrink suppressed: {suppress_reason}",
                    timestamp=utc_now(),
                )
        if stats.usage_percent < 50 and self._recovery_handler:
            target = stats.max_connections - self._expanded_by
            if self._recovery_handler.shrink_pool(target):
                self._expanded_by = 0
                return PoolRecoveryResult(
                    action=PoolRecoveryAction.NONE,
                    success=True,
                    message="Pool shrunk back to normal",
                    timestamp=utc_now(),
                )

        return PoolRecoveryResult(
            action=PoolRecoveryAction.NONE,
            success=True,
            message="Pool healthy, monitoring",
            timestamp=utc_now(),
        )

    def _send_alert(self, message: str, status: PoolHealthStatus) -> None:
        """Send alert via callback"""
        if self._alert_callback:
            try:
                self._alert_callback(message, status)
            except Exception as e:
                logger.exception(
                    "pool_watchdog.alert_send_failed",
                    error=e,
                )
