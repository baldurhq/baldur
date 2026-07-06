"""
Health Check Service

Health Check service layer that separates business logic from the View.
Provides default DB connectivity checks, connection-pool state queries,
and Kubernetes probe support.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.utils.singleton import make_singleton_factory

try:
    from baldur.metrics.recorders.health_check import (
        record_health_check,
        set_database_connected,
        set_health_status,
        set_pool_status,
    )
except ImportError:

    def record_health_check(
        check_type: str, result: str, duration: float, alias: str = ""
    ) -> None:
        return None

    def set_database_connected(alias: str, connected: bool) -> None:
        return None

    def set_health_status(check_type: str, status: str) -> None:
        return None

    def set_pool_status(alias: str, status: str) -> None:
        return None


logger = structlog.get_logger()


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class DatabaseCheck(SerializableMixin):
    """Database connection state."""

    alias: str
    vendor: str = ""
    is_connected: bool = False
    is_usable: bool = False
    error: str | None = None
    latency_ms: float | None = None


@dataclass
class PoolInfo(SerializableMixin):
    """Connection pool information."""

    alias: str
    vendor: str = ""
    is_usable: bool = False
    status: str = "unknown"
    error: str | None = None


@dataclass
class SystemHealthSummary(SerializableMixin):
    """Overall health state.

    Renamed from HealthStatus to avoid conflict with
    meta.health_probe.HealthStatus Enum (Item 22).
    """

    status: str  # healthy, degraded, unhealthy
    checks: dict[str, str] = field(default_factory=dict)
    services_count: int = 0
    timestamp: str | None = None
    emergency_level: str | None = None
    baldur_enabled: bool | None = None
    watchdog_status: str | None = None
    watchdog_components: dict[str, str] | None = None
    watchdog_last_check: str | None = None


@dataclass
class ReadinessStatus(SerializableMixin):
    """Kubernetes Readiness state."""

    status: str  # ready, not_ready
    checks: dict[str, str] = field(default_factory=dict)
    is_ready: bool = True


@dataclass
class PoolHealthSummary:
    """Connection pool health summary."""

    status: str  # healthy, degraded, error
    pool_info: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# =============================================================================
# Health Check Service
# =============================================================================


class HealthCheckService:
    """
    Service that owns Health Check business logic.

    Features:
    - Default DB connectivity check
    - Check all DB connections
    - Connection pool state query
    - Overall system health check
    - Kubernetes Liveness/Readiness probes

    Uses ProviderRegistry for statistics to maintain framework independence.

    Usage:
        service = HealthCheckService()

        # Overall health check
        health = service.get_overall_health()

        # Check a specific DB
        db_check = service.check_database("default")
    """

    def __init__(self) -> None:
        # Latch for the one-time ``meta_watchdog.enabled_but_unregistered``
        # WARNING. 558 made ``enabled=True`` the default, so a watchdog that is
        # configured-on but unregistered (the entitlement/wiring gap) is now a
        # meaningful misconfiguration — surface it once, never per-probe. A
        # fresh service instance (post ``reset_health_check_service``) re-arms
        # the latch.
        self._enabled_but_unregistered_warned = False

    def _get_circuit_breaker_count(self) -> int:
        """
        Get circuit breaker count using ProviderRegistry.

        Falls back to Redis repository if ORM not available.
        """
        try:
            from baldur.factory import ProviderRegistry

            stats_repo = ProviderRegistry.get_statistics_repo()
            summary = stats_repo.get_circuit_breaker_summary()
            return summary.total
        except Exception as e:
            logger.debug(
                "health_check.cb_count_via_stats",
                error=e,
            )
            try:
                from baldur.factory import ProviderRegistry

                cb_repo = ProviderRegistry.get_circuit_breaker_repo()
                states = cb_repo.get_all_states()
                return len(states)
            except Exception as e2:
                logger.debug(
                    "health_check.cb_count_via_redis",
                    e2=e2,
                )
                return 0

    def check_database(self, alias: str = "default") -> DatabaseCheck:
        """
        Check a single database connection.

        Single source of truth: ``info.is_usable`` from the registered
        DatabaseHealthProvider. ``DjangoDatabaseHealthAdapter.check_connection``
        already issues a real ``SELECT 1`` round-trip via ``conn.is_usable()``,
        so a separate ``PostgresRepository.ping()`` would duplicate the work
        and leak a Django-bound import into the framework cascade (473 D2).

        Args:
            alias: DB alias (default, replica, etc.)

        Returns:
            DatabaseCheck: connection state with ``is_connected == is_usable``.
        """
        from baldur.factory import ProviderRegistry

        start_time = time.time()
        try:
            db_provider = ProviderRegistry.database_health.get()
            info = db_provider.check_connection(alias)

            latency_ms = (time.time() - start_time) * 1000

            result_str = "healthy" if info.is_usable else "degraded"
            record_health_check("database", result_str, latency_ms / 1000, alias)
            set_database_connected(alias, info.is_usable)

            return DatabaseCheck(
                alias=alias,
                vendor=info.vendor,
                is_connected=info.is_usable,
                is_usable=info.is_usable,
                latency_ms=round(latency_ms, 2),
            )
        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            record_health_check("database", "error", latency_ms / 1000, alias)
            set_database_connected(alias, False)

            logger.exception(
                "health_check.database_check_failed",
                alias=alias,
                error=e,
            )
            return DatabaseCheck(
                alias=alias,
                is_connected=False,
                is_usable=False,
                error=str(e),
                latency_ms=round(latency_ms, 2),
            )

    def check_all_databases(self) -> list[DatabaseCheck]:
        """
        Check all database connections.

        Returns:
            List[DatabaseCheck]: list of all DB connection states
        """
        from baldur.factory import ProviderRegistry

        db_provider = ProviderRegistry.database_health.get()
        results = []
        for alias in db_provider.list_aliases():
            results.append(self.check_database(alias))
        return results

    def check_connection_pool(self, alias: str = "default") -> PoolInfo:
        """
        Query connection pool state.

        Args:
            alias: DB alias

        Returns:
            PoolInfo: connection pool information
        """
        from baldur.factory import ProviderRegistry

        try:
            db_provider = ProviderRegistry.database_health.get()
            info = db_provider.check_connection(alias)

            pool_status = "healthy" if info.is_usable else "degraded"
            set_pool_status(alias, pool_status)

            return PoolInfo(
                alias=alias,
                vendor=info.vendor,
                is_usable=info.is_usable,
                status=pool_status,
            )
        except Exception as e:
            set_pool_status(alias, "error")

            logger.exception(
                "health_check.connection_pool_check_failed",
                alias=alias,
                error=e,
            )
            return PoolInfo(
                alias=alias,
                is_usable=False,
                status="error",
                error=str(e),
            )

    def get_pool_health(self) -> PoolHealthSummary:
        """
        Overall connection pool health state.

        Returns:
            PoolHealthSummary: pool health state
        """
        pool_info = self.check_connection_pool("default")

        if pool_info.error:
            return PoolHealthSummary(
                status="error",
                pool_info=pool_info.to_dict(),
                error=pool_info.error,
            )

        return PoolHealthSummary(
            status=pool_info.status,
            pool_info=pool_info.to_dict(),
        )

    def get_readiness(self) -> ReadinessStatus:
        """
        Check Kubernetes Readiness state.

        Returns:
            ReadinessStatus: readiness state
        """
        db_checks = self.check_all_databases()

        checks = {}
        ready = True

        for db_check in db_checks:
            key = f"database_{db_check.alias}"
            if db_check.is_connected:
                checks[key] = "ready"
            else:
                checks[key] = "not_ready"
                ready = False

        record_health_check("readiness", "healthy" if ready else "unhealthy", 0.0)

        return ReadinessStatus(
            status="ready" if ready else "not_ready",
            checks=checks,
            is_ready=ready,
        )

    def get_overall_health(self) -> SystemHealthSummary:  # noqa: C901, PLR0915
        """
        Overall system health check.

        Uses ProviderRegistry for statistics to maintain framework independence.
        Logs cluster_id for multi-cluster observability.

        Returns:
            HealthStatus: overall health state
        """
        from baldur.utils.time import utc_now

        # Cluster Identity logging
        cluster_id, region, environment = self._get_cluster_info()

        try:
            db_check = self.check_database("default")

            if db_check.is_usable:
                services_count = self._get_circuit_breaker_count()
                health_status = "healthy"
                db_status = "healthy"
            else:
                # 473 D7 axis 1 (b) - DB unusability drives overall to
                # "unhealthy" so plan section 329 status differentiation holds
                # and the LB depool path (HTTP 503 via D6) becomes reachable.
                services_count = 0
                health_status = "unhealthy"
                db_status = "unhealthy"
            set_health_status("overall", health_status)
        except Exception as e:
            logger.exception(
                "health_check.overall_health_check_failed",
                error=e,
            )
            services_count = 0
            health_status = "unhealthy"
            db_status = "unhealthy"
            set_health_status("overall", health_status)

        # A5: Emergency level (fail-open)
        emergency_level = None
        try:
            from baldur_pro.services.emergency_mode import get_emergency_level

            emergency_level = get_emergency_level().value
        except Exception:
            pass

        # A6: Baldur enabled state (fail-open)
        baldur_enabled = None
        try:
            from baldur.services.system_control import is_baldur_enabled

            baldur_enabled = is_baldur_enabled()
        except Exception:
            pass

        # A7: Watchdog state (fail-open, 409 UU-E3)
        watchdog_status = None
        watchdog_components = None
        watchdog_last_check = None

        # Resolve the watchdog provider inside the fail-open envelope. Both the
        # import and ``safe_get()`` must be guarded: ``safe_get()`` only swallows
        # AdapterNotFoundError (the unregistered case → None), so a *registered*
        # callable provider that raises during instantiation would otherwise
        # propagate and crash the cascade. A resolve failure here is a genuine
        # error (not absence) → WARNING, fail-open with ``wd`` left None.
        try:
            from baldur.factory.registry import ProviderRegistry

            wd = ProviderRegistry.selfhealer_watchdog.safe_get()
        except Exception:
            logger.warning("health_check.watchdog_decoration_failed", exc_info=True)
        else:
            if wd is None:
                # Expected absence: OSS deployment, or PRO without an active
                # entitlement. Not a decoration failure — stay quiet on the hot
                # probe path (DEBUG at most). The latched guard below surfaces
                # the configured-on-but-unregistered misconfiguration once.
                logger.debug("health_check.watchdog_absent")
                self._warn_watchdog_enabled_but_unregistered_once()
            else:
                try:
                    wd_state = wd.get_state()
                    watchdog_status = wd_state.overall_status.value

                    # 473 D5 — dampening must fire before optional-field
                    # hydration so a hydration failure (component_statuses.items()
                    # / .value access / last_check.isoformat()) cannot bypass the
                    # cascade verdict. 473 D7 axis 2 (a) — DB-dominance: when DB
                    # is healthy but the watchdog reports degraded/unhealthy, cap
                    # overall at "degraded". Only is_usable=False can drive
                    # overall to "unhealthy".
                    if (
                        watchdog_status in ("degraded", "unhealthy")
                        and health_status == "healthy"
                    ):
                        health_status = "degraded"

                    # Optional decoration. Failure here leaves dampening intact.
                    watchdog_components = {
                        k: v.value for k, v in wd_state.component_statuses.items()
                    }
                    watchdog_last_check = wd_state.last_check.isoformat()
                except Exception:
                    # A *registered* watchdog whose state read / hydration
                    # raised — genuinely warn-worthy (real decoration failure,
                    # not absence). Non-fatal: dampening already fired.
                    logger.warning(
                        "health_check.watchdog_decoration_failed", exc_info=True
                    )

        # Log including cluster information
        logger.info(
            "health_check.event",
            cluster_id=cluster_id,
            target_region=region,
            environment=environment,
            health_status=health_status,
            services_count=services_count,
        )

        return SystemHealthSummary(
            status=health_status,
            checks={
                "database": db_status,
                "circuit_breaker": "enabled",
                "cluster_id": cluster_id,
                "region": region or "unknown",
            },
            services_count=services_count,
            timestamp=utc_now().isoformat(),
            emergency_level=emergency_level,
            baldur_enabled=baldur_enabled,
            watchdog_status=watchdog_status,
            watchdog_components=watchdog_components,
            watchdog_last_check=watchdog_last_check,
        )

    def _warn_watchdog_enabled_but_unregistered_once(self) -> None:
        """Emit a single latched WARNING for the watchdog entitlement/wiring gap.

        558 made ``meta_watchdog.enabled`` default to ``True``, so a deployment
        that has the Meta-Watchdog configured-on but registers no provider
        (OSS, or PRO without an active entitlement) is a meaningful
        misconfiguration worth surfacing — but *once*, not on every probe.

        Latched to the service instance so it never recurs on the hot path. The
        settings read is itself fail-open: a failure to resolve settings must
        not break the health cascade.
        """
        if self._enabled_but_unregistered_warned:
            return
        try:
            from baldur.settings.meta_watchdog import get_meta_watchdog_settings

            enabled = get_meta_watchdog_settings().enabled
        except Exception:
            return
        if enabled:
            self._enabled_but_unregistered_warned = True
            logger.warning("meta_watchdog.enabled_but_unregistered")

    def _get_cluster_info(self) -> tuple:
        """Query cluster information."""
        try:
            from baldur.core.cluster_identity import get_cluster_identity

            identity = get_cluster_identity()
            return identity.cluster_id, identity.region, identity.environment
        except Exception:
            import os

            return (
                os.environ.get("BALDUR_CLUSTER_ID", "unknown"),
                os.environ.get("BALDUR_NAMESPACE_REGION"),
                os.environ.get("BALDUR_NAMESPACE_ENV", "production"),
            )

    def is_alive(self) -> bool:
        """
        Liveness check (whether the application is running).

        Returns:
            bool: always True (if the app is running)
        """
        return True

    def is_ready(self) -> bool:
        """
        Readiness check (whether the app can serve traffic).

        Returns:
            bool: True when all DB connections are available
        """
        return self.get_readiness().is_ready


# =============================================================================
# Singleton & Factory
# =============================================================================


get_health_check_service, configure_health_check_service, reset_health_check_service = (
    make_singleton_factory("health_check_service", HealthCheckService)
)


__all__ = [
    "DatabaseCheck",
    "PoolInfo",
    "SystemHealthSummary",
    "ReadinessStatus",
    "PoolHealthSummary",
    "HealthCheckService",
    "get_health_check_service",
    "configure_health_check_service",
    "reset_health_check_service",
]
