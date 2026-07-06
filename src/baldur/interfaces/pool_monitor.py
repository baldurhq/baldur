"""
Pool Monitor Interface.

OSS-side abstractions for connection-pool stats discovery and pool health
monitoring. PRO ships ``ConnectionPoolMonitor`` and concrete providers
that depend on these abstractions; OSS adapters
(``InMemoryPoolStatsProvider``, ``SQLAlchemyPoolStatsProvider``) inherit
``PoolStatsProvider`` from here so they no longer reach into
``baldur_pro``.

The ``NoOpPoolStatsProvider`` is registered as the OSS default in
``ProviderRegistry.pool_monitor`` so callers can always resolve a
provider without ``is None`` branches; PRO overrides the default with a
realized backend at import time via ``register_pro_services()``.

Distinct from :mod:`baldur.interfaces.pool_info`: ``PoolInfoProvider``
returns a dict shape used by SQLAlchemy/dj-db-conn-pool stats discovery
(legacy OSS path), while ``PoolStatsProvider`` returns the dataclass
shape consumed by the PRO ``ConnectionPoolMonitor``. The two registries
coexist intentionally — different consumers, different shapes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from baldur.utils.time import utc_now


@runtime_checkable
class ConnectionPoolMonitor(Protocol):
    """Protocol for the PRO connection-pool monitor.

    PRO ships the realized backend; OSS adapters (e.g.,
    ``PoolWatchdog``) hold an injected monitor reference and call its
    public surface. Methods are Interface Segregation — only those
    OSS code currently calls are declared.
    """

    def check_health(self) -> Any: ...

    def detect_leaks(self) -> Any: ...

    def on_connection_released(self, *args: Any, **kwargs: Any) -> Any: ...


__all__ = [
    "ConnectionInfo",
    "ConnectionPoolMonitor",
    "LeakReport",
    "NoOpPoolStatsProvider",
    "PoolHealthStatus",
    "PoolStats",
    "PoolStatsProvider",
]


class PoolHealthStatus(str, Enum):
    """Connection pool health status."""

    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    EXHAUSTED = "exhausted"
    LEAK_SUSPECTED = "leak_suspected"
    UNKNOWN = "unknown"


@dataclass
class PoolStats:
    """Connection pool statistics."""

    pool_name: str
    max_connections: int
    active_connections: int
    available_connections: int
    waiting_requests: int = 0
    checked_at: datetime = field(default_factory=lambda: utc_now())

    @property
    def usage_percent(self) -> float:
        if self.max_connections == 0:
            return 0.0
        return (self.active_connections / self.max_connections) * 100

    @property
    def is_exhausted(self) -> bool:
        return (
            self.available_connections == 0
            and self.active_connections >= self.max_connections
        )


@dataclass
class ConnectionInfo:
    """Tracked connection information for leak detection."""

    connection_id: str
    acquired_at: datetime
    stack_trace: str | None = None
    query_info: str | None = None
    thread_id: int | None = None


@dataclass
class LeakReport:
    """Connection leak detection report."""

    suspected_leaks: list[ConnectionInfo]
    leak_threshold_seconds: float
    report_time: datetime

    @property
    def leak_count(self) -> int:
        return len(self.suspected_leaks)


class PoolStatsProvider(ABC):
    """Abstract interface for pool statistics providers.

    Concrete implementations live in ``baldur.adapters.pool`` (OSS:
    in-memory, SQLAlchemy-backed) and in ``baldur_pro`` (PRO: realized
    backends). The PRO ``ConnectionPoolMonitor`` consumes any
    implementation of this ABC.
    """

    @abstractmethod
    def get_stats(self) -> PoolStats:
        """Get current pool statistics."""


class NoOpPoolStatsProvider(PoolStatsProvider):
    """Default OSS pool stats provider — returns empty stats.

    Registered as ``ProviderRegistry.pool_monitor`` default so callers
    always receive a usable provider (never ``None``). PRO overrides the
    default with a realized backend when ``baldur_pro`` is installed.
    """

    def __init__(self, pool_name: str = "noop") -> None:
        self._pool_name = pool_name

    def get_stats(self) -> PoolStats:
        return PoolStats(
            pool_name=self._pool_name,
            max_connections=0,
            active_connections=0,
            available_connections=0,
            waiting_requests=0,
        )
