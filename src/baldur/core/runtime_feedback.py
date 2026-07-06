"""
Runtime Feedback Loop - real-time metric-based auto-tuning

Autonomous adjustment system in the style of Netflix Hystrix and Google Autopilot.

Core flow:
1. Collect metrics (Prometheus/Datadog/Internal)
2. Decide whether adjustment is needed (Decision Engine)
3. Validate safety bounds (Safety Bounds)
4. Apply config + audit log + alert
5. Auto-rollback on failure (Fallback safety net)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from baldur.settings.runtime_feedback import get_runtime_feedback_settings
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.meta.daemon_worker import (  # noqa: F401
        DaemonWorkerHandle,
    )

logger = structlog.get_logger()


class FeedbackLoopState(str, Enum):
    """Feedback loop state"""

    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"


@dataclass
class AdjustmentResult:
    """Adjustment result"""

    success: bool
    parameter: str
    old_value: float
    new_value: float
    reason: str
    timestamp: datetime = field(default_factory=lambda: utc_now())
    error: str | None = None
    rollback_available: bool = True


class MetricsAdapter(Protocol):
    """Metrics collection adapter protocol"""

    def fetch_current_metrics(self) -> dict[str, float]:
        """Fetch current metrics"""
        ...


class ConfigApplier(Protocol):
    """Config applier protocol"""

    def get_current(self, parameter: str) -> float:
        """Get current value"""
        ...

    def apply(self, parameter: str, value: float) -> bool:
        """Apply config"""
        ...

    def rollback(self, parameter: str, value: float) -> bool:
        """Apply rollback"""
        ...


class RuntimeFeedbackLoop:
    """
    Real-time feedback loop

    Auto-tuning with an auto-rollback safety net on failure.

    Safety Features:
    - Snapshot the previous value before adjustment
    - Health check after adjustment (metrics degradation detection)
    - Automatic rollback when a problem is detected
    - Pause the feedback loop after consecutive failures

    Setting values can be overridden via environment variables through
    RuntimeFeedbackSettings:
    - BALDUR_RUNTIME_FEEDBACK_MAX_CONSECUTIVE_FAILURES
    - BALDUR_RUNTIME_FEEDBACK_ROLLBACK_COOLDOWN
    - BALDUR_RUNTIME_FEEDBACK_ADJUSTMENT_WAIT
    """

    @property
    def MAX_CONSECUTIVE_FAILURES(self) -> int:
        """Maximum consecutive failures - auto-pause when exceeded"""
        return get_runtime_feedback_settings().max_consecutive_failures

    @property
    def POST_ROLLBACK_COOLDOWN(self) -> int:
        """Stabilization wait time after rollback (seconds)"""
        return get_runtime_feedback_settings().rollback_cooldown

    @property
    def POST_ADJUSTMENT_WAIT(self) -> int:
        """Wait time to confirm effect after adjustment (seconds)"""
        return get_runtime_feedback_settings().adjustment_wait

    def __init__(
        self,
        metrics_adapter: MetricsAdapter,
        decision_engine,  # DecisionEngine
        safety_bounds,  # SafetyBounds
        audit_adapter,  # AuditLogAdapter
        alert_manager,  # GateAlertManager or compatible interface
        config_applier: ConfigApplier,
        enabled: bool = True,
        interval_seconds: int = 60,
        auto_rollback_enabled: bool = True,
    ):
        self.metrics_adapter = metrics_adapter
        self.decision_engine = decision_engine
        self.safety_bounds = safety_bounds
        self.audit_adapter = audit_adapter
        self.alert_manager = alert_manager
        self.config_applier = config_applier

        self.enabled = enabled
        self.interval_seconds = interval_seconds
        self.auto_rollback_enabled = auto_rollback_enabled

        # State management
        self._state = FeedbackLoopState.STOPPED
        self._lock = threading.RLock()
        self._running = False
        self._stop_event = threading.Event()  # event for fast shutdown
        self._thread: threading.Thread | None = None
        self._handle: DaemonWorkerHandle | None = None  # impl 489 D9

        # Snapshot for rollback
        self._adjustment_history: list[AdjustmentResult] = []
        self._snapshot_before_adjustment: dict[str, float] = {}
        self._consecutive_failures = 0
        self._last_rollback_time: datetime | None = None

        # Metrics baseline (pre-adjustment baseline)
        self._baseline_metrics: dict[str, float] | None = None

        logger.info("runtime_feedback.initialized")

    @property
    def state(self) -> FeedbackLoopState:
        """Current state"""
        return self._state

    def start(self) -> bool:
        """Start the feedback loop"""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        with self._lock:
            if self._running:
                logger.warning("runtime_feedback.already_running")
                return False

            self._running = True
            self._stop_event.clear()  # reset event
            self._state = FeedbackLoopState.RUNNING
            self._spawn_thread()
            assert self._thread is not None  # set by _spawn_thread
            self._handle = DaemonWorkerHandle(
                thread=self._thread,
                tick_interval_seconds=float(self.interval_seconds),
                restart_callback=self._spawn_thread,
            )
            register_daemon_worker("RuntimeFeedback", self._handle)
            logger.info("runtime_feedback.started")
            return True

    def _spawn_thread(self) -> None:
        """Construct + start a fresh feedback loop thread (impl 489 D9)."""
        self._thread = threading.Thread(
            target=self._run_loop_with_crash_capture,
            daemon=True,
            name="RuntimeFeedback",
        )
        self._thread.start()
        if self._handle is not None:
            self._handle.thread = self._thread

    def _run_loop_with_crash_capture(self) -> None:
        try:
            self._run_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def stop(self) -> bool:
        """Stop the feedback loop"""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        with self._lock:
            if self._handle is not None:
                self._handle.is_stopping = True
            self._running = False
            self._state = FeedbackLoopState.STOPPED
            self._stop_event.set()  # immediately wake the waiting thread

        if self._thread:
            self._thread.join(timeout=1)  # 1s is enough (woken immediately by Event)
            unregister_daemon_worker("RuntimeFeedback")
            if self._thread.is_alive():
                logger.critical(
                    "daemon_worker.stop_join_timeout",
                    worker_name="RuntimeFeedback",
                    join_timeout_seconds=1.0,
                )
            self._thread = None

        logger.info("runtime_feedback.stopped")
        return True

    def pause(self, reason: str = "manual") -> bool:
        """Pause the feedback loop"""
        with self._lock:
            self._state = FeedbackLoopState.PAUSED
            logger.warning(
                "runtime_feedback.paused",
                reason=reason,
            )
            self._send_alert(
                "feedback_loop_paused", f"RuntimeFeedback paused: {reason}"
            )
            return True

    def resume(self) -> bool:
        """Resume the feedback loop"""
        with self._lock:
            if not self._running:
                logger.warning("runtime_feedback.running_cannot_resume")
                return False

            self._state = FeedbackLoopState.RUNNING
            self._consecutive_failures = 0
            logger.info("runtime_feedback.resumed")
            return True

    def _run_loop(self):
        """Main loop"""
        import time as _time

        while self._running:
            iter_start = _time.monotonic()
            try:
                if self._state == FeedbackLoopState.RUNNING and self.enabled:
                    self.observe_and_adjust()
            except Exception as e:
                logger.exception(
                    "runtime_feedback.loop_error",
                    error=e,
                )
                self._handle_loop_error(e)

            if self._handle is not None:
                self._handle.observe_iteration(_time.monotonic() - iter_start)
                self._handle.heartbeat()

            # Event.wait() returns immediately on set() (used instead of time.sleep)
            if self._stop_event.wait(timeout=self.interval_seconds):
                break  # shutdown signal received

    def _handle_loop_error(self, error: Exception):
        """Handle loop error"""
        with self._lock:
            self._consecutive_failures += 1

            if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                self.pause(
                    f"{self._consecutive_failures} consecutive failures: {error}"
                )
                self._state = FeedbackLoopState.ERROR

    def observe_and_adjust(self) -> dict[str, Any]:
        """
        Observe and adjust.

        Returns:
            Adjustment result dictionary
        """
        # Rollback cooldown check
        if self._is_in_rollback_cooldown():
            return {"adjusted": False, "reason": "in_rollback_cooldown"}

        # 1. Collect metrics
        try:
            metrics = self.metrics_adapter.fetch_current_metrics()
        except Exception as e:
            logger.exception(
                "runtime_feedback.metrics_fetch_failed",
                error=e,
            )
            return {
                "adjusted": False,
                "reason": "metrics_fetch_failed",
                "error": str(e),
            }

        # 2. Save baseline (on first run)
        if self._baseline_metrics is None:
            self._baseline_metrics = metrics.copy()

        # 3. Decide adjustments
        decisions = self.decision_engine.analyze(metrics)

        if not decisions:
            self._consecutive_failures = 0  # normal cycle
            return {"adjusted": False, "reason": "no_adjustment_needed"}

        adjustments_made = []

        for decision in decisions:
            result = self._apply_single_adjustment(decision, metrics)
            if result:
                adjustments_made.append(result)

        # 4. Post-adjustment health check (when auto-rollback is enabled)
        if self.auto_rollback_enabled and adjustments_made:
            self._schedule_health_check(adjustments_made, metrics)

        return {
            "adjusted": len(adjustments_made) > 0,
            "adjustments": [self._result_to_dict(r) for r in adjustments_made],
        }

    def _apply_single_adjustment(
        self,
        decision,
        current_metrics: dict[str, float],  # AdjustmentDecision
    ) -> AdjustmentResult | None:
        """Apply a single adjustment"""
        # 1. Validate safety bounds
        if not self.safety_bounds.is_within_bounds(
            decision.parameter, decision.suggested_value
        ):
            logger.warning(
                "runtime_feedback.rejected_safety_bounds",
                decision=decision.parameter,
                suggested_value=decision.suggested_value,
            )
            return None

        # 2. Snapshot current value (for rollback)
        old_value = self.config_applier.get_current(decision.parameter)
        self._snapshot_before_adjustment[decision.parameter] = old_value

        # 3. Apply config
        try:
            success = self.config_applier.apply(
                decision.parameter, decision.suggested_value
            )
        except Exception as e:
            logger.exception(
                "runtime_feedback.apply_failed",
                error=e,
            )
            return AdjustmentResult(
                success=False,
                parameter=decision.parameter,
                old_value=old_value,
                new_value=decision.suggested_value,
                reason=decision.reason,
                error=str(e),
            )

        if not success:
            return None

        result = AdjustmentResult(
            success=True,
            parameter=decision.parameter,
            old_value=old_value,
            new_value=decision.suggested_value,
            reason=decision.reason,
        )

        # 4. Save history
        self._adjustment_history.append(result)

        # 5. Audit log
        self._record_audit(result)

        # 6. Alert
        self._send_auto_tuning_alert(result)

        self._consecutive_failures = 0
        return result

    def _schedule_health_check(
        self, adjustments: list[AdjustmentResult], pre_metrics: dict[str, float]
    ):
        """
        Schedule post-adjustment health check.

        Confirm the adjustment's effect in a separate thread and roll back on problems.
        """

        def _health_check():
            time.sleep(self.POST_ADJUSTMENT_WAIT)

            try:
                post_metrics = self.metrics_adapter.fetch_current_metrics()

                if self._detect_degradation(pre_metrics, post_metrics):
                    logger.warning(
                        "runtime_feedback.degradation_detected_initiating_rollback"
                    )
                    self._rollback_adjustments(adjustments)
            except Exception as e:
                logger.exception(
                    "runtime_feedback.health_check_failed",
                    error=e,
                )

        thread = threading.Thread(target=_health_check, daemon=True)
        thread.start()

    def _detect_degradation(
        self, pre_metrics: dict[str, float], post_metrics: dict[str, float]
    ) -> bool:
        """
        Detect metric degradation.

        Detects error-rate increases, latency spikes, etc.
        """
        # Detect error-rate increase (increase of 20% or more)
        pre_error = pre_metrics.get("error_rate", 0)
        post_error = post_metrics.get("error_rate", 0)

        # Load degradation thresholds from settings
        try:
            from baldur.settings.runtime_feedback import (
                get_runtime_feedback_settings,
            )

            _rfs = get_runtime_feedback_settings()
            error_thresh = _rfs.error_increase_threshold
            zero_to_error = _rfs.zero_to_error_threshold
            latency_thresh = _rfs.latency_increase_threshold
        except Exception:
            error_thresh = 0.2
            zero_to_error = 0.05
            latency_thresh = 0.5

        if post_error > 0 and pre_error > 0:
            error_increase = (post_error - pre_error) / pre_error
            if error_increase > error_thresh:
                logger.warning(
                    "runtime_feedback.error_rate_increased",
                    error_increase=error_increase,
                )
                return True
        elif post_error > zero_to_error and pre_error == 0:
            # spike from zero
            return True

        # Detect latency spike
        pre_latency = pre_metrics.get("p99_latency_ms", 0)
        post_latency = post_metrics.get("p99_latency_ms", 0)

        if post_latency > 0 and pre_latency > 0:
            latency_increase = (post_latency - pre_latency) / pre_latency
            if latency_increase > latency_thresh:
                logger.warning(
                    "runtime_feedback.latency_increased",
                    latency_increase=latency_increase,
                )
                return True

        return False

    def _rollback_adjustments(self, adjustments: list[AdjustmentResult]):
        """
        Execute adjustment rollback.

        Roll back in reverse order, most recent adjustment first.
        """
        for result in reversed(adjustments):
            if not result.rollback_available:
                continue

            try:
                success = self.config_applier.rollback(
                    result.parameter, result.old_value
                )

                if success:
                    logger.info(
                        "runtime_feedback.rolled_back",
                        adjusted_parameter=result.parameter,
                        new_value=result.new_value,
                        old_value=result.old_value,
                    )
                    self._record_rollback_audit(result)
                    self._send_rollback_alert(result)
            except Exception as e:
                logger.exception(
                    "runtime_feedback.rollback_failed",
                    error=e,
                )

        # Start rollback cooldown
        with self._lock:
            self._last_rollback_time = utc_now()
            self._consecutive_failures += 1

    def _is_in_rollback_cooldown(self) -> bool:
        """Check whether in rollback cooldown"""
        with self._lock:
            if self._last_rollback_time is None:
                return False

            elapsed = (utc_now() - self._last_rollback_time).total_seconds()
            return elapsed < self.POST_ROLLBACK_COOLDOWN

    def _record_audit(self, result: AdjustmentResult):
        """Record audit log"""
        try:
            from baldur.interfaces.audit_adapter import AuditAction, AuditEntry

            entry = AuditEntry(
                action=AuditAction.CONFIG_CHANGE,
                target_type="auto_tuning",
                target_id=result.parameter,
                details={
                    "type": "automatic_adjustment",
                    "old_value": result.old_value,
                    "new_value": result.new_value,
                    "reason": result.reason,
                },
                actor_type="system",
                actor_id="runtime_feedback_loop",
            )
            self.audit_adapter.log(entry)
        except Exception as e:
            logger.warning(
                "runtime_feedback.audit_log_failed",
                error=e,
            )

    def _record_rollback_audit(self, result: AdjustmentResult):
        """Record rollback audit log"""
        try:
            from baldur.interfaces.audit_adapter import AuditAction, AuditEntry

            entry = AuditEntry(
                action=AuditAction.CONFIG_CHANGE,
                target_type="auto_tuning_rollback",
                target_id=result.parameter,
                details={
                    "type": "automatic_rollback",
                    "rolled_back_from": result.new_value,
                    "rolled_back_to": result.old_value,
                    "original_reason": result.reason,
                    "rollback_reason": "degradation_detected",
                },
                actor_type="system",
                actor_id="runtime_feedback_loop",
            )
            self.audit_adapter.log(entry)
        except Exception as e:
            logger.warning(
                "runtime_feedback.rollback_audit_log_failed",
                error=e,
            )

    def _send_auto_tuning_alert(self, result: AdjustmentResult):
        """Auto-tuning alert"""
        try:
            if hasattr(self.alert_manager, "send_auto_tuning_alert"):
                self.alert_manager.send_auto_tuning_alert(
                    parameter=result.parameter,
                    old_value=result.old_value,
                    new_value=result.new_value,
                    reason=result.reason,
                )
            else:
                self._send_alert(
                    "auto_tuning",
                    f"Auto-tuning: {result.parameter} {result.old_value} → {result.new_value}",
                )
        except Exception as e:
            logger.warning(
                "runtime_feedback.alert_failed",
                error=e,
            )

    def _send_rollback_alert(self, result: AdjustmentResult):
        """Rollback alert"""
        self._send_alert(
            "auto_rollback",
            f"🔄 Auto-rollback: {result.parameter} {result.new_value} → {result.old_value} "
            f"(reason: metric degradation detected)",
        )

    def _send_alert(self, alert_type: str, message: str):
        """Generic alert"""
        try:
            if hasattr(self.alert_manager, "_send_notification"):
                self.alert_manager._send_notification(
                    title=f"[RuntimeFeedback] {alert_type}",
                    message=message,
                    severity="warning",
                )
            else:
                logger.info(
                    "runtime_feedback.event",
                    alert_type=alert_type,
                    detail_message=message,
                )
        except Exception as e:
            logger.warning(
                "runtime_feedback.alert_failed",
                error=e,
            )

    def _result_to_dict(self, result: AdjustmentResult) -> dict[str, Any]:
        """Convert result to dictionary"""
        return {
            "success": result.success,
            "parameter": result.parameter,
            "old_value": result.old_value,
            "new_value": result.new_value,
            "reason": result.reason,
            "timestamp": result.timestamp.isoformat(),
            "error": result.error,
        }

    def get_status(self) -> dict[str, Any]:
        """Get status"""
        with self._lock:
            return {
                "state": self._state.value,
                "enabled": self.enabled,
                "interval_seconds": self.interval_seconds,
                "auto_rollback_enabled": self.auto_rollback_enabled,
                "consecutive_failures": self._consecutive_failures,
                "in_rollback_cooldown": self._is_in_rollback_cooldown(),
                "adjustment_count": len(self._adjustment_history),
                "last_adjustments": [
                    self._result_to_dict(r) for r in self._adjustment_history[-5:]
                ],
            }

    def manual_rollback(self, parameter: str) -> bool:
        """Manual rollback"""
        with self._lock:
            if parameter not in self._snapshot_before_adjustment:
                logger.warning(
                    "runtime_feedback.no_snapshot",
                    rollback_parameter=parameter,
                )
                return False

            old_value = self._snapshot_before_adjustment[parameter]

            try:
                success = self.config_applier.rollback(parameter, old_value)
                if success:
                    logger.info(
                        "runtime_feedback.manual_rollback",
                        rollback_parameter=parameter,
                        old_value=old_value,
                    )
                return success
            except Exception as e:
                logger.exception(
                    "runtime_feedback.manual_rollback_failed",
                    error=e,
                )
                return False


__all__ = [
    "RuntimeFeedbackLoop",
    "FeedbackLoopState",
    "AdjustmentResult",
    "MetricsAdapter",
    "ConfigApplier",
]
