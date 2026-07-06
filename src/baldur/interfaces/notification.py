"""
Notification Service Interface

Provides an abstraction for sending notifications to external channels
(Slack, Teams, PagerDuty, Email, Webhook, etc.) without coupling to
any specific implementation.

Tier principle: the OSS package ships only logging adapters —
"OSS observes (logs), PRO notifies (pushes)". The concrete external-push
transports live in the private PRO distribution and register themselves
into ``ProviderRegistry`` at PRO init time. An OSS-only install resolves
the default :class:`LoggingNotificationAdapter` and performs no external HTTP.

Design Philosophy:
- ABC-based interface with duck-typed adapter compatibility via ABC.register()
- Default implementations: stdout, logging
- User provides their own adapter for production (explicitly out-of-contract)
- All adapters are managed through ProviderRegistry

Usage:
    # Register your notification adapter
    from baldur.interfaces.notification import register_notification_adapter
    from baldur.models.notification import NotificationPayload

    class SlackNotificationAdapter(NotificationAdapter):
        def send(self, payload: NotificationPayload) -> bool:
            # Your Slack webhook implementation
            return True

    register_notification_adapter(SlackNotificationAdapter())
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

if TYPE_CHECKING:
    from baldur.models.notification import NotificationPayload

logger = structlog.get_logger()


# =============================================================================
# Unified Notification Manager Protocol (519 PR 3 / C-d1 rule 2)
# =============================================================================


@runtime_checkable
class UnifiedNotificationManager(Protocol):
    """Protocol for the PRO unified notification manager.

    Used as a TYPE_CHECKING-only annotation by OSS modules that hold an
    injected manager reference (e.g., the runbook approval gate) and
    call its public dispatch method. The realized backend lives in
    :mod:`baldur_pro.services.unified_notification`.
    """

    def send(self, *args: Any, **kwargs: Any) -> Any: ...

    def send_batch(self, *args: Any, **kwargs: Any) -> Any: ...

    # ``notify(payload: NotificationPayload)`` is the current dispatch entry
    # used by OSS callers (runbook approval gate, async logger, etc.). Typed
    # as Any to keep the OSS Protocol free of a PRO payload dependency.
    def notify(self, *args: Any, **kwargs: Any) -> Any: ...


# =============================================================================
# Notification Types
# =============================================================================


from baldur.interfaces.messaging_common import MessageChannel, MessageSeverity

# Backward-compatible aliases
NotificationSeverity = MessageSeverity
NotificationChannel = MessageChannel


# MessageSeverity has a WARNING level with no NotificationPriority peer; it maps
# to HIGH at the boundary. Every other value maps name-for-name. Used by
# send_notification() to translate its legacy ``severity`` argument into the
# canonical NotificationPayload.priority.
_SEVERITY_TO_PRIORITY: dict[str, str] = {
    "critical": "critical",
    "high": "high",
    "warning": "high",
    "medium": "medium",
    "low": "low",
    "info": "info",
}


def _severity_to_priority(severity: NotificationSeverity) -> Any:
    """Map a :class:`MessageSeverity` to a :class:`NotificationPriority`."""
    from baldur.models.notification import NotificationPriority

    return NotificationPriority(_SEVERITY_TO_PRIORITY.get(severity.value, "medium"))


# =============================================================================
# Notification Adapter Interface (ABC)
# =============================================================================


class NotificationAdapter(ABC):
    """
    ABC for notification adapters.

    Implement this class to send notifications to your preferred channel.
    Duck-typed adapters are also accepted via register_notification_adapter(),
    which auto-registers them as virtual subclasses.

    Example:
        class SlackNotificationAdapter(NotificationAdapter):
            def send(self, payload: NotificationPayload) -> bool:
                response = requests.post(
                    SLACK_WEBHOOK_URL,
                    json={"text": f"*{payload.title}*\\n{payload.message}"}
                )
                return response.ok

            def send_batch(self, payloads: list[NotificationPayload]) -> int:
                return sum(1 for p in payloads if self.send(p))

            @property
            def channel(self) -> NotificationChannel:
                return NotificationChannel.SLACK
    """

    @abstractmethod
    def send(self, payload: NotificationPayload) -> bool:
        """
        Send a single notification.

        Returns:
            True if sent successfully, False otherwise
        """
        ...

    @abstractmethod
    def send_batch(self, payloads: list[NotificationPayload]) -> int:
        """
        Send multiple notifications.

        Returns:
            Number of successfully sent notifications
        """
        ...

    @property
    @abstractmethod
    def channel(self) -> NotificationChannel:
        """Return the channel this adapter handles."""
        ...

    def is_available(self) -> bool:
        """Check if this adapter is available (configuration validity).

        Non-abstract concrete method — ABC-inheriting adapters inherit
        the default ``return True``. Network-based adapters should override
        with I/O-free self-diagnosis (e.g., URL format validation).

        Duck-typed adapters (virtual subclasses) do NOT inherit this —
        callers must use ``getattr(adapter, 'is_available', lambda: True)()``.
        """
        return True


# =============================================================================
# Default Implementations
# =============================================================================


class StdoutNotificationAdapter(NotificationAdapter):
    """Default adapter that prints to stdout."""

    def send(self, payload: NotificationPayload) -> bool:
        print(f"[{payload.priority.value.upper()}] {payload.title}: {payload.message}")
        return True

    def send_batch(self, payloads: list[NotificationPayload]) -> int:
        return sum(1 for p in payloads if self.send(p))

    @property
    def channel(self) -> NotificationChannel:
        return NotificationChannel.STDOUT


class LoggingNotificationAdapter(NotificationAdapter):
    """Adapter that logs notifications.

    The OSS default: notifications are observed via structlog, never
    pushed externally. Its ``channel`` is :attr:`NotificationChannel.LOG` so a
    recorded delivery reads as "logged, not pushed".
    """

    # priority -> structlog method mapping
    _PRIORITY_TO_LOG_METHOD = {
        "CRITICAL": "critical",
        "HIGH": "error",
        "WARNING": "warning",
        "MEDIUM": "warning",
        "LOW": "info",
        "INFO": "debug",
    }

    def __init__(self, logger_name: str = "baldur.notifications"):
        self._logger = structlog.get_logger().bind(logger_name=logger_name)

    def send(self, payload: NotificationPayload) -> bool:
        method_name = self._PRIORITY_TO_LOG_METHOD.get(
            payload.priority.value.upper(), "info"
        )
        log_method = getattr(self._logger, method_name)
        log_method(
            "notification.sent",
            source=payload.source,
            title=payload.title,
            message=payload.message,
            notification=payload.to_dict(),
        )
        return True

    def send_batch(self, payloads: list[NotificationPayload]) -> int:
        return sum(1 for p in payloads if self.send(p))

    @property
    def channel(self) -> NotificationChannel:
        return NotificationChannel.LOG


# =============================================================================
# Notification Service Registry (delegates to ProviderRegistry)
# =============================================================================


_default_adapter: NotificationAdapter = LoggingNotificationAdapter()


def register_notification_adapter(adapter: object) -> None:
    """Register a notification adapter for its channel.

    Delegates to ProviderRegistry. Auto-registers duck-typed adapters
    as virtual subclasses of NotificationAdapter.
    """
    if not isinstance(adapter, NotificationAdapter):
        required = ("send", "send_batch", "channel")
        missing = [m for m in required if not hasattr(adapter, m)]
        if missing:
            raise TypeError(
                f"Adapter {type(adapter).__name__} missing: {missing}. "
                f"Inherit from NotificationAdapter."
            )
        NotificationAdapter.register(type(adapter))
        logger.warning(
            "notification.duck_typed_adapter_registered",
            adapter_type=type(adapter).__name__,
        )

    from baldur.factory import ProviderRegistry

    channel_name = adapter.channel.value if hasattr(adapter, "channel") else "default"
    ProviderRegistry.register_notification(channel_name, lambda: adapter)
    logger.info("notification.adapter_registered", channel=channel_name)


def get_notification_adapter(
    channel: NotificationChannel | None = None,
) -> NotificationAdapter:
    """
    Get the notification adapter for a channel.

    Delegates to ProviderRegistry.

    Args:
        channel: Target channel, or None for default

    Returns:
        NotificationAdapter instance
    """
    from baldur.factory import ProviderRegistry

    name = channel.value if channel else None
    try:
        return ProviderRegistry.get_notification(name)
    except (ValueError, Exception):
        return _default_adapter


def send_notification(
    title: str,
    message: str,
    severity: NotificationSeverity = NotificationSeverity.MEDIUM,
    channel: NotificationChannel | None = None,
    **metadata,
) -> bool:
    """
    Convenience function to send a notification.

    Args:
        title: Notification title
        message: Notification body
        severity: Urgency level
        channel: Target channel (uses default if None)
        **metadata: Additional context

    Returns:
        True if sent successfully
    """
    from baldur.models.notification import NotificationPayload

    payload = NotificationPayload(
        title=title,
        message=message,
        priority=_severity_to_priority(severity),
        source="baldur",
        metadata=metadata,
        channels=[channel.value] if channel else None,
    )
    adapter = get_notification_adapter(channel)
    return adapter.send(payload)


# =============================================================================
# Convenience Functions for Chaos Scheduler Integration
# =============================================================================


def send_pending_approval_alert(
    pending_count: int,
    schedules: list[Any] | None = None,
    blast_radius: list[Any] | None = None,
) -> bool:
    """
    Send alert for pending chaos experiment approvals.

    Called by chaos_scheduler.check_and_alert_pending_approvals().
    """
    message = f"{pending_count} chaos experiments are pending approval."
    if schedules:
        message += f"\n- Scheduled: {len(schedules)}"
    if blast_radius:
        message += f"\n- Blast radius: {len(blast_radius)}"

    return send_notification(
        title="Chaos Experiments Pending Approval",
        message=message,
        severity=NotificationSeverity.MEDIUM,
        pending_count=pending_count,
    )


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Types
    "NotificationSeverity",
    "NotificationChannel",
    # ABC
    "NotificationAdapter",
    # Default adapters
    "StdoutNotificationAdapter",
    "LoggingNotificationAdapter",
    # Registry
    "register_notification_adapter",
    "get_notification_adapter",
    "send_notification",
    # Convenience
    "send_pending_approval_alert",
    # PRO Protocol (519 PR 3)
    "UnifiedNotificationManager",
]
