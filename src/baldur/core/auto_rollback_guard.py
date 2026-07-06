"""
Auto Rollback Guard - safety net against autonomous-adjustment failures

An independent safety net that automatically recovers when
RuntimeFeedbackLoop is not working or has made a wrong adjustment.

Core features:
1. Monitor system state via periodic health checks
2. Automatic rollback when severe degradation is detected
3. Detect failures of RuntimeFeedbackLoop itself
4. Emergency recovery mode (restore all settings to safe defaults)

This module is an additional safety net not specified in document 36.

Architecture:
┌─────────────────────────────────────────────────────────────┐
│                    AutoRollbackGuard                         │
│                                                              │
│  ┌──────────────────┐    ┌──────────────────┐               │
│  │ Health Monitor   │───▶│ Rollback Executor│               │
│  │ (indep. thread)  │    │                  │               │
│  └──────────────────┘    └──────────────────┘               │
│           │                       │                          │
│           ▼                       ▼                          │
│  ┌──────────────────┐    ┌──────────────────┐               │
│  │ Degradation      │    │ Safe Defaults    │               │
│  │ Detector         │    │ Registry         │               │
│  └──────────────────┘    └──────────────────┘               │
│                                                              │
└─────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from baldur.settings.auto_rollback import get_auto_rollback_settings
from baldur.utils.time import utc_now  # CLAUDE.md time handling rule

if TYPE_CHECKING:
    from baldur.meta.daemon_worker import (  # noqa: F401
        DaemonWorkerHandle,
    )

logger = structlog.get_logger()


class GuardState(str, Enum):
    """Guard state"""

    INACTIVE = "inactive"
    MONITORING = "monitoring"
    ALERT = "alert"
    EMERGENCY = "emergency"
    RECOVERING = "recovering"


class RollbackSeverity(str, Enum):
    """Degradation level"""

    NONE = "none"
    MINOR = "minor"  # minor - alert only
    MAJOR = "major"  # major - consider rollback
    CRITICAL = "critical"  # critical - immediate rollback


@dataclass
class RollbackHealthAssessment:
    """Health check result"""

    healthy: bool
    degradation_level: RollbackSeverity
    error_rate: float
    latency_p99_ms: float
    throughput_rps: float
    timestamp: datetime = field(default_factory=lambda: utc_now())
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class SafeDefault:
    """Safe default value definition"""

    parameter: str
    safe_value: float
    description: str


class MetricsProvider(Protocol):
    """Metrics provider protocol"""

    def get_error_rate(self) -> float:
        """Current error rate (0.0 ~ 1.0)"""
        ...

    def get_latency_p99(self) -> float:
        """P99 latency (ms)"""
        ...

    def get_throughput(self) -> float:
        """Throughput (rps)"""
        ...


class AutoRollbackGuard:
    """
    Independent safety net against autonomous-adjustment failures

    Operates independently of RuntimeFeedbackLoop to monitor system
    state and automatically recover when a problem occurs.

    Safety Features:
    1. Runs on an independent thread, unaffected by RuntimeFeedbackLoop failures
    2. Automatic rollback after consecutive health-check failures
    3. Emergency mode: restore all settings to safe defaults
    4. Manual trigger support

    Recovery strategies (in priority order):
    1. Last Known Good: roll back to the state right before the change (safest)
    2. DNA Declared: recover to the Desired state declared in the DNA
    3. System Defaults: hardcoded safe defaults (last resort)
    """

    class RecoveryStrategy(str, Enum):
        """Recovery strategy"""

        LAST_KNOWN_GOOD = "last_known_good"  # roll back to the state before the change
        DNA_DECLARED = "dna_declared"  # recover to the DNA-declared value
        SYSTEM_DEFAULTS = "system_defaults"  # hardcoded defaults (last resort)
        PAUSE_ONLY = "pause_only"  # only stop adjusting, keep current value

    # System defaults (truly last resort - used only when neither DNA nor snapshot exists)
    SYSTEM_DEFAULTS: list[SafeDefault] = [
        SafeDefault("timeout_ms", 5000, "System default timeout - conservative value"),
        SafeDefault("retry_count", 3, "System default retry - typical value"),
        SafeDefault("circuit_breaker_threshold", 0.5, "System default CB - mid value"),
        SafeDefault("jitter_range", 0.1, "System default jitter"),
        SafeDefault("rate_limit_rps", 1000, "System default Rate Limit - conservative"),
    ]

    @property
    def ERROR_RATE_MAJOR(self) -> float:
        """Major-level error-rate threshold (10% or more)"""
        return get_auto_rollback_settings().error_rate_major

    @property
    def ERROR_RATE_CRITICAL(self) -> float:
        """Critical-level error-rate threshold (30% or more)"""
        return get_auto_rollback_settings().error_rate_critical

    @property
    def LATENCY_MAJOR_MS(self) -> int:
        """Major-level latency threshold (ms)"""
        return get_auto_rollback_settings().latency_major_ms

    @property
    def LATENCY_CRITICAL_MS(self) -> int:
        """Critical-level latency threshold (ms)"""
        return get_auto_rollback_settings().latency_critical_ms

    @property
    def CONSECUTIVE_FAILURES_ALERT(self) -> int:
        """Consecutive failures that trigger an alert"""
        return get_auto_rollback_settings().failures_alert

    @property
    def CONSECUTIVE_FAILURES_EMERGENCY(self) -> int:
        """Consecutive failures that enter the emergency state"""
        return get_auto_rollback_settings().failures_emergency

    def __init__(
        self,
        metrics_provider: MetricsProvider,
        config_applier,  # ConfigApplier
        alert_callback: Callable[[str, str], None] | None = None,
        check_interval_seconds: int = 30,
        enabled: bool = True,
        event_publisher: Callable[[dict], None] | None = None,
    ):
        self.metrics_provider = metrics_provider
        self.config_applier = config_applier
        self.alert_callback = alert_callback
        self.check_interval_seconds = check_interval_seconds
        self.enabled = enabled
        self._event_publisher = event_publisher

        # State management
        self._state = GuardState.INACTIVE
        self._lock = threading.RLock()
        self._running = False
        self._stop_event = threading.Event()  # event for fast shutdown
        self._thread: threading.Thread | None = None
        self._handle: DaemonWorkerHandle | None = None  # impl 489 D9

        # Health check history
        self._health_history: list[RollbackHealthAssessment] = []
        self._consecutive_failures = 0
        self._last_rollback_time: datetime | None = None

        # Config snapshots (for rollback)
        self._config_snapshots: dict[str, list[dict[str, Any]]] = {}

        logger.info("auto_rollback_guard.initialized")

    @property
    def state(self) -> GuardState:
        """Current state"""
        return self._state

    def start(self) -> bool:
        """Start the guard"""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        with self._lock:
            if self._running:
                return False

            self._running = True
            self._stop_event.clear()  # reset event
            self._state = GuardState.MONITORING
            self._spawn_thread()
            assert self._thread is not None  # set by _spawn_thread
            self._handle = DaemonWorkerHandle(
                thread=self._thread,
                tick_interval_seconds=float(self.check_interval_seconds),
                restart_callback=self._spawn_thread,
            )
            register_daemon_worker("AutoRollbackGuard", self._handle)
            logger.info("auto_rollback_guard.started_monitoring")
            return True

    def _spawn_thread(self) -> None:
        """Construct + start a fresh monitor thread (impl 489 D9)."""
        self._thread = threading.Thread(
            target=self._monitor_loop_with_crash_capture,
            daemon=True,
            name="AutoRollbackGuard",
        )
        self._thread.start()
        if self._handle is not None:
            self._handle.thread = self._thread

    def _monitor_loop_with_crash_capture(self) -> None:
        try:
            self._monitor_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def stop(self) -> bool:
        """Stop the guard"""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        with self._lock:
            if self._handle is not None:
                self._handle.is_stopping = True
            self._running = False
            self._state = GuardState.INACTIVE
            self._stop_event.set()  # immediately wake the waiting thread

        if self._thread:
            self._thread.join(timeout=1)  # 1s is enough (woken immediately by Event)
            unregister_daemon_worker("AutoRollbackGuard")
            if self._thread.is_alive():
                logger.critical(
                    "daemon_worker.stop_join_timeout",
                    worker_name="AutoRollbackGuard",
                    join_timeout_seconds=1.0,
                )
            self._thread = None

        logger.info("auto_rollback_guard.stopped")
        return True

    def _monitor_loop(self):
        """Monitoring loop"""
        import time as _time

        while self._running:
            iter_start = _time.monotonic()
            if self.enabled:
                try:
                    self._perform_health_check()
                except Exception as e:
                    logger.exception(
                        "auto_rollback_guard.health_check_error",
                        error=e,
                    )
                    self._consecutive_failures += 1

            if self._handle is not None:
                self._handle.observe_iteration(_time.monotonic() - iter_start)
                self._handle.heartbeat()

            # Event.wait() returns immediately on set() (used instead of time.sleep)
            if self._stop_event.wait(timeout=self.check_interval_seconds):
                break  # shutdown signal received

    def _perform_health_check(self):
        """Perform health check"""
        try:
            error_rate = self.metrics_provider.get_error_rate()
            latency_p99 = self.metrics_provider.get_latency_p99()
            throughput = self.metrics_provider.get_throughput()
        except Exception as e:
            logger.warning(
                "auto_rollback_guard.metrics_fetch_failed",
                error=e,
            )
            self._consecutive_failures += 1
            self._check_failure_threshold()
            return

        # Determine degradation level
        degradation = self._assess_degradation(error_rate, latency_p99)

        result = RollbackHealthAssessment(
            healthy=degradation == RollbackSeverity.NONE,
            degradation_level=degradation,
            error_rate=error_rate,
            latency_p99_ms=latency_p99,
            throughput_rps=throughput,
        )

        # Save history (Phase 2: Settings-based history limit)
        self._health_history.append(result)
        max_health_history = get_auto_rollback_settings().max_health_history
        if len(self._health_history) > max_health_history:
            self._health_history = self._health_history[-max_health_history:]

        # Update state and take action
        self._handle_health_result(result)

    def _assess_degradation(
        self, error_rate: float, latency_p99: float
    ) -> RollbackSeverity:
        """Assess degradation level"""
        # Error-rate-based assessment
        if error_rate >= self.ERROR_RATE_CRITICAL:
            return RollbackSeverity.CRITICAL
        if error_rate >= self.ERROR_RATE_MAJOR:
            return RollbackSeverity.MAJOR

        # Latency-based assessment
        if latency_p99 >= self.LATENCY_CRITICAL_MS:
            return RollbackSeverity.CRITICAL
        if latency_p99 >= self.LATENCY_MAJOR_MS:
            return RollbackSeverity.MAJOR

        # Minor degradation — thresholds from AutoRollbackSettings
        try:
            from baldur.settings.auto_rollback import get_auto_rollback_settings

            _ars = get_auto_rollback_settings()
            minor_error = _ars.error_rate_minor
            minor_latency = _ars.latency_minor_ms
        except Exception:
            minor_error = 0.05
            minor_latency = 3000

        if error_rate >= minor_error or latency_p99 >= minor_latency:
            return RollbackSeverity.MINOR

        return RollbackSeverity.NONE

    def _handle_health_result(self, result: RollbackHealthAssessment):
        """Handle health check result"""
        with self._lock:
            if result.healthy:
                self._consecutive_failures = 0
                if self._state in (GuardState.ALERT, GuardState.RECOVERING):
                    self._state = GuardState.MONITORING
                    logger.info("auto_rollback_guard.system_recovered")
                return

            # Degradation detected
            self._consecutive_failures += 1

            if result.degradation_level == RollbackSeverity.CRITICAL:
                self._handle_critical_degradation(result)
            elif result.degradation_level == RollbackSeverity.MAJOR:
                self._handle_major_degradation(result)
            else:
                self._handle_minor_degradation(result)

    def _handle_minor_degradation(self, result: RollbackHealthAssessment):
        """Handle minor degradation - alert only"""
        if self._consecutive_failures >= self.CONSECUTIVE_FAILURES_ALERT:
            self._state = GuardState.ALERT
            self._send_alert(
                "minor_degradation",
                f"Minor system degradation detected (error rate: {result.error_rate:.1%}, "
                f"latency: {result.latency_p99_ms:.0f}ms)",
            )

    def _handle_major_degradation(self, result: RollbackHealthAssessment):
        """Handle major degradation - consider rollback"""
        self._state = GuardState.ALERT
        self._send_alert(
            "major_degradation",
            f"Major system degradation detected (error rate: {result.error_rate:.1%}, "
            f"latency: {result.latency_p99_ms:.0f}ms). Consider a rollback.",
        )

        # Auto-rollback on consecutive MAJOR degradation
        if self._consecutive_failures >= self.CONSECUTIVE_FAILURES_ALERT:
            self._execute_rollback("major_degradation_consecutive")

    def _handle_critical_degradation(self, result: RollbackHealthAssessment):
        """Handle critical degradation - immediate rollback"""
        self._state = GuardState.EMERGENCY
        self._send_alert(
            "critical_degradation",
            f"🚨 Critical system degradation! (error rate: {result.error_rate:.1%}, "
            f"latency: {result.latency_p99_ms:.0f}ms). Performing immediate rollback.",
        )

        self._execute_emergency_recovery()

    def _check_failure_threshold(self):
        """Check consecutive failure threshold"""
        if self._consecutive_failures >= self.CONSECUTIVE_FAILURES_EMERGENCY:
            self._state = GuardState.EMERGENCY
            self._send_alert(
                "health_check_failed",
                f"Health check failed {self._consecutive_failures} times in a row. "
                f"Performing emergency recovery.",
            )
            self._execute_emergency_recovery()

    def _execute_rollback(self, reason: str):
        """Execute rollback"""
        # Skip if within cooldown after a recent rollback (prevent infinite rollback)
        try:
            from baldur.settings.auto_rollback import get_auto_rollback_settings

            cooldown_min = get_auto_rollback_settings().cooldown_minutes
        except Exception:
            cooldown_min = 5

        if self._last_rollback_time:
            elapsed = utc_now() - self._last_rollback_time
            if elapsed < timedelta(minutes=cooldown_min):
                logger.warning("auto_rollback_guard.rollback_skipped_cooldown")
                return

        self._state = GuardState.RECOVERING
        logger.warning(
            "auto_rollback_guard.executing_rollback",
            reason=reason,
        )

        # Roll back to the most recent snapshot
        rolled_back_params: list[str] = []
        for param, snapshots in self._config_snapshots.items():
            if snapshots:
                last_good = snapshots[-1]
                try:
                    self.config_applier.rollback(param, last_good["value"])
                    rolled_back_params.append(param)
                    logger.info(
                        "auto_rollback_guard.rolled_back",
                        param=param,
                        last_good=last_good["value"],
                    )
                except Exception as e:
                    logger.warning(
                        "auto_rollback_guard.param_rollback_failed",
                        parameter=param,
                        error=str(e),
                    )

        self._last_rollback_time = utc_now()

        # Notify Leader via EventBus
        if self._event_publisher and rolled_back_params:
            try:
                self._event_publisher(
                    {
                        "reason": reason,
                        "parameters": rolled_back_params,
                        "timestamp": utc_now().isoformat(),
                    }
                )
            except Exception:
                logger.debug("auto_rollback_guard.event_publish_failed")

    def _execute_emergency_recovery(self):
        """
        Emergency recovery - 3-tier priority-based recovery

        Recovery priority:
        1. Last Known Good - roll back to the previous snapshot (safest)
        2. DNA Declared - recover to the value declared in the DNA
        3. System Defaults - hardcoded defaults (last resort)

        Why this order?
        - Previous state: it worked at least until then → most trustworthy
        - DNA declared value: the Desired state the operator wants
        - System defaults: conservative values applicable to any service
        """
        self._state = GuardState.EMERGENCY
        logger.critical("auto_rollback_guard.emergency_recovery_starting_tiered")

        self._send_alert(
            "emergency_recovery",
            "🆘 Emergency recovery mode activated! Starting tiered recovery.",
        )

        # Attempt recovery for each parameter
        recovery_results: dict[str, str] = {}

        for safe_default in self.SYSTEM_DEFAULTS:
            param = safe_default.parameter
            recovery_method = self._recover_parameter(param, safe_default.safe_value)
            recovery_results[param] = recovery_method

        # Notify recovery result
        self._send_alert("recovery_complete", f"Recovery complete: {recovery_results}")

        self._last_rollback_time = utc_now()
        self._state = GuardState.RECOVERING

    def _recover_parameter(self, parameter: str, system_default: float) -> str:
        """
        Recover an individual parameter (3-tier priority)

        Returns:
            String of which method recovered it
        """
        # Tier 1: Last Known Good (previous snapshot)
        if parameter in self._config_snapshots and self._config_snapshots[parameter]:
            # Use the oldest snapshot (state right before the last change)
            # Use the first ([0]) instead of the most recent ([-1])
            # because snapshots hold pre-change values, so the first is most stable
            snapshots = self._config_snapshots[parameter]
            if len(snapshots) >= 2:
                # State right before the most recent change (second oldest)
                last_good_value = snapshots[-2]["value"]
            else:
                # If there is only one snapshot, use it
                last_good_value = snapshots[0]["value"]

            try:
                self.config_applier.apply(parameter, last_good_value)
                logger.info(
                    "auto_rollback_guard.recovered_last_known_good",
                    rollback_parameter=parameter,
                    last_good_value=last_good_value,
                )
                return f"last_known_good:{last_good_value}"
            except Exception as e:
                logger.warning(
                    "auto_rollback_guard.last_known_good_failed",
                    rollback_parameter=parameter,
                    error=e,
                )

        # Tier 2: DNA Declared (DNA-declared value)
        dna_value = self._get_dna_declared_value(parameter)
        if dna_value is not None:
            try:
                self.config_applier.apply(parameter, dna_value)
                logger.info(
                    "auto_rollback_guard.recovered_dna_declared",
                    rollback_parameter=parameter,
                    dna_value=dna_value,
                )
                return f"dna_declared:{dna_value}"
            except Exception as e:
                logger.warning(
                    "auto_rollback_guard.dna_declared_failed",
                    rollback_parameter=parameter,
                    error=e,
                )

        # Tier 3: System Defaults (last resort)
        try:
            self.config_applier.apply(parameter, system_default)
            logger.info(
                "auto_rollback_guard.recovered_system_default",
                rollback_parameter=parameter,
                system_default=system_default,
            )
            return f"system_default:{system_default}"
        except Exception as e:
            logger.exception(
                "auto_rollback_guard.all_recovery_failed",
                rollback_parameter=parameter,
                error=e,
            )
            return "FAILED"

    def _get_dna_declared_value(self, parameter: str) -> float | None:
        """
        Get the Desired value declared in the DNA

        The DNA holds the "desired state" declared by the operator.
        It can come from another baldur module or external configuration.
        """
        # TODO: integrate with the actual DNA system
        # For now, assume config_applier has a get_dna_value method
        if hasattr(self.config_applier, "get_dna_value"):
            try:
                return self.config_applier.get_dna_value(parameter)
            except Exception:
                pass

        # If no DNA integration, return None → fall back to System Default
        return None

    def save_snapshot(self, parameter: str, value: float):
        """Save config snapshot (for rollback)"""
        with self._lock:
            if parameter not in self._config_snapshots:
                self._config_snapshots[parameter] = []

            self._config_snapshots[parameter].append(
                {
                    "value": value,
                    "timestamp": utc_now().isoformat(),
                }
            )

            # Keep only the most recent 10
            if len(self._config_snapshots[parameter]) > 10:
                self._config_snapshots[parameter] = self._config_snapshots[parameter][
                    -10:
                ]

    def trigger_manual_emergency(self, reason: str = "manual") -> bool:
        """Manually trigger emergency recovery"""
        logger.warning(
            "auto_rollback_guard.manual_emergency_triggered",
            reason=reason,
        )
        self._execute_emergency_recovery()
        return True

    def _send_alert(self, alert_type: str, message: str):
        """Send alert"""
        if self.alert_callback:
            try:
                self.alert_callback(alert_type, message)
            except Exception as e:
                logger.warning(
                    "auto_rollback_guard.alert_callback_failed",
                    error=e,
                )

        # Always log as well
        logger.warning(
            "auto_rollback_guard.event",
            alert_type=alert_type,
            detail_message=message,
        )

    def get_status(self) -> dict[str, Any]:
        """Get status"""
        with self._lock:
            return {
                "state": self._state.value,
                "enabled": self.enabled,
                "consecutive_failures": self._consecutive_failures,
                "last_rollback": (
                    self._last_rollback_time.isoformat()
                    if self._last_rollback_time
                    else None
                ),
                "health_history_count": len(self._health_history),
                "recent_health": [
                    {
                        "healthy": h.healthy,
                        "degradation": h.degradation_level.value,
                        "error_rate": h.error_rate,
                        "latency_ms": h.latency_p99_ms,
                        "timestamp": h.timestamp.isoformat(),
                    }
                    for h in self._health_history[-5:]
                ],
            }

    def get_safe_defaults(self) -> list[dict[str, Any]]:
        """Get the list of safe defaults (system defaults)"""
        return [
            {
                "parameter": sd.parameter,
                "safe_value": sd.safe_value,
                "description": sd.description,
            }
            for sd in self.SYSTEM_DEFAULTS
        ]

    def update_safe_default(
        self, parameter: str, safe_value: float, description: str | None = None
    ) -> bool:
        """Update a safe default"""
        for sd in self.SYSTEM_DEFAULTS:
            if sd.parameter == parameter:
                sd.safe_value = safe_value
                if description:
                    sd.description = description
                logger.info(
                    "auto_rollback_guard.updated_safe_default",
                    rollback_parameter=parameter,
                    safe_value=safe_value,
                )
                return True

        # Add a new parameter
        self.SYSTEM_DEFAULTS.append(
            SafeDefault(
                parameter=parameter,
                safe_value=safe_value,
                description=description or f"Safe default for {parameter}",
            )
        )
        return True


__all__ = [
    "AutoRollbackGuard",
    "GuardState",
    "RollbackSeverity",
    "RollbackHealthAssessment",
    "SafeDefault",
]
