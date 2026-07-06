"""
Regional Emergency — Multi-region namespace-scoped emergency isolation.

Extends ``emergency_mode/`` (single-instance lifecycle) with multi-region
capabilities: per-namespace state isolation, cross-region cascade detection,
partition reconciliation, and escalation audit trails.

Architecture:
    models.emergency.EmergencyLevel  (shared domain type)
            │
            ├── emergency_mode/        (single-instance lifecycle)
            └── regional_emergency/  ← this package (multi-region extension)

Components:
- AtomicStateQuery: Lua-based atomic Global+Regional state query
- EscalationAuditTrail: override decision audit logging
- NamespacedEmergencyTracker: per-namespace emergency state management
- RegionalCascadeDetector: multi-region cascade failure detection
- EmergencyHealthPenalty: Health Score penalty integration
- PartitionReconciliationService: network partition recovery

Reference:
    docs/baldur/middleware_system/73_NAMESPACE_AWARE_EMERGENCY.md
"""

# ScopedEmergencyState는 coordination/models.py에서 재export
# Phase 1: 안전 기반
from baldur.models.emergency import ScopedEmergencyState
from baldur.services.regional_emergency.atomic_query import (
    AtomicStateQuery,
    get_atomic_state_query,
)
from baldur.services.regional_emergency.cascade_detector import (
    CascadeDetectionEvent,
    RegionalCascadeDetector,
    get_cascade_detector,
    reset_cascade_detector,
)
from baldur.services.regional_emergency.escalation_audit import (
    EscalationAuditEntry,
    EscalationAuditTrail,
    EscalationDecisionType,
    get_escalation_audit_trail,
)

# Phase 3: 고급 기능
from baldur.services.regional_emergency.health_penalty import (
    EmergencyHealthPenalty,
    PenaltyBreakdown,
    get_emergency_health_penalty,
    reset_emergency_health_penalty,
)
from baldur.services.regional_emergency.partition_reconciliation import (
    PartitionReconciliationService,
    PartitionStatus,
    ReconciliationAction,
    ReconciliationResult,
    get_partition_reconciliation_service,
    reset_partition_reconciliation_service,
)

# Phase 2: 핵심 기능
from baldur.services.regional_emergency.tracker import (
    GLOBAL_NAMESPACE,
    NamespacedEmergencyTracker,
    get_namespaced_emergency_tracker,
    reset_namespaced_emergency_tracker,
)

__all__ = [
    # Phase 1
    "AtomicStateQuery",
    "get_atomic_state_query",
    "EscalationAuditTrail",
    "EscalationDecisionType",
    "EscalationAuditEntry",
    "get_escalation_audit_trail",
    # Phase 2
    "NamespacedEmergencyTracker",
    "get_namespaced_emergency_tracker",
    "reset_namespaced_emergency_tracker",
    "GLOBAL_NAMESPACE",
    "RegionalCascadeDetector",
    "CascadeDetectionEvent",
    "get_cascade_detector",
    "reset_cascade_detector",
    "ScopedEmergencyState",
    # Phase 3
    "EmergencyHealthPenalty",
    "PenaltyBreakdown",
    "get_emergency_health_penalty",
    "reset_emergency_health_penalty",
    "PartitionReconciliationService",
    "PartitionStatus",
    "ReconciliationResult",
    "ReconciliationAction",
    "get_partition_reconciliation_service",
    "reset_partition_reconciliation_service",
]
