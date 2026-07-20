"""
Policy Hooks — execution event observation module.

Provides Hook implementations that observe the PolicyComposer pipeline's
success/failure/rejection events. Every Hook follows the fail-open principle.

- AuditHook: audit logging
- SampledAuditHook: sampling-based audit logging
- MetricsHook: Prometheus metric collection
- EventBusHook: EventBus event publication
"""

from baldur.resilience.policies.hooks.audit import AuditHook
from baldur.resilience.policies.hooks.event_bus import EventBusHook
from baldur.resilience.policies.hooks.metrics import MetricsHook
from baldur.resilience.policies.hooks.sampled_audit import SampledAuditHook

__all__ = [
    "AuditHook",
    "EventBusHook",
    "MetricsHook",
    "SampledAuditHook",
]
