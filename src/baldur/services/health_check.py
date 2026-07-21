"""
Health Check Service

Health Check service layer that separates business logic from the View.
Provides default DB connectivity checks, connection-pool state queries,
and Kubernetes probe support.
"""

from __future__ import annotations

import contextvars
import threading
import time
from concurrent.futures import Future, wait
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.core.ttl_cache import TTLCacheBase
from baldur.utils.singleton import make_singleton_factory

if TYPE_CHECKING:
    from collections.abc import Callable

    from baldur.interfaces.database_health import DatabaseHealthProvider

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

# Name carried by every readiness probe worker thread, so a leaked probe is
# identifiable in a thread dump.
PROBE_THREAD_NAME = "baldur-readiness-probe"

# Single key for the whole-result readiness cache: every alias is probed as one
# set under one deadline, so there is nothing to key per alias.
_READINESS_CACHE_KEY = "readiness"


def _spawn_probe(fn: Callable[..., DatabaseCheck], *args: Any) -> Future[DatabaseCheck]:
    """Run ``fn(*args)`` on a fresh daemon thread, handing back its Future.

    Deliberately *not* a ``ThreadPoolExecutor``: ``concurrent.futures.thread``
    registers an atexit hook that joins every worker it owns regardless of the
    daemon flag, so a probe blocked in a driver call would delay process exit
    until the driver gives up. Threads spawned here are never registered there,
    so ``daemon=True`` is effective and the interpreter can exit with a probe
    still blocked.

    A ``Context`` cannot be entered concurrently, so the copy is taken per
    spawn — one context per thread, never one shared across a probe round.
    Propagation itself is required: adapters log from inside the probe body.
    """
    future: Future[DatabaseCheck] = Future()
    ctx = contextvars.copy_context()

    def runner() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(ctx.run(fn, *args))
        except BaseException as exc:  # noqa: BLE001 - published to the Future
            future.set_exception(exc)

    threading.Thread(target=runner, daemon=True, name=PROBE_THREAD_NAME).start()
    return future


