"""
Manual Control Mixin for Circuit Breaker Service

Provides manual force open/close and TTL management functionality.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import structlog

from baldur.audit.helpers import (
    log_cb_state_change_audit,
    log_kill_switch_override_audit,
)
from baldur.core.decision_logger import DecisionLogger, ReasonCode
from baldur.core.execution_mode import get_execution_mode
from baldur.utils.time import utc_now

from .config import CircuitBreakerResult, CircuitState

if TYPE_CHECKING:
    from baldur.interfaces.repositories import CircuitBreakerStateRepository

    from .config import CircuitBreakerConfig

logger = structlog.get_logger()


def _is_system_enabled() -> bool:
    """Check if baldur system is enabled (Kill Switch not activated)."""
    try:
        from baldur.services.system_control import SystemControlManager

        manager = SystemControlManager()
        return manager.is_enabled()
    except Exception:
        # If SystemControlManager not available, assume enabled
        return True


def _warn_if_manual_override_under_dry_run(service_name: str, action: str) -> None:
    """Emit an in-band WARNING when a manual force runs while observe-only.

    Manual force_open/force_close are explicit operator intent and stay live
    under dry-run by design (observe-only suppresses only Baldur's *automatic*
    interventions). This surfaces the override at the moment it happens so the
    operator is not surprised; it does not change behavior — the force still
    executes.

    The observe-only signal is read through the single resolver
    (``get_execution_mode()``), so the warning covers the runtime dry-run toggle
    and a shadow/evaluation deployment posture alike. The read is fail-safe:
    any resolver error leaves the warning silent and the force runs as before.
    """
    try:
        observe_only = get_execution_mode().is_dry_run
    except Exception:
        return
    if observe_only:
        logger.warning(
            "system_control.manual_override_under_dry_run",
            manual_control_action=action,
            service_name=service_name,
        )


class ManualControlMixin:
    """
    Mixin class providing manual control functionality for CircuitBreakerService.

    Includes:
    - Force open/close operations
    - Reset operation
    - Manual override TTL management
    - Conditional replay trigger
    """

    if TYPE_CHECKING:
        # Host contract — supplied by CircuitBreakerService (config) and by
        # the @property on the host (repository); _emit_event is provided by
        # the EventEmitterMixin sibling. All host symbols are declared inside
        # TYPE_CHECKING so they don't conflict with property overrides at
        # runtime, while type checkers still resolve self.X.
        config: CircuitBreakerConfig
        repository: CircuitBreakerStateRepository

        def _emit_event(
            self,
            event_type: str,
            data: dict,
            *,
            priority: object = None,
        ) -> None: ...

    # =========================================================================
    # Manual Control Operations
    # =========================================================================

    def force_open(
        self,
        service_name: str,
        reason: str = "",
        override_kill_switch: bool = False,
    ) -> CircuitBreakerResult:
        """
        Force the circuit breaker to OPEN state (block all requests).

        Use this when an external service is detected as down
        and you want to stop sending requests.

        This operation uses atomic locking to prevent race conditions
        when multiple operators try to change the state simultaneously.

        Actor information is read from ActorContext (set by Django middleware
        or Celery task signal handlers). Audit Trail is the single source of
        truth for "who triggered this action".

        Args:
            service_name: Name of the external service
            reason: Reason for opening (for audit)
            override_kill_switch: If True, bypass Kill Switch check

        Returns:
            CircuitBreakerResult with operation outcome
        """
        # Get actor from context (set by middleware/signal handlers)
        from baldur.context.actor_context import ActorContext

        actor = ActorContext.get_current()

        # Kill Switch check
        kill_switch_result = self._check_kill_switch(
            service_name, "force_open", reason, None, override_kill_switch
        )
        if kill_switch_result:
            return kill_switch_result

        _warn_if_manual_override_under_dry_run(service_name, "force_open")

        decision_logger = DecisionLogger(service_name=service_name)
        decision_logger.intervention_evaluated(
            allowed=True,
            reason=ReasonCode.INTERVENTION_ALLOWED,
        )

        # Log before action
        logger.info(
            "circuit_breaker.event",
            service_name=service_name,
            reason=reason,
            actor_id=actor.actor_id,
            actor_type=actor.actor_type,
        )

        try:
            # Use atomic operation to prevent race conditions
            # controlled_by_id=None — Audit Trail is single source of truth for "who"
            success, previous_state, new_state = self.repository.atomic_force_open(
                service_name=service_name,
                reason=reason,
                controlled_by_id=None,
                ttl_minutes=self.config.manual_override_ttl_minutes,
            )

            if success:
                # 476 G8: clear cluster-wide HALF_OPEN counter post-transition.
                # atomic_force_open already wrote state=OPEN; this is a counter-only
                # cleanup so the next OPEN→HALF_OPEN transition starts fresh.
                self.repository.reset_half_open_count(service_name)
                if previous_state == new_state:
                    logger.info(
                        "circuit_breaker.circuit_already_open",
                        service_name=service_name,
                    )
                    return CircuitBreakerResult.succeeded(
                        service_name=service_name,
                        previous_state=previous_state,
                        new_state=new_state,
                        message="Circuit breaker already open",
                    )
                logger.warning(
                    "circuit_breaker.force_opened_circuit_reason",
                    service_name=service_name,
                    previous_state=previous_state,
                    new_state=new_state,
                    reason=reason,
                )
                # Audit with actor info from context
                log_cb_state_change_audit(
                    cb_name=service_name,
                    old_state=previous_state,
                    new_state=new_state,
                    reason=(
                        f"force_open: {reason}" if reason else "force_open: manual"
                    ),
                    actor_id=actor.actor_id,
                    actor_type=actor.actor_type,
                )
                # Push event - record the CB state-change metric
                try:
                    from baldur.metrics.event_handlers import (
                        CircuitBreakerEventHandler,
                    )

                    CircuitBreakerEventHandler.on_state_changed(
                        service=service_name,
                        from_state=previous_state,
                        to_state=new_state,
                    )
                except ImportError:
                    pass  # Metrics not available

                # EventBus emission — Manual OPEN
                from baldur.services.event_bus import EventType
                from baldur.utils.time import utc_now

                self._emit_event(
                    EventType.CIRCUIT_BREAKER_OPENED,
                    data={
                        "service_name": service_name,
                        "previous_state": previous_state,
                        "timestamp": utc_now().isoformat(),
                        "trigger": "manual",
                    },
                )

                return CircuitBreakerResult.succeeded(
                    service_name=service_name,
                    previous_state=previous_state,
                    new_state=new_state,
                    message=f"Circuit breaker opened for {service_name}",
                )
            return CircuitBreakerResult.failed(
                service_name=service_name,
                error="Failed to force open circuit breaker",
            )
        except Exception as e:
            logger.exception(
                "circuit_breaker.force_open_failed",
                error=e,
            )
            return CircuitBreakerResult.failed(
                service_name=service_name,
                error=str(e),
            )

    def force_close(
        self,
        service_name: str,
        reason: str = "",
        trigger_replay: bool = False,
        override_kill_switch: bool = False,
    ) -> CircuitBreakerResult:
        """
        Force the circuit breaker to CLOSED state (allow all requests).

        Use this when an external service has recovered
        and you want to resume normal operations.

        This operation uses atomic locking to prevent race conditions
        when multiple operators try to change the state simultaneously.

        Actor information is read from ActorContext (set by Django middleware
        or Celery task signal handlers). Audit Trail is the single source of
        truth for "who triggered this action".

        Args:
            service_name: Name of the external service
            reason: Reason for closing (for audit)
            trigger_replay: Whether to trigger conditional replay for queued items
            override_kill_switch: If True, bypass Kill Switch check

        Returns:
            CircuitBreakerResult with operation outcome
        """
        # Get actor from context (set by middleware/signal handlers)
        from baldur.context.actor_context import ActorContext

        actor = ActorContext.get_current()

        # Kill Switch check
        kill_switch_result = self._check_kill_switch(
            service_name, "force_close", reason, None, override_kill_switch
        )
        if kill_switch_result:
            return kill_switch_result

        _warn_if_manual_override_under_dry_run(service_name, "force_close")

        decision_logger = DecisionLogger(service_name=service_name)
        decision_logger.intervention_evaluated(
            allowed=True,
            reason=ReasonCode.INTERVENTION_ALLOWED,
        )

        logger.info(
            "circuit_breaker.event",
            service_name=service_name,
            reason=reason,
            actor_id=actor.actor_id,
            actor_type=actor.actor_type,
            trigger_replay=trigger_replay,
        )

        try:
            # controlled_by_id=None — Audit Trail is single source of truth for "who"
            success, previous_state, new_state = self.repository.atomic_force_close(
                service_name=service_name,
                reason=reason,
                controlled_by_id=None,
            )

            if success:
                # 476 G8: see force_open above. atomic_force_close already
                # resets the counter on most adapters but the discrete call
                # is harmless and keeps the contract explicit at the service
                # tier.
                self.repository.reset_half_open_count(service_name)
                return self._handle_force_close_success(
                    service_name, previous_state, new_state, reason, trigger_replay
                )
            return CircuitBreakerResult.failed(
                service_name=service_name,
                error="Failed to force close circuit breaker",
            )
        except Exception as e:
            logger.exception(
                "circuit_breaker.force_close_failed",
                error=e,
            )
            return CircuitBreakerResult.failed(
                service_name=service_name,
                error=str(e),
            )

    def _check_kill_switch(
        self,
        service_name: str,
        action: str,
        reason: str,
        controlled_by_id: int | None,
        override_kill_switch: bool,
    ) -> CircuitBreakerResult | None:
        """Check the Kill Switch. Return a result when blocked, None when passed."""
        if _is_system_enabled():
            return None

        if not override_kill_switch:
            logger.warning(
                "circuit_breaker.blocked_kill_switch_active",
                manual_control_action=action,
                service_name=service_name,
            )
            return CircuitBreakerResult.failed(
                service_name=service_name,
                error="Kill Switch is active: use override_kill_switch=True for manual control",
            )

        # Audit record on Kill Switch override
        logger.warning(
            "circuit_breaker.kill_switch_override",
            manual_control_action=action,
            service_name=service_name,
            controlled_by_id=controlled_by_id,
        )
        log_kill_switch_override_audit(
            service_name=service_name,
            action=action,
            reason=reason,
            controlled_by_id=controlled_by_id,
        )

        return None

    def _handle_force_close_success(
        self,
        service_name: str,
        previous_state: str,
        new_state: str,
        reason: str,
        trigger_replay: bool,
    ) -> CircuitBreakerResult:
        """Handle a successful force close."""
        if previous_state == new_state:
            logger.info(
                "circuit_breaker.circuit_already_closed",
                service_name=service_name,
            )
            return CircuitBreakerResult.succeeded(
                service_name=service_name,
                previous_state=previous_state,
                new_state=new_state,
                message="Circuit breaker already closed",
            )

        logger.info(
            "circuit_breaker.force_closed_circuit_reason",
            service_name=service_name,
            previous_state=previous_state,
            new_state=new_state,
            reason=reason,
        )

        self._log_state_change_audit(
            service_name, previous_state, new_state, reason, "force_close"
        )
        self._emit_state_change_metric(service_name, previous_state, new_state)

        # EventBus emission — Manual CLOSE
        from baldur.services.event_bus import EventType
        from baldur.utils.time import utc_now

        self._emit_event(
            EventType.CIRCUIT_BREAKER_CLOSED,
            data={
                "service_name": service_name,
                "previous_state": previous_state,
                "timestamp": utc_now().isoformat(),
                "trigger": "manual",
                "trigger_replay": trigger_replay,
            },
        )

        return CircuitBreakerResult.succeeded(
            service_name=service_name,
            previous_state=previous_state,
            new_state=new_state,
            message=f"Circuit breaker closed for {service_name}",
        )

    def _log_state_change_audit(
        self,
        service_name: str,
        previous_state: str,
        new_state: str,
        reason: str,
        action: str,
    ) -> None:
        """CB state change audit log."""
        try:
            from baldur.context.actor_context import ActorContext

            actor = ActorContext.get_current()
            log_cb_state_change_audit(
                cb_name=service_name,
                old_state=previous_state,
                new_state=new_state,
                reason=f"{action}: {reason}" if reason else f"{action}: manual",
                actor_id=actor.actor_id,
                actor_type=actor.actor_type,
            )
        except Exception as e:
            logger.debug(
                "circuit_breaker.audit_log_failed",
                error=e,
            )

    def _emit_state_change_metric(
        self,
        service_name: str,
        previous_state: str,
        new_state: str,
    ) -> None:
        """Record the CB state-change metric."""
        try:
            from baldur.metrics.event_handlers import CircuitBreakerEventHandler

            CircuitBreakerEventHandler.on_state_changed(
                service=service_name,
                from_state=previous_state,
                to_state=new_state,
            )
        except ImportError:
            pass

    # =========================================================================
    # Reset Operations
    # =========================================================================

    def reset(
        self,
        service_name: str,
        controlled_by: int | None = None,
        reason: str = "",
    ) -> CircuitBreakerResult:
        """
        Reset a circuit breaker to initial state.

        Clears all counters and sets state to CLOSED.
        Uses atomic operation to prevent race conditions.

        Args:
            service_name: Name of the external service
            controlled_by: User ID who initiated the reset (optional)
            reason: Reason for reset (for audit)

        Returns:
            CircuitBreakerResult with operation outcome
        """
        try:
            # Use atomic operation to prevent race conditions
            success, previous_state, new_state = self.repository.atomic_reset(
                service_name=service_name,
                reason=reason,
                controlled_by_id=controlled_by,
            )

            if success:
                logger.info(
                    "circuit_breaker.reset_circuit_reason",
                    service_name=service_name,
                    previous_state=previous_state,
                    new_state=new_state,
                    reason=reason,
                )
                # Audit - reset is a state-reset event
                if previous_state != new_state:
                    log_cb_state_change_audit(
                        cb_name=service_name,
                        old_state=previous_state,
                        new_state=new_state,
                        reason=f"reset: {reason}" if reason else "reset: manual",
                    )
                # Push event - record the CB state-change metric
                if previous_state != new_state:
                    try:
                        from baldur.metrics.event_handlers import (
                            CircuitBreakerEventHandler,
                        )

                        CircuitBreakerEventHandler.on_state_changed(
                            service=service_name,
                            from_state=previous_state,
                            to_state=new_state,
                        )
                    except ImportError:
                        pass  # Metrics not available

                    # EventBus emission — Manual RESET (emits CLOSED)
                    from baldur.services.event_bus import EventType
                    from baldur.utils.time import utc_now

                    self._emit_event(
                        EventType.CIRCUIT_BREAKER_CLOSED,
                        data={
                            "service_name": service_name,
                            "previous_state": previous_state,
                            "timestamp": utc_now().isoformat(),
                            "trigger": "manual_reset",
                            "trigger_replay": True,
                        },
                    )

                return CircuitBreakerResult.succeeded(
                    service_name=service_name,
                    previous_state=previous_state,
                    new_state=new_state,
                    message=f"Circuit breaker reset for {service_name}",
                )
            return CircuitBreakerResult.failed(
                service_name=service_name,
                error=f"Circuit breaker for '{service_name}' does not exist",
            )
        except Exception as e:
            logger.exception(
                "circuit_breaker.reset_failed",
                error=e,
            )
            return CircuitBreakerResult.failed(
                service_name=service_name,
                error=str(e),
            )

    # =========================================================================
    # Manual Override TTL Management
    # =========================================================================

    def check_and_expire_manual_overrides(self) -> list[str]:
        """
        Check all circuit breakers for expired manual overrides.

        Manual overrides have a TTL to prevent "forgotten" blocks.
        When expired, circuits transition from OPEN to HALF_OPEN
        for gradual recovery testing.

        Returns:
            List of service names that had their overrides expired
        """
        expired_services = []

        try:
            # Get all states and filter manually controlled ones
            all_states = self.repository.get_all_states()
            current_time = utc_now()

            for state in all_states:
                if (
                    state.manually_controlled
                    and state.manual_override_expires_at
                    and state.manual_override_expires_at <= current_time
                ):
                    previous_state = state.state
                    previous_reason = state.control_reason or ""
                    f"{previous_reason} [EXPIRED]".strip()

                    # Expire the override - transition to HALF_OPEN for testing
                    self.repository.update_state(
                        service_name=state.service_name,
                        state=CircuitState.HALF_OPEN,
                    )
                    # Note: If you need to update control_reason,
                    # implement it in your repository adapter

                    self.repository.clear_manual_control(
                        state.service_name, preserve_reason=True
                    )

                    expired_services.append(state.service_name)
                    logger.warning(
                        "circuit_breaker.manual_override_expired",
                        target_service_name=state.service_name,
                        previous_state=previous_state,
                    )
        except Exception as e:
            logger.exception(
                "circuit_breaker.check_expired_overrides_failed",
                error=e,
            )

        return expired_services

    def extend_manual_override(
        self,
        service_name: str,
        additional_minutes: int = 90,
        controlled_by_id: int | None = None,
        reason: str = "",
    ) -> CircuitBreakerResult:
        """
        Extend the TTL of an existing manual override.

        Use this when an operator needs more time to resolve an issue.

        Args:
            service_name: Name of the external service
            additional_minutes: Minutes to extend the override
            controlled_by_id: User ID who initiated the extension
            reason: Reason for extension

        Returns:
            CircuitBreakerResult with operation outcome
        """
        try:
            state = self.repository.get_by_service_name(service_name)
            if state is None:
                return CircuitBreakerResult.failed(
                    service_name=service_name,
                    error=f"Circuit breaker for '{service_name}' does not exist",
                )

            if not state.manually_controlled:
                return CircuitBreakerResult.failed(
                    service_name=service_name,
                    error="Circuit is not under manual control",
                )

            # Extend TTL
            current_time = utc_now()
            if state.manual_override_expires_at:
                new_expires_at = state.manual_override_expires_at + timedelta(
                    minutes=additional_minutes
                )
            else:
                new_expires_at = current_time + timedelta(minutes=additional_minutes)

            new_reason = (
                f"{state.control_reason} | Extended: {reason}"
                if reason
                else state.control_reason
            )

            self.repository.set_manual_control(
                service_name=service_name,
                state=state.state,
                controlled_by_id=controlled_by_id,
                reason=new_reason,
                expires_at=new_expires_at,
            )

            logger.info(
                "circuit_breaker.extended_manual_override_minutes",
                service_name=service_name,
                additional_minutes=additional_minutes,
            )

            return CircuitBreakerResult.succeeded(
                service_name=service_name,
                previous_state=state.state,
                new_state=state.state,
                message=f"Manual override extended by {additional_minutes} minutes",
            )
        except Exception as e:
            logger.exception(
                "circuit_breaker.extend_override_failed",
                error=e,
            )
            return CircuitBreakerResult.failed(
                service_name=service_name,
                error=str(e),
            )
