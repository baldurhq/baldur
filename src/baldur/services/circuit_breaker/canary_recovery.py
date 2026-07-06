"""
Circuit Breaker Canary Recovery — staged half-open traffic ramp.

Instead of sending 100% traffic immediately in the HALF_OPEN state, traffic is
increased gradually to prevent a Thundering Herd. This is the circuit breaker's
own recovery strategy, operating on in-process traffic to a single dependency.
It is distinct from the PRO "Canary Recovery" feature, which rolls a fleet-wide
configuration change out in stages and auto-restores the previous configuration
on failure — a different concern at a different tier.

State machine:
    OPEN → HALF_OPEN → CANARY_1(10%) → CANARY_2(30%) → CANARY_3(60%) → CLOSED(100%)
                                ↓              ↓              ↓
                            on failure, revert to OPEN ───────┘
"""

from __future__ import annotations

import random
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.services.circuit_breaker.models import (
    CanaryRecoveryStageConfig,
    RecoveryStrategy,
)
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# =============================================================================
# Canary State
# =============================================================================


class CanaryRecoveryStage(str, Enum):
    """
    Canary Recovery state.

    Represents the gradual recovery stages after the HALF_OPEN state.
    """

    NOT_IN_CANARY = "not_in_canary"  # Not in Canary recovery
    CANARY_1 = "canary_1"  # Stage 1: 10% traffic
    CANARY_2 = "canary_2"  # Stage 2: 30% traffic
    CANARY_3 = "canary_3"  # Stage 3: 60% traffic
    CANARY_4 = "canary_4"  # Stage 4: 100% traffic (just before CLOSED)


# =============================================================================
# Canary Stage Metrics
# =============================================================================


