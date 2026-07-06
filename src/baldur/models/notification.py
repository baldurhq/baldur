"""Notification Domain Value Types.

OSS-tier value types for the unified notification system. Runtime-
instantiated DTOs/enums that must be available on OSS-only installs
(e.g., OSS handlers construct ``NotificationPayload`` instances before
handing them off to whatever notification manager is registered).

The ``UnifiedNotificationManager`` orchestrator itself stays PRO-tier
and is reached via the Protocol declared in
:mod:`baldur.interfaces.notification` (``UnifiedNotificationManager``).
This module's namespace (``baldur.models.notification``) does NOT
collide with :mod:`baldur.interfaces.notification` — different module
paths; ``models/`` holds value types, ``interfaces/`` holds Protocols.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now


class NotificationPriority(str, Enum):
    """Notification priority levels."""

    CRITICAL = "critical"
    """Immediate, all channels."""

    HIGH = "high"
    """Urgent, Slack only."""

    MEDIUM = "medium"
    """Normal, Slack only."""

    LOW = "low"
    """Low urgency, Slack only."""

    INFO = "info"
    """Log only unless configured."""


class NotificationCategory(str, Enum):
    """Notification categories for routing and filtering."""

    SECURITY = "security"
    OPERATIONS = "operations"
    SLA = "sla"
    CIRCUIT_BREAKER = "circuit_breaker"
    GOVERNANCE = "governance"
    APPROVAL = "approval"
    REPORT = "report"
    ERROR = "error"
    CHAOS = "chaos"


@dataclass
class NotificationPayload(SerializableMixin):
    """Unified notification payload.

    All notification sources construct this payload so the downstream
    manager can route, dedup, and deliver uniformly.
    """

    title: str
    message: str
    priority: NotificationPriority = NotificationPriority.MEDIUM
    category: NotificationCategory = NotificationCategory.OPERATIONS

    source: str = "unknown"
    """Originating subsystem (e.g., ``drift_detection``, ``circuit_breaker``)."""

    task_name: str | None = None
    task_id: str | None = None

    metadata: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: utc_now())

    channels: list[str] | None = None
    """Routing hint; the manager may override."""

    dedup_key: str | None = None
    """Optional key used for cooldown deduplication."""


__all__ = ["NotificationCategory", "NotificationPayload", "NotificationPriority"]
