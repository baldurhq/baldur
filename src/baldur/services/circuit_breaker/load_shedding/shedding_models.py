"""
Load Shedding Data Models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from baldur.core.serializable import SerializableMixin


class SheddingState(str, Enum):
    """Load Shedding state."""

    INACTIVE = "inactive"  # Shedding disabled
    LEVEL_1 = "level_1"  # Stage 1 (low 50% restricted)
    LEVEL_2 = "level_2"  # Stage 2 (low+medium 80% restricted)
    LEVEL_3 = "level_3"  # Stage 3 (low+medium fully blocked)
    CUSTOM = "custom"  # User-defined level


@dataclass
class SheddingDecision:
    """
    Load Shedding decision result.

    Attributes:
        allow_request: Whether the request is allowed
        allowed_traffic_percent: Traffic ratio allowed for the service (0~100)
        is_shed: Whether it is a Shedding target
        reason: Decision reason
        current_level: Current Shedding level
        service_criticality: Service criticality
    """

    allow_request: bool = True
    allowed_traffic_percent: float = 100.0
    is_shed: bool = False
    reason: str = ""
    current_level: str | None = None
    service_criticality: str | None = None


@dataclass
class SheddingStatus(SerializableMixin):
    """
    Current Load Shedding state.

    Attributes:
        active: Whether Shedding is active
        current_state: Current Shedding state
        current_level_index: Current level index (0-based, -1=inactive)
        critical_error_rate: Average error rate of critical services
        shed_services: List of services currently under Shedding
        timestamp: State-lookup time
    """

    active: bool = False
    current_state: SheddingState = SheddingState.INACTIVE
    current_level_index: int = -1
    current_level_description: str = ""
    critical_error_rate: float = 0.0
    shed_services: list[str] = field(default_factory=list)
    shed_criticality: list[str] = field(default_factory=list)
    traffic_limit: float = 100.0
    timestamp: str = ""
    activated_at: str | None = None


@dataclass
class SheddingAuditEntry(SerializableMixin):
    """
    Load Shedding Audit log entry.

    Attributes:
        event_type: Event type
        timestamp: Event time
        previous_level: Previous level
        new_level: New level
        critical_error_rate: critical error rate
        affected_services: Affected services
        reason: Change reason
    """

    event_type: str  # SHEDDING_ACTIVATED, SHEDDING_LEVEL_CHANGED, SHEDDING_DEACTIVATED
    timestamp: str
    previous_level: int = -1
    new_level: int = -1
    critical_error_rate: float = 0.0
    affected_services: list[str] = field(default_factory=list)
    reason: str = ""
