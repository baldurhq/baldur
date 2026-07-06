"""
Null Alert Adapter.

No-op adapter that discards all alerts.
Useful for testing or when alerting is disabled.
"""

from __future__ import annotations

from baldur.interfaces.alert_adapter import Alert, AlertAdapter


class NullAlertAdapter(AlertAdapter):
    """
    No-op alerting adapter.

    All operations are silent no-ops. Use for:
    - Testing where alerts are not needed
    - Explicitly disabling alerting
    - Benchmarking without I/O overhead

    Usage:
        adapter = NullAlertAdapter()
        adapter.send(alert)  # Does nothing
    """

    def send(self, alert: Alert) -> None:
        """No-op: discards the alert."""
        pass

    def resolve(self, alert_key: str) -> None:
        """No-op: does nothing."""
        pass
