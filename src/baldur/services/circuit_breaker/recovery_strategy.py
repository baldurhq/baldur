"""
Recovery Strategy Selector

Selects and manages a recovery strategy per service.
Supports the immediate vs canary strategies.

Strategies:
- immediate: allow 100% traffic immediately on HALF_OPEN (fast recovery, higher risk)
- canary: gradual traffic increase (safe recovery, takes time)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import structlog

from baldur.services.circuit_breaker.canary_recovery import (
    CanaryRecoveryManager,
    CanaryStageTransitionResult,
    get_canary_recovery_manager,
)
from baldur.services.circuit_breaker.models import (
    CanaryRecoveryStageConfig,
    RecoveryStrategy,
)
from baldur.services.circuit_breaker.service_config import (
    ServiceConfigManager,
    get_service_config_manager,
)
from baldur.services.circuit_breaker.stale_cache_integration import (
    CanaryWithStaleCacheService,
    get_canary_stale_cache_service,
)

logger = structlog.get_logger()


# =============================================================================
# Recovery Strategy Selection Result
# =============================================================================


@dataclass
class RecoveryStrategySelection:
    """
    Recovery-strategy selection result.

    Attributes:
        service_id: Service ID
        strategy_type: Selected strategy type ("immediate" | "canary")
        strategy: Detailed strategy configuration
        reason: Selection reason
        source: Strategy source ("service_config" | "criticality_based" | "default")
    """

    service_id: str
    strategy_type: str
    strategy: RecoveryStrategy
    reason: str = ""
    source: str = "default"

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dictionary."""
        return {
            "service_id": self.service_id,
            "strategy_type": self.strategy_type,
            "strategy": {
                "type": self.strategy.type,
                "strict_mode": self.strategy.strict_mode,
                "on_stage_failure": self.strategy.on_stage_failure,
                "stages_count": len(self.strategy.canary_stages),
            },
            "reason": self.reason,
            "source": self.source,
        }


# =============================================================================
# Recovery Decision
# =============================================================================


