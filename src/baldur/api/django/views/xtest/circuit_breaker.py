"""
X-Test-Mode Circuit Breaker Views

Circuit Breaker 관련 테스트 API:
- InjectCBFailureView: CB 장애 주입
- ResetCBView: CB 상태 초기화
- CBStatusDetailView: CB 상태 조회
- FastFailTestView: Fast Fail 검증
- TriggerCBRecoveryView: CB 복구 트리거
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
    Circuit Breaker 장애 주입 API.

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

        # 최대 주입 횟수 제한 (안전 장치)
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

        # Exception은 exception handler가 처리
        from baldur.services.circuit_breaker import (
            force_open_circuit,
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()

        # 이전 상태 기록
        previous_state = cb_service.get_state(service_name)

        # L1 우회하여 직접 실패 기록
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

        # 현재 상태 확인 (실패 주입 후)
        current_state = cb_service.get_state(service_name)

        # minimum_calls 조건 때문에 OPEN이 안 된 경우, 강제로 OPEN
        force_opened = False
        if current_state != "open" and request.data.get("force_open", True):
            result = force_open_circuit(
                service_name,
                reason=f"X-Test-Mode injection: {failure_count} failures by xtest:{request.user}",
            )
            if result.success:
                current_state = "open"
                force_opened = True

        # 스냅샷 수집
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

        # WAL Audit 기록
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
    Circuit Breaker 상태 초기화 API.

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

        # Exception은 exception handler가 처리
        from baldur.services.circuit_breaker import (
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()

        # 이전 상태 기록
        previous_state = cb_service.get_state(service_name)

        # 강제 닫기 — Actor information is read from ActorContext.
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

        # WAL Audit 기록
        self.log_xtest_cleanup(
            request=request,
            component="cb",
            cleaned_count=1,
            cleaned_ids=[service_name],
        )

        return Response(response_data)


class CBStatusDetailView(XTestModeMixin, APIView):
    """
    Circuit Breaker 상세 상태 조회 API.

    GET /api/baldur/xtest/cb-status/?service=database
    """

    def get(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        service_name = request.query_params.get("service")

        # Exception은 exception handler가 처리
        from baldur.services.circuit_breaker import (
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()

        if service_name:
            # 특정 서비스 상태
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
        # 전체 서비스 상태 (repository에서 조회)
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
    Fast Fail 검증 API - CB OPEN 상태에서 응답 시간 측정.

    GET /api/baldur/xtest/fast-fail-test/?service=database
    """

    def get(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        service_name = request.query_params.get("service", "database")

        # Exception은 exception handler가 처리
        from baldur.services.circuit_breaker import (
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()

        # 상태 확인
        current_state = cb_service.get_state(service_name)

        # should_allow 체크 시간 측정
        start_time = time.time()
        allowed = cb_service.should_allow(service_name)
        elapsed_ms = (time.time() - start_time) * 1000

        is_fast_fail = elapsed_ms < 100  # 100ms 미만

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
    CB Recovery 트리거 API - HALF_OPEN 상태에서 성공 기록하여 CLOSED로 복구.

    POST /api/baldur/xtest/trigger-cb-recovery/
    Body: {"service": "database", "success_count": 3, "force": false}

    HALF_OPEN 상태에서 record_success를 호출하여 CB를 CLOSED 상태로 복구시킵니다.
    - force=true: 직접 CLOSED로 전환 (테스트용)
    - force=false: record_success 호출 (정상 흐름)

    Note: DB 모델의 half_open_max_calls 기본값은 3입니다.
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        service_name = request.data.get("service", "database")
        success_count = request.data.get("success_count", 3)
        force_close = request.data.get("force", False)

        # Exception은 exception handler가 처리
        from baldur.services.circuit_breaker import (
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()

        # 현재 상태 확인
        state_before = cb_service.get_state(service_name)

        successes_recorded = 0

        if force_close and state_before in ("half_open", "open"):
            # 강제 CLOSED 전환 (테스트 전용)
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
            # 정상 복구 흐름: record_success 호출
            for _i in range(success_count):
                current_state = cb_service.get_state(service_name)
                if current_state == "half_open":
                    cb_service.record_success(service_name)
                    successes_recorded += 1
                elif current_state == "closed":
                    break
                else:
                    break

        # 최종 상태 확인
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

        # WAL Audit 기록
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
    CB OPEN → HALF_OPEN 전환 시도 API (도메인 프리).

    POST /api/baldur/xtest/try-recovery-transition/
    Body: {"service": "stage15_platinum"}

    **사람이 개입하는 명시적 전환 API**:
    - recovery_timeout이 지났으면 OPEN → HALF_OPEN으로 전환
    - recovery_timeout이 안 지났으면 대기 시간 반환
    - 도메인 프리: payment/order 등 특정 도메인에 종속되지 않음

    이 API는 should_allow()를 명시적으로 호출하여
    CB의 자동 전환 로직을 트리거합니다.
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        service_name = request.data.get("service", "database")

        # Exception은 exception handler가 처리
        from baldur.services.circuit_breaker import (
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()

        # 현재 상태 확인
        state_before = cb_service.get_or_create_state(service_name)
        state_str_before = state_before.state
        opened_at = state_before.opened_at

        # recovery_timeout 계산
        remaining_seconds = None
        recovery_timeout = cb_service.config.recovery_timeout

        if state_str_before == "open" and opened_at:
            elapsed = (timezone.now() - opened_at).total_seconds()
            remaining_seconds = max(0, recovery_timeout - elapsed)

        # should_allow 호출 - 이것이 OPEN → HALF_OPEN 전환을 트리거함
        allowed = cb_service.should_allow(service_name)

        # 전환 후 상태 확인
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

        # WAL Audit 기록
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
    CB를 자동 모드로 전환하는 API (manually_controlled=False 설정).

    POST /api/baldur/xtest/switch-to-auto/
    Body: {"service": "database"}

    force_open 후 manually_controlled=True 상태를 해제하여
    recovery_timeout 후 자동으로 HALF_OPEN으로 전환되도록 함.
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        service_name = request.data.get("service", "database")

        # Exception은 exception handler가 처리
        from baldur.services.circuit_breaker import (
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()

        # 현재 상태 확인
        state_before = cb_service.get_or_create_state(service_name)
        was_manually_controlled = state_before.manually_controlled

        # manually_controlled=False로 설정 (auto mode 전환)
        # clear_manual_control 메서드 사용 (preserve_reason=True로 상태 보존)
        cb_service.repository.clear_manual_control(
            service_name=service_name,
            preserve_reason=True,  # 이유는 보존하되 수동 제어만 해제
        )

        # 최종 상태 확인
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

        # WAL Audit 기록
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
