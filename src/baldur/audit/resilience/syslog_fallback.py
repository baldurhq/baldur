"""
Syslog Fallback for Critical Audit Events.

When all else fails, critical security events are logged
directly to the OS syslog. This provides a "last resort"
audit trail that survives application crashes.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()


class SyslogFallback:
    """
    OS-level syslog fallback for critical audit events.

    When all else fails, critical security events are logged
    directly to the OS syslog. This provides a "last resort"
    audit trail that survives application crashes.

    On Windows, uses Windows Event Log via win32 API or falls back to stderr.
    On Linux/macOS, uses standard syslog.
    """

    # Critical event types that always go to syslog
    CRITICAL_EVENTS = frozenset(
        [
            "security_policy_change",
            "authentication_config_change",
            "encryption_key_change",
            "admin_privilege_change",
            "audit_config_change",
            "circuit_breaker_open",
            "all_backends_failed",
        ]
    )

    _instance: SyslogFallback | None = None
    _lock = threading.Lock()

    def __init__(self):
        self._syslog_available = False
        self._stderr_fallback = True
        self._init_syslog()

    def _init_syslog(self) -> None:
        """Initialize syslog connection."""
        try:
            if sys.platform == "win32":
                # Windows doesn't have syslog, use stderr
                self._syslog_available = False
                logger.debug("syslog_fallback.windows_detected_using_stderr")
            else:
                import syslog

                syslog.openlog(
                    ident="baldur-audit",
                    logoption=syslog.LOG_PID | syslog.LOG_CONS,
                    facility=syslog.LOG_AUTH,  # Security/auth facility
                )
                self._syslog_available = True
                logger.debug("syslog_fallback.syslog_initialized")
        except Exception as e:
            logger.warning(
                "syslog_fallback.init_syslog_failed",
                error=e,
            )
            self._syslog_available = False

    @classmethod
    def get_instance(cls) -> SyslogFallback:
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

    def is_critical_event(self, event_type: str) -> bool:
        """Check if event type is critical."""
        return event_type in self.CRITICAL_EVENTS

    def log_critical(
        self,
        event_type: str,
        message: str,
        config_type: str | None = None,
        user: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> bool:
        """
        Log a critical event to syslog.

        Args:
            event_type: Type of event (should be in CRITICAL_EVENTS)
            message: Human-readable message
            config_type: Configuration type being changed
            user: User making the change
            details: Additional details

        Returns:
            True if logged successfully
        """
        timestamp = utc_now().isoformat()
        user_str = user or "system"
        config_str = config_type or "unknown"

        # Format: [AUDIT] type=X config=Y user=Z message
        log_line = (
            f"[AUDIT] timestamp={timestamp} "
            f"type={event_type} config={config_str} "
            f"user={user_str} msg={message}"
        )

        if details:
            # Add key details (limit size)
            for key, value in list(details.items())[:5]:
                log_line += f" {key}={value}"

        return self._write_to_syslog(log_line, event_type)

    def _write_to_syslog(self, message: str, event_type: str) -> bool:
        """Write message to syslog or fallback."""
        success = False

        # Try syslog first
        if self._syslog_available:
            try:
                import syslog

                # Use appropriate priority. The syslog module is POSIX-only;
                # Windows mypy lacks LOG_*/syslog stubs, so silence per-line.
                if event_type in ["security_policy_change", "all_backends_failed"]:
                    priority = syslog.LOG_CRIT  # type: ignore[attr-defined]
                elif event_type in [
                    "authentication_config_change",
                    "encryption_key_change",
                ]:
                    priority = syslog.LOG_WARNING  # type: ignore[attr-defined]
                else:
                    priority = syslog.LOG_NOTICE  # type: ignore[attr-defined]

                syslog.syslog(priority, message)  # type: ignore[attr-defined]
                success = True
            except Exception as e:
                logger.exception(
                    "syslog_fallback.syslog_write_failed",
                    error=e,
                )

        # Always also write to stderr for visibility (except during tests)
        if self._stderr_fallback and not os.environ.get("BALDUR_TEST_MODE"):
            try:
                print(f"AUDIT_CRITICAL: {message}", file=sys.stderr, flush=True)
                success = True
            except Exception:
                pass

        return success

    def log_backend_failure(self, backend_name: str, error: str) -> None:
        """Log backend failure as critical event."""
        self.log_critical(
            event_type="all_backends_failed",
            message=f"Audit backend {backend_name} failed",
            details={"backend": backend_name, "error": error[:100]},
        )

    def log_circuit_open(self, backend_name: str) -> None:
        """Log circuit breaker opening."""
        self.log_critical(
            event_type="circuit_breaker_open",
            message=f"Circuit breaker opened for {backend_name}",
            details={"backend": backend_name},
        )


def get_syslog_fallback() -> SyslogFallback:
    """Get the syslog fallback instance."""
    return SyslogFallback.get_instance()


def log_critical_to_syslog(
    event_type: str,
    message: str,
    **kwargs,
) -> bool:
    """Log a critical event to syslog."""
    return SyslogFallback.get_instance().log_critical(
        event_type=event_type,
        message=message,
        **kwargs,
    )


__all__ = ["SyslogFallback", "get_syslog_fallback", "log_critical_to_syslog"]