@dataclass
class RecoveryDecision:
    """
    Recovery decision result (immediate or canary unified).

    Attributes:
        allow_backend: Whether to allow the backend call
        is_canary_request: Whether this is a Canary request
        use_stale_cache: Whether to use Stale Cache
        stale_data: Cached data
        strategy_type: Applied strategy type
        current_stage: Current Canary stage (when using the canary strategy)
        traffic_percent: Current traffic ratio
        reason: Decision reason
        completed: Whether recovery is complete
    """

    allow_backend: bool = False
    is_canary_request: bool = False
    use_stale_cache: bool = False
    stale_data: Any | None = None
    strategy_type: str = "immediate"
    current_stage: str | None = None
    traffic_percent: float = 100.0
    reason: str = ""
    completed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dictionary."""
        return {
            "allow_backend": self.allow_backend,
            "is_canary_request": self.is_canary_request,
            "use_stale_cache": self.use_stale_cache,
            "stale_data": str(self.stale_data)[:100] if self.stale_data else None,
            "strategy_type": self.strategy_type,
            "current_stage": self.current_stage,
            "traffic_percent": self.traffic_percent,
            "reason": self.reason,
            "completed": self.completed,
        }


# =============================================================================
# Recovery Strategy Selector
# =============================================================================


class RecoveryStrategySelector:
    """
    Recovery-strategy selector.

    Selects the appropriate recovery strategy (immediate/canary) per service and
    manages request handling in the HALF_OPEN state.

    Strategy-selection priority:
    1. The service config's recovery_strategy
    2. criticality-based automatic selection
       - critical: canary + strict_mode
       - high: canary
       - medium: canary (fast settings)
       - low: immediate
    3. Default strategy

    Usage:
        selector = RecoveryStrategySelector()

        # Select a strategy
        selection = selector.select_strategy("payment-api")

        # Start recovery on HALF_OPEN entry
        selector.start_recovery("payment-api")

        # Handle a request
        decision = selector.handle_half_open_request(
            service_id="payment-api",
            cache_key="payment:user123",
        )

        if decision.allow_backend:
            result = call_backend()
            selector.record_success("payment-api")
        elif decision.use_stale_cache:
            # Return Stale Cache
            return decision.stale_data
    """

    _instance: RecoveryStrategySelector | None = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls) -> RecoveryStrategySelector:
        """Singleton pattern."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        default_strategy: RecoveryStrategy | None = None,
        canary_manager: CanaryRecoveryManager | None = None,
        stale_cache_service: CanaryWithStaleCacheService | None = None,
        service_config_manager: ServiceConfigManager | None = None,
    ):
        """
        Initialize.

        Args:
            default_strategy: Default recovery strategy
            canary_manager: Canary Recovery manager
            stale_cache_service: Stale Cache service
            service_config_manager: Service config manager
        """
        if getattr(self, "_initialized", False):
            return

        self._default_strategy = default_strategy or RecoveryStrategy()
        self._canary_manager = canary_manager or get_canary_recovery_manager()
        self._stale_cache = stale_cache_service or get_canary_stale_cache_service()
        self._service_config = service_config_manager or get_service_config_manager()

        # Default strategy per criticality
        self._criticality_strategies: dict[str, RecoveryStrategy] = {
            "critical": RecoveryStrategy(
                type="canary",
                strict_mode=True,
                on_stage_failure="restart",
                canary_stages=[
                    CanaryRecoveryStageConfig(
                        traffic_percent=5.0,
                        duration_seconds=10,
                        required_success_rate=99.0,
                        description="Critical Stage 1: 5% for 10s",
                    ),
                    CanaryRecoveryStageConfig(
                        traffic_percent=20.0,
                        duration_seconds=10,
                        required_success_rate=98.0,
                        description="Critical Stage 2: 20% for 10s",
                    ),
                    CanaryRecoveryStageConfig(
                        traffic_percent=50.0,
                        duration_seconds=10,
                        required_success_rate=97.0,
                        description="Critical Stage 3: 50% for 10s",
                    ),
                    CanaryRecoveryStageConfig(
                        traffic_percent=100.0,
                        duration_seconds=0,
                        required_success_rate=95.0,
                        description="Critical Stage 4: 100%",
                    ),
                ],
            ),
            "high": RecoveryStrategy(
                type="canary",
                strict_mode=False,
                on_stage_failure="restart",
            ),
            "medium": RecoveryStrategy(
                type="canary",
                strict_mode=False,
                on_stage_failure="restart",
                canary_stages=[
                    CanaryRecoveryStageConfig(
                        traffic_percent=20.0,
                        duration_seconds=5,
                        required_success_rate=90.0,
                        description="Medium Stage 1: 20% for 5s",
                    ),
                    CanaryRecoveryStageConfig(
                        traffic_percent=50.0,
                        duration_seconds=5,
                        required_success_rate=85.0,
                        description="Medium Stage 2: 50% for 5s",
                    ),
                    CanaryRecoveryStageConfig(
                        traffic_percent=100.0,
                        duration_seconds=0,
                        required_success_rate=80.0,
                        description="Medium Stage 3: 100%",
                    ),
                ],
            ),
            "low": RecoveryStrategy(
                type="immediate",
            ),
        }

        # Active recovery state per service
        self._active_recoveries: dict[str, str] = {}  # service_id -> strategy_type
        self._state_lock = threading.RLock()

        self._initialized = True

    # =========================================================================
    # Strategy Selection
    # =========================================================================

    def select_strategy(self, service_id: str) -> RecoveryStrategySelection:
        """
        Select the recovery strategy suited to the service.

        Selection priority:
        1. The service config's recovery_strategy
        2. criticality-based automatic selection
        3. Default strategy

        Args:
            service_id: Service ID

        Returns:
            RecoveryStrategySelection
        """
        # 1. Check the service config
        service_config = self._service_config.get_service_config(service_id)

        if service_config and service_config.recovery_strategy:
            strategy = service_config.recovery_strategy
            return RecoveryStrategySelection(
                service_id=service_id,
                strategy_type=strategy.type,
                strategy=strategy,
                reason=f"service-specific configuration for {service_id}",
                source="service_config",
            )

        # 2. Criticality-based selection
        if service_config:
            criticality = service_config.criticality
            if criticality in self._criticality_strategies:
                strategy = self._criticality_strategies[criticality]
                return RecoveryStrategySelection(
                    service_id=service_id,
                    strategy_type=strategy.type,
                    strategy=strategy,
                    reason=f"criticality-based selection ({criticality})",
                    source="criticality_based",
                )

        # 3. Default strategy
        return RecoveryStrategySelection(
            service_id=service_id,
            strategy_type=self._default_strategy.type,
            strategy=self._default_strategy,
            reason="default strategy (no service config found)",
            source="default",
        )

    def set_criticality_strategy(
        self, criticality: str, strategy: RecoveryStrategy
    ) -> None:
        """
        Set the default strategy per criticality.

        Args:
            criticality: "critical" | "high" | "medium" | "low"
            strategy: Recovery strategy
        """
        self._criticality_strategies[criticality] = strategy

    def set_default_strategy(self, strategy: RecoveryStrategy) -> None:
        """Set the default strategy."""
        self._default_strategy = strategy

    # =========================================================================
    # Recovery Lifecycle
    # =========================================================================

    def start_recovery(self, service_id: str) -> RecoveryStrategySelection:
        """
        Start service recovery (called on HALF_OPEN entry).

        Args:
            service_id: Service ID

        Returns:
            RecoveryStrategySelection: selected strategy
        """
        with self._state_lock:
            selection = self.select_strategy(service_id)

            if selection.strategy_type == "canary":
                # Start Canary recovery
                self._canary_manager.start_canary_recovery(
                    service_id=service_id,
                    strategy=selection.strategy,
                )
                logger.info(
                    "recovery_strategy.started_canary_recovery",
                    service_id=service_id,
                    selection=selection.strategy.strict_mode,
                )
            else:
                # Immediate strategy - allow 100% immediately
                logger.info(
                    "recovery_strategy.using_immediate_recovery",
                    service_id=service_id,
                )

            self._active_recoveries[service_id] = selection.strategy_type
            return selection

    def stop_recovery(self, service_id: str, reason: str = "manual") -> bool:
        """
        Stop service recovery.

        Args:
            service_id: Service ID
            reason: Stop reason

        Returns:
            True if stopped
        """
        with self._state_lock:
            if service_id not in self._active_recoveries:
                return False

            strategy_type = self._active_recoveries.pop(service_id, None)

            if strategy_type == "canary":
                self._canary_manager.stop_canary_recovery(service_id, reason)

            logger.info(
                "recovery_strategy.stopped_recovery",
                service_id=service_id,
                reason=reason,
            )
            return True

    def is_in_recovery(self, service_id: str) -> bool:
        """Check whether the service is in recovery."""
        with self._state_lock:
            return service_id in self._active_recoveries

    def get_recovery_type(self, service_id: str) -> str | None:
        """Look up the service's current recovery-strategy type."""
        with self._state_lock:
            return self._active_recoveries.get(service_id)

    # =========================================================================
    # Request Handling
    # =========================================================================

    def handle_half_open_request(
        self,
        service_id: str,
        cache_key: str | None = None,
        cb_state: str = "half_open",
    ) -> RecoveryDecision:
        """
        Handle a request in the HALF_OPEN state.

        Args:
            service_id: Service ID
            cache_key: Cache key (when using Stale Cache)
            cb_state: CB state

        Returns:
            RecoveryDecision
        """
        with self._state_lock:
            strategy_type = self._active_recoveries.get(service_id)

        if strategy_type is None:
            # Start if not already recovering
            selection = self.start_recovery(service_id)
            strategy_type = selection.strategy_type

        if strategy_type == "immediate":
            # Immediate strategy: allow 100% immediately
            return RecoveryDecision(
                allow_backend=True,
                is_canary_request=False,
                strategy_type="immediate",
                traffic_percent=100.0,
                reason="immediate recovery - all requests allowed",
            )

        # Canary strategy: use Stale Cache integration
        if cache_key:
            stale_decision = self._stale_cache.should_allow_with_fallback(
                service_id=service_id,
                cache_key=cache_key,
                cb_state=cb_state,
            )

            return RecoveryDecision(
                allow_backend=stale_decision.allow_backend,
                is_canary_request=stale_decision.is_canary_request,
                use_stale_cache=stale_decision.use_stale,
                stale_data=stale_decision.stale_data,
                strategy_type="canary",
                current_stage=(
                    stale_decision.current_stage.value
                    if stale_decision.current_stage
                    else None
                ),
                traffic_percent=stale_decision.traffic_percent,
                reason=stale_decision.reason,
            )

        # Use Canary only, without a cache_key
        canary_decision = self._canary_manager.should_allow_request(service_id)

        return RecoveryDecision(
            allow_backend=canary_decision.allow_backend,
            is_canary_request=canary_decision.is_canary_request,
            use_stale_cache=canary_decision.use_stale_cache,
            strategy_type="canary",
            current_stage=(
                canary_decision.current_stage.value
                if canary_decision.current_stage
                else None
            ),
            traffic_percent=canary_decision.traffic_percent,
            reason=canary_decision.reason,
        )

    # =========================================================================
    # Metrics Recording
    # =========================================================================

    def record_success(self, service_id: str) -> CanaryStageTransitionResult | None:
        """
        Record a success.

        Args:
            service_id: Service ID

        Returns:
            CanaryStageTransitionResult if stage transition occurred
        """
        with self._state_lock:
            strategy_type = self._active_recoveries.get(service_id)

        if strategy_type != "canary":
            return None

        result = self._canary_manager.record_success(service_id)

        # Check whether recovery is complete
        if result and result.completed:
            with self._state_lock:
                self._active_recoveries.pop(service_id, None)
            logger.info(
                "recovery_strategy.recovery_completed",
                service_id=service_id,
            )

        return result

    def record_failure(self, service_id: str) -> CanaryStageTransitionResult | None:
        """
        Record a failure.

        Args:
            service_id: Service ID

        Returns:
            CanaryStageTransitionResult if recovery failed
        """
        with self._state_lock:
            strategy_type = self._active_recoveries.get(service_id)

        if strategy_type != "canary":
            return None

        result = self._canary_manager.record_failure(service_id)

        # Check whether recovery failed
        if result and result.failed:
            with self._state_lock:
                self._active_recoveries.pop(service_id, None)
            logger.warning(
                "watchdog.recovery_failed",
                service_id=service_id,
            )

        return result

    # =========================================================================
    # Status & Diagnostics
    # =========================================================================

    def get_active_recoveries(self) -> dict[str, str]:
        """Look up the list of active recoveries."""
        with self._state_lock:
            return dict(self._active_recoveries)

    def get_recovery_status(self, service_id: str) -> dict[str, Any] | None:
        """
        Look up the recovery status of a service.

        Args:
            service_id: Service ID

        Returns:
            Recovery-status dictionary
        """
        with self._state_lock:
            strategy_type = self._active_recoveries.get(service_id)

        if strategy_type is None:
            return None

        selection = self.select_strategy(service_id)

        result: dict[str, Any] = {
            "service_id": service_id,
            "strategy_type": strategy_type,
            "strategy_selection": selection.to_dict(),
        }

        if strategy_type == "canary":
            canary_state = self._canary_manager.get_recovery_state(service_id)
            if canary_state:
                result["canary_state"] = canary_state.to_dict()

        return result

    def reset(self) -> None:
        """Reset all state."""
        with self._state_lock:
            self._active_recoveries.clear()
        logger.info("recovery_strategy.all_states_reset")