def _release_probe_thread_resources() -> None:
    """Release framework-managed per-thread DB connections at probe exit.

    The probe runs outside the request cycle, so Django's ``request_finished``
    signal never fires for it and the thread-local connection would be stranded
    in the dying worker — on every round, not only hung ones. The unconditional
    helper is required here: the request-boundary one is a no-op under
    persistent connections (``CONN_MAX_AGE > 0``).

    Fail-open and silent by design. This runs in the probe worker, which emits
    no logs at all so an abandoned thread that completes late cannot write over
    fresher state; and a cleanup failure must never turn a completed probe into
    an exceptional Future.
    """
    try:
        from baldur.adapters.django.utils import close_all_django_connections
    except ImportError:
        return
    try:
        close_all_django_connections()
    except Exception:
        pass


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
    timed_out: bool = False


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

        # Whole-result readiness cache. Constructed with ttl_seconds=0.0 on
        # purpose: the real TTL is passed as ttl_override on every call, so a
        # settings reload takes effect immediately. Reading it here instead
        # would freeze the TTL for the lifetime of this singleton.
        self._readiness_cache: TTLCacheBase[str, ReadinessStatus] = TTLCacheBase(
            ttl_seconds=0.0
        )
        # Aliases with a probe thread still outstanding from an earlier round.
        # The lock covers the whole check-then-submit-then-store sequence: a
        # check-then-act split lets two concurrent rounds both observe "nothing
        # outstanding" and both spawn, orphaning a thread nobody re-checks.
        self._outstanding_probes: dict[str, Future[DatabaseCheck]] = {}
        self._outstanding_lock = threading.Lock()

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

            # A dependency-down probe result is expected-class, not an
            # application error: WARNING per the ``_failed`` suffix floor, so a
            # sustained outage no longer emits ERROR+traceback every probe
            # interval on every pod. The traceback is demoted rather than
            # dropped — it is the only thing that says *why* the connection
            # failed, and unlike the level it cannot be recovered from logging
            # configuration once discarded at the call site.
            logger.warning(
                "health_check.database_check_failed",
                alias=alias,
                error=str(e),
            )
            logger.debug(
                "health_check.database_check_failed",
                alias=alias,
                exc_info=True,
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
        Check all database connections in parallel under one shared deadline.

        Every alias is probed on its own daemon thread and the round is bounded
        by ``readiness_probe_timeout_seconds``, so worst-case latency is the
        budget rather than the sum of per-alias latencies — a database that
        accepts connections but never answers can no longer hang the caller for
        the driver default.

        An alias that has not answered when the budget expires is returned with
        ``timed_out=True`` (distinct from a refused connection, which still
        returns fast with ``is_connected=False``). Its thread is abandoned, not
        cancelled — a blocking driver call is not interruptible from Python —
        so the next round suppresses that alias instead of stacking a second
        probe on it.

        Returns:
            List[DatabaseCheck]: one entry per alias, in ``list_aliases()``
            order.
        """
        from baldur.factory import ProviderRegistry
        from baldur.settings.health_check import get_health_check_settings

        # Resolved once here, on the calling thread, and passed down to the
        # workers: a worker must never touch the registry, or an abandoned
        # probe could read registry state that has since changed.
        provider = ProviderRegistry.database_health.get()
        aliases = list(provider.list_aliases())
        budget = get_health_check_settings().readiness_probe_timeout_seconds

        submitted = self._submit_probe_round(provider, aliases)

        started_at = time.monotonic()
        wait(list(submitted.values()), timeout=budget)
        elapsed = time.monotonic() - started_at

        return [
            self._classify_probe(alias, submitted.get(alias), elapsed)
            for alias in aliases
        ]

    def _probe_database_raw(
        self, provider: DatabaseHealthProvider, alias: str
    ) -> DatabaseCheck:
        """Probe one alias with no metrics and no logging.

        Both omissions are deliberate. This runs on an abandonable worker: a
        probe that finally completes minutes after its round was classified
        must not write a stale gauge or log line over fresher state. All
        emission happens on the calling thread, keyed to the final
        classification.
        """
        started_at = time.monotonic()
        try:
            info = provider.check_connection(alias)
            return DatabaseCheck(
                alias=alias,
                vendor=info.vendor,
                is_connected=info.is_usable,
                is_usable=info.is_usable,
                latency_ms=round((time.monotonic() - started_at) * 1000, 2),
            )
        except Exception as e:
            return DatabaseCheck(
                alias=alias,
                is_connected=False,
                is_usable=False,
                error=str(e),
                latency_ms=round((time.monotonic() - started_at) * 1000, 2),
            )
        finally:
            _release_probe_thread_resources()

    def _submit_probe_round(
        self, provider: DatabaseHealthProvider, aliases: list[str]
    ) -> dict[str, Future[DatabaseCheck]]:
        """Spawn one probe per alias that has no probe still outstanding.

        Returns the futures spawned by *this* round, keyed by alias. An alias
        omitted from the result is one whose earlier probe is still running.
        """
        submitted: dict[str, Future[DatabaseCheck]] = {}
        live_aliases = set(aliases)

        with self._outstanding_lock:
            for gone in [a for a in self._outstanding_probes if a not in live_aliases]:
                del self._outstanding_probes[gone]

            for alias in aliases:
                outstanding = self._outstanding_probes.get(alias)
                if outstanding is not None and not outstanding.done():
                    continue
                # A leftover future that has since completed is discarded
                # unread, never published: its verdict describes the moment it
                # was submitted, which may be minutes old, and publishing a
                # stale "connected" would un-depool a pod whose database is
                # still down.
                future = _spawn_probe(self._probe_database_raw, provider, alias)
                self._outstanding_probes[alias] = future
                submitted[alias] = future

        return submitted

    def _classify_probe(
        self, alias: str, future: Future[DatabaseCheck] | None, elapsed: float
    ) -> DatabaseCheck:
        """Turn one alias' round outcome into a DatabaseCheck, with emission.

        ``future is None`` means the alias was suppressed because an earlier
        probe is still outstanding; it is classified exactly like an alias that
        missed the deadline in this round.
        """
        if future is None or not future.done():
            return self._record_probe_timeout(alias, elapsed)

        with self._outstanding_lock:
            if self._outstanding_probes.get(alias) is future:
                del self._outstanding_probes[alias]

        # The submitted callable is wider than the probe body it wraps — the
        # context run can fail before the probe itself starts — so a completed
        # future may still carry an exception. Map it, never re-raise it.
        error = future.exception()
        if error is not None:
            check = DatabaseCheck(
                alias=alias,
                is_connected=False,
                is_usable=False,
                error=str(error),
                latency_ms=round(elapsed * 1000, 2),
            )
        else:
            check = future.result()

        self._record_probe_result(check, elapsed)
        return check

    def _record_probe_result(self, check: DatabaseCheck, elapsed: float) -> None:
        """Emit metrics and logs for a probe that answered within the budget."""
        latency_seconds = (
            check.latency_ms / 1000 if check.latency_ms is not None else elapsed
        )

        if check.error is not None:
            record_health_check("database", "error", latency_seconds, check.alias)
            set_database_connected(check.alias, False)
            logger.warning(
                "health_check.database_check_failed",
                alias=check.alias,
                error=check.error,
            )
            return

        record_health_check(
            "database",
            "healthy" if check.is_usable else "degraded",
            latency_seconds,
            check.alias,
        )
        set_database_connected(check.alias, check.is_usable)

    def _record_probe_timeout(self, alias: str, elapsed: float) -> DatabaseCheck:
        """Emit metrics and logs for an alias that missed the round budget.

        The observed duration is the measured round wall-clock, not the budget
        constant: a round in which every alias was suppressed returns almost
        immediately and must not report a full budget of latency.
        """
        record_health_check("database", "timeout", elapsed, alias)
        set_database_connected(alias, False)
        logger.warning(
            "health_check.database_check_timeout",
            alias=alias,
            elapsed_seconds=round(elapsed, 3),
        )
        return DatabaseCheck(
            alias=alias,
            is_connected=False,
            is_usable=False,
            timed_out=True,
        )

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

            # Same rule as database_check_failed above: WARNING level, with the
            # traceback kept at DEBUG for local debugging.
            logger.warning(
                "health_check.connection_pool_check_failed",
                alias=alias,
                error=str(e),
            )
            logger.debug(
                "health_check.connection_pool_check_failed",
                alias=alias,
                exc_info=True,
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

        The verdict is cached for ``readiness_cache_ttl_seconds`` and concurrent
        callers share one live round, so probe cadence × pods no longer means a
        constant ``SELECT 1`` load against a database that may already be
        struggling.

        Never raises: any ordinary failure on the way to a verdict — a
        malformed readiness setting, a provider that raises while being
        resolved or enumerated — is answered as not-ready rather than escaping
        as an HTTP 500. That preserves today's effective outcome (a raising
        provider depools the pod) while making it graceful and logged.

        Returns:
            ReadinessStatus: readiness state
        """
        try:
            from baldur.settings.health_check import get_health_check_settings

            # Read per call, not at construction: this service is a singleton,
            # so a TTL captured once would ignore every later settings reload.
            ttl = get_health_check_settings().readiness_cache_ttl_seconds
            status = self._readiness_cache.get_or_compute(
                _READINESS_CACHE_KEY, self._compute_readiness, ttl_override=ttl
            )
            # _compute_readiness always returns a ReadinessStatus and never
            # None, so get_or_compute's V|None return never yields None here.
            assert status is not None
            return status
        except Exception as e:
            logger.warning(
                "health_check.readiness_resolution_failed",
                error=str(e),
            )
            return ReadinessStatus(status="not_ready", checks={}, is_ready=False)

    def _compute_readiness(self) -> ReadinessStatus:
        """Run one live readiness round and map it to a verdict.

        A timed-out alias is reported as ``"timed_out"`` under both fail
        directions — the operator always sees which alias stopped answering.
        What the direction decides is only whether that flips the pod out of
        rotation. Refused and failed connections flip it either way.
        """
        from baldur.settings.health_check import get_health_check_settings

        started_at = time.monotonic()
        db_checks = self.check_all_databases()
        elapsed = time.monotonic() - started_at

        timeout_is_fatal = (
            get_health_check_settings().readiness_timeout_fail_direction == "not_ready"
        )

        checks = {}
        ready = True

        for db_check in db_checks:
            key = f"database_{db_check.alias}"
            if db_check.timed_out:
                checks[key] = "timed_out"
                if timeout_is_fatal:
                    ready = False
            elif db_check.is_connected:
                checks[key] = "ready"
            else:
                checks[key] = "not_ready"
                ready = False

        # Recorded on live rounds only — a cache hit is not a new evaluation.
        record_health_check("readiness", "healthy" if ready else "unhealthy", elapsed)

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
