"""
Control API Service - Models

Defines the ReasonClassification, ControlRequest, and ControlResponse data models.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from baldur.core.serializable import SerializableMixin

# =============================================================================
# Reason Classification
# =============================================================================


class ReasonClassification(str, Enum):
    """AI/system assigned reason classifications."""

    EXTERNAL_DEPENDENCY_FAILURE = "external-dependency-failure"
    INTERNAL_SERVICE_ERROR = "internal-service-error"
    MAINTENANCE_WINDOW = "maintenance-window"
    SLA_BREACH_MITIGATION = "sla-breach-mitigation"
    CHAOS_EXPERIMENT = "chaos-experiment"
    MANUAL_INTERVENTION = "manual-intervention"
    RECOVERY_PROCEDURE = "recovery-procedure"
    SECURITY_INCIDENT = "security-incident"
    UNKNOWN = "unknown"


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class ControlRequest:
    """Internal representation of a control API request."""

    service_name: str
    action: str
    reason: str
    environment: str
    ttl_minutes: int | None = None
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    metadata: dict = field(default_factory=dict)
    actor: str = "system"
    actor_role: str = "automation"


@dataclass
class ControlResponse(SerializableMixin):
    """Internal representation of a control API response."""

    status: str
    action_applied: str
    system_state: str = ""
    effective_until: str | None = None
    reason_classification: str = ""
    evidence: dict = field(default_factory=dict)
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    error_code: str = ""
    error_message: str = ""
    risk_level: str = ""

    def _post_serialize(self, data: dict[str, Any]) -> dict[str, Any]:
        """Remove falsy optional fields to match original to_dict() output."""
        # Pop falsy optional string/dict fields
        for key in (
            "system_state",
            "effective_until",
            "reason_classification",
            "evidence",
            "error_code",
            "error_message",
            "risk_level",
        ):
            if not data.get(key):
                data.pop(key, None)
        return super()._post_serialize(data)
