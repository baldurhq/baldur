"""
Health Probe Manager - subsystem health collection.

Periodically probes and collects the health status of each Baldur component
(Circuit Breaker, DLQ, Redis, etc.).
"""

from __future__ import annotations

import contextvars
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.repositories import CircuitBreakerStateEnum
from baldur.meta.config import MetaWatchdogSettings, get_meta_watchdog_settings
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.meta.daemon_worker import (  # noqa: F401
        DaemonWorkerHandle,
    )

logger = structlog.get_logger()

# Reasons carried by the two synthetic results a bounded pass can produce. Both
# are UNKNOWN and both carry observed=False; the reason distinguishes them for
# an operator reading the console.
PROBE_TRUNCATED_REASON = "pass budget exhausted"
PROBE_SINGLE_FLIGHT_REASON = "previous probe invocation still running"

# Identifiable in a thread dump alongside baldur-watchdog-beacon.
PROBE_WORKER_THREAD_PREFIX = "baldur-probe-"

# Consecutive single-flight skips of one component before the manager warns.
# A component skipped this often has an invocation that never returned.
_SINGLE_FLIGHT_STUCK_SKIPS = 3

# Worker verdict meaning "this component's feature is not active". The collector
# deletes the key so the component is absent from the pass entirely — the
# contract stated on HealthProbe.is_applicable. It must be distinguishable from
# "no verdict yet", which is what a truncated probe leaves behind.
_NOT_APPLICABLE = object()


class HealthStatus(str, Enum):
    """Subsystem health status."""

    HEALTHY = "healthy"
    """Normal."""

    DEGRADED = "degraded"
    """Degraded performance (still working, but needs attention)."""

    UNHEALTHY = "unhealthy"
    """Failing (recovery required)."""

    UNKNOWN = "unknown"
    """Status could not be determined."""


@dataclass
class ProbeResult:
    """Health probe result."""

    component: str
    """Component name."""

    status: HealthStatus
    """Health status."""

    latency_ms: float
    """Response time in milliseconds."""

    timestamp: datetime
    """When the probe ran."""

    details: dict[str, Any] = field(default_factory=dict)
    """Detailed information."""

    reason: str = ""
    """Human-readable context for the status determination."""

    error: str | None = None
    """Error message (on failure)."""

    observed: bool = True
    """Whether a probe actually ran and produced this verdict.

    ``False`` marks a synthetic result the manager wrote on the probe's behalf
    because it never got to run — truncated by the pass budget, or skipped
    because the component's previous invocation is still in flight. Consumers
    that record telemetry MUST exclude these: a probe that observed nothing is
    neither a failure nor a 0 ms latency sample.
    """


class HealthProbe(ABC):
    """
    Health probe interface.

    Implement this interface per subsystem to report its health status.
    """

    @property
    @abstractmethod
    def component_name(self) -> str:
        """Return the component name."""
        pass

    @abstractmethod
    def probe(self) -> ProbeResult:
        """
        Run the health probe.

        Returns:
            ProbeResult: probe result
        """
        pass

    def is_applicable(self) -> bool:
        """Whether this probe's subsystem is active in the current deployment.

        Returns ``False`` when the backing feature is disabled by configuration.
        The manager then skips the probe so the component is absent from the
        watchdog state entirely, rather than reporting a misleading HEALTHY for
        a feature that is not running. A disabled subsystem has nothing to
        monitor — no chaos experiment can become a zombie while chaos is off, a
        disabled error-budget gate blocks nothing — so probing it would only
        emit noise and surface a not-yet-active feature in the operator console.

        Defaults to ``True``; probes for default-disabled features override it.
        """
        return True


