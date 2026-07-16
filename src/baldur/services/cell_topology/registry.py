"""
Cell Registry — consistent-hash-based cell assignment.

Assigns services/tenants to cells and auto-manages the per-cell bulkheads.

Dependencies:
- BulkheadRegistry: per-cell bulkhead creation (when bulkhead_isolation_enabled=True)
- CellTopologySettings: settings injection
"""

from __future__ import annotations

import hashlib
import threading

import structlog

from baldur.services.cell_topology.models import (
    CellInfo,
    CellState,
)
from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.event_bus.emitter import EventEmitterMixin
from baldur.settings.cell_topology import CellTopologySettings

logger = structlog.get_logger()

VNODES_PER_CELL = 150
"""Number of virtual nodes per cell on the consistent hash ring."""


class CellRegistry(EventEmitterMixin):
    """
    Cell Registry — consistent-hash-ring-based cell assignment.

    Features:
    1. Assign services/tenants to cells via a consistent hash ring
    2. Cell state management (ACTIVE/WARMUP/DRAINING/ISOLATED)
    3. BulkheadRegistry integration — automatic per-cell bulkhead creation
    4. Cell listing and state inspection
    5. L1 (memory) + L2 (Redis) two-tier state synchronization
    6. Service-heartbeat-based dynamic assignment/expiry
    7. Runtime dynamic ring resizing

    Usage:
        registry = get_cell_registry()
        cell_id = registry.get_cell_for_key("user-12345")
        cell_info = registry.get_cell_info(cell_id)
    """

    _event_source = "cell_registry"

    def __init__(self, settings: CellTopologySettings | None = None):
        """
        Args:
            settings: Cell topology settings
        """
        from baldur.settings.cell_topology import get_cell_topology_settings

        self._settings = settings or get_cell_topology_settings()
        self._lock = threading.RLock()
        self._cells: dict[str, CellInfo] = {}
        self._hash_ring: list[tuple[int, str]] = []

        self._initialize_cells()

    def _initialize_cells(self) -> None:
        """Initialize cells and build the hash ring."""
        for i in range(self._settings.cell_count):
            cell_id = f"{self._settings.cell_prefix}-{i}"
            self._cells[cell_id] = CellInfo(cell_id=cell_id)

        self._build_hash_ring()

        # Automatic bulkhead registration
        if self._settings.bulkhead_isolation_enabled:
            self._register_cell_bulkheads()

        logger.info(
            "cellregistry.initialized_cells",
            cell_count=self._settings.cell_count,
            bulkhead_isolation_enabled=self._settings.bulkhead_isolation_enabled,
        )

    def _build_hash_ring(self) -> None:
        """
        Build the consistent hash ring.

        Creates virtual nodes (vnodes) for each cell to guarantee uniform
        distribution. Builds a fresh list copy-on-write, then swaps the
        reference atomically (GIL-safe).
        """
        ring: list[tuple[int, str]] = []

        for cell_id in self._cells:
            for vnode_idx in range(VNODES_PER_CELL):
                key = f"{cell_id}:vnode-{vnode_idx}"
                hash_val = self._hash(key)
                ring.append((hash_val, cell_id))

        ring.sort(key=lambda x: x[0])
        # Atomic reference swap (GIL-safe)
        self._hash_ring = ring

    @staticmethod
    def _hash(key: str) -> int:
        """SHA-256-based hash."""
        return int(hashlib.sha256(key.encode()).hexdigest(), 16)

    def get_cell_for_key(self, key: str) -> str:
        """
        Assign a key to a cell on the consistent hash ring.

        DRAINING/ISOLATED cells are skipped; the next ACTIVE cell is returned.

        Args:
            key: Assignment key (service name, tenant ID, user_id, etc.)

        Returns:
            cell_id (e.g. "cell-3")
        """
        if not self._settings.enabled:
            return f"{self._settings.cell_prefix}-0"  # default cell when disabled

        hash_val = self._hash(key)
        ring = self._hash_ring

        if not ring:
            return f"{self._settings.cell_prefix}-0"

        # Locate the position via binary search
        lo, hi = 0, len(ring) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if ring[mid][0] < hash_val:
                lo = mid + 1
            else:
                hi = mid

        # Walk the ring looking for an ACTIVE/WARMUP cell
        for offset in range(len(ring)):
            idx = (lo + offset) % len(ring)
            cell_id = ring[idx][1]
            cell = self._cells.get(cell_id)
            if not cell:
                continue

            if cell.state == CellState.ACTIVE:
                return cell_id

            # WARMUP cell: percentage-based probabilistic routing
            if cell.state == CellState.WARMUP:
                if (hash_val % 100) < cell.warmup_percentage:
                    return cell_id
                continue  # outside the percentage — move on to the next ACTIVE cell

        # All cells inactive — return the first match (last resort)
        return ring[lo % len(ring)][1]

    def get_cell_info(self, cell_id: str) -> CellInfo | None:
        """Look up cell info."""
        return self._cells.get(cell_id)

    def get_all_cells(self) -> dict[str, CellInfo]:
        """Look up all cell info."""
        return dict(self._cells)

    def get_active_cells(self) -> list[str]:
        """List of ACTIVE cell IDs."""
        return [
            cell_id
            for cell_id, info in self._cells.items()
            if info.state == CellState.ACTIVE
        ]

    def set_cell_state(self, cell_id: str, state: CellState, reason: str = "") -> bool:
        """
        Change a cell's state.

        L1→L2→emit ordering (doc 388, Q7):
        1. L1 dict write
        2. L2 Redis sync (_sync_state_to_redis)
        3. EventBus emit (cross-pod notification)

        Args:
            cell_id: Cell identifier
            state: New state
            reason: Reason for the change

        Returns:
            Whether the change succeeded
        """
        import time

        with self._lock:
            cell = self._cells.get(cell_id)
            if not cell:
                logger.warning(
                    "cell_registry.cell_not_found",
                    cell_id=cell_id,
                )
                return False

            old_state = cell.state
            cell.state = state
            cell.updated_at = time.time()
            cell.metadata["last_state_change"] = {
                "from": old_state.value,
                "to": state.value,
                "reason": reason,
            }
            cell.metadata["last_state_change_time"] = cell.updated_at

            logger.info(
                "cell_registry.state_changed",
                cell_id=cell_id,
                old_state=old_state.value,
                new_state=state.value,
                reason=reason,
            )

            # L2 sync before emit (write-then-notify, Q7)
            self._sync_state_to_redis(cell_id)

            self._emit_event(
                EventType.CELL_STATE_CHANGED,
                {
                    "cell_id": cell_id,
                    "old_state": old_state.value,
                    "new_state": state.value,
                    "reason": reason,
                },
            )
            return True

    def update_health_score(self, cell_id: str, score: float) -> None:
        """Update a cell's health score. Called by CellHealthAggregator."""
        cell = self._cells.get(cell_id)
        if cell:
            cell.health_score = max(0.0, min(1.0, score))

    def assign_service(self, service_name: str) -> str:
        """
        Assign a service to a cell and refresh its heartbeat.

        Not called per request — the CellTagger middleware's background
        heartbeat thread calls this on a 30-second cadence. On TTL expiry
        (5 minutes) the CellHealthAggregator evicts the service automatically.

        Args:
            service_name: Service name

        Returns:
            Assigned cell_id
        """
        cell_id = self.get_cell_for_key(service_name)
        cell = self._cells.get(cell_id)
        if cell:
            cell.assigned_services.add(service_name)
            # Record the heartbeat in L2 (Redis) — TTL auto-expiry
            self._record_service_heartbeat(cell_id, service_name)
        return cell_id

    def _record_service_heartbeat(self, cell_id: str, service_name: str) -> None:
        """
        Record a service heartbeat via Redis ZADD.

        Key: baldur:cell:{cell_id}:services
        Score: current timestamp
        TTL: a service with no heartbeat for 5 minutes expires automatically.
        """
        try:
            import time

            from baldur.adapters.redis import get_redis_client

            redis = get_redis_client()
            if redis is None:
                return
            key = f"baldur:cell:{cell_id}:services"
            redis.zadd(key, {service_name: time.time()})
        except Exception as e:
            logger.debug(
                "cell_registry.heartbeat_failed",
                error=e,
            )

    def _evict_expired_services(
        self, cell_id: str, ttl_seconds: float = 300.0
    ) -> list[str]:
        """
        Evict TTL-expired services from a cell.

        Called by CellHealthAggregator at reconciliation time. Detects
        services with no heartbeat for 5 minutes (300s) via ZRANGEBYSCORE.

        Returns:
            List of evicted services
        """
        evicted: list[str] = []
        try:
            import time

            from baldur.adapters.redis import get_redis_client

            redis = get_redis_client()
            if redis is None:
                return evicted
            key = f"baldur:cell:{cell_id}:services"
            cutoff = time.time() - ttl_seconds

            # Query expired services
            expired = redis.zrangebyscore(key, "-inf", cutoff)
            if expired:
                redis.zrem(key, *expired)

                # Also remove from L1 memory
                cell = self._cells.get(cell_id)
                if cell:
                    for svc in expired:
                        svc_str = svc if isinstance(svc, str) else svc.decode()
                        cell.assigned_services.discard(svc_str)
                        evicted.append(svc_str)

                logger.info(
                    "cell_registry.services_evicted",
                    evicted_count=len(evicted),
                    cell_id=cell_id,
                    evicted=evicted,
                )
        except Exception as e:
            logger.debug(
                "service.eviction_failed",
                cell_id=cell_id,
                error=e,
            )

        return evicted

    def _register_cell_bulkheads(self) -> None:
        """
        Register per-cell bulkheads with the BulkheadRegistry.

        Uses BulkheadRegistry.get_or_create() to auto-create the per-cell
        compartments.
        """
        try:
            from baldur.services.bulkhead.registry import get_bulkhead_registry

            bulkhead_registry = get_bulkhead_registry()
            for cell_id in self._cells:
                bulkhead_registry.get_or_create(
                    name=cell_id,
                    max_concurrent=self._settings.bulkhead_max_concurrent_per_cell,
                    bulkhead_type=self._settings.bulkhead_type,
                )

            logger.info(
                "cell_registry.bulkheads_registered",
                cells_count=len(self._cells),
                bulkhead_max_concurrent_per_cell=self._settings.bulkhead_max_concurrent_per_cell,
            )
        except Exception as e:
            logger.exception(
                "cell.bulkhead_registration_failed",
                error=e,
            )

    # ── L1/L2 synchronization ───────────────────────────────

    def _sync_state_to_redis(self, cell_id: str) -> None:
        """
        Write a cell's state to L2 (Redis hash).

        Called from set_cell_state(), add_cells(), remove_cells().
        Cross-pod propagation goes through the EventBus (Q2 — raw Pub/Sub removed).
        """
        try:
            from baldur.adapters.redis import get_redis_client

            redis = get_redis_client()
            if redis is None:
                return
            cell = self._cells.get(cell_id)
            if not cell:
                return

            key = f"baldur:cell:state:{cell_id}"
            redis.hset(key, mapping=cell.to_l2_dict())
        except Exception as e:
            logger.warning(
                "cell_registry.state_sync_failed",
                cell_id=cell_id,
                error=e,
            )

    def _load_all_states_from_redis(self) -> int:
        """
        Load every cell's state from L2 (Redis) into L1 (anti-entropy reconciliation).

        One hydration pass at worker startup plus periodic calls from the
        anti-entropy daemon thread. Compensates for missed EventBus events.

        Uses CellInfo.apply_l2_dict() for LWW+MRW hybrid comparison (Q19).

        Returns:
            Number of synchronized cells
        """
        synced = 0
        try:
            from baldur.adapters.redis import get_redis_client

            redis = get_redis_client()
            if redis is None:
                return synced

            # Key snapshot for thread safety (Q18)
            for cell_id in list(self._cells.keys()):
                key = f"baldur:cell:state:{cell_id}"
                data = redis.hgetall(key)
                if not data:
                    continue

                # Defensive .get() for concurrent deletion (Q20)
                cell = self._cells.get(cell_id)
                if cell is None:
                    continue

                if cell.apply_l2_dict(data):
                    synced += 1
        except Exception as e:
            logger.warning(
                "cell_registry.state_load_failed",
                error=e,
            )

        return synced

    def _load_single_state_from_redis(self, cell_id: str) -> bool:
        """
        Load a single cell's state from L2 (Redis) into L1.

        Called from the EventBus handler (_on_cell_state_event).
        Uses CellInfo.apply_l2_dict() for LWW+MRW hybrid comparison (Q19).

        Args:
            cell_id: Cell identifier.

        Returns:
            True if L1 state was updated.
        """
        try:
            from baldur.adapters.redis import get_redis_client

            redis = get_redis_client()
            if redis is None:
                return False

            key = f"baldur:cell:state:{cell_id}"
            data = redis.hgetall(key)
            if not data:
                return False

            # Defensive .get() — cell may have been removed concurrently (Q20)
            cell = self._cells.get(cell_id)
            if cell is None:
                return False

            return cell.apply_l2_dict(data)
        except Exception as e:
            logger.warning(
                "cell_registry.single_state_load_failed",
                cell_id=cell_id,
                error=e,
            )
            return False

    def _on_cell_state_event(self, event: object) -> None:
        """
        EventBus handler for CELL_STATE_CHANGED.

        Invalidation pattern (Q1): event = change notification,
        actual data = L2 re-fetch via _load_single_state_from_redis().
        Does not distinguish self-emitted vs cross-pod events (Q11).
        """
        data = getattr(event, "data", None) or {}
        cell_id = data.get("cell_id", "")
        if not cell_id:
            return
        self._load_single_state_from_redis(cell_id)

    # ── Dynamic scaling ─────────────────────────────────────

    def add_cells(self, count: int) -> list[str]:
        """
        Add cells at runtime and rebuild the hash ring.

        New cells start in the WARMUP state and take on traffic
        progressively.

        Args:
            count: Number of cells to add

        Returns:
            List of added cell_ids
        """
        with self._lock:
            added: list[str] = []
            current_count = len(self._cells)

            for i in range(count):
                cell_id = f"{self._settings.cell_prefix}-{current_count + i}"
                cell = CellInfo(
                    cell_id=cell_id,
                    state=CellState.WARMUP,
                    warmup_percentage=self._settings.warmup_initial_percentage,
                )
                self._cells[cell_id] = cell
                added.append(cell_id)

            # Copy-on-write ring rebuild (GIL-safe atomic swap)
            self._build_hash_ring()

            # Register bulkheads for the new cells
            if self._settings.bulkhead_isolation_enabled:
                self._register_cell_bulkheads()

            # Record the new cells' states in L2
            for cell_id in added:
                self._sync_state_to_redis(cell_id)

            logger.info(
                "added.cells_warmup_total",
                added_count=count,
                added=added,
                total_cells=len(self._cells),
            )
            return added

    def remove_cells(self, cell_ids: list[str]) -> list[str]:
        """
        DRAINING → ISOLATED → delete, before removing a cell.

        Does not delete immediately — only transitions to DRAINING.
        The actual deletion is invoked by CellEvacuationPolicy after the
        drain completes.

        Args:
            cell_ids: List of cell IDs to remove

        Returns:
            List of cell_ids transitioned to DRAINING
        """
        drained: list[str] = []
        for cell_id in cell_ids:
            if self.set_cell_state(cell_id, CellState.DRAINING, reason="scale_in"):
                self._sync_state_to_redis(cell_id)
                drained.append(cell_id)
        return drained


