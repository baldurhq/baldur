"""
In-Memory Repository Adapters

Provides in-memory implementations of repository interfaces for:
- Testing without database
- Standalone (non-framework) usage
- Development and prototyping

Storage Strategy:
- Memory (Default): works immediately on install — for tests and single-server deployments
- Layered (L1+L2): Memory + Redis/DB — high-throughput access in distributed environments

Module Structure (Refactored):
- circuit_breaker.py: InMemoryCircuitBreakerStateRepository
- layered_repository.py: LayeredCircuitBreakerStateRepository
- drift_reconciliation.py: DriftReconciler, DriftReconciliationResult
- shadow_logger.py: ShadowLogger, L2SyncFailureRecord

Status: Public
"""

from baldur.adapters.memory.canary_rollout import InMemoryCanaryRolloutStore
from baldur.adapters.memory.chaos_experiment import InMemoryChaosExperimentStore
from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
    LayeredCircuitBreakerStateRepository,
)
from baldur.adapters.memory.config_history import InMemoryConfigHistoryStore
from baldur.adapters.memory.cross_cluster import InMemoryCrossClusterStore
from baldur.adapters.memory.drift_reconciliation import (
    DriftReconciler,
    DriftReconciliationRecord,
    DriftReconciliationResult,
    get_drift_reconciler,
)
from baldur.adapters.memory.event_journal import (
    InMemoryEventJournalRepository,
)
from baldur.adapters.memory.failed_operation import (
    InMemoryFailedOperationRepository,
)
from baldur.adapters.memory.postmortem import (
    InMemoryPostmortemRepository,
)
from baldur.adapters.memory.recovery_session import (
    InMemoryRecoverySessionArchiveRepository,
)
from baldur.adapters.memory.security_incident import (
    InMemorySecurityIncidentRepository,
)
from baldur.adapters.memory.shadow_logger import (
    L2SyncFailureRecord,
    ShadowLogger,
    get_shadow_logger,
)

__all__ = [
    # Repositories
    "InMemoryFailedOperationRepository",
    "InMemoryCircuitBreakerStateRepository",
    "LayeredCircuitBreakerStateRepository",
    "InMemorySecurityIncidentRepository",
    "InMemoryPostmortemRepository",
    "InMemoryRecoverySessionArchiveRepository",
    "InMemoryEventJournalRepository",
    # Drift Reconciliation
    "DriftReconciler",
    "DriftReconciliationResult",
    "DriftReconciliationRecord",
    "get_drift_reconciler",
    # Shadow Logger
    "ShadowLogger",
    "L2SyncFailureRecord",
    "get_shadow_logger",
    # Domain State Stores
    "InMemoryConfigHistoryStore",
    "InMemoryCanaryRolloutStore",
    "InMemoryChaosExperimentStore",
    "InMemoryCrossClusterStore",
]
