"""
Integrity Package.

This package contains modular hash chain integrity implementations.
All classes are re-exported here for backward compatibility.
"""

from __future__ import annotations

# Daily Hash Anchor
from baldur.audit.integrity.anchor import (
    DailyHashAnchor,
)

# Cold Storage (Phase 6)
from baldur.audit.integrity.cold_storage import (
    AnchorColdStorage,
    ArchiveResult,
    LocalFileColdStorage,
)

# Factory
from baldur.audit.integrity.factory import (
    create_hash_chain_manager,
)

# Health Score (Phase 6)
from baldur.audit.integrity.health_score import (
    IntegrityHealthMetrics,
    IntegrityHealthScore,
    IntegrityRecoveryEvent,
    get_integrity_health_score,
    reset_integrity_health_score,
)

# Local Manager
from baldur.audit.integrity.local_manager import (
    HashChainManager,
    get_hash_chain_manager,
    reset_hash_chain_manager,
)

# Models and core functions
from baldur.audit.integrity.models import (
    IntegrityInfo,
    canonical_json_bytes,
    compute_hash,
)

# Protocol / Interface
from baldur.audit.integrity.protocol import (
    HashChainManagerProtocol,
)

# Reconciler
from baldur.audit.integrity.reconciler import (
    HashChainReconciler,
)

# Redis Manager
from baldur.audit.integrity.redis_manager import (
    RedisHashChainManager,
)

# Pending Sequence Manager
from baldur.audit.integrity.sequence import (
    PendingSequenceManager,
)

# Startup Sync
from baldur.audit.integrity.sync import (
    StartupHashChainSync,
)

# Verifier
from baldur.audit.integrity.verifier import (
    HashChainVerifier,
    verify_audit_log_integrity,
)

__all__ = [
    # Models
    "IntegrityInfo",
    "compute_hash",
    "canonical_json_bytes",
    # Protocol
    "HashChainManagerProtocol",
    # Verifier
    "HashChainVerifier",
    "verify_audit_log_integrity",
    # Managers
    "HashChainManager",
    "get_hash_chain_manager",
    "reset_hash_chain_manager",
    "RedisHashChainManager",
    # Factory
    "create_hash_chain_manager",
    # Pending Sequence
    "PendingSequenceManager",
    # Anchor
    "DailyHashAnchor",
    # Sync
    "StartupHashChainSync",
    # Reconciler
    "HashChainReconciler",
    # Cold Storage
    "AnchorColdStorage",
    "LocalFileColdStorage",
    "ArchiveResult",
    # Health Score
    "IntegrityHealthScore",
    "IntegrityHealthMetrics",
    "IntegrityRecoveryEvent",
    "get_integrity_health_score",
    "reset_integrity_health_score",
]