# =============================================================================
# Module-level Singleton Functions
# =============================================================================


_selector_instance: RecoveryStrategySelector | None = None
_selector_lock = threading.Lock()


def get_recovery_strategy_selector() -> RecoveryStrategySelector:
    """Return the singleton instance."""
    global _selector_instance
    if _selector_instance is None:
        with _selector_lock:
            if _selector_instance is None:
                _selector_instance = RecoveryStrategySelector()
    return _selector_instance


def reset_recovery_strategy_selector() -> None:
    """Reset the singleton instance (for tests)."""
    global _selector_instance
    with _selector_lock:
        if _selector_instance is not None:
            _selector_instance.reset()
        _selector_instance = None
        RecoveryStrategySelector._instance = None


# =============================================================================
# Convenience Functions
# =============================================================================


def select_recovery_strategy(service_id: str) -> RecoveryStrategySelection:
    """Select a service recovery strategy."""
    return get_recovery_strategy_selector().select_strategy(service_id)


def start_service_recovery(service_id: str) -> RecoveryStrategySelection:
    """Start service recovery."""
    return get_recovery_strategy_selector().start_recovery(service_id)


def stop_service_recovery(service_id: str, reason: str = "manual") -> bool:
    """Stop service recovery."""
    return get_recovery_strategy_selector().stop_recovery(service_id, reason)


def handle_half_open(
    service_id: str,
    cache_key: str | None = None,
) -> RecoveryDecision:
    """Handle a request in the HALF_OPEN state."""
    return get_recovery_strategy_selector().handle_half_open_request(
        service_id=service_id,
        cache_key=cache_key,
    )


def record_recovery_success(service_id: str) -> CanaryStageTransitionResult | None:
    """Record a recovery success."""
    return get_recovery_strategy_selector().record_success(service_id)


def record_recovery_failure(service_id: str) -> CanaryStageTransitionResult | None:
    """Record a recovery failure."""
    return get_recovery_strategy_selector().record_failure(service_id)
