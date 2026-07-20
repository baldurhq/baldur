"""
X-Test-Mode Circuit Breaker Views

Circuit Breaker test APIs:
- InjectCBFailureView: inject CB failures
- ResetCBView: reset CB state
- CBStatusDetailView: query CB state
- FastFailTestView: verify fast fail
- TriggerCBRecoveryView: trigger CB recovery
"""

import time

import structlog
from django.utils import timezone
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .base import XTestModeMixin, collect_system_snapshot

logger = structlog.get_logger()


class InjectCBFailureView(XTestModeMixin, APIView):
    """
    Circuit Breaker failure injection API.

    POST /api/baldur/xtest/inject-cb-failure/

    Request:
        {
            "service": "database",
            "count": 5  // failure_threshold (default)
        }

    Response:
        {
            "service": "database",
            "injected_failures": 5,
            "cb_state": "open",
            "previous_state": "closed",
            "timestamp": "2025-12-26T14:01:23+09:00",
            "snapshot": {...}
        }
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        service_name = request.data.get("service", "database")
        failure_count = int(request.data.get("count", 5))

        # Cap the injection count (safety guard)
        max_injection = 20
        if failure_count > max_injection:
            return Response(
                {
                    "status": "error",
                    "error": "injection_limit_exceeded",
                    "message": f"Maximum injection count is {max_injection}",
                    "requested": failure_count,
                    "max_allowed": max_injection,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Exceptions are handled by the exception handler
        from baldur.services.circuit_breaker import (
            force_open_circuit,
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()

        # Record the previous state
        previous_state = cb_service.get_state(service_name)

        # Bypass L1 and record the failures directly
        for i in range(failure_count):
            cb_service.record_failure(
                service_name,
                error_context={
                    "source": "x-test-mode",
                    "injection_number": i + 1,
                    "total_injections": failure_count,
                    "user": str(request.user),
                },
            )

        # Check the current state (after failure injection)
        current_state = cb_service.get_state(service_name)

        # Force OPEN when the minimum_calls condition kept it from opening
        force_opened = False
        if current_state != "open" and request.data.get("force_open", True):
            result = force_open_circuit(
                service_name,
                reason=f"X-Test-Mode injection: {failure_count} failures by xtest:{request.user}",
            )
            if result.success:
                current_state = "open"
                force_opened = True

        # Collect a snapshot
        snapshot = collect_system_snapshot()

        logger.info(
            "test.mode_cb_failure",
            service_name=service_name,
            failure_count=failure_count,
            previous_state=previous_state,
            current_state=current_state,
            request_user=request.user,
        )

        response_data = {
            "status": "success",
            "service": service_name,
            "injected_failures": failure_count,
            "previous_state": previous_state,
            "cb_state": current_state,
            "state_changed": previous_state != current_state,
            "force_opened": force_opened,
            "timestamp": timezone.now().isoformat(),
            "snapshot": snapshot,
        }

        # WAL audit record
        self.log_xtest_injection(
            request=request,
            component="cb",
            injection_type="failure",
            count=failure_count,
            target_ids=[service_name],
        )

        return Response(response_data)


class ResetCBView(XTestModeMixin, APIView):
    """
    Circuit Breaker state reset API.

    POST /api/baldur/xtest/reset-cb/

    Request:
        {
            "service": "database"
        }
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        service_name = request.data.get("service", "database")

        # Exceptions are handled by the exception handler
        from baldur.services.circuit_breaker import (
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()

        # Record the previous state
        previous_state = cb_service.get_state(service_name)

        # Force close - actor information is read from ActorContext.
        result = cb_service.force_close(
            service_name=service_name,
            reason=f"X-Test-Mode reset by {request.user}",
        )

        current_state = cb_service.get_state(service_name)

        logger.info(
            "test.mode_cb_reset",
            service_name=service_name,
            previous_state=previous_state,
            current_state=current_state,
            request_user=request.user,
        )

        response_data = {
            "status": "success",
            "service": service_name,
            "previous_state": previous_state,
            "cb_state": current_state,
            "reset_result": result.success if hasattr(result, "success") else True,
            "timestamp": timezone.now().isoformat(),
        }

        # WAL audit record
        self.log_xtest_cleanup(
            request=request,
            component="cb",
            cleaned_count=1,
            cleaned_ids=[service_name],
        )

        return Response(response_data)


class CBStatusDetailView(XTestModeMixin, APIView):
    """
    Circuit Breaker detailed status API.

    GET /api/baldur/xtest/cb-status/?service=database
    """

    def get(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        service_name = request.query_params.get("service")

        # Exceptions are handled by the exception handler
        from baldur.services.circuit_breaker import (
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()

        if service_name:
            # State of one specific service
            state_data = cb_service.get_or_create_state(service_name)

            return Response(
                {
                    "status": "success",
                    "service": service_name,
                    "cb_state": state_data.state,
                    "failure_count": state_data.failure_count,
                    "success_count": getattr(state_data, "success_count", 0),
                    "last_failure_time": getattr(state_data, "last_failure_time", None),
                    "opened_at": getattr(state_data, "opened_at", None),
                    "manually_controlled": getattr(
                        state_data, "manually_controlled", False
                    ),
                    "config": {
                        "failure_threshold": cb_service.config.failure_threshold,
                        "recovery_timeout": cb_service.config.recovery_timeout,
                        "success_threshold": cb_service.config.success_threshold,
                        "minimum_calls": cb_service.config.minimum_calls,
                    },
                    "timestamp": timezone.now().isoformat(),
                }
            )
        # State of all services (queried from the repository)
        all_states = cb_service.repository.get_all_states()

        services = {}
        for state_data in all_states:
            services[state_data.service_name] = {
                "state": state_data.state,
                "failure_count": state_data.failure_count,
                "success_count": getattr(state_data, "success_count", 0),
                "opened_at": getattr(state_data, "opened_at", None),
            }

        return Response(
            {
                "status": "success",
                "services": services,
                "total_count": len(services),
                "config": {
                    "failure_threshold": cb_service.config.failure_threshold,
                    "recovery_timeout": cb_service.config.recovery_timeout,
                },
                "timestamp": timezone.now().isoformat(),
            }
        )


class FastFailTestView(XTestModeMixin, APIView):
    """
    Fast fail verification API - measures response time while the CB is OPEN.

    GET /api/baldur/xtest/fast-fail-test/?service=database
    """

    def get(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        service_name = request.query_params.get("service", "database")

        # Exceptions are handled by the exception handler
        from baldur.services.circuit_breaker import (
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()

        # Check the state
        current_state = cb_service.get_state(service_name)

        # Measure the should_allow check time
        start_time = time.time()
        allowed = cb_service.should_allow(service_name)
        elapsed_ms = (time.time() - start_time) * 1000

        is_fast_fail = elapsed_ms < 100  # under 100ms

        return Response(
            {
                "status": "success",
                "service": service_name,
                "cb_state": current_state,
                "request_allowed": allowed,
                "response_time_ms": round(elapsed_ms, 2),
                "is_fast_fail": is_fast_fail,
                "fast_fail_threshold_ms": 100,
                "timestamp": timezone.now().isoformat(),
            }
        )


class TriggerCBRecoveryView(XTestModeMixin, APIView):
    """
    CB recovery trigger API - records successes in HALF_OPEN to recover to CLOSED.

    POST /api/baldur/xtest/trigger-cb-recovery/
    Body: {"service": "database", "success_count": 3, "force": false}

    Calls record_success while in HALF_OPEN to bring the CB back to CLOSED.
    - force=true: transition straight to CLOSED (for testing)
    - force=false: call record_success (normal flow)

    Note: The DB model's half_open_max_calls default is 3.
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        service_name = request.data.get("service", "database")
        success_count = request.data.get("success_count", 3)
        force_close = request.data.get("force", False)

        # Exceptions are handled by the exception handler
        from baldur.services.circuit_breaker import (
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()

        # Check the current state
        state_before = cb_service.get_state(service_name)

        successes_recorded = 0

        if force_close and state_before in ("half_open", "open"):
            # Forced transition to CLOSED (test-only)
            cb_service.repository.update_state(
                service_name=service_name,
                state="closed",
                failure_count=0,
                success_count=0,
                opened_at=None,
            )
            logger.info(
                "test.mode_cb_force",
                service_name=service_name,
            )
        else:
            # Normal recovery flow: call record_success
            for _i in range(success_count):
                current_state = cb_service.get_state(service_name)
                if current_state == "half_open":
                    cb_service.record_success(service_name)
                    successes_recorded += 1
                elif current_state == "closed":
                    break
                else:
                    break

        # Check the final state
        state_after = cb_service.get_state(service_name)

        recovery_success = state_after == "closed"

        logger.info(
            "test.mode_cb_recovery",
            service_name=service_name,
            state_before=state_before,
            state_after=state_after,
            successes_recorded=successes_recorded,
            force_close=force_close,
        )

        response_data = {
            "status": "success",
            "service": service_name,
            "state_before": state_before,
            "state_after": state_after,
            "successes_recorded": successes_recorded,
            "force_closed": force_close,
            "recovery_success": recovery_success,
            "timestamp": timezone.now().isoformat(),
        }

        # WAL audit record
        self.log_xtest_audit(
            request=request,
            action="trigger_recovery",
            component="cb",
            details={
                "service": service_name,
                "state_before": state_before,
                "state_after": state_after,
            },
            result="success" if recovery_success else "partial",
        )

        return Response(response_data)


class TryRecoveryTransitionView(XTestModeMixin, APIView):
    """
    CB OPEN → HALF_OPEN transition attempt API (domain-free).

    POST /api/baldur/xtest/try-recovery-transition/
    Body: {"service": "stage15_platinum"}

    **Explicit human-driven transition API**:
    - Transitions OPEN → HALF_OPEN once recovery_timeout has elapsed
    - Returns the remaining wait time if recovery_timeout has not elapsed
    - Domain-free: not tied to a specific domain such as payment/order

    This API explicitly calls should_allow() to trigger the CB's
    automatic transition logic.
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        service_name = request.data.get("service", "database")

        # Exceptions are handled by the exception handler
        from baldur.services.circuit_breaker import (
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()

        # Check the current state
        state_before = cb_service.get_or_create_state(service_name)
        state_str_before = state_before.state
        opened_at = state_before.opened_at

        # Compute recovery_timeout
        remaining_seconds = None
        recovery_timeout = cb_service.config.recovery_timeout

        if state_str_before == "open" and opened_at:
            elapsed = (timezone.now() - opened_at).total_seconds()
            remaining_seconds = max(0, recovery_timeout - elapsed)

        # Call should_allow - this triggers the OPEN → HALF_OPEN transition
        allowed = cb_service.should_allow(service_name)

        # Check the state after the transition
        state_after = cb_service.get_or_create_state(service_name)
        state_str_after = state_after.state

        transition_occurred = state_str_before != state_str_after

        logger.info(
            "test.mode_try_recovery",
            service_name=service_name,
            state_str_before=state_str_before,
            state_str_after=state_str_after,
            allowed=allowed,
            transition_occurred=transition_occurred,
        )

        response_data = {
            "status": "success",
            "service": service_name,
            "state_before": state_str_before,
            "state_after": state_str_after,
            "transition_occurred": transition_occurred,
            "allowed": allowed,
            "remaining_seconds": remaining_seconds,
            "recovery_timeout": recovery_timeout,
            "opened_at": opened_at.isoformat() if opened_at else None,
            "message": (
                f"Transition {state_str_before}→{state_str_after}"
                if transition_occurred
                else (
                    f"No transition yet, remaining: {remaining_seconds:.1f}s"
                    if remaining_seconds and remaining_seconds > 0
                    else f"State is {state_str_after}"
                )
            ),
            "timestamp": timezone.now().isoformat(),
        }

        # WAL audit record
        self.log_xtest_audit(
            request=request,
            action="try_recovery_transition",
            component="cb",
            details={
                "service": service_name,
                "state_before": state_str_before,
                "state_after": state_str_after,
                "transition_occurred": transition_occurred,
            },
            result="success",
        )

        return Response(response_data)


class SwitchToAutoModeView(XTestModeMixin, APIView):
    """
    API that switches a CB to auto mode (sets manually_controlled=False).

    POST /api/baldur/xtest/switch-to-auto/
    Body: {"service": "database"}

    Clears the manually_controlled=True state left behind by force_open so the
    CB automatically transitions to HALF_OPEN after recovery_timeout.
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        service_name = request.data.get("service", "database")

        # Exceptions are handled by the exception handler
        from baldur.services.circuit_breaker import (
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()

        # Check the current state
        state_before = cb_service.get_or_create_state(service_name)
        was_manually_controlled = state_before.manually_controlled

        # Set manually_controlled=False (switch to auto mode)
        # Use clear_manual_control (preserve_reason=True keeps the state)
        cb_service.repository.clear_manual_control(
            service_name=service_name,
            preserve_reason=True,  # keep the reason, release only manual control
        )

        # Check the final state
        state_after = cb_service.get_or_create_state(service_name)

        logger.info(
            "test.mode_cb_switched",
            service_name=service_name,
            was_manually_controlled=was_manually_controlled,
        )

        response_data = {
            "status": "success",
            "service": service_name,
            "cb_state": state_after.state,
            "was_manually_controlled": was_manually_controlled,
            "is_manually_controlled": state_after.manually_controlled,
            "message": f"Circuit breaker for '{service_name}' switched to auto mode",
            "timestamp": timezone.now().isoformat(),
        }

        # WAL audit record
        self.log_xtest_audit(
            request=request,
            action="switch_to_auto",
            component="cb",
            details={
                "service": service_name,
                "was_manually_controlled": was_manually_controlled,
            },
            result="success",
        )

        return Response(response_data)


__all__ = [
    "InjectCBFailureView",
    "ResetCBView",
    "CBStatusDetailView",
    "FastFailTestView",
    "TriggerCBRecoveryView",
    "TryRecoveryTransitionView",
    "SwitchToAutoModeView",
]
