"""
Capacity Reservation — pre-securing capacity for scheduled events.

Registers scheduled events (coupon drops, flash sales, etc.) and orchestrates
pre-adjustment of existing modules (RateController/PoolWatchdog/Bulkhead/
GracefulDegradation) N minutes before the event.
"""

from baldur.services.capacity_reservation.event_calendar import (
    EffectiveMultipliers,
    EventCalendar,
    EventStatus,
    ScheduledEvent,
)
from baldur.services.capacity_reservation.pre_warmer import (
    AdjustmentRecord,
    CoolDownResult,
    PreWarmer,
    SafetyValveMetricsProvider,
    WarmUpResult,
)
from baldur.services.capacity_reservation.safety_valve_provider import (
    SystemMetricsSafetyValveProvider,
)
from baldur.services.capacity_reservation.service import (
    CapacityReservationService,
)

__all__ = [
    "AdjustmentRecord",
    "CapacityReservationService",
    "CoolDownResult",
    "EffectiveMultipliers",
    "EventCalendar",
    "EventStatus",
    "PreWarmer",
    "SafetyValveMetricsProvider",
    "ScheduledEvent",
    "SystemMetricsSafetyValveProvider",
    "WarmUpResult",
]