class CircuitBreakerProbe(HealthProbe):
    """
    Circuit breaker health probe.

    Checks:
    - CB states (the canonical ``CircuitBreakerStateEnum`` values)
    - open-breaker count against the configured threshold
    - stuck breakers (held open past the stuck threshold)
    """

    @property
    def component_name(self) -> str:
        return "circuit_breaker"

    def probe(self) -> ProbeResult:
        start = time.time()
        try:
            # Try to read Circuit Breaker state
            open_count = 0
            stuck_count = 0
            all_states: dict[str, str] = {}

            try:
                from baldur.services.circuit_breaker import (
                    get_circuit_breaker_service,
                )

                cb_service = get_circuit_breaker_service()
                # Read the CB service state
                cb_states = cb_service.get_all_states()
                open_count = sum(
                    1
                    for s in cb_states
                    if s.get("state") == CircuitBreakerStateEnum.OPEN.value
                )
                all_states["cb_service_available"] = "true"
                all_states["open_cb_count"] = str(open_count)

                # Stuck CB detection: a breaker held OPEN past
                # stuck_threshold_seconds without a re-open transition (which
                # resets opened_at) is "locked open" — the guide's flagship
                # stuck example. Computed in this inner try so any failure falls
                # open to stuck_count=0 via manager_error below.
                stuck_count = self._count_stuck_open_breakers(cb_states)
            except ImportError:
                all_states["cb_service_available"] = "false"
            except Exception as e:
                all_states["manager_error"] = str(e)

            # Determine the status
            status = HealthStatus.HEALTHY
            reason = ""

            # Many OPEN breakers → DEGRADED
            from baldur.settings.health_check import get_health_check_settings

            hc_settings = get_health_check_settings()
            threshold = hc_settings.probe_cb_open_threshold
            if open_count > threshold:
                status = HealthStatus.DEGRADED
                reason = f"{open_count} circuit breakers open (threshold: {threshold})"

            # Any stuck breaker → UNHEALTHY
            if stuck_count > 0:
                status = HealthStatus.UNHEALTHY
                reason = f"{stuck_count} circuit breakers stuck in OPEN state"

            return ProbeResult(
                component=self.component_name,
                status=status,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                reason=reason,
                details={
                    "open_count": open_count,
                    "stuck_count": stuck_count,
                    "states": all_states,
                },
            )
        except Exception as e:
            return ProbeResult(
                component=self.component_name,
                status=HealthStatus.UNKNOWN,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                error=str(e),
            )

    @staticmethod
    def _count_stuck_open_breakers(cb_states: list[dict[str, Any]]) -> int:
        """Count circuit breakers locked OPEN past ``stuck_threshold_seconds``.

        A breaker is "stuck" when it has been OPEN for at least
        ``stuck_threshold_seconds`` without a re-open transition (which resets
        ``opened_at``). ``get_all_states()`` serializes ``opened_at`` with
        ``.isoformat()``, so the probe receives an ISO string in production; it
        is parsed back to a tz-aware ``datetime`` here. A ``datetime`` is also
        accepted as-is for direct callers. ``None`` / unparseable / tz-naive /
        otherwise-malformed timestamps fall through the per-state guards and are
        skipped so one bad entry cannot mask the rest (fail-safe): a tz-naive
        value makes the aware-minus-naive subtraction raise ``TypeError``, which
        is caught and skipped.
        """
        stuck_threshold = get_meta_watchdog_settings().stuck_threshold_seconds
        probe_now = utc_now()
        stuck_count = 0
        for s in cb_states:
            if s.get("state") != CircuitBreakerStateEnum.OPEN.value:
                continue
            opened_at = s.get("opened_at")
            if isinstance(opened_at, str):
                try:
                    opened_at = datetime.fromisoformat(opened_at)
                except ValueError:
                    continue
            if not isinstance(opened_at, datetime):
                continue
            try:
                open_duration_seconds = (probe_now - opened_at).total_seconds()
            except (TypeError, ValueError):
                continue
            if open_duration_seconds >= stuck_threshold:
                stuck_count += 1
        return stuck_count


