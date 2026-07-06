"""
Alert Adapter Interface

Provides an abstraction for alerting, allowing users to choose
how and where alerts are sent without being tied to any specific
alerting system.

Design Philosophy:
- No forced dependencies (no PagerDuty API, no Slack webhook, etc.)
- User chooses: stdout, file, email, Slack, PagerDuty, or custom
- Default is non-invasive (file or stdout)

Usage:
    # Use default stdout adapter
    from baldur.adapters.alert import StdoutAlertAdapter
    adapter = StdoutAlertAdapter()

    # Or implement your own
    class MySlackAdapter(AlertAdapter):
        def send(self, alert: Alert) -> None:
            slack_webhook.post(alert.to_dict())
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.interfaces.messaging_common import MessageSeverity
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# Backward-compatible alias: AlertSeverity is unified into MessageSeverity.
# Existing AlertSeverity.CRITICAL / .WARNING / .INFO are all included in MessageSeverity.
AlertSeverity = MessageSeverity


class AlertCategory(str, Enum):
    """Alert categories for routing."""

    AVAILABILITY = "availability"  # Service down
    LATENCY = "latency"  # Response time issues
    ERROR_RATE = "error_rate"  # High error rate
    CIRCUIT_BREAKER = "circuit_breaker"  # CB state changes
    DLQ = "dlq"  # DLQ issues
    RESOURCE = "resource"  # CPU, Memory, Disk
    SECURITY = "security"  # Security incidents
    SLO_VIOLATION = "slo_violation"  # SLO breached
    FAILSAFE = "failsafe"  # Fail-safe mode activated (baldur degraded)


@dataclass
class Alert(SerializableMixin):
    """
    Alert containing all relevant context.

    Captures:
    - What happened (title, description)
    - How severe (severity)
    - What category (category)
    - Where (source, service_name)
    - Additional context (details)
    """

    title: str
    description: str
    severity: AlertSeverity = AlertSeverity.WARNING
    category: AlertCategory = AlertCategory.AVAILABILITY

    timestamp: datetime = field(default_factory=lambda: utc_now())

    # Source information
    source: str = "baldur"  # Component that generated alert
    service_name: str | None = None
    domain: str | None = None

    # SLO context (if SLO violation)
    slo_name: str | None = None
    slo_target: float | None = None
    slo_current: float | None = None

    # Additional context
    details: dict[str, Any] = field(default_factory=dict)
    runbook_url: str | None = None

    # Deduplication
    alert_key: str | None = None  # For grouping/deduping alerts

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), default=str)

    @property
    def key(self) -> str:
        """Generate alert key for deduplication."""
        if self.alert_key:
            return self.alert_key
        return f"{self.category.value}:{self.service_name or 'unknown'}:{self.title}"


class AlertAdapter(ABC):
    """
    Abstract interface for alerting.

    Implementations can send alerts to:
    - stdout (StdoutAlertAdapter)
    - Files (FileAlertAdapter)
    - Slack/Teams (user implements)
    - PagerDuty/OpsGenie (user implements)
    - Email (user implements)
    - Nowhere (NullAlertAdapter)
    """

    @abstractmethod
    def send(self, alert: Alert) -> None:
        """
        Send an alert.

        Args:
            alert: The alert to send
        """
        pass

    @abstractmethod
    def resolve(self, alert_key: str) -> None:
        """
        Resolve/close an alert.

        Args:
            alert_key: The key of the alert to resolve
        """
        pass

    def alert_cb_opened(
        self,
        service_name: str,
        failure_count: int,
        threshold: int,
        is_manual: bool = False,
    ) -> None:
        """Convenience method for Circuit Breaker open alert."""
        self.send(
            Alert(
                title=f"Circuit Breaker Opened: {service_name}",
                description=(
                    f"Circuit breaker for {service_name} has been {'manually' if is_manual else 'automatically'} opened. "
                    f"Failure count: {failure_count}/{threshold}"
                ),
                severity=AlertSeverity.WARNING,
                category=AlertCategory.CIRCUIT_BREAKER,
                service_name=service_name,
                details={
                    "failure_count": failure_count,
                    "threshold": threshold,
                    "is_manual": is_manual,
                },
                alert_key=f"cb:open:{service_name}",
            )
        )

    def alert_cb_closed(
        self,
        service_name: str,
        is_manual: bool = False,
    ) -> None:
        """Convenience method for Circuit Breaker close (resolves alert)."""
        self.resolve(f"cb:open:{service_name}")

    def alert_dlq_threshold(
        self,
        domain: str,
        pending_count: int,
        threshold: int,
    ) -> None:
        """Convenience method for DLQ threshold alert."""
        self.send(
            Alert(
                title=f"DLQ Threshold Exceeded: {domain}",
                description=(
                    f"DLQ for domain '{domain}' has {pending_count} pending items, "
                    f"exceeding threshold of {threshold}. Human review required."
                ),
                severity=AlertSeverity.WARNING,
                category=AlertCategory.DLQ,
                domain=domain,
                details={
                    "pending_count": pending_count,
                    "threshold": threshold,
                },
                alert_key=f"dlq:threshold:{domain}",
            )
        )

    def alert_slo_violation(
        self,
        slo_name: str,
        target: float,
        current: float,
        service_name: str | None = None,
    ) -> None:
        """Convenience method for SLO violation alert."""
        self.send(
            Alert(
                title=f"SLO Violation: {slo_name}",
                description=(
                    f"SLO '{slo_name}' is violated. Target: {target:.2%}, Current: {current:.2%}"
                ),
                severity=AlertSeverity.CRITICAL,
                category=AlertCategory.SLO_VIOLATION,
                service_name=service_name,
                slo_name=slo_name,
                slo_target=target,
                slo_current=current,
                alert_key=f"slo:violation:{slo_name}",
            )
        )

    def alert_high_error_rate(
        self,
        service_name: str,
        error_rate: float,
        threshold: float,
    ) -> None:
        """Convenience method for high error rate alert."""
        self.send(
            Alert(
                title=f"High Error Rate: {service_name}",
                description=(
                    f"Error rate for {service_name} is {error_rate:.1%}, "
                    f"exceeding threshold of {threshold:.1%}"
                ),
                severity=AlertSeverity.WARNING,
                category=AlertCategory.ERROR_RATE,
                service_name=service_name,
                details={
                    "error_rate": error_rate,
                    "threshold": threshold,
                },
                alert_key=f"error_rate:{service_name}",
            )
        )

    def alert_failsafe_activated(
        self,
        component: str,
        error_message: str,
        fallback_action: str = "PROCEED",
    ) -> None:
        """
        CRITICAL: Fail-Safe mode activation alert.

        Sent when part of the Baldur system fails and transitions into
        Fail-Safe mode. This alert requires immediate attention.

        Args:
            component: Component that failed (e.g., "error_budget", "circuit_breaker")
            error_message: Failure cause message
            fallback_action: Fallback action taken (e.g., "PROCEED", "ALLOW")

        Note:
            This alert is designed to prevent "silent failures".
            When Fail-Safe activates, the system continues operating, but the
            operations team must be notified immediately to address the root cause.
        """
        self.send(
            Alert(
                title=f"FAIL-SAFE ACTIVATED: {component}",
                description=(
                    f"Baldur '{component}' subsystem has transitioned to Fail-Safe mode due to a failure.\n\n"
                    f"Error: {error_message}\n"
                    f"Fallback action: {fallback_action}\n\n"
                    f"Deployment was permitted, but the system requires recovery."
                ),
                severity=AlertSeverity.CRITICAL,  # Always CRITICAL
                category=AlertCategory.FAILSAFE,
                source="baldur",
                details={
                    "component": component,
                    "error_message": error_message,
                    "fallback_action": fallback_action,
                    "failsafe_applied": True,
                    "requires_immediate_attention": True,
                },
                runbook_url="https://docs.internal/runbooks/baldur-failsafe",
                alert_key=f"failsafe:{component}",
            )
        )

    def resolve_failsafe(self, component: str) -> None:
        """Resolve the alert when Fail-Safe recovers."""
        self.resolve(f"failsafe:{component}")

    def alert_failsafe_recovered(
        self,
        component: str,
        downtime_seconds: float,
        recovery_reason: str = "System recovered automatically",
    ) -> None:
        """
        Recovery notification: sent when normal operation is restored from Fail-Safe mode.

        Similar to PagerDuty/OpsGenie "resolved" events,
        actively notifies that the failure has been cleared.

        Args:
            component: Component that recovered (e.g., "error_budget", "circuit_breaker")
            downtime_seconds: Failure duration (seconds)
            recovery_reason: Description of the recovery cause

        Note:
            This alert prevents "silent recoveries".
            When a failure is cleared it is explicitly announced, so the
            operations team does not need to keep tracking the failure state.
        """
        # Downtime formatting
        if downtime_seconds < 60:
            downtime_str = f"{downtime_seconds:.0f} sec"
        elif downtime_seconds < 3600:
            downtime_str = f"{downtime_seconds / 60:.1f} min"
        else:
            downtime_str = f"{downtime_seconds / 3600:.1f} hr"

        self.send(
            Alert(
                title=f"RECOVERED: {component}",
                description=(
                    f"Baldur '{component}' subsystem has recovered to normal operation.\n\n"
                    f"Recovery reason: {recovery_reason}\n"
                    f"Downtime: {downtime_str}\n\n"
                    f"The system is back to normal operational state."
                ),
                severity=AlertSeverity.INFO,  # Recovery is INFO level
                category=AlertCategory.FAILSAFE,
                source="baldur",
                details={
                    "component": component,
                    "downtime_seconds": downtime_seconds,
                    "recovery_reason": recovery_reason,
                    "recovered": True,
                },
                alert_key=f"failsafe:recovered:{component}",
            )
        )
        # Also resolve the existing failure alert
        self.resolve(f"failsafe:{component}")

    def alert_override_escalation(
        self,
        override_type: str,
        requester: str,
        reason: str,
        service_name: str | None = None,
        escalation_channel: str = "#governance",
        escalation_mention: str = "@cto @security",
    ) -> None:
        """
        Override escalation alert: sent when deployment is overridden under Error Budget exhaustion.

        Netflix CAB (Change Advisory Board) style — when a deployment is forced through
        while the Error Budget is exhausted, escalates to a senior decision-maker / governance channel.

        Args:
            override_type: Override type (hotfix, security_patch, business_critical, etc.)
            requester: Override requester
            reason: Override reason
            service_name: Target service name
            escalation_channel: Escalation channel (e.g., #governance)
            escalation_mention: Mention target (e.g., @cto @security)

        Note:
            This alert provides visibility into "risky actions".
            Every action that bypasses the Error Budget policy must be tracked.
        """
        self.send(
            Alert(
                title=f"OVERRIDE ESCALATION: {override_type}",
                description=(
                    f"Deployment Override approved under Error Budget exhaustion.\n\n"
                    f"Type: {override_type}\n"
                    f"Requester: {requester}\n"
                    f"Reason: {reason}\n"
                    f"Target: {service_name or 'N/A'}\n\n"
                    f"Channel: {escalation_channel}\n"
                    f"Mention: {escalation_mention}\n\n"
                    f"This override is recorded in the audit log."
                ),
                severity=AlertSeverity.WARNING,
                category=AlertCategory.SLO_VIOLATION,  # Classified under SLO violation category
                source="baldur",
                service_name=service_name,
                details={
                    "override_type": override_type,
                    "requester": requester,
                    "reason": reason,
                    "escalation_channel": escalation_channel,
                    "escalation_mention": escalation_mention,
                    "is_escalation": True,
                },
                alert_key=f"override:escalation:{override_type}:{service_name or 'global'}",
            )
        )
