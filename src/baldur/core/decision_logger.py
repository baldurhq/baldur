"""
Decision Record Logger (Sink-Only)

This module provides structured logging for Decision Record events.
Logs are emitted at decision boundaries with fixed fields only.

Logged fields:
- event
- allowed (for INTERVENTION_EVALUATED only)
- reason (for INTERVENTION_EVALUATED only)
- service_name
- policy_version
- timestamp
"""

from __future__ import annotations

import json
from enum import Enum

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()


class ReasonCode(str, Enum):
    """Fixed set of allowed reason codes."""

    THRESHOLD_NOT_MET = "THRESHOLD_NOT_MET"
    STABILITY_OK_NO_INTERVENTION = "STABILITY_OK_NO_INTERVENTION"
    POLICY_CONSTRAINT_ACTIVE = "POLICY_CONSTRAINT_ACTIVE"
    INTERVENTION_ALLOWED = "INTERVENTION_ALLOWED"


class DecisionBoundaryEventType(str, Enum):
    """Decision boundary event types."""

    ENTER_PRE_DECISION_ZONE = "ENTER_PRE_DECISION_ZONE"
    INTERVENTION_EVALUATED = "INTERVENTION_EVALUATED"
    EXIT_PRE_DECISION_ZONE = "EXIT_PRE_DECISION_ZONE"


def log_enter_pre_decision_zone(
    service_name: str,
    policy_version: str | None = None,
) -> None:
    """
    Log ENTER_PRE_DECISION_ZONE event.

    Args:
        service_name: Affected service identifier
        policy_version: Optional policy snapshot reference
    """
    record = {
        "event": DecisionBoundaryEventType.ENTER_PRE_DECISION_ZONE.value,
        "service_name": service_name,
        "policy_version": policy_version,
        "timestamp": utc_now().isoformat(),
    }
    logger.info(json.dumps(record))


def log_intervention_evaluated(
    service_name: str,
    allowed: bool,
    reason: ReasonCode,
    policy_version: str | None = None,
) -> None:
    """
    Log INTERVENTION_EVALUATED event.

    Args:
        service_name: Affected service identifier
        allowed: Whether intervention is allowed
        reason: Reason code from fixed set
        policy_version: Optional policy snapshot reference
    """
    record = {
        "event": DecisionBoundaryEventType.INTERVENTION_EVALUATED.value,
        "allowed": allowed,
        "reason": reason.value,
        "service_name": service_name,
        "policy_version": policy_version,
        "timestamp": utc_now().isoformat(),
    }
    logger.info(json.dumps(record))


def log_exit_pre_decision_zone(
    service_name: str,
    policy_version: str | None = None,
) -> None:
    """
    Log EXIT_PRE_DECISION_ZONE event.

    Args:
        service_name: Affected service identifier
        policy_version: Optional policy snapshot reference
    """
    record = {
        "event": DecisionBoundaryEventType.EXIT_PRE_DECISION_ZONE.value,
        "service_name": service_name,
        "policy_version": policy_version,
        "timestamp": utc_now().isoformat(),
    }
    logger.info(json.dumps(record))


class DecisionLogger:
    """
    Decision Record Logger class.

    Provides a class-based interface for decision boundary logging.
    """

    def __init__(self, service_name: str, policy_version: str | None = None):
        """
        Initialize the decision logger.

        Args:
            service_name: Service name for all events
            policy_version: Optional policy version
        """
        self._service_name = service_name
        self._policy_version = policy_version

    def enter_pre_decision_zone(self) -> None:
        """Record entering pre-decision zone."""
        log_enter_pre_decision_zone(
            service_name=self._service_name,
            policy_version=self._policy_version,
        )

    def intervention_evaluated(
        self,
        allowed: bool,
        reason: ReasonCode,
    ) -> None:
        """Record intervention evaluation."""
        log_intervention_evaluated(
            service_name=self._service_name,
            allowed=allowed,
            reason=reason,
            policy_version=self._policy_version,
        )

    def exit_pre_decision_zone(self) -> None:
        """Record exiting pre-decision zone."""
        log_exit_pre_decision_zone(
            service_name=self._service_name,
            policy_version=self._policy_version,
        )


__all__ = [
    "ReasonCode",
    "DecisionBoundaryEventType",
    "DecisionLogger",
    "log_enter_pre_decision_zone",
    "log_intervention_evaluated",
    "log_exit_pre_decision_zone",
]
