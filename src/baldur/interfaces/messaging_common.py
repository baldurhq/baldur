"""
Common Messaging Types.

Shared enums for AlertAdapter and NotificationAdapter.
Eliminates duplicate severity/channel definitions across
the two interfaces while preserving their separate responsibilities.

Static Enum for MessageChannel:
- Type safety: mypy/IDE autocomplete support
- Config validation: automatic Pydantic validation
- Governance: explicit channel list for audit tracking
- Extension frequency: ~1-2 new channels per year (2 lines + 1 adapter)
"""

from __future__ import annotations

from enum import Enum


class MessageSeverity(str, Enum):
    """Unified severity levels for alerts and notifications."""

    CRITICAL = "critical"
    HIGH = "high"
    WARNING = "warning"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class MessageChannel(str, Enum):
    """Shared channel enum for AlertAdapter and NotificationAdapter.

    Channel types are fixed as Enum; adapter implementations
    are dynamically registered via ProviderRegistry.
    """

    SLACK = "slack"
    TEAMS = "teams"
    PAGERDUTY = "pagerduty"
    WEBHOOK = "webhook"
    STDOUT = "stdout"
    FILE = "file"
    LOG = "log"
