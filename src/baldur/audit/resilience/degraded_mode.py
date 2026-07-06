"""
Degraded Mode Manager for Audit Logging.

Manages degraded mode operation when primary backends fail.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

import structlog

from baldur.utils.time import utc_now

from .circuit_breaker import CircuitBreakerRegistry
from .metrics import AuditMetrics
from .syslog_fallback import SyslogFallback

logger = structlog.get_logger()


class DegradedModeManager:
    """
    Manages degraded mode operation for audit logging.

    When primary backends fail (circuit breakers open), the system
    automatically switches to degraded mode using only local fallbacks.

    Degraded Mode:
    - File adapter only (via ProviderRegistry.get_audit_adapter())
    - stderr output
    - Syslog for critical events
    - Metrics still collected for monitoring
    """

    _instance: DegradedModeManager | None = None
    _lock = threading.Lock()

    def __init__(self):
        self._degraded = False
        self._degraded_since: datetime | None = None
        self._degraded_reason: str | None = None
        self._auto_recovery_enabled = True
        self._check_interval_seconds = 60
        self._manager_lock = threading.RLock()

        self._metrics = AuditMetrics.get_instance()
        self._syslog = SyslogFallback.get_instance()
        self._registry = CircuitBreakerRegistry.get_instance()

    @classmethod
    def get_instance(cls) -> DegradedModeManager:
        """Get singleton instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance for test isolation."""
        cls._instance = None

    @property
    def is_degraded(self) -> bool:
        """Check if in degraded mode."""
        with self._manager_lock:
            return self._degraded

    def enter_degraded_mode(self, reason: str) -> None:
        """Enter degraded mode."""
        with self._manager_lock:
            if not self._degraded:
                self._degraded = True
                self._degraded_since = utc_now()
                self._degraded_reason = reason

                self._metrics.set_degraded_mode(True)

                logger.warning(
                    "degraded_mode.entered_degraded_mode",
                    reason=reason,
                )

                # Log to syslog
                self._syslog.log_critical(
                    event_type="audit_config_change",
                    message=f"Audit entered degraded mode: {reason}",
                )

                # Broadcast state change
                from .degradation_protocol import DegradationBroadcaster

                DegradationBroadcaster.notify(
                    "external_backends",
                    True,
                    None,
                    reason,
                )

    def exit_degraded_mode(self) -> None:
        """Exit degraded mode."""
        with self._manager_lock:
            if self._degraded:
                duration = None
                if self._degraded_since:
                    duration = (utc_now() - self._degraded_since).total_seconds()

                self._degraded = False
                self._degraded_since = None
                self._degraded_reason = None

                self._metrics.set_degraded_mode(False)

                logger.info(
                    "degraded_mode.exited_degraded_mode_after",
                    duration=duration,
                )

                # Broadcast state change
                from .degradation_protocol import DegradationBroadcaster

                DegradationBroadcaster.notify(
                    "external_backends",
                    False,
                    None,
                    "recovered",
                )

    def check_and_update(self) -> None:
        """
        Check circuit breakers and update degraded mode status.

        Call this periodically or after backend operations.
        """
        open_circuits = self._registry.get_open_circuits()

        with self._manager_lock:
            # Count how many external backends have open circuits
            external_backends = {"CloudWatch", "Datadog", "S3WORM", "RemoteAudit"}
            open_external = [c for c in open_circuits if c in external_backends]

            # Enter degraded if any external backend circuit is open
            if open_external and not self._degraded:
                self.enter_degraded_mode(
                    f"Circuit breakers open: {', '.join(open_external)}"
                )
            # Exit degraded if all circuits closed
            elif not open_external and self._degraded and self._auto_recovery_enabled:
                self.exit_degraded_mode()

    def get_status(self) -> dict[str, Any]:
        """Get degraded mode status."""
        with self._manager_lock:
            duration = None
            if self._degraded and self._degraded_since:
                duration = (utc_now() - self._degraded_since).total_seconds()

            return {
                "degraded": self._degraded,
                "since": (
                    self._degraded_since.isoformat() if self._degraded_since else None
                ),
                "duration_seconds": duration,
                "reason": self._degraded_reason,
                "auto_recovery_enabled": self._auto_recovery_enabled,
                "open_circuits": self._registry.get_open_circuits(),
            }

    def set_auto_recovery(self, enabled: bool) -> None:
        """Enable or disable automatic recovery from degraded mode."""
        with self._manager_lock:
            self._auto_recovery_enabled = enabled

    def force_degraded(self, reason: str = "Manual override") -> None:
        """Manually force degraded mode."""
        self.enter_degraded_mode(reason)
        self._auto_recovery_enabled = False

    def force_normal(self) -> None:
        """Manually force normal mode."""
        self._auto_recovery_enabled = True
        self.exit_degraded_mode()


def get_degraded_mode_manager() -> DegradedModeManager:
    """Get the degraded mode manager instance."""
    return DegradedModeManager.get_instance()


__all__ = ["DegradedModeManager", "get_degraded_mode_manager"]
