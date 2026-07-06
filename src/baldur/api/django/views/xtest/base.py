"""
X-Test-Mode Base Module

공통 유틸리티, Mixin, 헬퍼 함수들을 정의합니다.

Security (2중 보안 장치):
1차 - Django RBAC: HasChaosTestPermission 권한 클래스
2차 - XTestModeMixin: X-Test-Mode 헤더 + 환경 변수 검증

Requirements:
- X-Test-Mode: chaos-monkey 헤더 필수
- DEBUG 또는 CHAOS_ENABLED 환경 변수 필요
- production 환경에서는 완전 차단

Regional Scope:
- GLOBAL scope API는 X-Region 헤더 필수
- X-Region 값이 현재 클러스터 리전과 일치해야 허용
- 리전 불일치 시 403 Forbidden 반환
"""

import os
import re
import threading
import uuid
from typing import Any

import psutil
import structlog
from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.authentication import BasicAuthentication, SessionAuthentication
from rest_framework.request import Request
from rest_framework.response import Response

from baldur.api.django.permissions import HasChaosTestPermission
from baldur.audit.helpers import (
    log_xtest_cleanup_audit,
    log_xtest_injection_audit,
    log_xtest_operation_audit,
)
from baldur.core.test_mode_context import TestModeContext

logger = structlog.get_logger()


# =============================================================================
# Global Scope Endpoint Patterns (리전 경계 강제 필요)
# =============================================================================

# GLOBAL scope API: 다른 리전에 영향을 줄 수 있는 엔드포인트
# 이 패턴과 매칭되는 API는 X-Region 헤더 필수 + 현재 리전 일치 검증
GLOBAL_SCOPE_ENDPOINT_PATTERNS: list[str] = [
    r"xtest/emergency/global/.*",  # 전역 Emergency 상태 변경
    r"xtest/isolation/region/.*",  # 리전 격리 조작
    r"xtest/governance/global/.*",  # 전역 거버넌스 설정
]

# 컴파일된 패턴 (성능 최적화)
_COMPILED_GLOBAL_PATTERNS: list[re.Pattern] = [
    re.compile(pattern, re.IGNORECASE) for pattern in GLOBAL_SCOPE_ENDPOINT_PATTERNS
]


# =============================================================================
# X-Test-Mode Security Mixin
# =============================================================================


