"""
Layered Circuit Breaker State Repository Package.

Hybrid layered repository (L1 Memory + L2 Shared Storage).

Design principles:
- L1 (Local Memory): all decisions are made instantly in memory first (0.01ms)
- L2 (Shared Storage): Redis or DB is synchronized asynchronously in the background
- Timeout applied: if the L2 response is slow, give up immediately and operate on L1 only (Fail-Fast)
- Shadow Logging: record changes locally during an L2 outage
"""

from __future__ import annotations

from baldur.adapters.memory.drift_reconciliation import DriftReconciler
from baldur.interfaces.repositories import (
    CircuitBreakerStateRepository,
)

from .audit_helpers import AuditHelpersMixin

# Import base and mixins
from .base import LayeredRepositoryBase
from .drift_operations import DriftOperationsMixin
from .error_handling import ErrorHandlingMixin
from .l2_load import L2LoadMixin
from .l2_sync import L2SyncMixin
from .monitoring import MonitoringMixin
from .repository_operations import RepositoryOperationsMixin


class LayeredCircuitBreakerStateRepository(
    L2LoadMixin,
    ErrorHandlingMixin,
    DriftOperationsMixin,
    L2SyncMixin,
    RepositoryOperationsMixin,
    MonitoringMixin,
    AuditHelpersMixin,
    LayeredRepositoryBase,
    CircuitBreakerStateRepository,
):
    """
    Hybrid layered repository (L1 Memory + L2 Shared Storage).

    Advantages:
    - Even if external dependencies (Redis/DB) die briefly, the system keeps running on L1 only
    - Maintains eventual consistency even in distributed environments
    - Does not intrude on the host DB (L2 is opt-in)

    Usage:
        # Memory only (default, single server)
        repo = LayeredCircuitBreakerStateRepository()

        # Add Redis as L2 (distributed environment)
        from baldur.adapters.redis import RedisCircuitBreakerStateRepository
        repo = LayeredCircuitBreakerStateRepository(
            l2_repo=RedisCircuitBreakerStateRepository(),
            sync_interval_seconds=5,
        )
    """

    pass


def reset_layered_repository_executor() -> None:
    """Shutdown shared ThreadPoolExecutor for test isolation.

    Uses cancel_futures=True to cancel queued tasks (Python 3.9+).
    Non-daemon executor threads block process termination if not shut down.
    Precedent: core/timeout_executor.py uses the same strategy.
    """
    executor = LayeredRepositoryBase._executor
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)
        LayeredRepositoryBase._executor = None


__all__ = [
    "LayeredCircuitBreakerStateRepository",
    # Base and mixins for extension
    "LayeredRepositoryBase",
    "L2LoadMixin",
    "ErrorHandlingMixin",
    "DriftOperationsMixin",
    "L2SyncMixin",
    "RepositoryOperationsMixin",
    "MonitoringMixin",
    "AuditHelpersMixin",
    "reset_layered_repository_executor",
]
