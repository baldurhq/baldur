"""
Default Alert Adapter Implementations.

Provides non-invasive alerting implementations:
- StdoutAlertAdapter: Output to stdout (default for containers)
- FileAlertAdapter: Write to files (for local development)
- NullAlertAdapter: No-op (for testing or opt-out)

Users can implement their own adapters for:
- Slack/Teams webhooks
- PagerDuty/OpsGenie
- Email
- Custom solutions
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .file_adapter import FileAlertAdapter
from .null_adapter import NullAlertAdapter
from .stdout_adapter import StdoutAlertAdapter

if TYPE_CHECKING:
    from baldur.interfaces.alert_adapter import AlertAdapter


def get_alert_adapter(name: str | None = None) -> AlertAdapter:
    """Get AlertAdapter via ProviderRegistry.

    Resolves the 6 existing call sites that previously had no
    registry-backed get_alert_adapter() function:
    - chaos/blast_radius.py
    - chaos/safety_guard/guard.py
    - chaos/reports.py
    - error_budget/enums.py
    """
    from baldur.factory import ProviderRegistry

    return ProviderRegistry.get_alert(name)


__all__ = [
    "StdoutAlertAdapter",
    "FileAlertAdapter",
    "NullAlertAdapter",
    "get_alert_adapter",
]
