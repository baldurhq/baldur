"""
Certificate Expiry Monitor

Proactive monitoring of certificate expiration.
Can be run as scheduled task to alert before expiry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()


class CertificateStatus(str, Enum):
    """Certificate validity status"""

    VALID = "valid"
    EXPIRING_SOON = "expiring_soon"  # < 30 days
    CRITICAL = "critical"  # < 7 days
    EXPIRED = "expired"


@dataclass
class CertificateInfo:
    """Certificate information"""

    endpoint: str
    subject: str
    issuer: str
    not_before: datetime
    not_after: datetime
    status: CertificateStatus
    days_remaining: int
    checked_at: datetime

    @property
    def is_valid(self) -> bool:
        """True if certificate is valid (not expired)."""
        return self.status != CertificateStatus.EXPIRED

    @property
    def needs_attention(self) -> bool:
        """True if certificate needs attention (expiring soon or critical)."""
        return self.status in {
            CertificateStatus.EXPIRING_SOON,
            CertificateStatus.CRITICAL,
        }

    @property
    def is_urgent(self) -> bool:
        """True if certificate status is urgent (critical or expired)."""
        return self.status in {CertificateStatus.CRITICAL, CertificateStatus.EXPIRED}


class CertificateExpiryMonitor:
    """Monitor certificate expiration for endpoints"""

    def __init__(
        self,
        warning_days: int = 30,
        critical_days: int = 7,
        alert_callback: Callable[[CertificateInfo], None] | None = None,
    ):
        """
        Initialize certificate expiry monitor.

        Args:
            warning_days: Days before expiry to start warning (default 30)
            critical_days: Days before expiry for critical status (default 7)
            alert_callback: Optional callback for certificate alerts
        """
        self._warning_days = warning_days
        self._critical_days = critical_days
        self._alert_callback = alert_callback
        self._monitored_endpoints: dict[str, CertificateInfo] = {}

    @property
    def warning_days(self) -> int:
        """Get warning threshold in days."""
        return self._warning_days

    @property
    def critical_days(self) -> int:
        """Get critical threshold in days."""
        return self._critical_days

    def check_expiry(
        self,
        not_after: datetime,
        endpoint: str = "",
        subject: str = "",
        issuer: str = "",
        not_before: datetime | None = None,
    ) -> CertificateInfo:
        """
        Check certificate expiry status.

        Args:
            not_after: Certificate expiration datetime
            endpoint: Endpoint URL
            subject: Certificate subject
            issuer: Certificate issuer
            not_before: Certificate valid-from datetime

        Returns:
            CertificateInfo with status
        """
        now = utc_now()

        # Ensure timezone aware
        if not_after.tzinfo is None:
            not_after = not_after.replace(tzinfo=UTC)

        if not_before and not_before.tzinfo is None:
            not_before = not_before.replace(tzinfo=UTC)

        days_remaining = (not_after - now).days

        if days_remaining < 0:
            status = CertificateStatus.EXPIRED
        elif days_remaining <= self._critical_days:
            status = CertificateStatus.CRITICAL
        elif days_remaining <= self._warning_days:
            status = CertificateStatus.EXPIRING_SOON
        else:
            status = CertificateStatus.VALID

        cert_info = CertificateInfo(
            endpoint=endpoint,
            subject=subject,
            issuer=issuer,
            not_before=not_before or now,
            not_after=not_after,
            status=status,
            days_remaining=max(0, days_remaining),
            checked_at=now,
        )

        # Store for monitoring
        if endpoint:
            self._monitored_endpoints[endpoint] = cert_info

        # Alert if needed
        if cert_info.needs_attention and self._alert_callback:
            try:
                self._alert_callback(cert_info)
            except Exception as e:
                logger.exception(
                    "error.certificate_alert_callback",
                    error=e,
                )

        return cert_info

    def get_status_message(self, cert_info: CertificateInfo) -> str:
        """
        Get human-readable status message.

        Args:
            cert_info: Certificate information

        Returns:
            Status message string
        """
        endpoint_display = cert_info.endpoint or "Unknown endpoint"

        if cert_info.status == CertificateStatus.EXPIRED:
            return f"CRITICAL: Certificate for {endpoint_display} has EXPIRED!"
        if cert_info.status == CertificateStatus.CRITICAL:
            return f"CRITICAL: Certificate for {endpoint_display} expires in {cert_info.days_remaining} days!"
        if cert_info.status == CertificateStatus.EXPIRING_SOON:
            return f"WARNING: Certificate for {endpoint_display} expires in {cert_info.days_remaining} days"
        return f"OK: Certificate for {endpoint_display} valid for {cert_info.days_remaining} days"

    def get_all_monitored(self) -> dict[str, CertificateInfo]:
        """Get all monitored endpoints and their certificate info."""
        return dict(self._monitored_endpoints)

    def get_expiring_certificates(
        self,
        within_days: int | None = None,
    ) -> list[CertificateInfo]:
        """
        Get certificates expiring within specified days.

        Args:
            within_days: Days threshold (default: warning_days)

        Returns:
            List of expiring certificates
        """
        threshold = within_days if within_days is not None else self._warning_days

        return [
            info
            for info in self._monitored_endpoints.values()
            if info.days_remaining <= threshold
        ]

    def clear_monitoring(self) -> None:
        """Clear all monitored endpoints."""
        self._monitored_endpoints.clear()

    def remove_endpoint(self, endpoint: str) -> bool:
        """
        Remove an endpoint from monitoring.

        Args:
            endpoint: Endpoint URL to remove

        Returns:
            True if endpoint was being monitored
        """
        if endpoint in self._monitored_endpoints:
            del self._monitored_endpoints[endpoint]
            return True
        return False


class CertificateAlertManager:
    """Manages certificate expiry alerts with deduplication."""

    def __init__(
        self,
        alert_interval_hours: int = 24,
    ):
        """
        Initialize alert manager.

        Args:
            alert_interval_hours: Minimum hours between repeated alerts for same endpoint
        """
        self._alert_interval = timedelta(hours=alert_interval_hours)
        self._last_alerts: dict[str, datetime] = {}

    def should_alert(self, endpoint: str) -> bool:
        """
        Check if we should send an alert for this endpoint.

        Args:
            endpoint: Endpoint URL

        Returns:
            True if enough time has passed since last alert
        """
        now = utc_now()

        if endpoint not in self._last_alerts:
            return True

        time_since_last = now - self._last_alerts[endpoint]
        return time_since_last >= self._alert_interval

    def record_alert(self, endpoint: str) -> None:
        """Record that an alert was sent for this endpoint."""
        self._last_alerts[endpoint] = utc_now()

    def clear_alerts(self) -> None:
        """Clear alert history."""
        self._last_alerts.clear()
