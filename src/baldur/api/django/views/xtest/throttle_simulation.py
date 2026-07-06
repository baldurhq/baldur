"""
X-Test-Mode Throttle Simulation Views.

Adaptive Throttle의 Emergency/CB 연동 동작을 X-Test 환경에서 시뮬레이션하는 API.

Endpoints:
- POST /api/baldur/xtest/throttle/simulate-emergency/ - Emergency 레벨 상승 시뮬레이션
- POST /api/baldur/xtest/throttle/simulate-cb-open/ - CB OPEN 시뮬레이션
- POST /api/baldur/xtest/throttle/inject-rtt-delay/ - RTT 지연 주입
- GET  /api/baldur/xtest/throttle/status/ - Throttle 상태 조회
- POST /api/baldur/xtest/throttle/reset/ - Throttle 상태 초기화

Security:
- X-Test-Mode: chaos-monkey 헤더 필수
- DEBUG 또는 CHAOS_ENABLED 환경 변수 필요
- production 환경에서는 완전 차단
"""

import time
from typing import Any, cast

import structlog
from django.utils import timezone
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .base import XTestModeMixin

logger = structlog.get_logger()


# Debug-only views inspect AdaptiveThrottle internals (current_limit,
# _gradient_calculator, _base_limit_before_emergency, etc.) that the OSS
# Protocol intentionally hides. cast(Any, ...) at the boundary keeps the
# debug surface working without leaking PRO impl knowledge into the contract.
def _as_any(throttle: object) -> Any:
    return cast(Any, throttle)