class DaemonWorkerProbe(HealthProbe):
    """Cross-shape daemon worker liveness + respawn coordinator (impl 489 D3 + D7).

    Iterates the module-level handle registry from
    ``baldur.metrics.recorders.daemon_worker`` and produces a single
    ``ProbeResult`` per probe tick:

    - ``handle.is_stopping=True`` → skipped (graceful stop in progress).
    - dead thread → UNHEALTHY; respawn coordinator may run if the handle
      is respawnable AND the global flag is on AND the gate counter is
      below ``respawn_max_attempts`` AND the elapsed-time backoff has
      cleared.
    - heartbeat older than ``handle.staleness_threshold_seconds`` → UNHEALTHY
      (livelock detection; respawn never fires on staleness — only on
      dead-thread detection).
    - otherwise → HEALTHY; sets ``handle.last_healthy_observed_at`` so the
      sustained-health reset gate can forgive earlier transients.

    The probe runs inside ``HealthProbeManager.probe_all`` under a
    ``TimeoutExecutor`` wrap; the respawn coordinator is therefore
    non-blocking — backoff is an elapsed-time gate, not a sleep.
    """

    @property
    def component_name(self) -> str:
        return "daemon_workers"

    def probe(self) -> ProbeResult:
        start = time.time()
        try:
            from baldur.metrics.recorders.daemon_worker import (
                get_registered_daemon_workers,
            )
            from baldur.settings.daemon_worker import get_daemon_worker_settings
        except Exception as e:
            return ProbeResult(
                component=self.component_name,
                status=HealthStatus.UNKNOWN,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                error=str(e),
            )

        settings = get_daemon_worker_settings()
        handles = get_registered_daemon_workers()
        now_mono = time.monotonic()

        per_worker: dict[str, dict[str, Any]] = {}
        worst_status = HealthStatus.HEALTHY
        unhealthy_names: list[str] = []

        for name, handle in handles.items():
            if handle.is_stopping:
                per_worker[name] = {"status": "STOPPING"}
                continue

            try:
                alive = handle.thread.is_alive()
            except Exception:
                alive = False

            heartbeat_age = max(0.0, now_mono - handle.last_heartbeat_at)
            staleness = handle.staleness_threshold_seconds or float("inf")

            if not alive:
                per_worker[name] = {
                    "status": "DEAD",
                    "heartbeat_age_seconds": heartbeat_age,
                    "restart_count": handle.restart_count,
                }
                unhealthy_names.append(name)
                worst_status = HealthStatus.UNHEALTHY
                self._handle_dead_worker(name, handle, settings, now_mono)
            elif heartbeat_age > staleness:
                per_worker[name] = {
                    "status": "STALE",
                    "heartbeat_age_seconds": heartbeat_age,
                    "staleness_threshold_seconds": staleness,
                }
                unhealthy_names.append(name)
                worst_status = HealthStatus.UNHEALTHY
            else:
                per_worker[name] = {
                    "status": "HEALTHY",
                    "heartbeat_age_seconds": heartbeat_age,
                }
                handle.last_healthy_observed_at = now_mono

        reason = ""
        if unhealthy_names:
            reason = f"{len(unhealthy_names)} unhealthy daemon worker(s): " + ", ".join(
                unhealthy_names
            )

        return ProbeResult(
            component=self.component_name,
            status=worst_status,
            latency_ms=(time.time() - start) * 1000,
            timestamp=utc_now(),
            reason=reason,
            details={"workers": per_worker, "total": len(handles)},
        )

    def _handle_dead_worker(  # noqa: C901
        self,
        name: str,
        handle: Any,
        settings: Any,
        now_mono: float,
    ) -> None:
        """Emit DAEMON_WORKER_DIED + (optionally) attempt respawn (impl 489 D7).

        Sustained-health reset gate runs first: if the worker was observed
        HEALTHY long enough ago that earlier transients should be forgiven,
        the handle's ``restart_count`` resets to 0 before the max-attempts
        check. The lifetime Prometheus Counter is not affected — operators
        still detect borderline flakiness via PromQL.
        """
        # Emit DAEMON_WORKER_DIED once — track via the handle so repeat probe
        # ticks against the same dead thread do not spam the bus.
        was_already_dead = getattr(handle, "_died_event_emitted", False)
        if not was_already_dead:
            handle._died_event_emitted = True
            heartbeat_age = max(0.0, now_mono - handle.last_heartbeat_at)
            logger.critical(
                "daemon_worker.died",
                worker_name=name,
                heartbeat_age_seconds=heartbeat_age,
                crash_reason=handle.last_crash_reason,
            )
            self._emit_died_event(
                worker_name=name,
                was_respawnable=handle.restart_callback is not None,
                heartbeat_age_seconds=heartbeat_age,
                crash_reason=handle.last_crash_reason,
            )

        # Respawn gate evaluation
        if handle.restart_callback is None:
            return
        if not settings.respawn_enabled:
            return

        # Sustained-health reset gate
        if handle.last_healthy_observed_at is not None:
            healthy_age = now_mono - handle.last_healthy_observed_at
            if healthy_age >= settings.respawn_count_reset_seconds:
                handle.restart_count = 0

        if handle.restart_count >= settings.respawn_max_attempts:
            return

        # Elapsed-time backoff gate
        if handle.last_respawn_attempt_at is not None:
            from baldur.core.backoff import ExponentialBackoff

            backoff = ExponentialBackoff(
                base_delay=settings.respawn_backoff_base_seconds,
                max_delay=settings.respawn_backoff_max_seconds,
                multiplier=2.0,
                jitter=True,
            )
            # restart_count is the prior attempt count; pass +1 for the
            # 1-indexed calculate() contract.
            wait = backoff.calculate(handle.restart_count + 1)
            if (now_mono - handle.last_respawn_attempt_at) < wait:
                return

        handle.last_respawn_attempt_at = now_mono
        try:
            handle.restart_callback()
        except Exception as e:
            logger.exception(
                "daemon_worker.respawn_callback_failed",
                worker_name=name,
                error=e,
            )
            return

        # Two-layer counter increment
        handle.restart_count += 1
        try:
            from baldur.metrics.recorders.daemon_worker import (
                record_daemon_worker_restart,
            )

            record_daemon_worker_restart(name)
        except Exception as e:
            logger.debug(
                "daemon_worker.restart_counter_increment_failed",
                worker_name=name,
                error=e,
            )

        # Reset the died-emitted flag so a subsequent death after a
        # successful respawn re-emits the event.
        handle._died_event_emitted = False
        # Clear the captured crash reason for the now-respawned worker so a
        # later death isn't attributed to the prior incident.
        handle.last_crash_reason = None

        logger.warning(
            "daemon_worker.respawned",
            worker_name=name,
            restart_count=handle.restart_count,
        )
        self._emit_respawned_event(worker_name=name, restart_count=handle.restart_count)

    @staticmethod
    def _emit_died_event(
        worker_name: str,
        was_respawnable: bool,
        heartbeat_age_seconds: float,
        crash_reason: str | None,
    ) -> None:
        try:
            from baldur.services.event_bus.bus.convenience import get_event_bus
            from baldur.services.event_bus.bus.event_types import (
                EventPriority,
                EventType,
            )

            bus = get_event_bus()
            bus.emit(
                EventType.DAEMON_WORKER_DIED,
                data={
                    "worker_name": worker_name,
                    "was_respawnable": was_respawnable,
                    "last_heartbeat_age_seconds": heartbeat_age_seconds,
                    "crash_reason": crash_reason,
                },
                source="daemon_worker_probe",
                priority=EventPriority.CRITICAL,
            )
        except Exception as e:
            logger.warning(
                "daemon_worker.died_event_emit_failed",
                worker_name=worker_name,
                error=e,
            )

    @staticmethod
    def _emit_respawned_event(worker_name: str, restart_count: int) -> None:
        try:
            from baldur.services.event_bus.bus.convenience import get_event_bus
            from baldur.services.event_bus.bus.event_types import (
                EventPriority,
                EventType,
            )

            bus = get_event_bus()
            bus.emit(
                EventType.DAEMON_WORKER_RESPAWNED,
                data={
                    "worker_name": worker_name,
                    "restart_count": restart_count,
                },
                source="daemon_worker_probe",
                priority=EventPriority.HIGH,
            )
        except Exception as e:
            logger.warning(
                "daemon_worker.respawned_event_emit_failed",
                worker_name=worker_name,
                error=e,
            )


