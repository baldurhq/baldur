"""
Meta-Watchdog Module.

Monitors the health of the Baldur system itself and performs automatic
recovery or escalates to human operators on failure.

Core components:
- MetaWatchdogSettings: configuration
- HealthProbeManager: subsystem health collection
- StuckDetector: zero-variance based stuck detection
- EscalationManager: human-intervention request (PagerDuty, Slack)
- SelfhealerWatchdog (PRO): aggregated watchdog — resolve via
  ``ProviderRegistry.selfhealer_watchdog.safe_get()``.

Status: Internal
"""

from baldur.meta.config import (
    MetaWatchdogSettings,
    get_meta_watchdog_settings,
    reset_meta_watchdog_settings,
)
from baldur.meta.escalation import (
    EscalationEvent,
    EscalationLevel,
    EscalationManager,
)
from baldur.meta.health_probe import (
    HealthProbe,
    HealthProbeManager,
    HealthStatus,
    ProbeResult,
)
from baldur.meta.stuck_detector import (
    StuckDetectionResult,
    StuckDetector,
    get_stuck_detector,
)

__all__ = [
    # Config
    "MetaWatchdogSettings",
    "get_meta_watchdog_settings",
    "reset_meta_watchdog_settings",
    # Health Probe
    "HealthStatus",
    "ProbeResult",
    "HealthProbe",
    "HealthProbeManager",
    # Stuck Detector
    "StuckDetectionResult",
    "StuckDetector",
    "get_stuck_detector",
    # Escalation
    "EscalationLevel",
    "EscalationEvent",
    "EscalationManager",
]
