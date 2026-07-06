"""
Actionable Alert URL Builder.

Generates actionable links to include in Circuit Breaker alerts.

Features:
- Dashboard URL: Grafana Circuit Breaker dashboard
- Admin URL: Admin control panel (passes context via query parameters)
- Runbook URL: Incident-response manual

Environment Variables:
- CB_DASHBOARD_URL: Grafana dashboard base URL (e.g., https://grafana.internal/d/circuit-breaker)
- CB_ADMIN_BASE_URL: Admin control-panel base URL (e.g., https://admin.internal/admin/baldur/circuitbreaker/)
- CB_RUNBOOK_URL: Runbook base URL (e.g., https://docs.internal/runbooks/circuit-breaker-recovery)

Prefer absolute http(s) URLs — Slack renders them as one-click link buttons.
A relative value (e.g., /admin/baldur/circuitbreaker/) is absolutized against
the explicitly configured site_url setting; when site_url is unset it falls
back to a plain text field in the alert instead of a button.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from urllib.parse import urlencode

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.utils.url import absolutize_against_site_url

logger = structlog.get_logger()


@dataclass
class ActionableUrls(SerializableMixin):
    """
    Collection of URLs to include in an Actionable Alert.

    Attributes:
        dashboard_url: Grafana dashboard URL (read-only)
        admin_url: Admin control-panel URL (passes context via query parameters)
        runbook_url: Incident-response manual URL
    """

    dashboard_url: str | None = None
    admin_url: str | None = None
    runbook_url: str | None = None

    def has_any_url(self) -> bool:
        """Check whether at least one URL is set."""
        return any([self.dashboard_url, self.admin_url, self.runbook_url])


class ActionableAlertUrlBuilder:
    """
    Actionable Alert URL builder.

    Reads base URLs from environment variables and generates per-service /
    per-situation URLs.

    Design principles:
    - Preserve governance: every operation is audit-logged through Admin
    - Preserve context: query parameters allow immediate lookup of the service
    - Safety: operators can decide after checking the state

    Usage:
        builder = get_actionable_alert_url_builder()
        urls = builder.build_cb_open_urls(
            service_name="payment_service",
            trigger_time="2026-01-06T10:00:00Z",
        )
    """

    def __init__(self):
        """Load base URLs from environment variables."""
        self._dashboard_base_url = os.getenv("CB_DASHBOARD_URL", "")
        self._admin_base_url = os.getenv("CB_ADMIN_BASE_URL", "")
        self._runbook_base_url = os.getenv("CB_RUNBOOK_URL", "")

        logger.debug(
            "actionable_alert_url_builder.initialized",
            dashboard_configured=bool(self._dashboard_base_url),
            admin_configured=bool(self._admin_base_url),
            runbook_configured=bool(self._runbook_base_url),
        )

    def build_cb_open_urls(
        self,
        service_name: str,
        trigger_time: str | None = None,
    ) -> ActionableUrls:
        """
        Generate Actionable URLs for a CB OPEN event.

        Args:
            service_name: Name of the service whose Circuit Breaker opened
            trigger_time: Event occurrence time (ISO 8601 format)

        Returns:
            ActionableUrls: Collection of dashboard, Admin, and Runbook URLs
        """
        return ActionableUrls(
            dashboard_url=self._build_dashboard_url(service_name),
            admin_url=self._build_admin_url(
                service_name=service_name,
                action="review",
                trigger_time=trigger_time,
            ),
            runbook_url=self._build_runbook_url(),
        )

    def build_cb_closed_urls(
        self,
        service_name: str,
        recovery_time: str | None = None,
    ) -> ActionableUrls:
        """
        Generate Actionable URLs for a CB CLOSED (recovery) event.

        Args:
            service_name: Name of the service whose Circuit Breaker closed
            recovery_time: Recovery-completion time (ISO 8601 format)

        Returns:
            ActionableUrls: Collection of dashboard and Admin URLs (Runbook not needed)
        """
        return ActionableUrls(
            dashboard_url=self._build_dashboard_url(service_name),
            admin_url=self._build_admin_url(
                service_name=service_name,
                action="history",
                trigger_time=recovery_time,
            ),
            runbook_url=None,  # Runbook not needed on recovery
        )

    def build_governance_blocked_urls(
        self,
        service_name: str,
        reason: str,
    ) -> ActionableUrls:
        """
        Generate Actionable URLs for a Governance Blocked event.

        Used when a CB operation is blocked by the Blast Radius policy.

        Args:
            service_name: Name of the blocked service
            reason: Block reason

        Returns:
            ActionableUrls: Collection of dashboard, Admin, and Runbook URLs
        """
        return ActionableUrls(
            dashboard_url=self._build_dashboard_url(service_name),
            admin_url=self._build_admin_url(
                service_name=service_name,
                action="governance_review",
            ),
            runbook_url=self._build_runbook_url("governance"),
        )

    def _build_dashboard_url(self, service_name: str) -> str | None:
        """
        Generate the Grafana dashboard URL.

        If the CB_DASHBOARD_URL environment variable is set, append the service parameter.

        Examples:
            - Base: https://grafana.internal/d/circuit-breaker
            - Result: https://grafana.internal/d/circuit-breaker?service=payment_service
        """
        if not self._dashboard_base_url:
            return None

        # Check whether the URL already contains ?
        separator = "&" if "?" in self._dashboard_base_url else "?"
        return absolutize_against_site_url(
            f"{self._dashboard_base_url}{separator}service={service_name}"
        )

    def _build_admin_url(
        self,
        service_name: str,
        action: str = "review",
        trigger_time: str | None = None,
    ) -> str | None:
        """
        Generate the Admin control-panel URL.

        Passes context via query parameters so the service can be looked up immediately.

        Design principles:
        - Navigate to the Admin control panel instead of a one-click release
        - Preserve governance (every operation is audit-logged)

        Examples:
            - Base: /admin/baldur/circuitbreaker/
            - Result: /admin/baldur/circuitbreaker/?service_id=payment_service&action=review
        """
        if not self._admin_base_url:
            return None

        params = {
            "service_id": service_name,
            "action": action,
        }

        if trigger_time:
            params["trigger_time"] = trigger_time

        # Check whether the URL already contains ?
        base_url = self._admin_base_url.rstrip("/") + "/"
        return absolutize_against_site_url(f"{base_url}?{urlencode(params)}")

    def _build_runbook_url(self, context: str | None = None) -> str | None:
        """
        Generate the Runbook URL.

        Args:
            context: Additional context (e.g., 'governance' → jump to the governance section)

        Examples:
            - Base: https://docs.internal/runbooks/circuit-breaker-recovery
            - Result: https://docs.internal/runbooks/circuit-breaker-recovery#governance
        """
        if not self._runbook_base_url:
            return None

        if context:
            return absolutize_against_site_url(f"{self._runbook_base_url}#{context}")

        return absolutize_against_site_url(self._runbook_base_url)

    def is_configured(self) -> bool:
        """Check whether at least one URL is set via environment variables."""
        return any(
            [
                self._dashboard_base_url,
                self._admin_base_url,
                self._runbook_base_url,
            ]
        )

    def get_config_status(self) -> dict:
        """Return the current URL configuration status (for debugging)."""
        return {
            "dashboard_configured": bool(self._dashboard_base_url),
            "admin_configured": bool(self._admin_base_url),
            "runbook_configured": bool(self._runbook_base_url),
        }


# =============================================================================
# Singleton Pattern
# =============================================================================

_instance: ActionableAlertUrlBuilder | None = None
_instance_lock = threading.Lock()


def get_actionable_alert_url_builder() -> ActionableAlertUrlBuilder:
    """
    Return the ActionableAlertUrlBuilder singleton instance.

    Returns:
        ActionableAlertUrlBuilder: URL builder instance
    """
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = ActionableAlertUrlBuilder()
    return _instance


def reset_actionable_alert_url_builder() -> None:
    """Reset the singleton instance (for tests)."""
    global _instance
    _instance = None