class DLQProbe(HealthProbe):
    """
    DLQ (Dead Letter Queue) health probe.

    Checks:
    - DLQ pending queue size
    - Processing rate (entries/sec)
    - Consumer liveness
    """

    @property
    def component_name(self) -> str:
        return "dlq"

    def probe(self) -> ProbeResult:
        start = time.time()
        try:
            pending_count = 0

            try:
                from baldur.factory import ProviderRegistry

                # ``has_runtime_adapter`` / ``get_runtime`` are not declared on
                # ProviderRegistry; duck-type so PRO can register a runtime slot
                # without OSS coupling. Falls open to pending_count=0 in OSS.
                has_adapter = getattr(ProviderRegistry, "has_runtime_adapter", None)
                get_runtime_fn = getattr(ProviderRegistry, "get_runtime", None)
                if callable(has_adapter) and callable(get_runtime_fn) and has_adapter():
                    runtime = get_runtime_fn()
                    pending_count = runtime.count_pending()
            except ImportError:
                pass
            except Exception as e:
                return ProbeResult(
                    component=self.component_name,
                    status=HealthStatus.UNKNOWN,
                    latency_ms=(time.time() - start) * 1000,
                    timestamp=utc_now(),
                    error=f"Runtime adapter error: {e}",
                )

            settings = get_meta_watchdog_settings()
            status = HealthStatus.HEALTHY
            reason = ""
            threshold = settings.dlq_stuck_threshold_entries

            # Large pending backlog → DEGRADED
            if pending_count > threshold:
                status = HealthStatus.DEGRADED
                reason = (
                    f"DLQ backlog: {pending_count} entries (threshold: {threshold})"
                )

            # No throughput with a very large backlog → UNHEALTHY
            if pending_count > threshold * 2:
                status = HealthStatus.UNHEALTHY
                reason = f"DLQ critically backed up: {pending_count} entries"

            # Zero-variance stuck detection (the guide's "key trick"): feed the
            # per-tick pending_count into the shared StuckDetector and upgrade to
            # UNHEALTHY when the queue is pinned (variance ≈ 0) while backlogged.
            # The error gate uses >= so a queue pinned at exactly the threshold —
            # the flagship "1,000 pending that never drains" case, which the >
            # level logic above leaves HEALTHY — still trips. Fail-open but NOT
            # silent: a detector fault keeps the level verdict (never UNKNOWN)
            # and is surfaced via details.
            stuck_detection_error: str | None = None
            try:
                from baldur.meta.stuck_detector import get_stuck_detector

                detector = get_stuck_detector()
                detector.record(
                    component=self.component_name,
                    value=pending_count,
                    error=pending_count >= threshold,
                )
                if detector.check(self.component_name).is_stuck:
                    status = HealthStatus.UNHEALTHY
                    reason = (
                        f"DLQ stuck: pending pinned at {pending_count} "
                        "(near-zero variance)"
                    )
            except Exception as e:
                stuck_detection_error = str(e)

            details: dict[str, Any] = {"pending_count": pending_count}
            if stuck_detection_error is not None:
                details["stuck_detection_error"] = stuck_detection_error

            return ProbeResult(
                component=self.component_name,
                status=status,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                reason=reason,
                details=details,
            )
        except Exception as e:
            return ProbeResult(
                component=self.component_name,
                status=HealthStatus.UNKNOWN,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                error=str(e),
            )


class RecoveryPipelineProbe(HealthProbe):
    """
    Recovery pipeline health probe.

    Checks:
    - Active recovery job count
    - Stuck recovery jobs (running too long)
    - Failure rate
    """

    @property
    def component_name(self) -> str:
        return "recovery_pipeline"

    def probe(self) -> ProbeResult:
        start = time.time()
        try:
            # Read the recovery pipeline state
            active_recoveries = 0
            stuck_recoveries = 0

            # Read state from the recovery coordinator when one is available
            try:
                from baldur_pro.services.coordination.recovery_coordinator import (
                    get_recovery_coordinator,
                )

                get_recovery_coordinator()
                # Basic availability check
            except ImportError:
                pass
            except Exception:
                pass

            status = HealthStatus.HEALTHY
            reason = ""

            from baldur.settings.health_check import get_health_check_settings

            hc_settings = get_health_check_settings()
            threshold = hc_settings.probe_active_recoveries_threshold
            if stuck_recoveries > 0:
                status = HealthStatus.UNHEALTHY
                reason = f"{stuck_recoveries} stuck recovery jobs detected"
            elif active_recoveries > threshold:
                status = HealthStatus.DEGRADED
                reason = (
                    f"{active_recoveries} active recoveries (threshold: {threshold})"
                )

            return ProbeResult(
                component=self.component_name,
                status=status,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                reason=reason,
                details={
                    "active_recoveries": active_recoveries,
                    "stuck_recoveries": stuck_recoveries,
                },
            )
        except Exception as e:
            return ProbeResult(
                component=self.component_name,
                status=HealthStatus.UNKNOWN,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                error=str(e),
            )


