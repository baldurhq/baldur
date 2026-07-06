"""
Emergency Domain Types.

Shared emergency domain types used by both emergency_mode (single-instance
lifecycle) and regional_emergency (multi-region namespace isolation).

Keeping these in models/ breaks the dependency of regional_emergency on
emergency_mode for basic type definitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now


class EmergencyLevel(str, Enum):
    """
    Emergency level definitions.

    Each level determines per-tier traffic multipliers.
    Ordering: NORMAL < LEVEL_1 < LEVEL_2 < LEVEL_3 (severity-based).
    """

    NORMAL = "normal"  # Normal operation (all traffic allowed)
    LEVEL_1 = "level_1"  # Minor incident - Tier 3 (Non-Essential) blocked
    LEVEL_2 = "level_2"  # Moderate incident - Tier 2, 3 blocked, Tier 1 at 100%
    LEVEL_3 = "level_3"  # Severe incident - Tier 1 at 50% only

    @property
    def severity(self) -> int:
        """Numeric severity for ordering comparisons and backward compatibility."""
        return _SEVERITY_ORDER[self]

    def __ge__(self, other: object) -> bool:
        if isinstance(other, EmergencyLevel):
            return _SEVERITY_ORDER[self] >= _SEVERITY_ORDER[other]
        return NotImplemented

    def __gt__(self, other: object) -> bool:
        if isinstance(other, EmergencyLevel):
            return _SEVERITY_ORDER[self] > _SEVERITY_ORDER[other]
        return NotImplemented

    def __le__(self, other: object) -> bool:
        if isinstance(other, EmergencyLevel):
            return _SEVERITY_ORDER[self] <= _SEVERITY_ORDER[other]
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, EmergencyLevel):
            return _SEVERITY_ORDER[self] < _SEVERITY_ORDER[other]
        return NotImplemented

    @classmethod
    def from_severity(cls, severity: int) -> EmergencyLevel:
        """Create EmergencyLevel from numeric severity (0-3).

        Supports legacy integer-based code that used IntEnum values.
        """
        for level, sev in _SEVERITY_ORDER.items():
            if sev == severity:
                return level
        raise ValueError(f"Unknown severity: {severity}")


_SEVERITY_ORDER: dict[EmergencyLevel, int] = {
    EmergencyLevel.NORMAL: 0,
    EmergencyLevel.LEVEL_1: 1,
    EmergencyLevel.LEVEL_2: 2,
    EmergencyLevel.LEVEL_3: 3,
}


class EmergencyScope(str, Enum):
    """Scope of emergency-mode application.

    ``REGIONAL`` — applied to a specific region/namespace only.
    ``GLOBAL`` — applied to all clusters.

    Used by ``regional_emergency`` to coordinate per-namespace state
    isolation with cross-region cascade detection.
    """

    REGIONAL = "regional"
    GLOBAL = "global"


@dataclass
class ScopedEmergencyState(SerializableMixin):
    """Per-namespace emergency state for regional isolation.

    Each region/namespace carries its own state so emergency activation
    in one region does not implicitly impose constraints on others.
    """

    namespace: str
    """Namespace identifier (e.g. ``'seoul'``, ``'tokyo'``, ``'oregon'``)."""

    emergency_level: EmergencyLevel = EmergencyLevel.NORMAL
    """Current emergency level for this namespace."""

    governance_mode: str = "NORMAL"
    """Current governance mode (``'STRICT'`` or ``'NORMAL'``)."""

    scope: EmergencyScope = EmergencyScope.REGIONAL
    """Application scope."""

    activated_at: datetime | None = None
    """Activation time."""

    activated_by: str | None = None
    """Activator (admin ID or ``'system'``)."""

    reason: str | None = None
    """Activation reason."""

    expires_at: datetime | None = None
    """Automatic expiry time (TTL)."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Additional metadata."""

    def is_active(self) -> bool:
        """Return True when emergency level is not NORMAL."""
        return self.emergency_level != EmergencyLevel.NORMAL

    def is_expired(self) -> bool:
        """Return True when ``expires_at`` is set and in the past."""
        if self.expires_at is None:
            return False
        return utc_now() >= self.expires_at


__all__ = [
    "EmergencyLevel",
    "EmergencyScope",
    "ScopedEmergencyState",
]
