"""Cascade Event domain model.

Framework-agnostic dataclass replacing AbstractCascadeEventArchive's data role.
Django Abstract Model remains as adapter-internal persistence detail.

Reference:
    docs/baldur/middleware_system/366_MODEL_LAYER_SEPARATION.md
    models/cascade_event_archive.py (original Django Abstract Model)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from baldur.core.serializable import SerializableMixin

__all__ = [
    "TriggerType",
    "CascadeEventData",
]


class TriggerType(str, Enum):
    """Cascade trigger type."""

    EMERGENCY_LEVEL_CHANGED = "EMERGENCY_LEVEL_CHANGED"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"
    MANUAL_ACTIVATION = "MANUAL_ACTIVATION"
    CANARY_ROLLBACK = "CANARY_ROLLBACK"
    CIRCUIT_BREAKER_OPENED = "CIRCUIT_BREAKER_OPENED"
    GOVERNANCE_MODE_CHANGED = "GOVERNANCE_MODE_CHANGED"
    ERROR_BUDGET_EXHAUSTED = "ERROR_BUDGET_EXHAUSTED"
    RECOVERY_STARTED = "RECOVERY_STARTED"
    DEESCALATION = "DEESCALATION"


@dataclass
class CascadeEventData(SerializableMixin):
    """Framework-agnostic Cascade Event domain model.

    Replaces AbstractCascadeEventArchive's data role.
    Django Model remains as adapter-internal persistence detail.
    """

    cascade_id: str
    """Unique Cascade Event ID (e.g., cascade-evt-abc123)."""

    namespace: str
    """Namespace where the event occurred."""

    trigger_type: str
    """Trigger type that initiated the cascade (TriggerType enum value)."""

    current_hash: str
    """SHA-256 hash of the current Cascade Event."""

    timestamp: datetime | None = None
    """Event occurrence time."""

    trigger_details: dict[str, Any] = field(default_factory=dict)
    """Trigger details (old_level, new_level, etc.)."""

    effects: list[dict[str, Any]] = field(default_factory=list)
    """List of cascading effects."""

    causation_chain: list[str] = field(default_factory=list)
    """Causation event ID chain [trigger_id, effect_1_id, ...]."""

    previous_hash: str = ""
    """Hash of the previous Cascade Event (chain link)."""

    total_effects: int = 0
    """Total number of effects."""

    success_count: int = 0
    """Number of successful effects."""

    failure_count: int = 0
    """Number of failed effects."""

    archived_at: datetime | None = None
    """Time when archived from Redis to PostgreSQL."""

    external_trace: dict[str, Any] | None = None
    """W3C Trace Context / OpenTelemetry integration info."""

    version: str = "1.0"
    """Data schema version."""

    is_test: bool = False
    """Whether this is a test environment event."""

    def verify_hash_integrity(self) -> bool:
        """Verify SHA-256 hash integrity against stored current_hash."""
        from baldur.utils.serialization import fast_canonical_dumps

        content = {
            "id": self.cascade_id,
            "trigger": {
                "trigger_type": self.trigger_type,
                "details": self.trigger_details,
            },
            "effects": self.effects,
            "namespace": self.namespace,
            "timestamp": self.timestamp.isoformat() if self.timestamp else "",
            "previous_hash": self.previous_hash,
        }
        computed_hash = hashlib.sha256(fast_canonical_dumps(content)).hexdigest()

        return computed_hash == self.current_hash

    def get_causation_chain_display(self) -> str:
        """Format causation chain as 'event1 → event2 → event3'."""
        if not self.causation_chain:
            return "No chain"
        return " → ".join(self.causation_chain)

    @classmethod
    def from_cascade_event(cls, event: Any) -> CascadeEventData:
        """Create from CascadeEvent (Redis Hot Tier).

        Args:
            event: CascadeEvent instance

        Returns:
            CascadeEventData instance
        """
        # Parse timestamp
        ts = event.timestamp
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))

        return cls(
            cascade_id=event.id,
            namespace=event.namespace,
            trigger_type=event.trigger.trigger_type,
            trigger_details=event.trigger.details,
            effects=[e.to_dict() for e in event.effects],
            causation_chain=event.get_causation_chain(),
            previous_hash=event.previous_hash,
            current_hash=event.current_hash,
            total_effects=event.total_effects,
            success_count=event.success_count,
            failure_count=event.failure_count,
            timestamp=ts,
            external_trace=(
                event.external_trace.to_dict() if event.external_trace else None
            ),
            version=getattr(event, "version", "1.0"),
            is_test=getattr(event, "is_test", False),
        )
