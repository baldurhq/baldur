"""
Escalation Manager - human-intervention requests.

When automatic recovery fails, escalates to a human through the notification
seam (Slack, PagerDuty). The EscalationManager is the tier-neutral orchestrator
(cross-worker dedup + per-process cooldown); the concrete external-push
transport is resolved via ``ProviderRegistry``: an OSS-only install resolves
the default logging adapter and **logs** the escalation, while a PRO install
resolves the concrete transport and **pushes**.

Channels by level:
- PagerDuty: CRITICAL
- Slack: WARNING, ERROR, CRITICAL
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.notification import (
    NotificationChannel,
    get_notification_adapter,
)
from baldur.meta.config import MetaWatchdogSettings, get_meta_watchdog_settings
from baldur.utils.singleton import make_singleton_factory
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.models.notification import NotificationPayload

logger = structlog.get_logger()

__all__ = [
    "EscalationLevel",
    "EscalationEvent",
    "EscalationResult",
    "EscalationManager",
    "get_escalation_manager",
    "reset_escalation_manager",
]


class EscalationLevel(str, Enum):
    """Escalation severity level."""

    INFO = "info"
    """Informational notification."""

    WARNING = "warning"
    """Attention required."""

    ERROR = "error"
    """An error occurred."""

    CRITICAL = "critical"
    """Urgent intervention required."""


@dataclass
class EscalationEvent:
    """An escalation event."""

    level: EscalationLevel
    """Severity level."""

    title: str
    """Notification title."""

    description: str
    """Detailed description."""

    component: str
    """Related component."""

    details: dict[str, Any] = field(default_factory=dict)
    """Additional detail."""

    timestamp: datetime = field(default_factory=lambda: utc_now())
    """Occurrence time."""


@dataclass
class EscalationResult:
    """Result of an escalation."""

    success: bool
    """Whether the escalation succeeded."""

    channels_sent: list[str]
    """Channels delivered successfully (the resolved adapter's channel — ``log``
    on an OSS-only install, ``slack``/``pagerduty`` on PRO; a PRO install
    delivering through the logging fallback is therefore visible as ``log``)."""

    channels_failed: list[str]
    """Channels that failed to deliver."""

    error_message: str | None = None
    """Error message."""


# EscalationLevel -> NotificationPriority value. The recorded delivery channel
# and Block-Kit appearance are driven by ``level`` (carried in payload metadata);
# priority only sets the logging adapter's log level on OSS installs.
_LEVEL_TO_PRIORITY_VALUE = {
    EscalationLevel.INFO: "info",
    EscalationLevel.WARNING: "medium",
    EscalationLevel.ERROR: "high",
    EscalationLevel.CRITICAL: "critical",
}


class EscalationManager:
    """
    Escalation Manager.

    Escalates to a human when automatic recovery fails.

    Responsibilities:
    - Channel selection by severity
    - Cooldown management (alert-storm prevention)
    - Delivery through the ProviderRegistry notification seam

    Example:
        manager = EscalationManager()

        event = EscalationEvent(
            level=EscalationLevel.CRITICAL,
            title="DLQ Consumer Stuck",
            description="DLQ consumer stopped processing for 5 minutes",
            component="dlq",
        )

        result = manager.escalate(event)
        if result.success:
            print("Escalation sent")
    """

    def __init__(
        self,
        settings: MetaWatchdogSettings | None = None,
    ):
        """
        Initialize.

        Args:
            settings: Meta-Watchdog settings for escalation gating/policy
                (cooldown, maintenance, ``escalation_enabled``, ``dry_run_mode``).
                Defaults are loaded if None.

        Note:
            Transport config (``slack_webhook_url`` / ``pagerduty_routing_key`` /
            ``escalation_api_timeout_seconds``) is NOT taken from ``settings``.
            The PRO escalation adapters resolved through the ProviderRegistry
            notification seam read their own config home — the global
            ``get_meta_watchdog_settings()`` singleton (env-driven, e.g.
            ``BALDUR_META_WATCHDOG_SLACK_WEBHOOK_URL``). Injecting a webhook here
            sets gating only; to point delivery at a different URL, set the env
            var (or the singleton) before sending. An injected transport value
            that diverges from the singleton is logged as a warning to surface
            this seam.
        """
        self._settings = settings or get_meta_watchdog_settings()
        if settings is not None:
            self._warn_on_ignored_transport_config(settings)
        self._lock = threading.RLock()
        self._last_escalation: dict[str, float] = {}

    @staticmethod
    def _warn_on_ignored_transport_config(injected: MetaWatchdogSettings) -> None:
        """Warn when injected transport config will be ignored by the adapters.

        The PRO escalation adapters read transport config from the global
        ``get_meta_watchdog_settings()`` singleton, not from settings injected
        into this manager (which drive gating only). A caller who injects a
        webhook expecting it to be used would otherwise see a silent no-send
        ("not configured"). Only a non-empty injected value that differs from
        the singleton is reported.
        """
        singleton = get_meta_watchdog_settings()
        if singleton is injected:
            return
        diverged = [
            field
            for field in ("slack_webhook_url", "pagerduty_routing_key")
            if getattr(injected, field, None)
            and getattr(injected, field, None) != getattr(singleton, field, None)
        ]
        if diverged:
            logger.warning(
                "escalation.injected_transport_config_ignored",
                diverged_fields=diverged,
                detail=(
                    "EscalationManager(settings=...) drives gating only; PRO "
                    "adapters read transport config from "
                    "get_meta_watchdog_settings() — set BALDUR_META_WATCHDOG_* "
                    "env vars (or the singleton) to change delivery target"
                ),
            )

    def _can_escalate(self, component: str) -> bool:
        """
        Check the cooldown.

        Args:
            component: component name

        Returns:
            Whether escalation is allowed
        """
        with self._lock:
            last_time = self._last_escalation.get(component, 0)
            return time.time() - last_time > self._settings.escalation_cooldown_seconds

    def _record_escalation(self, component: str) -> None:
        """
        Record an escalation.

        Args:
            component: component name
        """
        with self._lock:
            self._last_escalation[component] = time.time()

    def _is_maintenance_component(self, component: str) -> bool:
        """
        Check whether the component is under maintenance.

        Args:
            component: component name

        Returns:
            Whether the component is under maintenance
        """
        return component in self._settings.maintenance_components

    def _build_payload(self, event: EscalationEvent) -> NotificationPayload:
        """Map an escalation event onto the canonical notification payload.

        The escalation-specific fields (``component``, ``level``, ``timestamp``,
        ``details``) ride in ``metadata`` so the PRO escalation adapters can
        reproduce escalation's source-specific Block-Kit / PagerDuty shape.
        """
        from baldur.models.notification import (
            NotificationCategory,
            NotificationPayload,
            NotificationPriority,
        )

        return NotificationPayload(
            title=event.title,
            message=event.description,
            priority=NotificationPriority(_LEVEL_TO_PRIORITY_VALUE[event.level]),
            category=NotificationCategory.OPERATIONS,
            source=event.component,
            metadata={
                "component": event.component,
                "level": event.level.value,
                "timestamp": event.timestamp.isoformat(),
                "details": event.details,
            },
        )

    @staticmethod
    def _send_via(adapter, payload: NotificationPayload) -> tuple[bool, str | None]:
        """Send ``payload`` through ``adapter``, returning ``(ok, reason)``.

        PRO escalation adapters expose ``send_with_reason`` to surface a
        per-channel failure cause for the operator self-test; the uniform
        ``NotificationAdapter.send`` contract returns bool only, so a generic
        reason is synthesized for the logging fallback.
        """
        send_with_reason = getattr(adapter, "send_with_reason", None)
        if callable(send_with_reason):
            return send_with_reason(payload)
        ok = adapter.send(payload)
        return ok, (None if ok else "delivery failed")

    def _precheck(self, event: EscalationEvent) -> EscalationResult | None:
        """Apply the Redis-independent escalation gates.

        Returns a terminal :class:`EscalationResult` when escalation should
        short-circuit (disabled / dry-run / maintenance / local cooldown), or
        ``None`` when the event should proceed to the cross-worker gate and
        delivery. The dry-run case returns a *success* result on the synthetic
        ``dry_run`` channel.
        """
        # Disabled check
        if not self._settings.escalation_enabled:
            logger.debug("escalation.escalation_disabled")
            return EscalationResult(
                success=False,
                channels_sent=[],
                channels_failed=[],
                error_message="Escalation disabled",
            )

        # Dry-run mode check
        if self._settings.dry_run_mode:
            logger.info(
                "escalation.dry_run_escalation",
                escalation_component=event.component,
                title=event.title,
            )
            return EscalationResult(
                success=True,
                channels_sent=["dry_run"],
                channels_failed=[],
            )

        # Maintenance component check
        if self._is_maintenance_component(event.component):
            logger.debug(
                "escalation.maintenance_skipped",
                escalation_component=event.component,
            )
            return EscalationResult(
                success=False,
                channels_sent=[],
                channels_failed=[],
                error_message="Component in maintenance",
            )

        # Local cooldown check (fast, Redis-independent layer).
        if not self._can_escalate(event.component):
            logger.debug(
                "escalation.cooldown_active",
                escalation_component=event.component,
            )
            return EscalationResult(
                success=False,
                channels_sent=[],
                channels_failed=[],
                error_message="Cooldown active",
            )

        return None

    def escalate(self, event: EscalationEvent) -> EscalationResult:
        """
        Run an escalation.

        Args:
            event: the escalation event

        Returns:
            EscalationResult
        """
        rejection = self._precheck(event)
        if rejection is not None:
            return rejection

        # Cross-worker dedup gate: only one worker/pod in the cluster pages per
        # cooldown window. Slack has no native dedup (PagerDuty dedups via
        # dedup_key), so the watchdog runs per gunicorn worker and would
        # otherwise page N×M times for one incident. The lock is SET NX EX
        # against the shared store keyed to the cooldown TTL; it fails open
        # (returns True when Redis is down), degrading to the per-process
        # _last_escalation cooldown above. The won lock auto-expires via its
        # EX TTL, so a crashing winner frees the slot for the next cycle.
        if not self._acquire_cross_worker_slot(event.component):
            logger.debug(
                "escalation.cross_worker_deduped",
                escalation_component=event.component,
            )
            return EscalationResult(
                success=False,
                channels_sent=[],
                channels_failed=[],
                error_message="Cross-worker cooldown active",
            )

        payload = self._build_payload(event)

        # Channel selection by level. Both targets may resolve to the same
        # adapter on OSS (the logging fallback) — dedup by the resolved channel
        # so an OSS install records one ``log``, not one per attempted channel.
        targets: list[NotificationChannel] = []
        if event.level == EscalationLevel.CRITICAL:
            targets.append(NotificationChannel.PAGERDUTY)
        if event.level in (
            EscalationLevel.WARNING,
            EscalationLevel.ERROR,
            EscalationLevel.CRITICAL,
        ):
            targets.append(NotificationChannel.SLACK)

        channels_sent: list[str] = []
        channels_failed: list[str] = []
        seen_resolved: set[str] = set()
        for channel in targets:
            adapter = get_notification_adapter(channel)
            resolved = adapter.channel.value
            if resolved in seen_resolved:
                continue
            seen_resolved.add(resolved)
            ok, _reason = self._send_via(adapter, payload)
            if ok:
                channels_sent.append(resolved)
            else:
                channels_failed.append(resolved)

        # Success if at least one channel delivered.
        success = len(channels_sent) > 0

        if success:
            self._record_escalation(event.component)
            logger.warning(
                "escalation.escalated",
                escalation_component=event.component,
                title=event.title,
                channels_sent=channels_sent,
            )
        else:
            # All channels failed: release the cross-worker slot so the next
            # cycle / another worker may retry (mirrors the local layer, which
            # is likewise not recorded on failure).
            self._release_cross_worker_slot(event.component)

        return EscalationResult(
            success=success,
            channels_sent=channels_sent,
            channels_failed=channels_failed,
        )

    def _acquire_cross_worker_slot(self, component: str) -> bool:
        """Claim the cluster-wide escalation slot for ``component``.

        Delegates to the dormant-until-now ``WatchdogStateStore`` distributed
        lock (``SET NX EX``). TTL is the escalation cooldown so the slot stays
        held for one cooldown window. Fails open: when Redis is unreachable the
        store returns ``True`` and dedup degrades to the per-process cooldown.

        Args:
            component: component name (dedup key granularity)

        Returns:
            True if this worker won the slot (or Redis is down), False if
            another worker already holds it.
        """
        try:
            from baldur.meta.state_store import get_watchdog_state_store

            return get_watchdog_state_store().acquire_escalation_lock(
                component,
                lock_ttl_seconds=int(self._settings.escalation_cooldown_seconds),
            )
        except ImportError:
            return True
        except Exception as e:
            logger.debug("escalation.cross_worker_lock_error", error=e)
            return True

    def _release_cross_worker_slot(self, component: str) -> None:
        """Release the cluster-wide escalation slot for ``component``.

        Called only on all-channel delivery failure so a retry is not blocked
        for the full cooldown window.

        Args:
            component: component name
        """
        try:
            from baldur.meta.state_store import get_watchdog_state_store

            get_watchdog_state_store().release_escalation_lock(component)
        except ImportError:
            pass
        except Exception as e:
            logger.debug("escalation.cross_worker_unlock_error", error=e)

    def send_test(self) -> EscalationResult:
        """
        Send an operator self-test through every configured channel.

        Unlike :meth:`escalate`, this routes by **configuration** (every
        channel with a configured credential), not by severity level, and
        bypasses all of escalate()'s gates — cooldown, maintenance,
        ``escalation_enabled``, and ``dry_run_mode``. That is intentional:
        the test *is* the configuration check, an operator may run it
        repeatedly, and ``dry_run_mode`` would otherwise short-circuit to a
        fake success that makes the self-test lie.

        Delivery goes through the notification seam: on an OSS-only
        install the configured channels resolve to the logging adapter, so the
        self-test validates config and **logs intent** (recorded channel
        ``log``); live external delivery is a PRO capability.

        An unconfigured channel is silently skipped — it is never counted as
        "failed". ``success`` is True iff at least one channel is configured
        and every configured channel delivered. When a configured channel
        fails, ``error_message`` aggregates the per-channel cause; when no
        channel is configured, it says so.

        Returns:
            EscalationResult with per-channel sent/failed lists.
        """
        event = EscalationEvent(
            level=EscalationLevel.INFO,
            title="Baldur escalation self-test",
            description=(
                "This is a test notification triggered by an operator to "
                "verify the configured escalation channel delivers. "
                "No action is required."
            ),
            component="escalation_self_test",
        )
        payload = self._build_payload(event)

        # Route by configuration, not by level — attempt every configured
        # channel. Dedup the resolved channel so OSS (both → log) records once.
        attempts: list[NotificationChannel] = []
        if self._settings.slack_webhook_url:
            attempts.append(NotificationChannel.SLACK)
        if self._settings.pagerduty_routing_key:
            attempts.append(NotificationChannel.PAGERDUTY)

        channels_sent: list[str] = []
        channels_failed: list[str] = []
        failure_reasons: list[str] = []
        seen_resolved: set[str] = set()

        for channel in attempts:
            adapter = get_notification_adapter(channel)
            resolved = adapter.channel.value
            if resolved in seen_resolved:
                continue
            seen_resolved.add(resolved)
            ok, reason = self._send_via(adapter, payload)
            if ok:
                channels_sent.append(resolved)
            else:
                channels_failed.append(resolved)
                failure_reasons.append(f"{resolved}: {reason}")

        configured_any = bool(channels_sent or channels_failed)
        success = configured_any and not channels_failed

        if not configured_any:
            error_message: str | None = "No escalation channel configured"
        elif failure_reasons:
            error_message = "; ".join(failure_reasons)
        else:
            error_message = None

        result = EscalationResult(
            success=success,
            channels_sent=channels_sent,
            channels_failed=channels_failed,
            error_message=error_message,
        )

        # Outcome-adaptive, self-test-framed summary (string-literal event
        # names selected by outcome).
        if result.success:
            logger.info(
                "escalation.self_test_completed",
                channels_sent=channels_sent,
                channels_failed=channels_failed,
                error_message=error_message,
            )
        else:
            logger.warning(
                "escalation.self_test_failed",
                channels_sent=channels_sent,
                channels_failed=channels_failed,
                error_message=error_message,
            )

        return result

    def get_last_escalation_time(self, component: str) -> float | None:
        """
        Get the last escalation time.

        Args:
            component: component name

        Returns:
            Unix timestamp (None if absent)
        """
        with self._lock:
            return self._last_escalation.get(component)

    def reset_cooldown(self, component: str | None = None) -> None:
        """Reset the **local** per-process cooldown.

        This clears only the in-process ``_last_escalation`` layer. The
        cross-worker Redis lock acquired in :meth:`escalate` is **not**
        force-released here — it auto-expires via its ``EX`` TTL (the
        escalation cooldown window). A future operator force-repage path that
        needs the cluster slot freed immediately must additionally call
        ``get_watchdog_state_store().release_escalation_lock(component)``.

        Args:
            component: reset a single component only (None resets all)
        """
        with self._lock:
            if component:
                self._last_escalation.pop(component, None)
            else:
                self._last_escalation.clear()


# =============================================================================
# Singleton
# =============================================================================

(
    get_escalation_manager,
    configure_escalation_manager,
    reset_escalation_manager,
) = make_singleton_factory("escalation_manager", EscalationManager)
