"""
Bulkhead Pattern - prevent cascading failures through resource isolation.

The Bulkhead pattern, originating from ship design, isolates resources so that
a failure in one component does not propagate to others.

Key components:
- SemaphoreBulkhead: semaphore-based concurrency limit (I/O bound)
- AsyncSemaphoreBulkhead: async semaphore-based bulkhead
- BulkheadRegistry: per-domain bulkhead management
- @bulkhead: sync/async auto-dispatching decorator
- BulkheadPolicy: ResiliencePolicy adapter for composition

Usage:
    from baldur.services.bulkhead import (
        SemaphoreBulkhead,
        bulkhead,
        get_bulkhead_registry,
    )
    from baldur.core.connection_health import ConnectionType

    # Direct use
    bulkhead = SemaphoreBulkhead("my_domain", max_concurrent=10)
    with bulkhead.acquire(timeout=5.0):
        do_work()

    # Registry use
    registry = get_bulkhead_registry()
    db_bulkhead = registry.get(ConnectionType.DATABASE)
    with db_bulkhead.acquire():
        db_operation()

    # Decorator use
    @bulkhead(ConnectionType.DATABASE)
    def db_operation():
        pass

    @bulkhead(ConnectionType.DATABASE)
    async def async_db_operation():
        pass

Status: Public
"""

from baldur.services.bulkhead.async_semaphore import AsyncSemaphoreBulkhead
from baldur.services.bulkhead.base import (
    Bulkhead,
    BulkheadState,
    BulkheadType,
)
from baldur.services.bulkhead.decorator import (
    bulkhead,
    bulkhead_for_cache,
    bulkhead_for_database,
)
from baldur.services.bulkhead.exceptions import (
    BulkheadError,
    BulkheadFullError,
    BulkheadNotFoundError,
    BulkheadTimeoutError,
)
from baldur.services.bulkhead.metrics import (
    BulkheadMetricsUpdater,
    get_metrics_updater,
    increment_rejected_count,
    start_metrics_updater,
    stop_metrics_updater,
    update_bulkhead_metrics,
)
from baldur.services.bulkhead.policy import (
    AsyncBulkheadPolicy,
    BulkheadPolicy,
    async_bulkhead_policy,
    bulkhead_policy,
)
from baldur.services.bulkhead.registry import (
    BulkheadRegistry,
    get_bulkhead_registry,
    reset_bulkhead_registry,
)
from baldur.services.bulkhead.semaphore import SemaphoreBulkhead

__all__ = [
    # Base
    "Bulkhead",
    "BulkheadState",
    "BulkheadType",
    # Implementations
    "SemaphoreBulkhead",
    "AsyncSemaphoreBulkhead",
    # Registry
    "BulkheadRegistry",
    "get_bulkhead_registry",
    "reset_bulkhead_registry",
    # Exceptions
    "BulkheadError",
    "BulkheadFullError",
    "BulkheadNotFoundError",
    "BulkheadTimeoutError",
    # Decorator
    "bulkhead",
    "bulkhead_for_database",
    "bulkhead_for_cache",
    # Metrics
    "BulkheadMetricsUpdater",
    "get_metrics_updater",
    "start_metrics_updater",
    "stop_metrics_updater",
    "update_bulkhead_metrics",
    "increment_rejected_count",
    # Policy
    "BulkheadPolicy",
    "AsyncBulkheadPolicy",
    "bulkhead_policy",
    "async_bulkhead_policy",
]
