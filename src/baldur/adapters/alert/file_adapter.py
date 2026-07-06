"""
File-based Alert Adapter.

Writes alerts to JSON files for persistence and later analysis.
Supports tracking active vs resolved alerts.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import structlog

from baldur.interfaces.alert_adapter import Alert, AlertAdapter
from baldur.utils.time import utc_now

logger = structlog.get_logger()


class FileAlertAdapter(AlertAdapter):
    """
    File-based alerting adapter.

    Features:
    - JSON Lines format for alert history
    - Separate file for active alerts
    - Automatic directory creation

    Usage:
        adapter = FileAlertAdapter("logs/alerts")
        adapter.send(Alert(title="Service Down", ...))
    """

    def __init__(
        self,
        directory: str | Path,
        active_alerts_file: str = "active_alerts.json",
        history_file: str = "alert_history.jsonl",
    ):
        """
        Initialize file alert adapter.

        Args:
            directory: Directory for alert files
            active_alerts_file: File to store currently active alerts
            history_file: File to store alert history (JSON Lines)
        """
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

        self.active_alerts_path = self.directory / active_alerts_file
        self.history_path = self.directory / history_file

        # Only keys are persisted (see _load_active_alerts using
        # dict.fromkeys); the Alert value slot is unused.
        self._active_alerts: dict[str, Alert | None] = {}
        self._load_active_alerts()

    def _load_active_alerts(self) -> None:
        """Load active alerts from file."""
        if self.active_alerts_path.exists():
            try:
                with open(self.active_alerts_path, encoding="utf-8") as f:
                    data = json.load(f)
                    # Note: We just track keys, not full Alert objects
                    self._active_alerts = dict.fromkeys(data.get("keys", []))
            except Exception as e:
                logger.warning(
                    "file_alert_adapter.error_loading_active_alerts",
                    error=e,
                )

    def _save_active_alerts(self) -> None:
        """Save active alerts to file."""
        try:
            with open(self.active_alerts_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "keys": list(self._active_alerts.keys()),
                        "updated_at": utc_now().isoformat(),
                    },
                    f,
                    indent=2,
                )
        except Exception as e:
            logger.exception(
                "file_alert_adapter.error_saving_active_alerts",
                error=e,
            )

    def send(self, alert: Alert) -> None:
        """Send an alert by writing to file."""
        # Track as active
        self._active_alerts[alert.key] = alert
        self._save_active_alerts()

        # Append to history
        try:
            with open(self.history_path, "a", encoding="utf-8") as f:
                entry = {
                    "event": "alert_sent",
                    "timestamp": utc_now().isoformat(),
                    "alert": alert.to_dict(),
                }
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.exception(
                "file_alert_adapter.error_writing_alert_history",
                error=e,
            )

    def resolve(self, alert_key: str) -> None:
        """Resolve an alert by removing from active set."""
        was_active = alert_key in self._active_alerts

        if alert_key in self._active_alerts:
            del self._active_alerts[alert_key]
            self._save_active_alerts()

        # Append resolution to history
        try:
            with open(self.history_path, "a", encoding="utf-8") as f:
                entry = {
                    "event": "alert_resolved",
                    "timestamp": utc_now().isoformat(),
                    "alert_key": alert_key,
                    "was_active": was_active,
                }
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.exception(
                "file_alert_adapter.error_writing_resolution",
                error=e,
            )

    def get_active_alerts(self) -> list[str]:
        """Get list of active alert keys."""
        return list(self._active_alerts.keys())

    def get_history(
        self,
        limit: int = 100,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> list[dict]:
        """
        Get alert history.

        Args:
            limit: Maximum entries to return
            start_time: Filter from this time
            end_time: Filter until this time

        Returns:
            List of alert history entries
        """
        entries: list[dict] = []

        if not self.history_path.exists():
            return entries

        try:
            with open(self.history_path, encoding="utf-8") as f:
                for line in f:
                    if len(entries) >= limit:
                        break

                    try:
                        entry = json.loads(line.strip())

                        # Filter by time if specified
                        if start_time or end_time:
                            entry_time = datetime.fromisoformat(
                                entry.get("timestamp", "").replace("Z", "+00:00")
                            )
                            if start_time and entry_time < start_time:
                                continue
                            if end_time and entry_time > end_time:
                                continue

                        entries.append(entry)

                    except (json.JSONDecodeError, ValueError):
                        continue

        except Exception as e:
            logger.warning(
                "file_alert_adapter.error_reading_history",
                error=e,
            )

        return entries
