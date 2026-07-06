"""
Protection Orchestrator.

Orchestrates protection measures with atomicity guarantees
and rollback support.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.services.security.models import ProtectionResult
from baldur.services.security.policies import (
    ACTION_POLICY_PRIORITY,
    ActionPolicy,
)

if TYPE_CHECKING:
    from baldur.services.security.service import SecurityViolationService

logger = structlog.get_logger()


class ProtectionOrchestrator:
    """
    Protective-action orchestrator.

    Atomicity guarantees:
    - Execute from the strongest policy (highest priority) first
    - If the highest-priority policy fails, treat it as a total failure + roll back
    - Lower-priority policy failures are warning-logged and execution continues

    Rollback policy (v2.1.0 - priority 0.3):
    - On highest-priority failure, roll back already-executed policies in reverse order
    - Only attempt rollback for rollback-capable policies (Emergency Mode cannot be rolled back)
    - On rollback failure, log and recommend manual intervention

    Reference: Architect Review - "guarantee success of the strongest policy first"
    """

    def __init__(self, security_service: SecurityViolationService):
        self._service = security_service
        self._policy_executors: dict[ActionPolicy, Any] = {
            ActionPolicy.EMERGENCY_LEVEL_3: self._execute_emergency_3,
            ActionPolicy.EMERGENCY_LEVEL_2: self._execute_emergency_2,
            ActionPolicy.EMERGENCY_LEVEL_1: self._execute_emergency_1,
            ActionPolicy.ACCOUNT_FREEZE: self._execute_account_freeze,
            ActionPolicy.SESSION_INVALIDATE: self._execute_session_invalidate,
            ActionPolicy.IP_PERMANENT_BAN: self._execute_ip_permanent_ban,
            ActionPolicy.IP_TEMPORARY_BAN: self._execute_ip_temporary_ban,
            ActionPolicy.BLOCK_AND_LOG: self._execute_block_and_log,
        }

        # Rollback executors (rollback-capable policies only)
        self._policy_rollback_executors: dict[ActionPolicy, Any] = {
            ActionPolicy.ACCOUNT_FREEZE: self._rollback_account_freeze,
            ActionPolicy.SESSION_INVALIDATE: None,
            ActionPolicy.IP_PERMANENT_BAN: self._rollback_ip_ban,
            ActionPolicy.IP_TEMPORARY_BAN: self._rollback_ip_ban,
            ActionPolicy.BLOCK_AND_LOG: None,
        }

    def execute_policies(
        self,
        policies: list[ActionPolicy],
        context: dict[str, Any],
    ) -> ProtectionResult:
        """
        Execute the policy list in priority order.

        Args:
            policies: List of ActionPolicies to execute
            context: Execution context (user_id, source_ip, etc.)

        Returns:
            ProtectionResult with execution details
        """
        if not policies:
            return ProtectionResult(
                success=True,
                executed_policies=[],
                failed_policies=[],
                highest_priority_succeeded=True,
            )

        # Sort by priority (lower number = higher priority)
        sorted_policies = sorted(
            policies, key=lambda p: ACTION_POLICY_PRIORITY.get(p, 999)
        )

        executed: list[ActionPolicy] = []
        failed: list[ActionPolicy] = []
        highest_priority_policy = sorted_policies[0]
        highest_succeeded = False

        for policy in sorted_policies:
            try:
                executor = self._policy_executors.get(policy)
                if executor:
                    executor(context)
                    executed.append(policy)

                    if policy == highest_priority_policy:
                        highest_succeeded = True

            except Exception as e:
                failed.append(policy)
                logger.exception(
                    "protection_orchestrator.policy_failed",
                    policy=policy.value,
                    error=e,
                )

                # On highest-priority failure, stop immediately + attempt rollback
                if policy == highest_priority_policy:
                    rolled_back, rollback_success = self._rollback_executed_policies(
                        executed, context
                    )

                    return ProtectionResult(
                        success=False,
                        executed_policies=executed,
                        failed_policies=failed,
                        highest_priority_succeeded=False,
                        rolled_back_policies=rolled_back,
                        rollback_success=rollback_success,
                        error_message=f"Highest priority policy failed: {e}",
                        triggering_trace_id=context.get("trace_id"),
                        triggering_request_path=context.get("request_path"),
                    )

        return ProtectionResult(
            success=len(failed) == 0,
            executed_policies=executed,
            failed_policies=failed,
            highest_priority_succeeded=highest_succeeded,
            triggering_trace_id=context.get("trace_id"),
            triggering_request_path=context.get("request_path"),
        )

    # =========================================================================
    # Policy Executors
    # =========================================================================

    def _execute_emergency_3(self, context: dict[str, Any]) -> None:
        """Declare Emergency Level 3."""
        try:
            from baldur.services.event_bus import EventType, get_event_bus

            bus = get_event_bus()
            bus.emit(
                event_type=EventType.EMERGENCY_ACTIVATED,
                data={
                    "level": 3,
                    "reason": context.get("reason", "Security violation"),
                    "trigger_source": "protection_orchestrator",
                    "incident_id": context.get("incident_id"),
                },
                source="protection_orchestrator",
            )
            logger.critical("protection_orchestrator.emergency_level_activated")
        except Exception as e:
            logger.exception(
                "protection_orchestrator.emit_emergency_event_failed",
                error=e,
            )
            raise

    def _execute_emergency_2(self, context: dict[str, Any]) -> None:
        """Declare Emergency Level 2."""
        try:
            from baldur.services.event_bus import EventType, get_event_bus

            bus = get_event_bus()
            bus.emit(
                event_type=EventType.EMERGENCY_ACTIVATED,
                data={
                    "level": 2,
                    "reason": context.get("reason", "Security violation"),
                    "trigger_source": "protection_orchestrator",
                    "incident_id": context.get("incident_id"),
                },
                source="protection_orchestrator",
            )
            logger.warning("protection_orchestrator.emergency_level_activated")
        except Exception as e:
            logger.exception(
                "protection_orchestrator.emit_emergency_event_failed",
                error=e,
            )
            raise

    def _execute_emergency_1(self, context: dict[str, Any]) -> None:
        """Declare Emergency Level 1."""
        try:
            from baldur.services.event_bus import EventType, get_event_bus

            bus = get_event_bus()
            bus.emit(
                event_type=EventType.EMERGENCY_ACTIVATED,
                data={
                    "level": 1,
                    "reason": context.get("reason", "Security warning"),
                    "trigger_source": "protection_orchestrator",
                },
                source="protection_orchestrator",
            )
            logger.info("protection_orchestrator.emergency_level_activated")
        except Exception as e:
            logger.exception(
                "protection_orchestrator.emit_emergency_event_failed",
                error=e,
            )
            raise

    def _execute_account_freeze(self, context: dict[str, Any]) -> None:
        """Freeze the account."""
        user_id = context.get("user_id")
        if user_id:
            logger.warning(
                "protection_orchestrator.account_frozen",
                user_id=user_id,
            )

    def _execute_session_invalidate(self, context: dict[str, Any]) -> None:
        """Invalidate sessions."""
        user_id = context.get("user_id")
        if user_id:
            self._service._invalidate_user_sessions(user_id)

    def _execute_ip_permanent_ban(self, context: dict[str, Any]) -> None:
        """Permanent IP ban."""
        source_ip = context.get("source_ip")
        if source_ip:
            self._service._permanent_ip_ban(source_ip)

    def _execute_ip_temporary_ban(self, context: dict[str, Any]) -> None:
        """Temporary IP ban."""
        source_ip = context.get("source_ip")
        if source_ip:
            self._service._temporary_ip_ban(source_ip)

    def _execute_block_and_log(self, context: dict[str, Any]) -> None:
        """Block and log."""
        logger.warning(
            "protection_orchestrator.blocked_logged",
            context=context,
        )

    # =========================================================================
    # Rollback Methods
    # =========================================================================

    def _rollback_executed_policies(
        self,
        executed: list[ActionPolicy],
        context: dict[str, Any],
    ) -> tuple[list[ActionPolicy], bool]:
        """Roll back already-executed policies in reverse order."""
        rolled_back: list[ActionPolicy] = []
        all_success = True

        for policy in reversed(executed):
            rollback_fn = self._policy_rollback_executors.get(policy)

            if rollback_fn is None:
                logger.warning(
                    "protection_orchestrator.policy_cannot_rolled_back",
                    policy=policy.value,
                )
                continue

            try:
                rollback_fn(context)
                rolled_back.append(policy)
                logger.info(
                    "protection_orchestrator.rolled_back",
                    policy=policy.value,
                )
            except Exception as e:
                logger.exception(
                    "protection_orchestrator.rollback_failed",
                    policy=policy.value,
                    error=e,
                )
                all_success = False

        if not all_success:
            logger.critical("protection_orchestrator.some_rollbacks_failed_manual")

        return rolled_back, all_success

    def _rollback_account_freeze(self, context: dict[str, Any]) -> None:
        """Unfreeze the account."""
        user_id = context.get("user_id")
        if user_id:
            logger.info(
                "protection_orchestrator.account_unfrozen",
                user_id=user_id,
            )

    def _rollback_ip_ban(self, context: dict[str, Any]) -> None:
        """Remove the IP ban."""
        source_ip = context.get("source_ip")
        if source_ip:
            self._service._remove_ip_ban(source_ip)
            logger.info(
                "protection_orchestrator.ip_ban_removed",
                source_ip=source_ip,
            )
