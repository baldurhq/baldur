"""
Cell Topology — logical traffic bulkhead management.

Assigns services/tenants to cells over a consistent hash ring and enforces
logical traffic isolation through a per-cell bulkhead.

A cell is a logical concurrency pool and shares the DB/Redis/cache cluster.
Physical region separation is handled by the multiregion module.
"""

from baldur.services.cell_topology.health import (
    CellHealthAggregator,
    CellHealthSnapshot,
    get_cell_health_aggregator,
    reset_cell_health_aggregator,
    setup_cell_health_scheduler,
)
from baldur.services.cell_topology.models import (
    CELL_STATE_PRIORITY,
    CellInfo,
    CellState,
)
from baldur.services.cell_topology.policy import (
    CellEvacuationPolicy,
    EvacuationRecord,
    get_cell_evacuation_policy,
    reset_cell_evacuation_policy,
)
from baldur.services.cell_topology.registry import (
    CellRegistry,
    get_cell_registry,
    register_cell_handlers,
    reset_cell_registry,
    unregister_cell_handlers,
)
from baldur.services.cell_topology.service import (
    CellTopologyService,
    get_cell_topology_service,
    reset_cell_topology_service,
)

__all__ = [
    "CELL_STATE_PRIORITY",
    "CellEvacuationPolicy",
    "CellHealthAggregator",
    "CellHealthSnapshot",
    "CellInfo",
    "CellRegistry",
    "CellState",
    "EvacuationRecord",
    "get_cell_evacuation_policy",
    "get_cell_health_aggregator",
    "CellTopologyService",
    "get_cell_registry",
    "get_cell_topology_service",
    "register_cell_handlers",
    "reset_cell_evacuation_policy",
    "reset_cell_health_aggregator",
    "reset_cell_registry",
    "reset_cell_topology_service",
    "setup_cell_health_scheduler",
    "unregister_cell_handlers",
]