class ThrottleEmergencySimulationView(XTestModeMixin, APIView):
    """
    Emergency 레벨 상승 시뮬레이션 API.

    POST /api/baldur/xtest/throttle/simulate-emergency/

    Emergency Level에 따른 Throttle limit 조정을 시뮬레이션합니다.
    실제 Emergency Manager 상태와는 독립적으로 Throttle만 조정합니다.

    Request Body:
        {
            "level": 2,           // Emergency Level (0=NORMAL, 1-3)
            "service": "default"  // 서비스 이름 (선택, 기본: default)
        }

    Response:
        {
            "status": "success",
            "simulation": "emergency_level_change",
            "level": 2,
            "previous_limit": 100,
            "new_limit": 50,
            "multiplier": 0.5,
            "gradient_frozen": false,
            "timestamp": "2026-01-29T12:00:00Z"
        }
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        level = request.data.get("level", 0)
        service = request.data.get("service", "default")

        # 레벨 유효성 검증
        if not isinstance(level, int) or level < 0 or level > 3:
            return Response(
                {
                    "status": "error",
                    "error": "invalid_level",
                    "message": "level must be integer 0-3 (0=NORMAL, 1=LEVEL_1, 2=LEVEL_2, 3=LEVEL_3)",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Exception은 DRF exception handler가 처리
        # settings.py: EXCEPTION_HANDLER = 'baldur.api.django.exceptions.handler.baldur_exception_handler'
        try:
            from baldur.factory.registry import ProviderRegistry

            throttle = ProviderRegistry.adaptive_throttle.safe_get()
        except ImportError:
            throttle = None

        if throttle is None:
            raise RuntimeError("baldur_pro AdaptiveThrottle not registered")
        throttle = _as_any(throttle)
        previous_limit = throttle.current_limit
        previous_level = throttle.get_emergency_level()

        # Emergency 레벨 조정 시뮬레이션
        throttle.adjust_for_emergency(level)

        new_limit = throttle.current_limit
        gradient_frozen = throttle.is_gradient_frozen()

        # 배율 계산
        multiplier_map = {0: 1.0, 1: 0.8, 2: 0.5, 3: 0.0}
        multiplier = multiplier_map.get(level, 1.0)

        logger.info(
            "test.mode_throttle_emergency",
            previous_level=previous_level,
            level=level,
            previous_limit=previous_limit,
            new_limit=new_limit,
        )

        # 감사 로그 기록
        self.log_xtest_audit(
            request=request,
            action="simulate_emergency",
            component="throttle",
            details={
                "level": level,
                "previous_level": previous_level,
                "previous_limit": previous_limit,
                "new_limit": new_limit,
                "service": service,
            },
            result="success",
        )

        return Response(
            {
                "status": "success",
                "simulation": "emergency_level_change",
                "level": level,
                "previous_level": previous_level,
                "previous_limit": previous_limit,
                "new_limit": new_limit,
                "multiplier": multiplier,
                "gradient_frozen": gradient_frozen,
                "full_stop_active": throttle.is_full_stop_active(),
                "recovery_dampening": throttle.get_recovery_dampening_progress(),
                "timestamp": timezone.now().isoformat(),
            },
            status=status.HTTP_200_OK,
        )


class ThrottleCBOpenSimulationView(XTestModeMixin, APIView):
    """
    Circuit Breaker OPEN 시뮬레이션 API.

    POST /api/baldur/xtest/throttle/simulate-cb-open/

    CB OPEN 상태에 따른 Throttle limit 조정을 시뮬레이션합니다.
    실제 Circuit Breaker 상태와는 독립적으로 Throttle만 조정합니다.

    Request Body:
        {
            "service": "payment-api",  // CB 서비스 이름
            "state": "open"            // CB 상태: open, half_open, closed
        }

    Response:
        {
            "status": "success",
            "simulation": "cb_state_change",
            "service": "payment-api",
            "cb_state": "open",
            "previous_limit": 100,
            "new_limit": 10,
            "timestamp": "2026-01-29T12:00:00Z"
        }
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        service = request.data.get("service", "default")
        cb_state = request.data.get("state", "open").lower()

        # 상태 유효성 검증
        valid_states = ["open", "half_open", "closed"]
        if cb_state not in valid_states:
            return Response(
                {
                    "status": "error",
                    "error": "invalid_state",
                    "message": f"state must be one of: {valid_states}",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Exception은 DRF exception handler가 처리
        from baldur.settings import get_throttle_settings

        try:
            from baldur.factory.registry import ProviderRegistry

            throttle = ProviderRegistry.adaptive_throttle.safe_get()
        except ImportError:
            throttle = None

        if throttle is None:
            raise RuntimeError("baldur_pro AdaptiveThrottle not registered")
        throttle = _as_any(throttle)
        settings = get_throttle_settings()

        previous_limit = throttle.current_limit
        base_limit = throttle._base_limit_before_emergency

        # CB 상태에 따른 limit 계산
        if cb_state == "open":
            # CB OPEN: cb_open_limit_percent (기본 0 = min_limit)
            percent = settings.cb_open_limit_percent
            if percent == 0.0:
                new_limit = settings.min_limit
            else:
                new_limit = int(base_limit * percent)
        elif cb_state == "half_open":
            # CB HALF_OPEN: cb_half_open_limit_percent (기본 50%)
            percent = settings.cb_half_open_limit_percent
            new_limit = int(base_limit * percent)
        else:
            # CB CLOSED: 정상 limit 복구
            new_limit = base_limit

        # limit 적용
        throttle.current_limit = max(new_limit, settings.min_limit)
        actual_new_limit = throttle.current_limit

        logger.info(
            "test.mode_throttle_cb",
            service=service,
            cb_state=cb_state,
            previous_limit=previous_limit,
            actual_new_limit=actual_new_limit,
        )

        # 감사 로그 기록
        self.log_xtest_audit(
            request=request,
            action="simulate_cb_open",
            component="throttle",
            details={
                "service": service,
                "cb_state": cb_state,
                "previous_limit": previous_limit,
                "new_limit": actual_new_limit,
            },
            result="success",
        )

        return Response(
            {
                "status": "success",
                "simulation": "cb_state_change",
                "service": service,
                "cb_state": cb_state,
                "previous_limit": previous_limit,
                "new_limit": actual_new_limit,
                "base_limit": base_limit,
                "timestamp": timezone.now().isoformat(),
            },
            status=status.HTTP_200_OK,
        )


class ThrottleRTTDelayInjectionView(XTestModeMixin, APIView):
    """
    RTT 지연 주입 API.

    POST /api/baldur/xtest/throttle/inject-rtt-delay/

    RTT 샘플을 주입하여 Gradient 알고리즘의 동작을 테스트합니다.

    Request Body:
        {
            "rtt_ms": 300,       // 주입할 RTT 값 (ms)
            "count": 5,          // 주입 횟수 (기본: 1)
            "interval_ms": 100   // 주입 간격 (ms, 기본: 0)
        }

    Response:
        {
            "status": "success",
            "simulation": "rtt_delay_injection",
            "rtt_ms": 300,
            "samples_injected": 5,
            "previous_limit": 100,
            "new_limit": 70,
            "gradient": 0.15,
            "sla_status": "warning",
            "timestamp": "2026-01-29T12:00:00Z"
        }
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        rtt_ms = request.data.get("rtt_ms", 100)
        count = request.data.get("count", 1)
        interval_ms = request.data.get("interval_ms", 0)

        # 유효성 검증
        if not isinstance(rtt_ms, (int, float)) or rtt_ms <= 0:
            return Response(
                {
                    "status": "error",
                    "error": "invalid_rtt",
                    "message": "rtt_ms must be a positive number",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not isinstance(count, int) or count < 1 or count > 100:
            return Response(
                {
                    "status": "error",
                    "error": "invalid_count",
                    "message": "count must be integer 1-100",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Exception은 DRF exception handler가 처리
        from baldur.settings import get_throttle_settings

        try:
            from baldur.factory.registry import ProviderRegistry

            throttle = ProviderRegistry.adaptive_throttle.safe_get()
        except ImportError:
            throttle = None

        if throttle is None:
            raise RuntimeError("baldur_pro AdaptiveThrottle not registered")
        throttle = _as_any(throttle)
        settings = get_throttle_settings()

        previous_limit = throttle.current_limit
        previous_gradient = throttle._gradient_calculator.get_gradient()

        # RTT 샘플 주입
        interval_seconds = interval_ms / 1000.0
        for i in range(count):
            throttle.record_response(float(rtt_ms))
            if interval_seconds > 0 and i < count - 1:
                time.sleep(interval_seconds)

        new_limit = throttle.current_limit
        new_gradient = throttle._gradient_calculator.get_gradient()
        current_rtt = throttle._gradient_calculator.get_current_rtt()

        # SLA 상태 판단
        if rtt_ms >= settings.sla_critical_ms:
            sla_status = "critical"
        elif rtt_ms >= settings.sla_warning_ms:
            sla_status = "warning"
        else:
            sla_status = "normal"

        logger.info(
            "test.mode_throttle_rtt",
            rtt_ms=rtt_ms,
            count=count,
            previous_limit=previous_limit,
            new_limit=new_limit,
            new_gradient=new_gradient,
        )

        # 감사 로그 기록
        self.log_xtest_audit(
            request=request,
            action="inject_rtt_delay",
            component="throttle",
            details={
                "rtt_ms": rtt_ms,
                "count": count,
                "previous_limit": previous_limit,
                "new_limit": new_limit,
                "gradient": new_gradient,
            },
            result="success",
        )

        return Response(
            {
                "status": "success",
                "simulation": "rtt_delay_injection",
                "rtt_ms": rtt_ms,
                "samples_injected": count,
                "previous_limit": previous_limit,
                "new_limit": new_limit,
                "previous_gradient": previous_gradient,
                "new_gradient": new_gradient,
                "current_rtt": current_rtt,
                "sla_status": sla_status,
                "sla_thresholds": {
                    "warning_ms": settings.sla_warning_ms,
                    "critical_ms": settings.sla_critical_ms,
                },
                "timestamp": timezone.now().isoformat(),
            },
            status=status.HTTP_200_OK,
        )


class ThrottleStatusView(XTestModeMixin, APIView):
    """
    Throttle 전체 상태 조회 API.

    GET /api/baldur/xtest/throttle/status/

    Response:
        {
            "status": "success",
            "throttle": {
                "current_limit": 80,
                "min_limit": 10,
                "max_limit": 500,
                "gradient": 0.05,
                "current_rtt_ms": 150,
                "emergency": {...},
                "recovery": {...},
                "stats": {...}
            },
            "timestamp": "2026-01-29T12:00:00Z"
        }
    """

    def get(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        # Exception은 DRF exception handler가 처리
        from baldur.settings import get_throttle_settings

        try:
            from baldur.factory.registry import ProviderRegistry

            throttle = ProviderRegistry.adaptive_throttle.safe_get()
        except ImportError:
            throttle = None

        if throttle is None:
            raise RuntimeError("baldur_pro AdaptiveThrottle not registered")
        throttle = _as_any(throttle)
        settings = get_throttle_settings()
        stats = throttle.get_stats()

        logger.info("test_mode.throttle_status")

        self.log_xtest_audit(
            request=request,
            action="query_status",
            component="throttle",
            details={"current_limit": throttle.current_limit},
            result="success",
        )

        return Response(
            {
                "status": "success",
                "throttle": {
                    "current_limit": throttle.current_limit,
                    "min_limit": settings.min_limit,
                    "max_limit": settings.max_limit,
                    "initial_limit": settings.initial_limit,
                    "gradient": throttle._gradient_calculator.get_gradient(),
                    "current_rtt_ms": throttle._gradient_calculator.get_current_rtt(),
                    "emergency": stats.get("emergency", {}),
                    "recovery": stats.get("recovery", {}),
                    "adaptive": stats.get("adaptive", {}),
                    "gradient_stats": stats.get("gradient", {}),
                },
                "settings": {
                    "sla_warning_ms": settings.sla_warning_ms,
                    "sla_critical_ms": settings.sla_critical_ms,
                    "recovery_dampening_enabled": settings.recovery_dampening_enabled,
                },
                "timestamp": timezone.now().isoformat(),
            },
            status=status.HTTP_200_OK,
        )


class ThrottleResetView(XTestModeMixin, APIView):
    """
    Throttle 상태 초기화 API.

    POST /api/baldur/xtest/throttle/reset/

    Throttle 상태를 초기 상태로 리셋합니다.
    Emergency, Recovery Dampening, Gradient 계산 등 모든 상태가 초기화됩니다.

    Response:
        {
            "status": "success",
            "action": "throttle_reset",
            "previous_limit": 50,
            "new_limit": 100,
            "timestamp": "2026-01-29T12:00:00Z"
        }
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        # Exception은 DRF exception handler가 처리
        try:
            from baldur_pro.services.throttle.adaptive import (
                get_adaptive_throttle,
                reset_adaptive_throttle,
            )
        except ImportError:
            get_adaptive_throttle = None  # type: ignore[assignment,misc]
            reset_adaptive_throttle = None  # type: ignore[assignment,misc]

        # 현재 상태 저장
        throttle = get_adaptive_throttle()
        previous_limit = throttle.current_limit
        previous_level = throttle.get_emergency_level()

        # 리셋
        reset_adaptive_throttle()

        # 새로운 인스턴스 가져오기
        new_throttle = get_adaptive_throttle()
        new_limit = new_throttle.current_limit

        logger.info(
            "test.mode_throttle_reset",
            previous_limit=previous_limit,
            new_limit=new_limit,
            previous_level=previous_level,
        )

        self.log_xtest_audit(
            request=request,
            action="reset",
            component="throttle",
            details={
                "previous_limit": previous_limit,
                "new_limit": new_limit,
                "previous_level": previous_level,
            },
            result="success",
        )

        return Response(
            {
                "status": "success",
                "action": "throttle_reset",
                "previous_limit": previous_limit,
                "new_limit": new_limit,
                "previous_level": previous_level,
                "timestamp": timezone.now().isoformat(),
            },
            status=status.HTTP_200_OK,
        )


__all__ = [
    "ThrottleEmergencySimulationView",
    "ThrottleCBOpenSimulationView",
    "ThrottleRTTDelayInjectionView",
    "ThrottleStatusView",
    "ThrottleResetView",
]