# =============================================================================
# Singleton
# =============================================================================

_registry: CellRegistry | None = None
_registry_lock = threading.Lock()


def get_cell_registry() -> CellRegistry:
    """Return the CellRegistry singleton."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = CellRegistry()
    return _registry


def reset_cell_registry() -> None:
    """Reset the singleton (for testing)."""
    global _registry
    with _registry_lock:
        if _registry is not None:
            try:
                unregister_cell_handlers(_registry)
            except Exception:
                pass
        _registry = None


# =============================================================================
# EventBus Handler Registration (Q14 — module-level, like mesh_coordinator.py)
# =============================================================================


def register_cell_handlers(registry: CellRegistry) -> None:
    """Register CellRegistry as EventBus subscriber for CELL_STATE_CHANGED."""
    from baldur.services.event_bus import get_event_bus

    bus = get_event_bus()
    bus.subscribe(EventType.CELL_STATE_CHANGED, registry._on_cell_state_event)
    logger.info("cell_registry.handlers_registered")


def unregister_cell_handlers(registry: CellRegistry) -> None:
    """Unregister CellRegistry EventBus subscriptions."""
    from baldur.services.event_bus import get_event_bus

    bus = get_event_bus()
    bus.unsubscribe(EventType.CELL_STATE_CHANGED, registry._on_cell_state_event)
    logger.info("cell_registry.handlers_unregistered")