class RedisProbe(HealthProbe):
    """
    Redis health probe.

    Checks:
    - Connectivity (PING)
    - Response time
    - Memory usage
    """

    @property
    def component_name(self) -> str:
        return "redis"

    def probe(self) -> ProbeResult:
        start = time.time()
        try:
            # Try to obtain a Redis client
            redis_client = None
            try:
                from baldur.adapters.cache.redis_adapter import RedisCacheAdapter

                adapter = RedisCacheAdapter()
                redis_client = adapter._redis
            except ImportError:
                pass
            except Exception:
                pass

            if redis_client is None:
                return ProbeResult(
                    component=self.component_name,
                    status=HealthStatus.UNKNOWN,
                    latency_ms=(time.time() - start) * 1000,
                    timestamp=utc_now(),
                    error="Redis client not available",
                )

            # PING test
            redis_client.ping()

            # INFO lookup
            used_memory = 0
            max_memory = 0
            memory_usage_ratio = 0.0

            try:
                info = redis_client.info(section="memory")
                used_memory = info.get("used_memory", 0)
                max_memory = info.get("maxmemory", 0)
                if max_memory > 0:
                    memory_usage_ratio = used_memory / max_memory
            except Exception:
                pass

            from baldur.settings.health_check import get_health_check_settings

            hc_settings = get_health_check_settings()
            status = HealthStatus.HEALTHY
            reason = ""
            threshold = hc_settings.probe_memory_usage_threshold

            # Memory usage above the threshold → DEGRADED
            if memory_usage_ratio > threshold:
                status = HealthStatus.DEGRADED
                reason = f"Redis memory usage at {memory_usage_ratio:.0%} (threshold: {threshold:.0%})"

            return ProbeResult(
                component=self.component_name,
                status=status,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                reason=reason,
                details={
                    "used_memory_bytes": used_memory,
                    "max_memory_bytes": max_memory,
                    "memory_usage_ratio": memory_usage_ratio,
                },
            )
        except Exception as e:
            return ProbeResult(
                component=self.component_name,
                status=HealthStatus.UNHEALTHY,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                error=str(e),
            )


class ChaosSchedulerProbe(HealthProbe):
    """Detect zombie experiments via experiment TTL + global fallback threshold."""

    @property
    def component_name(self) -> str:
        return "chaos_scheduler"

    def is_applicable(self) -> bool:
        """Chaos is a default-disabled feature; probe only when enabled."""
        from baldur.settings.chaos import get_chaos_settings

        return get_chaos_settings().enabled

    def probe(self) -> ProbeResult:  # noqa: C901
        start = time.time()
        try:
            from baldur.factory.registry import ProviderRegistry
            from baldur.settings.chaos import get_chaos_settings

            scheduler = ProviderRegistry.chaos_scheduler.safe_get()
            if scheduler is None:
                raise RuntimeError("baldur_pro ChaosScheduler not registered")
            settings = get_chaos_settings()
            running = scheduler.get_running_experiments()

            if not running:
                return ProbeResult(
                    component=self.component_name,
                    status=HealthStatus.HEALTHY,
                    latency_ms=(time.time() - start) * 1000,
                    timestamp=utc_now(),
                )

            current_mono = time.monotonic()
            zombies = []

            for schedule_id, info in running.items():
                is_zombie = False

                # Primary: experiment's own TTL (same logic as zombie hunter)
                instance = scheduler._get_experiment_instance(info.experiment_id)
                if instance:
                    if hasattr(instance, "_is_expired_monotonic"):
                        is_zombie = instance._is_expired_monotonic()
                    elif hasattr(instance, "is_expired"):
                        is_zombie = instance.is_expired()

                # Fallback: global threshold (TTL not set or instance already gone)
                if not is_zombie:
                    elapsed = current_mono - info.started_at_monotonic
                    if elapsed > settings.experiment_timeout_seconds:
                        is_zombie = True

                if is_zombie:
                    zombies.append(
                        {
                            "schedule_id": schedule_id,
                            "experiment_id": info.experiment_id,
                        }
                    )

            if zombies:
                return ProbeResult(
                    component=self.component_name,
                    status=HealthStatus.DEGRADED,
                    latency_ms=(time.time() - start) * 1000,
                    timestamp=utc_now(),
                    reason=f"{len(zombies)} zombie experiments detected",
                    details={
                        "zombie_count": len(zombies),
                        "zombie_experiments": zombies,
                    },
                )

            return ProbeResult(
                component=self.component_name,
                status=HealthStatus.HEALTHY,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                details={"running_count": len(running)},
            )

        except Exception as e:
            return ProbeResult(
                component=self.component_name,
                status=HealthStatus.UNKNOWN,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                error=str(e),
            )


def _probe_is_applicable(probe: HealthProbe) -> bool:
    """Return whether ``probe`` should run this cycle.

    Duck-typed because not every registered probe inherits the ABC
    (AuditSystemProbe is a structural HealthProbe), and fail-safe: any error
    determining applicability falls back to ``True`` so a transient
    settings-read failure never silently hides a real component.
    """
    check = getattr(probe, "is_applicable", None)
    if check is None:
        return True
    try:
        return bool(check())
    except Exception:  # noqa: BLE001
        return True


