"""
Control API Service - Risk Assessment

Defines the classify_reason() and assess_risk_level() functions.
"""

from __future__ import annotations

from baldur.core.constants import (
    ControlAPIActions,
    ControlAPIEnvironments,
    RiskLevels,
)

from .models import ReasonClassification


def classify_reason(reason: str) -> str:
    """
    Classify the provided reason string.

    Args:
        reason: Human-provided reason text

    Returns:
        Classification string
    """
    reason_lower = reason.lower()

    # Pattern matching for classification (order matters - more specific first)
    patterns = [
        (
            ReasonClassification.MAINTENANCE_WINDOW,
            ["maintenance", "scheduled", "upgrade", "deploy"],
        ),
        (
            ReasonClassification.SLA_BREACH_MITIGATION,
            ["sla", "breach", "violation", "threshold"],
        ),
        (ReasonClassification.CHAOS_EXPERIMENT, ["chaos", "experiment", "resilience"]),
        (
            ReasonClassification.RECOVERY_PROCEDURE,
            ["recovery", "recovered", "restored", "fixed"],
        ),
        (
            ReasonClassification.SECURITY_INCIDENT,
            ["security", "attack", "ddos", "vulnerability"],
        ),
        (
            ReasonClassification.EXTERNAL_DEPENDENCY_FAILURE,
            ["external", "pg", "payment gateway", "api down", "timeout", "latency"],
        ),
        (
            ReasonClassification.INTERNAL_SERVICE_ERROR,
            ["internal", "service", "error", "bug"],
        ),
    ]

    for classification, keywords in patterns:
        if any(kw in reason_lower for kw in keywords):
            return classification.value

    return ReasonClassification.MANUAL_INTERVENTION.value


# =============================================================================
# Risk Assessment
# =============================================================================


def assess_risk_level(action: str, environment: str) -> str:
    """
    Assess the risk level for an action in an environment.

    Evaluates the risk level based on the environment and operation type.

    Args:
        action: Action type
        environment: Environment type

    Returns:
        Risk level string
    """
    risk_matrix = {
        (ControlAPIActions.ALLOW, ControlAPIEnvironments.TEST): RiskLevels.INFO,
        (ControlAPIActions.ALLOW, ControlAPIEnvironments.CHAOS): RiskLevels.INFO,
        (ControlAPIActions.ALLOW, ControlAPIEnvironments.OPS): RiskLevels.WARNING,
        (ControlAPIActions.BLOCK, ControlAPIEnvironments.TEST): RiskLevels.INFO,
        (ControlAPIActions.BLOCK, ControlAPIEnvironments.CHAOS): RiskLevels.WARNING,
        (ControlAPIActions.BLOCK, ControlAPIEnvironments.OPS): RiskLevels.HIGH,
        (ControlAPIActions.OVERRIDE, ControlAPIEnvironments.TEST): RiskLevels.WARNING,
        (ControlAPIActions.OVERRIDE, ControlAPIEnvironments.CHAOS): RiskLevels.HIGH,
        (ControlAPIActions.OVERRIDE, ControlAPIEnvironments.OPS): RiskLevels.CRITICAL,
        (ControlAPIActions.RESET, ControlAPIEnvironments.TEST): RiskLevels.INFO,
        (ControlAPIActions.RESET, ControlAPIEnvironments.CHAOS): RiskLevels.WARNING,
        (ControlAPIActions.RESET, ControlAPIEnvironments.OPS): RiskLevels.WARNING,
        (
            ControlAPIActions.INJECT_FAILURE,
            ControlAPIEnvironments.TEST,
        ): RiskLevels.INFO,
        (
            ControlAPIActions.INJECT_FAILURE,
            ControlAPIEnvironments.CHAOS,
        ): RiskLevels.HIGH,
        (
            ControlAPIActions.INJECT_FAILURE,
            ControlAPIEnvironments.OPS,
        ): RiskLevels.FORBIDDEN,
        (
            ControlAPIActions.INJECT_SUCCESS,
            ControlAPIEnvironments.TEST,
        ): RiskLevels.INFO,
        (
            ControlAPIActions.INJECT_SUCCESS,
            ControlAPIEnvironments.CHAOS,
        ): RiskLevels.INFO,
        (
            ControlAPIActions.INJECT_SUCCESS,
            ControlAPIEnvironments.OPS,
        ): RiskLevels.FORBIDDEN,
    }

    return risk_matrix.get((action, environment), RiskLevels.WARNING)
