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
    """격벽 상태 정보 (ConnectionType별 active_count, max_concurrent 등)"""

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

        # 카오스 테스트용 시뮬레이션 오버라이드
        self._simulation_overrides: dict[str, ConnectionHealth] = {}
        self._partition_override: PartitionState | None = None
        self._simulation_experiment_id: str | None = None

    @classmethod
    def from_settings(
        cls, settings=None, **overrides
    ) -> DefaultConnectionHealthMonitor:
        """
        Settings 기반 인스턴스 생성.

        Args:
            settings: PoolMonitorSettings 인스턴스 (None이면 자동 로드)
            **overrides: 개별 필드 오버라이드

        Returns:
            DefaultConnectionHealthMonitor: Settings 기반 인스턴스
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
        특정 연결에 대한 시뮬레이션 상태 설정.

        Args:
            connection_type: 연결 타입 (DATABASE, CACHE, EXTERNAL_API)
            name: 연결 이름
            status: 강제할 연결 상태
            experiment_id: 관련 카오스 실험 ID (감사 추적용)

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
        네트워크 파티션 시뮬레이션 설정.

        Args:
            partition_state: 강제할 파티션 상태
            experiment_id: 관련 카오스 실험 ID (감사 추적용)
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
        모든 시뮬레이션 오버라이드 해제.
        """
        self._simulation_overrides.clear()
        self._partition_override = None
        self._simulation_experiment_id = None
        logger.info("connection_health.simulation_overrides_cleared")

    def is_simulation_active(self) -> bool:
        """시뮬레이션 오버라이드가 활성화되어 있는지 확인."""
        return bool(self._simulation_overrides) or self._partition_override is not None

    def get_simulation_experiment_id(self) -> str | None:
        """현재 시뮬레이션과 연관된 실험 ID 반환."""
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

        시뮬레이션 오버라이드를 지원합니다.
        """
        key = f"{connection_type.value}:{name}"

        # 시뮬레이션 오버라이드 체크
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

        시뮬레이션 오버라이드를 지원합니다.
        Bulkhead 상태도 함께 수집합니다.
        """
        # 파티션 시뮬레이션 오버라이드 체크
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

        # Bulkhead 상태 수집
        state.bulkhead_states = self._collect_bulkhead_states()

        return state

    def _collect_bulkhead_states(self) -> dict[str, dict]:
        """
        모든 Bulkhead의 현재 상태 수집.

        Returns:
            격벽 이름 -> 상태 정보 딕셔너리
        """
        try:
            from baldur.factory.registry import ProviderRegistry

            registry = ProviderRegistry.bulkhead_registry.safe_get()
            if registry is None:
                raise RuntimeError("baldur_pro BulkheadRegistry not registered")
            states = registry.get_all_states()

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
        except ImportError:
            return {}
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