@dataclass
class CanaryStageMetrics:
    """
    Metrics for an individual Canary Stage.

    Attributes:
        stage: Current Canary stage
        started_at: Stage start time
        total_requests: Total number of requests
        success_count: Number of successful requests
        failure_count: Number of failed requests
        current_success_rate: Current success rate
    """

    stage: CanaryRecoveryStage
    started_at: datetime = field(default_factory=lambda: utc_now())
    total_requests: int = 0
    success_count: int = 0
    failure_count: int = 0

    @property
    def current_success_rate(self) -> float:
        """Compute the current success rate (0~100%)."""
        if self.total_requests == 0:
            return 100.0  # No requests → treated as 100% success
        return (self.success_count / self.total_requests) * 100.0

    def record_success(self) -> None:
        """Record a success."""
        self.total_requests += 1
        self.success_count += 1

    def record_failure(self) -> None:
        """Record a failure."""
        self.total_requests += 1
        self.failure_count += 1

    def elapsed_seconds(self) -> float:
        """Elapsed time since the stage started (seconds)."""
        return (utc_now() - self.started_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dictionary."""
        return {
            "stage": self.stage.value,
            "started_at": self.started_at.isoformat(),
            "total_requests": self.total_requests,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "current_success_rate": self.current_success_rate,
            "elapsed_seconds": self.elapsed_seconds(),
        }


# =============================================================================
# Canary Recovery State
# =============================================================================


@dataclass
class CanaryRecoveryState(SerializableMixin):
    """
    Per-service Canary Recovery state.

    Attributes:
        service_id: Service ID
        current_stage: Current Canary stage
        stage_index: Current stage index (0~3)
        metrics: Metrics for the current stage
        recovery_started_at: Recovery start time
        recovery_strategy: Applied recovery strategy
        stage_history: Stage history
    """

    service_id: str
    current_stage: CanaryRecoveryStage = CanaryRecoveryStage.NOT_IN_CANARY
    stage_index: int = -1
    metrics: CanaryStageMetrics | None = None
    recovery_started_at: datetime | None = None
    recovery_strategy: RecoveryStrategy | None = None
    stage_history: list[dict[str, Any]] = field(default_factory=list)

    def start_recovery(self, strategy: RecoveryStrategy) -> None:
        """Start Canary recovery."""
        self.current_stage = CanaryRecoveryStage.CANARY_1
        self.stage_index = 0
        self.recovery_started_at = utc_now()
        self.recovery_strategy = strategy
        self.metrics = CanaryStageMetrics(stage=CanaryRecoveryStage.CANARY_1)
        self.stage_history = []

    def advance_stage(self) -> bool:
        """
        Advance to the next stage.

        Returns:
            True if advanced, False if already at last stage
        """
        if self.recovery_strategy is None:
            return False

        # Append the current metrics to the history
        if self.metrics:
            self.stage_history.append(self.metrics.to_dict())

        # Advance to the next stage
        next_index = self.stage_index + 1

        if next_index >= len(self.recovery_strategy.canary_stages):
            # All stages complete → CLOSED
            self.current_stage = CanaryRecoveryStage.NOT_IN_CANARY
            self.stage_index = -1
            self.metrics = None
            return False

        # Set the next Canary stage
        stage_names = [
            CanaryRecoveryStage.CANARY_1,
            CanaryRecoveryStage.CANARY_2,
            CanaryRecoveryStage.CANARY_3,
            CanaryRecoveryStage.CANARY_4,
        ]
        self.stage_index = next_index
        self.current_stage = stage_names[min(next_index, len(stage_names) - 1)]
        self.metrics = CanaryStageMetrics(stage=self.current_stage)
        return True

    def reset(self) -> None:
        """Reset the Canary state (on failure)."""
        if self.metrics:
            self.stage_history.append(
                {
                    **self.metrics.to_dict(),
                    "result": "failed",
                }
            )

        self.current_stage = CanaryRecoveryStage.NOT_IN_CANARY
        self.stage_index = -1
        self.metrics = None
        self.recovery_started_at = None

    def is_in_canary(self) -> bool:
        """Check whether Canary recovery is in progress."""
        return self.current_stage != CanaryRecoveryStage.NOT_IN_CANARY

    def get_current_config(self) -> CanaryRecoveryStageConfig | None:
        """Return the configuration of the current stage."""
        if self.recovery_strategy is None or self.stage_index < 0:
            return None
        if self.stage_index >= len(self.recovery_strategy.canary_stages):
            return None
        return self.recovery_strategy.canary_stages[self.stage_index]

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dictionary."""
        return {
            "service_id": self.service_id,
            "current_stage": self.current_stage.value,
            "stage_index": self.stage_index,
            "is_in_canary": self.is_in_canary(),
            "metrics": self.metrics.to_dict() if self.metrics else None,
            "recovery_started_at": (
                self.recovery_started_at.isoformat()
                if self.recovery_started_at
                else None
            ),
            "stage_history": self.stage_history,
        }


# =============================================================================
# Canary Decision
# =============================================================================


@dataclass
class CanaryRecoveryDecision(SerializableMixin):
    """
    Canary request-allow decision result.

    Attributes:
        allow_backend: Whether to allow the backend call
        is_canary_request: Whether this is a Canary request (metric-tracking target)
        use_stale_cache: Whether to use Stale Cache
        current_stage: Current Canary stage
        traffic_percent: Traffic ratio of the current stage
        reason: Decision reason
    """

    allow_backend: bool = False
    is_canary_request: bool = False
    use_stale_cache: bool = False
    current_stage: CanaryRecoveryStage | None = None
    traffic_percent: float = 0.0
    reason: str = ""


# =============================================================================
# Canary Stage Transition Result
# =============================================================================


@dataclass
class CanaryStageTransitionResult(SerializableMixin):
    """
    Canary stage-transition result.

    Attributes:
        transitioned: Whether a stage transition occurred
        previous_stage: Previous stage
        new_stage: New stage
        success_rate: Success rate at the transition point
        reason: Transition reason
        completed: Whether all stages are complete (transition to CLOSED)
        failed: Whether it reverted to OPEN due to failure
    """

    transitioned: bool = False
    previous_stage: CanaryRecoveryStage | None = None
    new_stage: CanaryRecoveryStage | None = None
    success_rate: float = 0.0
    reason: str = ""
    completed: bool = False
    failed: bool = False


# =============================================================================
# Canary Recovery Manager
# =============================================================================


class CanaryRecoveryManager:
    """
    Canary Recovery manager.

    Manages service recovery by gradually increasing traffic in the HALF_OPEN state.

    State machine:
        HALF_OPEN → CANARY_1(10%) → CANARY_2(30%) → CANARY_3(60%) → CANARY_4(100%) → CLOSED
                        ↓              ↓              ↓              ↓
                    on failure ─────────────────────────────────────→ OPEN

    Usage:
        manager = CanaryRecoveryManager()

        # Start Canary on HALF_OPEN entry
        manager.start_canary_recovery("payment-api")

        # Decide per request
        decision = manager.should_allow_request("payment-api")
        if decision.allow_backend:
            try:
                result = call_backend()
                manager.record_success("payment-api")
            except:
                manager.record_failure("payment-api")
    """

    _instance: CanaryRecoveryManager | None = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls) -> CanaryRecoveryManager:
        """Singleton pattern implementation."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        default_strategy: RecoveryStrategy | None = None,
        on_stage_advanced: Callable[[str, CanaryStageTransitionResult], None]
        | None = None,
        on_recovery_completed: Callable[[str, dict[str, Any]], None] | None = None,
        on_recovery_failed: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        """
        Initialize.

        Args:
            default_strategy: Default recovery strategy
            on_stage_advanced: Callback on stage transition
            on_recovery_completed: Callback on recovery completion
            on_recovery_failed: Callback on recovery failure
        """
        if getattr(self, "_initialized", False):
            return

        self._default_strategy = default_strategy or RecoveryStrategy()
        self._recovery_states: dict[str, CanaryRecoveryState] = {}
        self._service_strategies: dict[str, RecoveryStrategy] = {}
        self._state_lock = threading.RLock()

        # Callbacks
        self._on_stage_advanced = on_stage_advanced
        self._on_recovery_completed = on_recovery_completed
        self._on_recovery_failed = on_recovery_failed

        self._initialized = True

    # =========================================================================
    # Configuration
    # =========================================================================

    def set_default_strategy(self, strategy: RecoveryStrategy) -> None:
        """Set the default recovery strategy."""
        self._default_strategy = strategy

    def set_service_strategy(self, service_id: str, strategy: RecoveryStrategy) -> None:
        """Set the per-service recovery strategy."""
        with self._state_lock:
            self._service_strategies[service_id] = strategy

    def get_strategy(self, service_id: str) -> RecoveryStrategy:
        """Look up the service recovery strategy."""
        with self._state_lock:
            return self._service_strategies.get(service_id, self._default_strategy)

    # =========================================================================
    # Canary Recovery Control
    # =========================================================================

    def start_canary_recovery(
        self,
        service_id: str,
        strategy: RecoveryStrategy | None = None,
    ) -> CanaryRecoveryState:
        """
        Start Canary recovery (called on HALF_OPEN entry).

        Args:
            service_id: Service ID
            strategy: Recovery strategy (uses the default or per-service setting if absent)

        Returns:
            CanaryRecoveryState: created recovery state
        """
        with self._state_lock:
            effective_strategy = strategy or self.get_strategy(service_id)

            # The immediate strategy does not use Canary
            if effective_strategy.type == "immediate":
                logger.info(
                    "canary_recovery.immediate_strategy_skipping_canary",
                    service_id=service_id,
                )
                return CanaryRecoveryState(service_id=service_id)

            # Create the Canary recovery state
            state = CanaryRecoveryState(service_id=service_id)
            state.start_recovery(effective_strategy)
            self._recovery_states[service_id] = state

            logger.info(
                "canary_recovery.started_canary_recovery",
                service_id=service_id,
                current_stage=state.current_stage.value,
                effective_strategy=effective_strategy.canary_stages[0].traffic_percent,
            )

            return state

    def stop_canary_recovery(self, service_id: str, reason: str = "manual") -> bool:
        """
        Stop Canary recovery.

        Args:
            service_id: Service ID
            reason: Stop reason

        Returns:
            True if stopped, False if not in recovery
        """
        with self._state_lock:
            state = self._recovery_states.get(service_id)
            if state is None or not state.is_in_canary():
                return False

            state.reset()
            logger.info(
                "canary_recovery.stopped_canary_recovery",
                service_id=service_id,
                reason=reason,
            )
            return True

    def get_recovery_state(self, service_id: str) -> CanaryRecoveryState | None:
        """
        Look up the service's Canary recovery state.

        Args:
            service_id: Service ID

        Returns:
            CanaryRecoveryState or None if not in recovery
        """
        with self._state_lock:
            return self._recovery_states.get(service_id)

    def is_in_canary_recovery(self, service_id: str) -> bool:
        """
        Check whether the service is in Canary recovery.

        Args:
            service_id: Service ID

        Returns:
            True if in canary recovery
        """
        with self._state_lock:
            state = self._recovery_states.get(service_id)
            return state is not None and state.is_in_canary()

    # =========================================================================
    # Request Decision
    # =========================================================================

    def should_allow_request(self, service_id: str) -> CanaryRecoveryDecision:
        """
        Decide whether to allow a request (applying the Canary ratio).

        Args:
            service_id: Service ID

        Returns:
            CanaryRecoveryDecision: request-allow decision
        """
        with self._state_lock:
            state = self._recovery_states.get(service_id)

            # Allow all if not in Canary recovery
            if state is None or not state.is_in_canary():
                return CanaryRecoveryDecision(
                    allow_backend=True,
                    is_canary_request=False,
                    reason="not in canary recovery",
                )

            stage_config = state.get_current_config()
            if stage_config is None:
                return CanaryRecoveryDecision(
                    allow_backend=True,
                    is_canary_request=False,
                    reason="no stage config",
                )

            # Probability-based Canary selection
            is_canary = random.random() * 100 < stage_config.traffic_percent

            if is_canary:
                return CanaryRecoveryDecision(
                    allow_backend=True,
                    is_canary_request=True,
                    use_stale_cache=False,
                    current_stage=state.current_stage,
                    traffic_percent=stage_config.traffic_percent,
                    reason=f"canary request ({stage_config.traffic_percent}%)",
                )
            return CanaryRecoveryDecision(
                allow_backend=False,
                is_canary_request=False,
                use_stale_cache=True,
                current_stage=state.current_stage,
                traffic_percent=stage_config.traffic_percent,
                reason=f"non-canary request, use stale cache ({100 - stage_config.traffic_percent}%)",
            )

    # =========================================================================
    # Metrics Recording
    # =========================================================================

    def record_success(self, service_id: str) -> CanaryStageTransitionResult | None:
        """
        Record a success and check for a stage transition.

        Args:
            service_id: Service ID

        Returns:
            CanaryStageTransitionResult if stage transition occurred
        """
        with self._state_lock:
            state = self._recovery_states.get(service_id)
            if state is None or not state.is_in_canary() or state.metrics is None:
                return None

            state.metrics.record_success()
            return self._check_stage_transition(service_id, state)

    def record_failure(self, service_id: str) -> CanaryStageTransitionResult | None:
        """
        Record a failure and check for recovery failure.

        Args:
            service_id: Service ID

        Returns:
            CanaryStageTransitionResult if recovery failed
        """
        with self._state_lock:
            state = self._recovery_states.get(service_id)
            if state is None or not state.is_in_canary() or state.metrics is None:
                return None

            state.metrics.record_failure()
            return self._check_stage_transition(service_id, state)

    def _check_stage_transition(  # noqa: C901
        self,
        service_id: str,
        state: CanaryRecoveryState,
    ) -> CanaryStageTransitionResult | None:
        """
        Check and process the stage-transition conditions.

        Transition conditions:
        1. The stage hold time (duration_seconds) has elapsed
        2. The success rate is at or above required_success_rate

        Failure condition:
        - Success rate below required_success_rate with enough samples (10 or more)
        """
        if state.metrics is None or state.recovery_strategy is None:
            return None

        stage_config = state.get_current_config()
        if stage_config is None:
            return None

        metrics = state.metrics
        previous_stage = state.current_stage

        # Check the minimum sample count
        min_samples = 5
        if metrics.total_requests < min_samples:
            return None  # Too early to judge yet

        # Check the success rate
        success_rate = metrics.current_success_rate
        required_rate = stage_config.required_success_rate

        # strict_mode requires 100%
        if state.recovery_strategy.strict_mode:
            required_rate = 100.0

        # Failure condition: success rate below required (10 or more samples)
        if metrics.total_requests >= 10 and success_rate < required_rate:
            # Recovery failed → revert to OPEN
            result = CanaryStageTransitionResult(
                transitioned=True,
                previous_stage=previous_stage,
                new_stage=None,
                success_rate=success_rate,
                reason=f"success rate {success_rate:.1f}% < required {required_rate:.1f}%",
                failed=True,
            )

            state.reset()

            logger.warning(
                "canary_recovery.recovery_failed",
                service_id=service_id,
                previous_stage=previous_stage.value,
                success_rate=success_rate,
                required_rate=required_rate,
            )

            if self._on_recovery_failed:
                self._on_recovery_failed(service_id, result.to_dict())

            return result

        # Success condition: time elapsed + success rate met
        elapsed = metrics.elapsed_seconds()
        if elapsed >= stage_config.duration_seconds and success_rate >= required_rate:
            # Advance to the next stage
            if state.advance_stage():
                new_stage = state.current_stage
                result = CanaryStageTransitionResult(
                    transitioned=True,
                    previous_stage=previous_stage,
                    new_stage=new_stage,
                    success_rate=success_rate,
                    reason=f"advanced after {elapsed:.1f}s with {success_rate:.1f}% success rate",
                )

                logger.info(
                    "canary_recovery.advanced",
                    service_id=service_id,
                    previous_stage=previous_stage.value,
                    new_stage=new_stage.value,
                    success_rate=success_rate,
                )

                if self._on_stage_advanced:
                    self._on_stage_advanced(service_id, result)

                return result
            # All stages complete → CLOSED
            result = CanaryStageTransitionResult(
                transitioned=True,
                previous_stage=previous_stage,
                new_stage=None,
                success_rate=success_rate,
                reason="all stages completed, ready for CLOSED",
                completed=True,
            )

            logger.info(
                "canary_recovery.recovery_completed_final",
                service_id=service_id,
                success_rate=success_rate,
            )

            if self._on_recovery_completed:
                self._on_recovery_completed(service_id, state.to_dict())

            return result

        return None

    # =========================================================================
    # Status & Diagnostics
    # =========================================================================

    def get_all_recovery_states(self) -> dict[str, dict[str, Any]]:
        """Look up the Canary recovery state of all services."""
        with self._state_lock:
            return {
                service_id: state.to_dict()
                for service_id, state in self._recovery_states.items()
            }

    def get_active_recoveries(self) -> list[str]:
        """List of services currently in Canary recovery."""
        with self._state_lock:
            return [
                service_id
                for service_id, state in self._recovery_states.items()
                if state.is_in_canary()
            ]

    def get_recovery_stats(self, service_id: str) -> dict[str, Any] | None:
        """
        Canary recovery statistics for a service.

        Args:
            service_id: Service ID

        Returns:
            Recovery-statistics dictionary
        """
        with self._state_lock:
            state = self._recovery_states.get(service_id)
            if state is None:
                return None

            return state.to_dict()

    def reset(self) -> None:
        """Reset all state."""
        with self._state_lock:
            self._recovery_states.clear()
            self._service_strategies.clear()
            logger.info("canary_recovery.all_states_reset")


# =============================================================================
# Module-level Singleton Functions
# =============================================================================


_manager_instance: CanaryRecoveryManager | None = None
_manager_lock = threading.Lock()


def get_canary_recovery_manager() -> CanaryRecoveryManager:
    """Return the singleton CanaryRecoveryManager instance."""
    global _manager_instance
    if _manager_instance is None:
        with _manager_lock:
            if _manager_instance is None:
                _manager_instance = CanaryRecoveryManager()
    return _manager_instance


def reset_canary_recovery_manager() -> None:
    """Reset the singleton instance (for tests)."""
    global _manager_instance
    with _manager_lock:
        if _manager_instance is not None:
            _manager_instance.reset()
        _manager_instance = None
        CanaryRecoveryManager._instance = None


# =============================================================================
# Convenience Functions
# =============================================================================


def start_canary_recovery(
    service_id: str,
    strategy: RecoveryStrategy | None = None,
) -> CanaryRecoveryState:
    """Start Canary recovery."""
    return get_canary_recovery_manager().start_canary_recovery(service_id, strategy)


def stop_canary_recovery(service_id: str, reason: str = "manual") -> bool:
    """Stop Canary recovery."""
    return get_canary_recovery_manager().stop_canary_recovery(service_id, reason)


def is_in_canary_recovery(service_id: str) -> bool:
    """Check whether in Canary recovery."""
    return get_canary_recovery_manager().is_in_canary_recovery(service_id)


def canary_should_allow_request(service_id: str) -> CanaryRecoveryDecision:
    """Canary request-allow decision."""
    return get_canary_recovery_manager().should_allow_request(service_id)


def canary_record_success(service_id: str) -> CanaryStageTransitionResult | None:
    """Record a Canary success."""
    return get_canary_recovery_manager().record_success(service_id)


def canary_record_failure(service_id: str) -> CanaryStageTransitionResult | None:
    """Record a Canary failure."""
    return get_canary_recovery_manager().record_failure(service_id)


def get_canary_recovery_state(service_id: str) -> CanaryRecoveryState | None:
    """Look up the Canary recovery state."""
    return get_canary_recovery_manager().get_recovery_state(service_id)
