"""
X-Test integration test scenario package.

Aggregates the scenario modules that are split by domain.
"""

# Base classes and models
from .base import (
    IntegrationScenario,
    ScenarioResult,
    ScenarioStatus,
    ScenarioStep,
    TimelineEvent,
    clear_scenario_results,
    get_scenario_result,
    store_scenario_result,
)

# Circuit Breaker scenarios
from .circuit_breaker import (
    CBOpenDLQScenario,
)

# DLQ and Replay scenarios
from .dlq_replay import (
    DLQReplayFailureScenario,
    DLQReplaySuccessScenario,
    IdempotentReplayScenario,
    RateLimitRetryScenario,
    RetryExhaustScenario,
)

# Emergency scenarios
from .emergency import (
    FullEmergencyRecoveryScenario,
    SafetyInterlockCanaryRollbackScenario,
)

# Recovery scenarios
from .recovery import (
    FullRecoveryScenario,
)

# Regional scenarios (144 implementation)
from .regional import (
    MultiRegionIsolationTestScenario,
    RegionalOverrideConflictScenario,
)

# =============================================================================
# Scenario registry
# =============================================================================


SCENARIO_REGISTRY: dict[str, type] = {
    # Circuit Breaker scenarios
    "cb_open_dlq_flow": CBOpenDLQScenario,
    # DLQ and Replay scenarios
    "retry_exhaust_dlq": RetryExhaustScenario,
    "rate_limit_retry": RateLimitRetryScenario,
    "dlq_replay_success": DLQReplaySuccessScenario,
    "dlq_replay_failure": DLQReplayFailureScenario,
    "idempotent_replay": IdempotentReplayScenario,
    # Recovery scenarios
    "full_recovery_cycle": FullRecoveryScenario,
    # Emergency scenarios
    "full_emergency_recovery_flow": FullEmergencyRecoveryScenario,
    "safety_interlock_canary_rollback": SafetyInterlockCanaryRollbackScenario,
    # Regional scenarios (144 implementation)
    "regional_override_conflict": RegionalOverrideConflictScenario,
    "multi_region_isolation_test": MultiRegionIsolationTestScenario,
}


def get_scenario_class(scenario_name: str) -> type | None:
    """Look up a scenario class by name."""
    return SCENARIO_REGISTRY.get(scenario_name)


def list_available_scenarios() -> list[str]:
    """Return the list of available scenarios."""
    return list(SCENARIO_REGISTRY.keys())


__all__ = [
    # Base classes and models
    "ScenarioStatus",
    "ScenarioStep",
    "TimelineEvent",
    "ScenarioResult",
    "IntegrationScenario",
    "store_scenario_result",
    "get_scenario_result",
    "clear_scenario_results",
    # Scenario classes
    "CBOpenDLQScenario",
    "RetryExhaustScenario",
    "RateLimitRetryScenario",
    "DLQReplaySuccessScenario",
    "DLQReplayFailureScenario",
    "IdempotentReplayScenario",
    "FullRecoveryScenario",
    "FullEmergencyRecoveryScenario",
    "SafetyInterlockCanaryRollbackScenario",
    "RegionalOverrideConflictScenario",
    "MultiRegionIsolationTestScenario",
    # Registry and helpers
    "SCENARIO_REGISTRY",
    "get_scenario_class",
    "list_available_scenarios",
]
