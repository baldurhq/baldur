"""
Connection Health Monitor

Tracks health of different connection types independently:
- Database connections
- Cache connections (Redis, Memcached)
- External API connections

Enables graceful degradation when partial failures occur.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger().bind(component="connection_health_monitor")


class ConnectionType(str, Enum):
    """Types of connections to monitor"""

    DATABASE = "database"
    CACHE = "cache"
    EXTERNAL_API = "external_api"
    MESSAGE_QUEUE = "message_queue"


class ConnectionStatus(str, Enum):
    """Health status of a connection"""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class ConnectionHealth:
    """Health status of a single connection"""

    connection_type: ConnectionType
    name: str
    status: ConnectionStatus = ConnectionStatus.UNKNOWN
    last_check: datetime | None = None
    last_success: datetime | None = None
    last_failure: datetime | None = None
    consecutive_failures: int = 0
    error_message: str = ""
    latency_ms: float | None = None


@dataclass
class PartitionState:
    """Current state of network partitions"""

    db_available: bool = True
    cache_available: bool = True
    external_apis: dict[str, bool] = field(default_factory=dict)
    detected_at: datetime | None = None
    bulkhead_states: dict[str, dict] = field(default_factory=dict)
    """Bulkhead state info (active_count, max_concurrent, etc. per ConnectionType)"""

    @property
    def is_partial_partition(self) -> bool:
        """True if some but not all connections are down"""
        statuses = [self.db_available, self.cache_available] + list(
            self.external_apis.values()
        )
        # Partial partition = some up AND some down
        return any(statuses) and not all(statuses)

    @property
    def is_full_partition(self) -> bool:
        """True if all connections are down"""
        statuses = [self.db_available, self.cache_available] + list(
            self.external_apis.values()
        )
        return not any(statuses) if statuses else False

    @property
    def is_healthy(self) -> bool:
        """True if all connections are healthy"""
        statuses = [self.db_available, self.cache_available] + list(
            self.external_apis.values()
        )
        return all(statuses) if statuses else True

    @property
    def has_bulkhead_pressure(self) -> bool:
        """True if any bulkhead has high utilization (>80%)"""
        for state in self.bulkhead_states.values():
            utilization = state.get("utilization_percent", 0)
            if utilization > 80:
                return True
        return False


class ConnectionHealthMonitor(ABC):
    """Abstract interface for connection health monitoring"""

    @abstractmethod
    def check_health(
        self, connection_type: ConnectionType, name: str
    ) -> ConnectionHealth:
        """Check health of a specific connection"""
        pass

    @abstractmethod
    def get_partition_state(self) -> PartitionState:
        """Get current partition state across all connections"""
        pass

    @abstractmethod
    def register_health_check(
        self, connection_type: ConnectionType, name: str, check_fn: Callable[[], bool]
    ) -> None:
        """Register a health check function for a connection"""
        pass

    @abstractmethod
    def unregister_health_check(
        self,
        connection_type: ConnectionType,
        name: str,
    ) -> bool:
        """Unregister a health check. Returns True if was registered."""
        pass


class DefaultConnectionHealthMonitor(ConnectionHealthMonitor):
    """Default implementation of connection health monitoring"""

    def __init__(self, failure_threshold: int = 3):
        """
        Initialize the connection health monitor.

        Args:
            failure_threshold: Number of consecutive failures before marking UNHEALTHY
        """
        self._health_checks: dict[str, Callable[[], bool]] = {}
        self._health_states: dict[str, ConnectionHealth] = {}
        self._failure_threshold = failure_threshold

        # Simulation overrides for chaos testing
        self._simulation_overrides: dict[str, ConnectionHealth] = {}
        self._partition_override: PartitionState | None = None
        self._simulation_experiment_id: str | None = None

    @classmethod
    def from_settings(
        cls, settings=None, **overrides
    ) -> DefaultConnectionHealthMonitor:
        """
        Create an instance from settings.

        Args:
            settings: PoolMonitorSettings instance (auto-loaded if None)
            **overrides: Per-field overrides

        Returns:
            DefaultConnectionHealthMonitor: settings-based instance
        """
        from baldur.settings.pool_monitor import get_pool_monitor_settings

        s = settings or get_pool_monitor_settings()
        return cls(
            failure_threshold=overrides.get(
                "failure_threshold", s.connection_failure_threshold
            ),
        )

    def set_simulation_override(
        self,
        connection_type: ConnectionType,
        name: str,
        status: ConnectionStatus,
        experiment_id: str | None = None,
    ) -> None:
        """
        Set a simulated status for a specific connection.

        Args:
            connection_type: Connection type (DATABASE, CACHE, EXTERNAL_API)
            name: Connection name
            status: Connection status to force
            experiment_id: Related chaos experiment ID (for audit tracing)

        Example:
            monitor.set_simulation_override(
                ConnectionType.DATABASE,
                "primary",
                ConnectionStatus.UNHEALTHY,
                experiment_id="exp-123",
            )
        """
        key = f"{connection_type.value}:{name}"
        self._simulation_overrides[key] = ConnectionHealth(
            connection_type=connection_type,
            name=name,
            status=status,
        )
        self._simulation_experiment_id = experiment_id
        logger.info(
            "connection_health.simulation_override_set",
            override_key=key,
            connection_health_status=status.value,
            experiment_id=experiment_id,
        )

    def set_partition_simulation(
        self,
        partition_state: PartitionState,
        experiment_id: str | None = None,
    ) -> None:
        """
        Set a simulated network-partition state.

        Args:
            partition_state: Partition state to force
            experiment_id: Related chaos experiment ID (for audit tracing)
        """
        self._partition_override = partition_state
        self._simulation_experiment_id = experiment_id
        logger.info(
            "connection_health.partition_simulation_set",
            is_partial_partition=partition_state.is_partial_partition,
            is_full_partition=partition_state.is_full_partition,
            experiment_id=experiment_id,
        )

    def clear_all_simulation_overrides(self) -> None:
        """
        Clear all simulation overrides.
        """
        self._simulation_overrides.clear()
        self._partition_override = None
        self._simulation_experiment_id = None
        logger.info("connection_health.simulation_overrides_cleared")

    def is_simulation_active(self) -> bool:
        """Check whether any simulation override is active."""
        return bool(self._simulation_overrides) or self._partition_override is not None

    def get_simulation_experiment_id(self) -> str | None:
        """Return the experiment ID tied to the current simulation."""
        return self._simulation_experiment_id

    def register_health_check(
        self, connection_type: ConnectionType, name: str, check_fn: Callable[[], bool]
    ) -> None:
        """Register a health check function for monitoring."""
        key = f"{connection_type.value}:{name}"
        self._health_checks[key] = check_fn
        self._health_states[key] = ConnectionHealth(
            connection_type=connection_type,
            name=name,
        )

    def unregister_health_check(
        self,
        connection_type: ConnectionType,
        name: str,
    ) -> bool:
        """Unregister a health check."""
        key = f"{connection_type.value}:{name}"
        if key in self._health_checks:
            del self._health_checks[key]
            del self._health_states[key]
            return True
        return False

    def check_health(
        self, connection_type: ConnectionType, name: str
    ) -> ConnectionHealth:
        """
        Check health of a specific connection.

        Supports simulation overrides.
        """
        key = f"{connection_type.value}:{name}"

        # Simulation override check
        if key in self._simulation_overrides:
            logger.debug(
                "connection_health.simulated_health_returned", override_key=key
            )
            return self._simulation_overrides[key]

        if key not in self._health_checks:
            return ConnectionHealth(
                connection_type=connection_type,
                name=name,
                status=ConnectionStatus.UNKNOWN,
            )

        health = self._health_states[key]
        check_fn = self._health_checks[key]

        try:
            start = utc_now()
            success = check_fn()
            end = utc_now()

            health.last_check = end
            health.latency_ms = (end - start).total_seconds() * 1000

            if success:
                health.status = ConnectionStatus.HEALTHY
                health.last_success = end
                health.consecutive_failures = 0
                health.error_message = ""
            else:
                self._record_failure(health, "Health check returned False")

        except Exception as e:
            health.last_check = utc_now()
            self._record_failure(health, str(e))

        return health

    def _record_failure(self, health: ConnectionHealth, error: str) -> None:
        """Record a failure and update status accordingly."""
        health.consecutive_failures += 1
        health.last_failure = utc_now()
        health.error_message = error

        if health.consecutive_failures >= self._failure_threshold:
            health.status = ConnectionStatus.UNHEALTHY
        else:
            health.status = ConnectionStatus.DEGRADED

    def get_partition_state(self) -> PartitionState:
        """
        Get current partition state across all connections.

        Supports simulation overrides.
        Also collects bulkhead states.
        """
        # Partition simulation override check
        if self._partition_override is not None:
            logger.debug("connection_health.simulated_partition_returned")
            return self._partition_override

        state = PartitionState()
        state.detected_at = utc_now()

        for key, health in self._health_states.items():
            conn_type, name = key.split(":", 1)
            is_healthy = health.status == ConnectionStatus.HEALTHY

            if conn_type == ConnectionType.DATABASE.value:
                state.db_available = is_healthy
            elif conn_type == ConnectionType.CACHE.value:
                state.cache_available = is_healthy
            elif conn_type == ConnectionType.EXTERNAL_API.value:
                state.external_apis[name] = is_healthy

        # Collect bulkhead states
        state.bulkhead_states = self._collect_bulkhead_states()

        return state

    def _collect_bulkhead_states(self) -> dict[str, dict]:
        """
        Collect the current state of every bulkhead.

        The resolution chain always yields a registry (the built-in
        compartments at minimum), so the snapshot is never empty by design;
        an unexpected error still degrades to an empty dict (fail-open —
        health collection must not break on a bulkhead contract violation).

        Returns:
            Bulkhead name -> state-info dictionary
        """
        try:
            from baldur.services.bulkhead.registry import get_bulkhead_registry

            states = get_bulkhead_registry().get_all_states()

            return {
                name: {
                    "type": state.bulkhead_type.value,
                    "max_concurrent": state.max_concurrent,
                    "active_count": state.active_count,
                    "waiting_count": state.waiting_count,
                    "rejected_count": state.rejected_count,
                    "available_permits": state.available_permits,
                    "utilization_percent": round(state.utilization_percent, 2),
                }
                for name, state in states.items()
            }
        except Exception as e:
            logger.warning(
                "connection_health.bulkhead_states_collection_failed", error=str(e)
            )
            return {}

    def get_all_health_states(self) -> dict[str, ConnectionHealth]:
        """Get all registered connection health states."""
        return dict(self._health_states)

    def reset_health(self, connection_type: ConnectionType, name: str) -> bool:
        """Reset health state for a connection. Returns True if found."""
        key = f"{connection_type.value}:{name}"
        if key in self._health_states:
            self._health_states[key] = ConnectionHealth(
                connection_type=connection_type,
                name=name,
            )
            return True
        return False
