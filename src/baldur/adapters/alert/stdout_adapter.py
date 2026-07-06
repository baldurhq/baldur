"""
Stdout Alert Adapter.

Outputs alerts to stdout in a formatted manner.
Ideal for containerized environments or local development.
"""

from __future__ import annotations

import sys
from typing import ClassVar

from baldur.interfaces.alert_adapter import Alert, AlertAdapter, AlertSeverity


class StdoutAlertAdapter(AlertAdapter):
    """
    Stdout-based alerting adapter.

    Features:
    - Colored output for different severity levels
    - JSON or human-readable format
    - Works with log aggregators

    Usage:
        adapter = StdoutAlertAdapter()
        adapter.send(Alert(title="Service Down", ...))
    """

    # ANSI color codes
    COLORS: ClassVar[dict[AlertSeverity, str]] = {
        AlertSeverity.CRITICAL: "\033[91m",  # Red
        AlertSeverity.WARNING: "\033[93m",  # Yellow
        AlertSeverity.INFO: "\033[94m",  # Blue
    }
    RESET: ClassVar[str] = "\033[0m"

    def __init__(self, use_color: bool = True, json_format: bool = False):
        """
        Initialize stdout alert adapter.

        Args:
            use_color: If True, use ANSI colors for severity
            json_format: If True, output JSON instead of human-readable
        """
        self.use_color = use_color
        self.json_format = json_format
        self._active_alerts: dict[str, Alert] = {}

    def send(self, alert: Alert) -> None:
        """Send an alert to stdout."""
        self._active_alerts[alert.key] = alert

        output = alert.to_json() if self.json_format else self._format_alert(alert)

        if self.use_color:
            color = self.COLORS.get(alert.severity, "")
            output = f"{color}{output}{self.RESET}"

        print(f"[ALERT] {output}", file=sys.stdout, flush=True)

    def resolve(self, alert_key: str) -> None:
        """Resolve an alert."""
        if alert_key in self._active_alerts:
            alert = self._active_alerts.pop(alert_key)
            print(
                f"[ALERT RESOLVED] {alert.title} (key={alert_key})",
                file=sys.stdout,
                flush=True,
            )
        else:
            print(
                f"[ALERT RESOLVED] Unknown alert (key={alert_key})",
                file=sys.stdout,
                flush=True,
            )

    def _format_alert(self, alert: Alert) -> str:
        """Format alert as human-readable string."""
        parts = [
            f"[{alert.severity.value.upper()}]",
            f"[{alert.category.value}]",
            alert.title,
        ]

        if alert.service_name:
            parts.append(f"(service={alert.service_name})")

        parts.append(f"- {alert.description}")

        if alert.slo_name:
            parts.append(
                f"SLO: {alert.slo_name} target={alert.slo_target} current={alert.slo_current}"
            )

        return " ".join(parts)
