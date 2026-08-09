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

    # Which state fields cross which way between the layers. Declared here, on
    # the assembled class, because the lanes live in several mixins and the
    # contract is a property of the pair rather than of any one lane.
    #
    # L1 -> L2, the generic state mirror driven by traffic. Narrow on purpose:
    # a mirror that also carried the manual-control fields would let one
    # process's unpinned snapshot clear an operator's pin in the shared store.
    # Pins reach L2 only through the manual-control primitives.
    _L2_STATE_MIRROR_FIELDS = (
        "state",
        "failure_count",
        "success_count",
        "opened_at",
    )

    # L2 -> L1, the wholesale hydration set (``hydrate_snapshot``), used where
    # the local row is absent or a full restore is intended. Carries the
    # manual-control fields, so a Block survives into a process that did not
    # take it.
    _L1_HYDRATION_FIELDS = (
        "state",
        "failure_count",
        "success_count",
        "opened_at",
        "last_failure_at",
        "manually_controlled",
        "controlled_by_id",
        "control_reason",
        "manual_override_expires_at",
        "metadata",
    )

    # Never bulk-transferred, and why:
    # - half_open_request_count / half_open_window_started_at are owned by the
    #   L2-authoritative atomic slot primitives; a bulk copy would race them.
    # - id / created_at / updated_at are layer-local identity and clock.
    # ``service_name`` is the row key rather than payload, so it belongs to
    # none of the three sets.
    _TRANSFER_EXCLUDED_FIELDS = (
        "id",
        "created_at",
        "updated_at",
        "half_open_request_count",
        "half_open_window_started_at",
    )


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