class HealthProbeManager:
    """
    Health Probe Manager.

    Manages a set of probes and runs them periodically to collect the health
    status of the subsystems.

    Example:
        manager = HealthProbeManager()
        manager.start()  # start background probing

        # Query the current status
        results = manager.get_last_results()
        overall = manager.get_overall_status()

        manager.stop()  # stop probing
    """

    def __init__(
        self,
        settings: MetaWatchdogSettings | None = None,
        probes: list[HealthProbe] | None = None,
    ):
        """
        Initialize.

        Args:
            settings: Meta-Watchdog settings (defaults when None)
            probes: probes to run (default probe set when None)
        """
        self._settings = settings or get_meta_watchdog_settings()
        self._probes = probes if probes is not None else self._create_default_probes()
        self._lock = threading.RLock()
        self._last_results: dict[str, ProbeResult] = {}
        self._running = False
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._handle: DaemonWorkerHandle | None = None  # impl 489 D9
        # Single-flight, keyed by COMPONENT NAME rather than by probe instance:
        # remove_probe(name) + add_probe(new_instance) is a public API pair that
        # installs a fresh object under the same name, and the external state a
        # probe touches belongs to the component, not to the object. Instance
        # keying would let a stranded old invocation run beside the new one.
        self._single_flight_locks: dict[str, threading.Lock] = {}
        self._single_flight_skips: dict[str, int] = {}

    def _create_default_probes(self) -> list[HealthProbe]:
        """Build the default probe list."""
        from baldur.meta.audit_probe import AuditSystemProbe
        from baldur.meta.cache_probe import PrecomputedCacheProbe
        from baldur.meta.canary_stuck_probe import CanaryStuckProbe
        from baldur.meta.emergency_stuck_probe import EmergencyStuckProbe
        from baldur.meta.error_budget_gate_probe import ErrorBudgetGateProbe
        from baldur.meta.notification_probe import NotificationChannelProbe
        from baldur.meta.throttle_stuck_probe import ThrottleStuckProbe

        # AuditSystemProbe is a structural HealthProbe (same component_name +
        # probe surface) but does not declare the ABC inheritance because its
        # probe() returns a richer AuditProbeResult dataclass. Treated as a
        # HealthProbe at the registration boundary.
        #
        # The three semantic-stuck probes (canary/emergency/throttle) resolve
        # their PRO service lazily via ProviderRegistry.*.safe_get(); each is
        # inert (is_applicable() False) in an OSS-only checkout, so registering
        # them unconditionally is safe.
        return [
            CircuitBreakerProbe(),
            DLQProbe(),
            DaemonWorkerProbe(),
            RecoveryPipelineProbe(),
            RedisProbe(),
            AuditSystemProbe(),  # type: ignore[list-item]
            ChaosSchedulerProbe(),
            NotificationChannelProbe(),
            PrecomputedCacheProbe(),
            ErrorBudgetGateProbe(),
            CanaryStuckProbe(),
            EmergencyStuckProbe(),
            ThrottleStuckProbe(),
        ]

    def add_probe(self, probe: HealthProbe) -> None:
        """
        Add a probe.

        Args:
            probe: probe to add
        """
        with self._lock:
            self._probes.append(probe)

    def remove_probe(self, component_name: str) -> bool:
        """
        Remove a probe.

        Args:
            component_name: component name of the probe to remove

        Returns:
            Whether the probe was removed
        """
        with self._lock:
            for i, probe in enumerate(self._probes):
                if probe.component_name == component_name:
                    self._probes.pop(i)
                    return True
            return False

    def probe_all(self, deadline: float | None = None) -> dict[str, ProbeResult]:
        """
        Run all probes, optionally under a wall-clock budget for the whole pass.

        Args:
            deadline: a ``time.monotonic()`` instant by which the pass must be
                over. With a deadline the probes run concurrently on daemon
                threads and any probe still running at the deadline yields
                ``UNKNOWN`` instead of extending the pass — coverage is the
                product function here, so every probe gets the full window
                rather than the tail of the list starving behind a stalled
                head. Without one (``None``), the historical serial
                per-probe-timeout behavior is preserved for callers that have
                no pass budget to protect.

        Returns:
            Per-component probe results. Components whose feature is inactive
            (``is_applicable()`` False) are absent, never ``UNKNOWN``.
        """
        if sys.is_finalizing():
            return {}
        if deadline is None:
            return self._probe_all_serially()
        return self._probe_all_within(deadline)

    def _probe_all_serially(self) -> dict[str, ProbeResult]:
        """Run every applicable probe one at a time under its own timeout.

        Single-flight applies here too. A timed-out probe on this path is
        abandoned exactly as on the bounded path — the executor stops waiting,
        the call keeps running — so without the same lock a stranded invocation
        would run beside the next pass's, which is the very race the lock
        advertises against. One guarantee, not one per execution path.
        """
        from baldur.core.timeout_executor import TimeoutExecutor

        timeout = self._settings.probe_timeout_seconds
        results: dict[str, ProbeResult] = {}

        executor = TimeoutExecutor()

        def _probe_runner(bound_probe: HealthProbe, name: str) -> Any:
            def _run(stop_event: Any) -> Any:
                # Acquired and released ON THE WORKER, never around
                # executor.execute(): a timed-out call here is abandoned, not
                # cancelled, so a lock the caller released at the timeout would
                # leave the still-running invocation unguarded — which is the
                # race this lock exists to close.
                lock = self._single_flight_lock(name)
                acquired = lock.acquire(blocking=False)
                try:
                    # Applicability first, for the same reason as the bounded
                    # path: an inactive component is absent, never UNKNOWN.
                    if not _probe_is_applicable(bound_probe):
                        return _NOT_APPLICABLE
                    if not acquired:
                        return self._single_flight_skipped_result(name)
                    with self._lock:
                        self._single_flight_skips.pop(name, None)
                    return bound_probe.probe()
                finally:
                    if acquired:
                        lock.release()

            return _run

        for probe in self._probes:
            if sys.is_finalizing():
                break
            name = probe.component_name
            try:
                result = executor.execute(
                    fn=_probe_runner(probe, name),
                    timeout_seconds=timeout,
                )
            except Exception as e:
                logger.warning(
                    "health_probe_manager.probe_failed",
                    probe=name,
                    error=str(e),
                    timeout_seconds=timeout,
                )
                results[name] = ProbeResult(
                    component=name,
                    status=HealthStatus.UNKNOWN,
                    latency_ms=0,
                    timestamp=utc_now(),
                    error=str(e),
                )
                continue
            if result is _NOT_APPLICABLE:
                logger.debug(
                    "health_probe_manager.probe_skipped",
                    probe=name,
                    reason="feature_disabled",
                )
                continue
            results[name] = result

        with self._lock:
            self._last_results = results

        return results

    def _probe_all_within(self, deadline: float) -> dict[str, ProbeResult]:
        """Fan every probe out concurrently and collect until ``deadline``.

        One plain daemon thread per probe — deliberately not a
        ``ThreadPoolExecutor``, whose workers are non-daemon and are joined at
        interpreter exit regardless of any flag, so an abandoned hung probe
        would hold shutdown open. Thread count per pass is unchanged from the
        serial path (one per probe); only their overlap is.
        """
        timeout = self._settings.probe_timeout_seconds
        with self._lock:
            probes = list(self._probes)

        pass_start = time.monotonic()
        # A probe keeps its own timeout, measured from the fan-out (all workers
        # start together), and the pass deadline caps it. Collection is
        # sequential but execution is not, so the collection order costs
        # nothing and no probe is starved by its position in the list.
        collect_until = min(pass_start + timeout, deadline)

        verdicts: dict[str, Any] = {}
        completions: dict[str, threading.Event] = {}
        for probe in probes:
            if sys.is_finalizing():
                break
            name = probe.component_name
            if name in completions:
                continue  # duplicate component name: the first registration wins
            done = threading.Event()
            completions[name] = done
            # Captured on THIS thread: the worker inherits the caller's
            # structlog binding and trace context, matching the serial path.
            ctx = contextvars.copy_context()
            threading.Thread(
                target=ctx.run,
                args=(self._run_probe_worker, probe, name, verdicts, done),
                name=f"{PROBE_WORKER_THREAD_PREFIX}{name}",
                daemon=True,
            ).start()

        results: dict[str, ProbeResult] = {}
        truncated: list[str] = []
        for name, done in completions.items():
            done.wait(max(0.0, collect_until - time.monotonic()))
            verdict = verdicts.get(name)
            if verdict is _NOT_APPLICABLE:
                continue  # absent from the pass, exactly as the serial skip is
            if verdict is None:
                results[name] = self._truncated_result(name)
                truncated.append(name)
                continue
            results[name] = verdict

        # Rebound, never mutated in place: a stranded worker from an earlier
        # pass writes into that pass's own dict, and get_overall_status()
        # iterates its snapshot outside the lock.
        with self._lock:
            self._last_results = results

        if truncated:
            self._report_truncated_pass(truncated, deadline - pass_start)

        return results

    def _run_probe_worker(
        self,
        probe: HealthProbe,
        name: str,
        verdicts: dict[str, Any],
        done: threading.Event,
    ) -> None:
        """Run one probe under its component's single-flight lock (worker thread).

        Writes exactly one verdict into this pass's own dict before signalling
        completion, so a collector that observes the event sees a settled slot.
        ``is_applicable()`` is evaluated here rather than on the caller so a
        slow applicability check is bounded by the same deadline as the probe.
        """
        lock = self._single_flight_lock(name)
        acquired = lock.acquire(blocking=False)
        try:
            if not _probe_is_applicable(probe):
                # Checked BEFORE the single-flight verdict, not after: absence
                # is the stronger contract. A component whose feature is off
                # must not surface as UNKNOWN just because an invocation from
                # the pass that ran while it was still enabled is stranded —
                # that drags the overall verdict to DEGRADED for a component
                # the operator switched off. Still on the worker thread, so a
                # slow applicability check stays bounded by the deadline.
                logger.debug(
                    "health_probe_manager.probe_skipped",
                    probe=name,
                    reason="feature_disabled",
                )
                verdicts[name] = _NOT_APPLICABLE
                return
            if not acquired:
                verdicts[name] = self._single_flight_skipped_result(name)
                return
            with self._lock:
                self._single_flight_skips.pop(name, None)
            verdicts[name] = probe.probe()
        except Exception as e:  # noqa: BLE001 - a probe fault is never fatal
            logger.warning(
                "health_probe_manager.probe_failed",
                probe=name,
                error=str(e),
            )
            verdicts[name] = ProbeResult(
                component=name,
                status=HealthStatus.UNKNOWN,
                latency_ms=0,
                timestamp=utc_now(),
                error=str(e),
            )
        finally:
            if acquired:
                lock.release()
            done.set()

    def _single_flight_lock(self, component_name: str) -> threading.Lock:
        """Return (creating on first use) the single-flight lock for a component."""
        with self._lock:
            lock = self._single_flight_locks.get(component_name)
            if lock is None:
                lock = threading.Lock()
                self._single_flight_locks[component_name] = lock
            return lock

    def _single_flight_skipped_result(self, component_name: str) -> ProbeResult:
        """Build the skip verdict and surface a component stuck across passes."""
        with self._lock:
            skips = self._single_flight_skips.get(component_name, 0) + 1
            self._single_flight_skips[component_name] = skips

        if skips >= _SINGLE_FLIGHT_STUCK_SKIPS:
            logger.warning(
                "health_probe_manager.probe_single_flight_stuck",
                probe=component_name,
                consecutive_skips=skips,
            )
        self._record_single_flight_skip(component_name)

        return ProbeResult(
            component=component_name,
            status=HealthStatus.UNKNOWN,
            latency_ms=0.0,
            timestamp=utc_now(),
            reason=PROBE_SINGLE_FLIGHT_REASON,
            observed=False,
        )

    @staticmethod
    def _truncated_result(component_name: str) -> ProbeResult:
        """Build the verdict for a probe still running at the pass deadline."""
        return ProbeResult(
            component=component_name,
            status=HealthStatus.UNKNOWN,
            latency_ms=0.0,
            timestamp=utc_now(),
            reason=PROBE_TRUNCATED_REASON,
            observed=False,
        )

    @staticmethod
    def _report_truncated_pass(components: list[str], budget_seconds: float) -> None:
        """Warn + count a pass that ran out of budget. Names only the truncated.

        Inapplicable components are never listed here (they were deleted from
        the pass), and neither are single-flight skips (they have their own
        counter) — an operator reading this line is reading the components the
        budget actually cut off.
        """
        logger.warning(
            "watchdog.pass_budget_exhausted",
            truncated_components=components,
            budget_seconds=round(budget_seconds, 3),
        )
        try:
            from baldur.metrics.recorders.watchdog import (
                record_watchdog_pass_truncated,
            )

            record_watchdog_pass_truncated()
        except Exception as e:  # noqa: BLE001 - telemetry is fail-open
            logger.debug("watchdog.pass_truncated_metric_failed", error=e)

    @staticmethod
    def _record_single_flight_skip(component_name: str) -> None:
        """Count a single-flight skip. Fail-open, like every recorder call here."""
        try:
            from baldur.metrics.recorders.watchdog import (
                record_watchdog_single_flight_skipped,
            )

            record_watchdog_single_flight_skipped(component_name)
        except Exception as e:  # noqa: BLE001 - telemetry is fail-open
            logger.debug("watchdog.single_flight_metric_failed", error=e)

    def get_overall_status(self) -> HealthStatus:
        """
        Return the overall health status.

        Returns the most severe status observed.
        UNHEALTHY > DEGRADED > UNKNOWN > HEALTHY

        Returns:
            Overall health status
        """
        with self._lock:
            results = self._last_results

        if not results:
            return HealthStatus.UNKNOWN

        statuses = [r.status for r in results.values()]

        if HealthStatus.UNHEALTHY in statuses:
            return HealthStatus.UNHEALTHY
        if HealthStatus.DEGRADED in statuses:
            return HealthStatus.DEGRADED
        if HealthStatus.UNKNOWN in statuses:
            return HealthStatus.DEGRADED

        return HealthStatus.HEALTHY

    def get_last_results(self) -> dict[str, ProbeResult]:
        """
        Return the last probe results.

        Returns:
            Last probe result per component
        """
        with self._lock:
            return dict(self._last_results)

    def get_component_status(self, component: str) -> HealthStatus | None:
        """
        Return the status of a single component.

        Args:
            component: component name

        Returns:
            That component's status (None when unknown)
        """
        with self._lock:
            result = self._last_results.get(component)
            return result.status if result else None

    def _run_loop(self) -> None:
        """Background probe loop."""
        while self._running:
            iter_start = time.monotonic()
            if sys.is_finalizing():
                break
            try:
                self.probe_all()
            except Exception as e:
                logger.exception(
                    "health_probe_manager.loop_error",
                    error=e,
                )

            if self._handle is not None:
                self._handle.observe_iteration(time.monotonic() - iter_start)
                self._handle.heartbeat()

            self._stop_event.wait(self._settings.probe_interval_seconds)
            if self._stop_event.is_set():
                break

    def _run_loop_with_crash_capture(self) -> None:
        try:
            self._run_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def start(self) -> None:
        """Start background probing."""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        if self._running:
            return

        self._stop_event.clear()
        self._running = True
        self._spawn_worker_thread()
        assert self._worker is not None  # populated by _spawn_worker_thread
        self._handle = DaemonWorkerHandle(
            thread=self._worker,
            tick_interval_seconds=self._settings.probe_interval_seconds,
            restart_callback=self._spawn_worker_thread,
        )
        register_daemon_worker("HealthProbeManager", self._handle)
        logger.info("health_probe_manager.started")

    def _spawn_worker_thread(self) -> None:
        """Construct + start a fresh probe-loop thread (impl 489 D9).

        Guarded on the thread object rather than on the ``_running`` flag: a
        respawn observation made against a stale handle must not start a second
        live loop, while a genuinely dead worker still respawns. Consulting
        ``_running`` here would break the restart-callback contract instead.
        """
        existing = self._worker
        if existing is not None and existing.is_alive():
            logger.debug("health_probe_manager.spawn_skipped_worker_alive")
            return

        self._worker = threading.Thread(
            target=self._run_loop_with_crash_capture,
            name="HealthProbeManager",
            daemon=True,
        )
        self._worker.start()
        if self._handle is not None:
            self._handle.thread = self._worker

    def stop(self) -> None:
        """Stop probing."""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker
        from baldur.settings.health_check import get_health_check_settings

        if self._handle is not None:
            self._handle.is_stopping = True
        self._running = False
        self._stop_event.set()
        if self._worker:
            timeout = get_health_check_settings().probe_worker_join_timeout
            self._worker.join(timeout=timeout)
            unregister_daemon_worker("HealthProbeManager")
            if self._worker.is_alive():
                logger.critical(
                    "daemon_worker.stop_join_timeout",
                    worker_name="HealthProbeManager",
                    join_timeout_seconds=timeout,
                )
            self._worker = None
        logger.info("health_probe_manager.stopped")

    def is_running(self) -> bool:
        """Return whether the probe loop is running."""
        return self._running
