"""
Drift Threshold Configuration Model.

Provides dynamic configuration for metric drift thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now


@dataclass
class DriftThresholdConfig(SerializableMixin):
    """
    Drift threshold configuration.

    Operators can adjust these dynamically; every change is written to the
    audit log.

    Thresholds:
        - warning: 5% - warning, log only
        - critical: 20% - critical, send a notification
        - incident: 50% - incident, suspected event loss

    Example:
        >>> config = DriftThresholdConfig()
        >>> print(f"Warning at: {config.warning_threshold * 100}%")
        Warning at: 5.0%
        >>>
        >>> # Custom thresholds
        >>> config = DriftThresholdConfig(
        ...     warning_threshold=0.10,
        ...     critical_threshold=0.30,
        ... )
    """

    # Thresholds (0.0 - 1.0)
    warning_threshold: float = 0.05  # 5%
    critical_threshold: float = 0.20  # 20%
    incident_threshold: float = 0.50  # 50%

    # Notification settings
    alert_enabled: bool = True
    incident_auto_create: bool = True

    # Metadata
    updated_at: str | None = None
    updated_by: str | None = None

    def __post_init__(self) -> None:
        """Run validation after construction."""
        self._validate()

    def _validate(self) -> None:
        """Validate the thresholds."""
        if not (
            0
            < self.warning_threshold
            < self.critical_threshold
            < self.incident_threshold
            <= 1.0
        ):
            raise ValueError(
                "Thresholds must be: 0 < warning < critical < incident <= 1.0. "
                f"Got: warning={self.warning_threshold}, critical={self.critical_threshold}, "
                f"incident={self.incident_threshold}"
            )

    @classmethod
    def from_env(cls) -> DriftThresholdConfig:
        """Build from environment variables (delegated to BaseSettings).

        Env-var parsing is delegated to DriftThresholdSettings(BaseSettings),
        removing manual os.environ.get() parsing (202 paradigm unification).
        """
        from baldur.settings.drift_threshold import DriftThresholdSettings

        settings = DriftThresholdSettings()
        return cls(
            warning_threshold=settings.warning_threshold,
            critical_threshold=settings.critical_threshold,
            incident_threshold=settings.incident_threshold,
            alert_enabled=settings.alert_enabled,
            incident_auto_create=settings.incident_auto_create,
        )

    def update(
        self,
        actor_id: str | None = None,
        **kwargs: Any,
    ) -> DriftThresholdConfig:
        """
        Return a config updated with the new values.

        Args:
            actor_id: ID of the user performing the update
            **kwargs: Fields to update

        Returns:
            A new DriftThresholdConfig instance with the updates applied
        """
        current = self.to_dict()
        current.update(kwargs)
        current["updated_at"] = utc_now().isoformat()
        current["updated_by"] = actor_id
        return self.from_dict(current)

    def get_threshold_percent_display(self) -> dict[str, str]:
        """Return the thresholds as percentage strings."""
        return {
            "warning": f"{self.warning_threshold * 100:.1f}%",
            "critical": f"{self.critical_threshold * 100:.1f}%",
            "incident": f"{self.incident_threshold * 100:.1f}%",
        }


__all__ = ["DriftThresholdConfig"]