class XTestModeMixin:
    """
    X-Test-Mode 2중 보안 검증 믹스인.

    Security (2중 보안 장치):
    1차 - Django RBAC: HasChaosTestPermission (인증/그룹 기반)
    2차 - XTestModeMixin: 헤더 + 환경 변수 검증

    Requirements:
    1. Django 인증 + HasChaosTestPermission 권한
    2. X-Test-Mode: chaos-monkey 헤더
    3. DEBUG=True 또는 CHAOS_ENABLED=true
    4. ENVIRONMENT != production
    """

    # 1차 보안: Django RBAC 기반 인증/권한
    authentication_classes = [SessionAuthentication, BasicAuthentication]
    permission_classes = [HasChaosTestPermission]

    # 2차 보안: 헤더 검증용 상수
    CHAOS_HEADER = "X-Test-Mode"
    CHAOS_VALUE = "chaos-monkey"

    def is_chaos_allowed(self, request: Request) -> tuple[bool, str]:
        """
        Chaos 모드 허용 여부 검증.

        Returns:
            (allowed: bool, reason: str)
        """
        # 1. 헤더 확인
        header_value = request.headers.get(self.CHAOS_HEADER, "")
        if header_value != self.CHAOS_VALUE:
            return False, f"Missing or invalid {self.CHAOS_HEADER} header"

        # 2. 프로덕션 차단
        environment = os.getenv("ENVIRONMENT", "development").lower()
        if environment == "production":
            return False, "X-Test-Mode is disabled in production"

        # 3. DEBUG 또는 CHAOS_ENABLED 확인
        debug_mode = getattr(settings, "DEBUG", False)
        chaos_enabled = os.getenv("CHAOS_ENABLED", "false").lower() == "true"

        if not debug_mode and not chaos_enabled:
            return False, "Chaos mode requires DEBUG=True or CHAOS_ENABLED=true"

        return True, "Chaos mode allowed"

    def get_current_region(self) -> str | None:
        """
        현재 클러스터의 리전 조회.

        환경변수 BALDUR_NAMESPACE_REGION 또는 ClusterIdentity에서 리전 정보를 가져옵니다.

        Returns:
            리전 식별자 (예: 'seoul', 'tokyo') 또는 None
        """
        # 1. 환경변수에서 직접 조회 (가장 빠름)
        region = os.getenv("BALDUR_NAMESPACE_REGION")
        if region:
            return region

        # 2. ClusterIdentity에서 조회
        try:
            from baldur.core.cluster_identity import get_cluster_identity

            identity = get_cluster_identity()
            return identity.region
        except Exception as e:
            logger.warning(
                "test.mode_failed_get",
                error=e,
            )
            return None

    def is_global_scope_endpoint(self, request: Request) -> bool:
        """
        현재 요청이 GLOBAL scope API인지 판정.

        GLOBAL scope API는 다른 리전에 영향을 줄 수 있는 엔드포인트입니다:
        - xtest/emergency/global/* : 전역 Emergency 상태 변경
        - xtest/isolation/region/* : 리전 격리 조작
        - xtest/governance/global/* : 전역 거버넌스 설정

        Args:
            request: HTTP 요청 객체

        Returns:
            GLOBAL scope이면 True, LOCAL scope이면 False
        """
        path = request.path.lstrip("/")

        return any(pattern.search(path) for pattern in _COMPILED_GLOBAL_PATTERNS)

    def _get_endpoint_pattern_name(self, request: Request) -> str:
        """
        GLOBAL scope 엔드포인트 패턴 이름 추출.

        Args:
            request: HTTP 요청 객체

        Returns:
            패턴 이름 (e.g., 'emergency', 'isolation', 'governance')
        """
        path = request.path.lower()
        if "emergency" in path:
            return "emergency"
        if "isolation" in path:
            return "isolation"
        if "governance" in path:
            return "governance"
        return "unknown"

    def _record_regional_scope_metrics(
        self,
        request: Request,
        current_region: str | None,
        target_region: str | None,
        result: str,
    ) -> None:
        """
        리전 스코프 관련 메트릭 기록.

        Args:
            request: HTTP 요청 객체
            current_region: 현재 클러스터 리전
            target_region: 요청된 타겟 리전
            result: 결과 ('allowed', 'denied_no_header', 'denied_mismatch', 'denied_no_region')
        """
        try:
            from baldur.services.metrics.recorders import (
                record_xtest_cross_region_denied,
                record_xtest_global_scope_request,
            )

            pattern_name = self._get_endpoint_pattern_name(request)
            region = current_region or "unknown"

            # GLOBAL scope 요청 메트릭 기록
            record_xtest_global_scope_request(
                endpoint_pattern=pattern_name,
                region=region,
                result=result,
            )

            # cross-region 거부 시 추가 메트릭
            if result == "denied_mismatch" and current_region and target_region:
                record_xtest_cross_region_denied(
                    current_region=current_region,
                    target_region=target_region,
                )

        except Exception as e:
            logger.warning(
                "test.mode_failed_record",
                error=e,
            )

    def check_regional_scope(self, request: Request) -> tuple[bool, Response | None]:
        """
        GLOBAL scope API에 대한 리전 경계 검증.

        GLOBAL scope API 호출 시:
        1. X-Region 헤더 존재 확인
        2. 헤더 값과 현재 클러스터 리전 일치 확인
        3. 불일치 시 403 Forbidden 반환

        Args:
            request: HTTP 요청 객체

        Returns:
            (is_allowed, response): 허용 여부와 거부 시 Response
        """
        # LOCAL scope API는 리전 체크 불필요
        if not self.is_global_scope_endpoint(request):
            return True, None

        # 현재 클러스터 리전 조회
        current_region = self.get_current_region()

        # 리전 미설정 환경에서는 GLOBAL scope 차단
        if not current_region:
            environment = os.getenv("ENVIRONMENT", "development").lower()
            if environment == "development":
                # 개발 환경에서는 경고만 출력
                logger.warning("testmode.development_flag_set")
                return True, None

            logger.warning("test_mode.global_flag_warning")
            return False, Response(
                {
                    "status": "error",
                    "error": "region_not_configured",
                    "message": "BALDUR_NAMESPACE_REGION not configured. GLOBAL scope API denied.",
                    "hint": "Set BALDUR_NAMESPACE_REGION environment variable",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # X-Region 헤더 확인
        target_region = request.headers.get("X-Region")

        if not target_region:
            logger.warning(
                "testmode.region_header_missing",
                current_region=current_region,
                request_path=request.path,
            )
            self._record_regional_scope_metrics(
                request, current_region, None, "denied_no_header"
            )
            return False, Response(
                {
                    "status": "error",
                    "error": "missing_region_header",
                    "message": "X-Region header required for GLOBAL scope API",
                    "current_region": current_region,
                    "hint": f"Add header 'X-Region: {current_region}'",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # 리전 일치 확인
        if target_region.lower() != current_region.lower():
            logger.warning(
                "testmode.cross_region_denied",
                current_region=current_region,
                target_region=target_region,
                request_path=request.path,
            )
            self._record_regional_scope_metrics(
                request, current_region, target_region, "denied_mismatch"
            )
            return False, Response(
                {
                    "status": "error",
                    "error": "cross_region_xtest_denied",
                    "message": (
                        f"Cross-region X-Test operation denied. "
                        f"Target region '{target_region}' does not match "
                        f"current cluster region '{current_region}'."
                    ),
                    "current_region": current_region,
                    "target_region": target_region,
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        self._record_regional_scope_metrics(
            request, current_region, target_region, "allowed"
        )
        logger.debug(
            "testmode.regional_scope_validated",
            current_region=current_region,
            request_path=request.path,
        )
        return True, None

    def check_resource_constraints(self, request: Request) -> Response | None:
        """
        시스템 리소스 제약 체크.

        CPU 80% 초과 또는 메모리 85% 초과 시 429 응답 반환.
        시스템 과부하 상태에서 X-Test가 추가 부담을 주는 것을 방지.

        Returns:
            None if allowed, 429 Response if resource overloaded
        """
        try:
            from baldur_pro.services.chaos.safety_guard import (
                get_resource_guard,
            )

            guard = get_resource_guard()
            result = guard.is_safe_for_chaos()

            if not result.is_safe:
                logger.warning(
                    "test.mode_resource_constraint",
                    block_reason=result.block_reason,
                    request_user=request.user,
                )

                response = Response(
                    {
                        "status": "error",
                        **result.to_response_dict(),
                    },
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )
                response["Retry-After"] = str(guard.get_recommended_wait())
                return response

            logger.debug(
                "test.mode_resource_check",
                cpu_percent=result.cpu_percent,
                memory_percent=result.memory_percent,
            )
            return None

        except ImportError:
            logger.debug("test_mode.resource_guard_unavailable")
            return None
        except Exception as e:
            logger.warning(
                "test.mode_resource_check",
                error=e,
            )
            # 체크 실패 시 보수적으로 허용 (가용성 우선)
            return None

    def check_chaos_permission(self, request: Request) -> Response | None:
        """
        Chaos 권한 체크. 실패시 Response 반환.

        검증 순서:
        1. 리소스 제약 체크 (CPU/메모리 과부하)
        2. Chaos 모드 허용 여부 (헤더, 환경변수)
        3. GLOBAL scope API인 경우 리전 경계 검증

        Returns:
            None if allowed, Response if denied
        """
        # 1. 리소스 제약 체크 (CPU/메모리)
        resource_response = self.check_resource_constraints(request)
        if resource_response is not None:
            return resource_response

        # 2. Chaos 모드 기본 검증
        allowed, reason = self.is_chaos_allowed(request)
        if not allowed:
            logger.warning(
                "test.mode_denied_user",
                reason=reason,
                request_user=request.user,
            )
            return Response(
                {
                    "status": "error",
                    "error": "chaos_mode_disabled",
                    "message": reason,
                    "hint": f"Add header '{self.CHAOS_HEADER}: {self.CHAOS_VALUE}' and ensure CHAOS_ENABLED=true",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # 3. GLOBAL scope API 리전 경계 검증
        region_allowed, region_response = self.check_regional_scope(request)
        if not region_allowed:
            return region_response

        return None

    def get_xtest_session_id(self, request: Request) -> str:
        """X-Test 세션 ID 추출. 헤더가 없으면 자동 생성."""
        header = request.headers.get("X-Test-Session")
        return str(header) if header else str(uuid.uuid4())[:8]

    def ensure_xtest_session(self, request: Request) -> str:
        """
        X-Test 세션 생성 또는 갱신.

        세션이 없으면 새로 생성하고, 있으면 기존 세션을 반환합니다.
        세션 메타데이터는 Redis에 저장되어 자동 정리 시 사용됩니다.

        Args:
            request: HTTP 요청 객체

        Returns:
            세션 ID
        """
        session_id = self.get_xtest_session_id(request)
        user = self.get_xtest_user(request)

        try:
            from baldur.services.xtest_session_manager import (
                get_xtest_session_manager,
            )

            session_manager = get_xtest_session_manager()

            # 기존 세션 확인
            existing = session_manager.get_session(session_id)
            if not existing:
                # 새 세션 생성
                session_manager.create_session(session_id=session_id, user=user)
                logger.debug(
                    "test.mode_created_new",
                    session_id=session_id,
                )

        except ImportError:
            logger.debug("test_mode.session_manager_unavailable")
        except Exception as e:
            logger.warning(
                "test.mode_failed_ensure",
                error=e,
            )

        return session_id

    def register_xtest_artifact(
        self,
        request: Request,
        artifact_id: str,
        component: str,
    ) -> bool:
        """
        X-Test 아티팩트를 세션에 등록.

        테스트 중 생성된 DLQ 항목, CB 상태 변경 등을 세션에 등록하여
        세션 만료 시 자동으로 정리될 수 있도록 합니다.

        Args:
            request: HTTP 요청 객체
            artifact_id: 아티팩트 ID (DLQ entry ID, CB service name 등)
            component: 컴포넌트 이름 (dlq, cb, idempotency 등)

        Returns:
            등록 성공 여부
        """
        session_id = self.get_xtest_session_id(request)

        try:
            from baldur.services.xtest_session_manager import (
                get_xtest_session_manager,
            )

            session_manager = get_xtest_session_manager()

            success = session_manager.register_artifact(
                session_id=session_id,
                artifact_id=artifact_id,
                component=component,
            )

            if success:
                logger.debug(
                    "cell_registry.bulkheads_registered",
                    session_id=session_id,
                    component=component,
                    artifact_id=artifact_id,
                )
            return success

        except ImportError:
            logger.debug("test_mode.session_manager_unavailable")
            return False
        except Exception as e:
            logger.warning(
                "test.mode_failed_register",
                error=e,
            )
            return False

    def enter_synthetic_context(self, request: Request) -> None:
        """
        합성 요청 컨텍스트 진입.

        X-Test 요청 처리 시작 시 호출하여 TestModeContext를 활성화합니다.
        이후 모든 메트릭과 Redis 키가 합성 요청으로 태깅됩니다.
        세션이 없으면 자동으로 생성합니다.

        Args:
            request: HTTP 요청 객체
        """
        session_id = self.ensure_xtest_session(request)
        TestModeContext.enter_synthetic_mode(session_id=session_id)
        logger.debug(
            "test_mode.synthetic_context_unavailable",
            session_id=session_id,
        )

    def exit_synthetic_context(self) -> None:
        """
        합성 요청 컨텍스트 종료.

        X-Test 요청 처리 완료 시 호출하여 TestModeContext를 비활성화합니다.
        """
        TestModeContext.exit_synthetic_mode()
        logger.debug("test_mode.synthetic_context_unavailable")

    def get_xtest_user(self, request: Request) -> str:
        """X-Test 사용자 추출."""
        if hasattr(request, "user") and request.user.is_authenticated:
            return str(request.user)
        return "anonymous"

    def log_xtest_audit(
        self,
        request: Request,
        action: str,
        component: str,
        details: dict[str, Any],
        result: str = "success",
        error_message: str | None = None,
    ) -> int | None:
        """
        X-Test 작업을 WAL Audit 로그에 기록.

        Args:
            request: HTTP 요청 객체
            action: 수행 작업 (inject, force_status, reset, query 등)
            component: 대상 컴포넌트 (dlq, cb, idempotency 등)
            details: 응답 데이터 또는 작업 상세
            result: 결과 상태 (success, failed, error)
            error_message: 실패 시 에러 메시지

        Returns:
            WAL 시퀀스 번호
        """
        session_id = self.get_xtest_session_id(request)
        user = self.get_xtest_user(request)
        trace_id = request.headers.get("X-Trace-ID")

        return log_xtest_operation_audit(
            session_id=session_id,
            action=action,
            component=component,
            details=details,
            result=result,
            user=user,
            trace_id=trace_id,
            error_message=error_message,
        )

    def log_xtest_injection(
        self,
        request: Request,
        component: str,
        injection_type: str,
        count: int,
        target_ids: list,
    ) -> int | None:
        """
        X-Test 데이터 주입을 WAL Audit 로그에 기록.

        Args:
            request: HTTP 요청 객체
            component: 대상 컴포넌트
            injection_type: 주입 유형 (create, override 등)
            count: 주입된 항목 수
            target_ids: 생성된 ID 목록
        """
        session_id = self.get_xtest_session_id(request)
        user = self.get_xtest_user(request)

        return log_xtest_injection_audit(
            session_id=session_id,
            component=component,
            injection_type=injection_type,
            count=count,
            target_ids=target_ids,
            user=user,
        )

    def log_xtest_cleanup(
        self,
        request: Request,
        component: str,
        cleaned_count: int,
        cleaned_ids: list,
    ) -> int | None:
        """
        X-Test 정리(Reset)를 WAL Audit 로그에 기록.

        Args:
            request: HTTP 요청 객체
            component: 대상 컴포넌트
            cleaned_count: 정리된 항목 수
            cleaned_ids: 정리된 ID 목록
        """
        session_id = self.get_xtest_session_id(request)
        user = self.get_xtest_user(request)

        return log_xtest_cleanup_audit(
            session_id=session_id,
            component=component,
            cleaned_count=cleaned_count,
            cleaned_ids=cleaned_ids,
            user=user,
        )


# =============================================================================
# System Snapshot Utility
# =============================================================================


def collect_system_snapshot() -> dict[str, Any]:  # noqa: C901, PLR0912
    """시스템 스냅샷 수집 (CPU, Memory, Connections, Error/Request Rate).

    Postmortem 타임라인 스냅샷에 포함될 시스템 상태를 수집합니다.

    Returns:
        시스템 스냅샷 딕셔너리:
        - timestamp: 캡처 시각
        - cpu_percent: CPU 사용률
        - memory_percent: 메모리 사용률
        - memory_used_mb: 사용 메모리 (MB)
        - memory_available_mb: 가용 메모리 (MB)
        - db_active_connections: DB 활성 연결 수
        - error_rate: 에러율 (있는 경우)
        - request_rate: 요청률 (있는 경우)
    """
    try:
        # 캐시에서 CPU/Memory 조회 (~0ms), 캐시 미가동 시 직접 측정으로 fallback (100ms)
        try:
            from baldur.services.system_metrics_cache import (
                get_system_metrics_cache,
            )

            cache = get_system_metrics_cache()
            if cache.is_running():
                metrics = cache.get_metrics()
                snapshot = {
                    "timestamp": timezone.now().isoformat(),
                    "cpu_percent": metrics.cpu_percent,
                    "memory_percent": metrics.memory_percent,
                    "memory_used_mb": metrics.memory_used_mb,
                    "memory_available_mb": metrics.memory_available_mb,
                    "metrics_source": metrics.source,
                }
            else:
                raise RuntimeError("Cache not running")
        except Exception:
            # Fallback: 직접 측정 (기존 동작 유지)
            cpu_percent = psutil.cpu_percent(interval=0.1)
            memory = psutil.virtual_memory()
            snapshot = {
                "timestamp": timezone.now().isoformat(),
                "cpu_percent": round(cpu_percent, 1),
                "memory_percent": round(memory.percent, 1),
                "memory_used_mb": round(memory.used / (1024 * 1024), 1),
                "memory_available_mb": round(memory.available / (1024 * 1024), 1),
                "metrics_source": "direct",
            }

        # DB active connection count via the pg_admin registry surface.
        try:
            from baldur.factory import ProviderRegistry

            pg_admin = ProviderRegistry.pg_admin.get()
            if pg_admin.is_available():
                snapshot["db_active_connections"] = (
                    pg_admin.get_active_connection_count()
                )
            else:
                snapshot["db_active_connections"] = None
        except Exception:
            snapshot["db_active_connections"] = None

        # Error Budget에서 에러율 조회
        try:
            from baldur_pro.services.error_budget import (
                get_error_budget_service,
            )

            error_budget_service = get_error_budget_service()
            budget_status = error_budget_service.get_budget_status()
            if budget_status is not None:
                snapshot["error_rate"] = float(budget_status.burn_rate_1h)
                snapshot["remaining_budget_percent"] = float(
                    budget_status.budget_remaining_percent
                )
        except Exception:
            snapshot["error_rate"] = None

        # 메트릭 어댑터에서 요청률 조회
        try:
            from baldur.adapters.metrics import get_metric_adapter

            adapter = get_metric_adapter()
            # MetricSourceAdapter에서 요청 카운터 조회 시도
            if hasattr(adapter, "get_counter_value"):
                request_counter = adapter.get_counter_value(
                    "baldur_http_requests_total"
                )
                if request_counter is not None:
                    snapshot["request_rate"] = request_counter
            else:
                snapshot["request_rate"] = None
        except Exception:
            snapshot["request_rate"] = None

        return snapshot
    except Exception as e:
        logger.warning(
            "test.mode_snapshot_collection",
            error=e,
        )
        return {"timestamp": timezone.now().isoformat(), "error": str(e)}


# =============================================================================
# In-Memory Event Storage + Redis Persistence
# =============================================================================

_healing_events_lock = threading.Lock()
_healing_events: list[dict[str, Any]] = []
_max_events = 500


def add_healing_event(event: dict[str, Any]) -> None:
    """
    힐링 이벤트 기록.

    Redis에 저장하여 다중 워커 간 동기화를 지원합니다.
    Redis 실패 시 In-Memory에만 저장됩니다.
    """
    global _healing_events

    # Redis 저장 시도
    try:
        from baldur.services.healing_events_store import add_healing_event_redis

        add_healing_event_redis(event)
    except ImportError:
        pass
    except Exception as e:
        logger.debug(
            "test.mode_redis_event",
            error=e,
        )

    # In-Memory에도 저장 (빠른 조회용 캐시)
    with _healing_events_lock:
        if "recorded_at" not in event:
            event["recorded_at"] = timezone.now().isoformat()
        _healing_events.append(event)
        if len(_healing_events) > _max_events:
            _healing_events = _healing_events[-_max_events:]


def get_healing_events(limit: int = 50, use_redis: bool = True) -> list[dict[str, Any]]:
    """
    힐링 이벤트 조회.

    Redis에서 조회를 시도하고, 실패 시 In-Memory에서 조회합니다.

    Args:
        limit: 반환할 최대 이벤트 수
        use_redis: Redis 조회 사용 여부

    Returns:
        이벤트 딕셔너리 리스트 (최신순)
    """
    if use_redis:
        try:
            from baldur.services.healing_events_store import (
                get_healing_events_redis,
            )

            return get_healing_events_redis(limit=limit, days_back=1)
        except ImportError:
            pass
        except Exception as e:
            logger.debug(
                "test.mode_redis_event",
                error=e,
            )

    # In-Memory fallback
    with _healing_events_lock:
        return list(_healing_events[-limit:])


def get_healing_events_count(use_redis: bool = True) -> int:
    """
    힐링 이벤트 총 개수.

    Args:
        use_redis: Redis 조회 사용 여부

    Returns:
        이벤트 총 개수
    """
    if use_redis:
        try:
            from baldur.services.healing_events_store import (
                get_healing_events_count_redis,
            )

            return get_healing_events_count_redis(days_back=1)
        except ImportError:
            pass
        except Exception as e:
            logger.debug(
                "test.mode_redis_event",
                error=e,
            )

    # In-Memory fallback
    with _healing_events_lock:
        return len(_healing_events)


def clear_healing_events() -> int:
    """
    힐링 이벤트 초기화 (테스트용).

    Returns:
        초기화된 이벤트 개수
    """
    global _healing_events

    # Redis 초기화 시도
    try:
        from baldur.services.healing_events_store import clear_healing_events_redis

        clear_healing_events_redis()
    except ImportError:
        pass
    except Exception as e:
        logger.debug(
            "test.mode_redis_event",
            error=e,
        )

    # In-Memory 초기화
    with _healing_events_lock:
        count = len(_healing_events)
        _healing_events = []
        return count
