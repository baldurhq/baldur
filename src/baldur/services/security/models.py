"""
Security Violation Data Models.

Contains data classes for security violation handling results
and configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from baldur.services.security.policies import ActionPolicy


@dataclass
class ProtectionResult:
    """
    Result of executing a protective action (priority 0 - v2.0.0).

    v2.1.0: added rollback-related fields (priority 0.3)
    v2.2.0: added triggering_trace_id - lets the dashboard trace, in one click,
    "which request caused this user's session to be invalidated."
    """

    success: bool
    executed_policies: list[ActionPolicy] = field(default_factory=list)
    failed_policies: list[ActionPolicy] = field(default_factory=list)
    highest_priority_succeeded: bool = True
    # v2.1.0: rollback-related (priority 0.3)
    rolled_back_policies: list[ActionPolicy] = field(default_factory=list)
    rollback_success: bool = True
    error_message: str = ""
    # v2.2.0: Tracing Deep Link
    triggering_trace_id: str | None = None
    """trace_id of the original request that triggered the protective action (for Jaeger/Zipkin integration)."""
    triggering_request_path: str | None = None
    """Path of the original request that triggered the protective action (e.g., POST /api/payments/)."""

    def get_trace_url(self, template: str = "") -> str | None:
        """
        Generate a Trace UI Deep Link.

        Args:
            template: URL template (e.g., "https://jaeger.example.com/trace/{trace_id}")

        Returns:
            Deep Link URL or None
        """
        if not self.triggering_trace_id:
            return None
        if not template:
            template = os.environ.get("BALDUR_TRACE_URL_TEMPLATE", "")
        if not template:
            return None
        return template.replace("{trace_id}", self.triggering_trace_id)


@dataclass
class SecurityViolationResult:
    """Result of security violation handling."""

    success: bool
    incident_id: int | None = None
    action_taken: str = ""
    error: str | None = None
    protection_result: ProtectionResult | None = None

    @classmethod
    def handled(
        cls,
        incident_id: int,
        action: str,
        protection_result: ProtectionResult | None = None,
    ) -> SecurityViolationResult:
        """Factory for successfully handled violation."""
        return cls(
            success=True,
            incident_id=incident_id,
            action_taken=action,
            protection_result=protection_result,
        )

    @classmethod
    def failed(cls, error: str) -> SecurityViolationResult:
        """Factory for failed handling."""
        return cls(success=False, error=error)


@dataclass
class SecurityConfig:
    """Configuration for security violation handling."""

    # IP ban settings
    temporary_ban_hours: int = 1
    permanent_ban_threshold: int = 5  # violations before permanent ban

    # Suspicious IP tracking cache timeout (seconds)
    suspicious_ip_cache_timeout: int = 86400  # 24 hours

    # Injection attempt ban duration (hours)
    injection_ban_hours: int = 24

    # Suspicious IP tracking cache key prefixes
    suspicious_ip_cache_prefix: str = "security:suspicious_ip:"
    banned_ip_cache_prefix: str = "security:banned_ip:"

    # Session settings (synced from SecuritySettings)
    session_engine: str = "django.contrib.sessions.backends.db"
    session_cookie_age: int = 1209600  # 14 days

    @classmethod
    def from_settings(cls) -> SecurityConfig:
        """Load configuration from settings.

        Layered read so a console edit of the security domain (ban thresholds,
        cache timeout, session cookie age) takes effect; env base when no
        RuntimeConfigManager is registered.
        """
        from baldur.settings.layered_provider import get_layered_settings
        from baldur.settings.security import SecuritySettings

        security = get_layered_settings(SecuritySettings, "security")
        return cls(
            temporary_ban_hours=security.temporary_ban_hours,
            permanent_ban_threshold=security.permanent_ban_threshold,
            suspicious_ip_cache_timeout=security.suspicious_ip_cache_timeout,
            injection_ban_hours=security.injection_ban_hours,
            suspicious_ip_cache_prefix=security.suspicious_ip_cache_prefix,
            banned_ip_cache_prefix=security.banned_ip_cache_prefix,
            session_engine=security.session_engine,
            session_cookie_age=security.session_cookie_age,
        )
