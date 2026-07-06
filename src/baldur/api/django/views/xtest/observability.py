"""
X-Test-Mode Observability & Blast Radius Views

테스트용 Observability 관련 API:
- HealingTimelineView: 힐링 타임라인 조회
- BlastRadiusTestView: 단일 서비스 Blast Radius 격리 테스트
- MultiServiceBlastRadiusView: 다중 서비스 격리 매트릭스 테스트
- RecordHealingEventView: 힐링 이벤트 기록

Production Post-mortem API는 views/postmortem.py를 참조하세요:
- POST /postmortem/generate/ - Post-mortem 생성
- GET /postmortem/incidents/ - 인시던트 목록 조회
"""

import structlog
from django.utils import timezone
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .base import (
    XTestModeMixin,
    add_healing_event,
    collect_system_snapshot,
    get_healing_events,
    get_healing_events_count,
)

logger = structlog.get_logger()


class HealingTimelineView(XTestModeMixin, APIView):
    """
    Stage 51: Baldur 타임라인 조회 API.

    GET /api/baldur/xtest/healing-timeline/?service=database&limit=50

    장애 감지, CB 상태 변경, 복구 등의 이벤트 타임라인을 조회합니다.
    """

    @staticmethod
    def _get_timeline_default_limit() -> int:
        """Settings에서 timeline_default_limit 조회."""
        try:
            from baldur.settings.api_view import get_api_view_settings

            return get_api_view_settings().xtest_timeline_default_limit
        except Exception:
            return 50  # 기본값

    def get(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        service_filter = request.query_params.get("service")
        default_limit = self._get_timeline_default_limit()
        limit = int(request.query_params.get("limit", default_limit))

        # 이벤트 버스에서 히스토리 조회
        from baldur.services.event_bus import get_event_bus

        bus = get_event_bus()
        history = bus.get_history(event_type=None, limit=limit)

        # 로컬 이벤트 추가
        local_events = get_healing_events(limit)

        # 필터링
        if service_filter:
            history = [
                e
                for e in history
                if e.get("data", {}).get("service") == service_filter
                or e.get("data", {}).get("service_name") == service_filter
            ]
            local_events = [
                e for e in local_events if e.get("service") == service_filter
            ]

        # CB 상태 정보 추가
        from baldur.services.circuit_breaker import (
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()

        cb_states = {}
        all_states = cb_service.repository.get_all_states()
        for state in all_states:
            cb_states[state.service_name] = {
                "state": state.state,
                "failure_count": state.failure_count,
                "success_count": getattr(state, "success_count", 0),
                "opened_at": str(getattr(state, "opened_at", None)),
            }

        return Response(
            {
                "status": "success",
                "service_filter": service_filter,
                "event_bus_events": history,
                "local_events": local_events,
                "current_cb_states": cb_states,
                "total_events": len(history) + len(local_events),
                "timestamp": timezone.now().isoformat(),
            }
        )


class BlastRadiusTestView(XTestModeMixin, APIView):
    """
    Stage 51: Blast Radius (영향 범위) 격리 테스트 API.

    POST /api/baldur/xtest/blast-radius-test/
    Body: {"affected_service": "service_a", "check_services": ["service_b", "service_c"]}

    특정 서비스에 장애를 주입하고, 다른 서비스들이 영향받지 않는지 확인합니다.

    - affected_service: 장애를 주입할 서비스 (필수)
    - check_services: 영향 확인할 서비스 목록 (생략 시 CB에 등록된 모든 서비스)
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        affected_service = request.data.get("affected_service")
        if not affected_service:
            return Response(
                {"error": "affected_service is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        check_services = request.data.get("check_services", [])
        failure_count = int(request.data.get("failure_count", 5))

        results = {
            "affected_service": affected_service,
            "isolation_verified": True,
            "affected_services": [],
            "unaffected_services": [],
            "details": {},
        }

        from baldur.services.circuit_breaker import (
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()

        # check_services가 비어있으면 CB에 등록된 모든 서비스 조회
        if not check_services:
            all_states = cb_service.repository.get_all_states()
            check_services = [
                s.service_name for s in all_states if s.service_name != affected_service
            ]

        # Step 1: 대상 서비스에 장애 주입
        for _ in range(failure_count):
            cb_service.record_failure(
                affected_service, error_context={"source": "blast-radius-test"}
            )

        affected_state = cb_service.get_state(affected_service)
        results["affected_service_state"] = affected_state
        results["affected_services"].append(affected_service)

        # 이벤트 기록
        add_healing_event(
            {
                "event_type": "blast_radius_test_started",
                "service": affected_service,
                "failure_count": failure_count,
                "check_services": check_services,
            }
        )

        # Step 2: 다른 서비스들의 상태 확인
        for service in check_services:
            if service == affected_service:
                continue

            service_state = cb_service.get_state(service)
            allowed = cb_service.should_allow(service)

            results["details"][service] = {
                "state": service_state,
                "allowed": allowed,
                "isolated": service_state != "open" and allowed,
            }

            if service_state == "open" or not allowed:
                results["isolation_verified"] = False
                results["affected_services"].append(service)
            else:
                results["unaffected_services"].append(service)

        # Step 3: 결과 스냅샷 저장
        snapshot = collect_system_snapshot()

        # 이벤트 기록
        add_healing_event(
            {
                "event_type": "blast_radius_test_completed",
                "service": affected_service,
                "isolation_verified": results["isolation_verified"],
                "affected_count": len(results["affected_services"]),
                "unaffected_count": len(results["unaffected_services"]),
            }
        )

        # Step 4: 대상 서비스 복구 (테스트 종료)
        cb_service.force_close(affected_service, reason="Blast radius test cleanup")

        logger.info(
            "stage.blast_radius_test",
            affected_service=affected_service,
            results=results["isolation_verified"],
            items_count=len(results["unaffected_services"]),
        )

        # WAL Audit 기록
        self.log_xtest_audit(
            request=request,
            action="blast_radius_test",
            component="observability",
            details={
                "affected_service": affected_service,
                "isolation_verified": results["isolation_verified"],
                "affected_count": len(results["affected_services"]),
                "unaffected_count": len(results["unaffected_services"]),
            },
            result="success" if results["isolation_verified"] else "partial",
        )

        return Response(
            {
                "status": "success",
                **results,
                "snapshot": snapshot,
                "timestamp": timezone.now().isoformat(),
            }
        )


# =============================================================================
# Multi-Service Blast Radius Helpers (Complexity Reduction)
# =============================================================================


def _get_test_services(cb_service, requested_services: list) -> list:
    """테스트할 서비스 목록 조회. 비어있으면 CB에 등록된 모든 서비스 반환."""
    if requested_services:
        return requested_services
    all_states = cb_service.repository.get_all_states()
    return [s.service_name for s in all_states]


def _reset_all_services(cb_service, services: list, reason: str) -> None:
    """모든 서비스를 CLOSED 상태로 리셋."""
    for svc in services:
        cb_service.force_close(svc, reason=reason)


def _inject_failures(cb_service, service: str, failure_count: int, source: str) -> None:
    """특정 서비스에 장애 주입."""
    for _ in range(failure_count):
        cb_service.record_failure(service, error_context={"source": source})


def _check_service_isolation(
    cb_service, affected_service: str, check_service: str
) -> bool:
    """다른 서비스가 영향 받았는지 확인. True면 격리됨(영향 없음)."""
    state = cb_service.get_state(check_service)
    allowed = cb_service.should_allow(check_service)
    return bool(state != "open" and allowed)


def _build_isolation_matrix(
    cb_service,
    test_services: list,
    failure_count: int,
) -> dict:
    """각 서비스별 영향 매트릭스 구성."""
    matrix: dict[str, dict[str, list[str]]] = {}

    for affected_service in test_services:
        matrix[affected_service] = {"affects": [], "does_not_affect": []}

        # 모든 서비스 초기화
        _reset_all_services(cb_service, test_services, "matrix test reset")

        # 대상 서비스에 장애 주입
        _inject_failures(
            cb_service, affected_service, failure_count, "multi-blast-radius-test"
        )

        # 다른 서비스 확인
        for check_service in test_services:
            if check_service == affected_service:
                continue

            if _check_service_isolation(cb_service, affected_service, check_service):
                matrix[affected_service]["does_not_affect"].append(check_service)
            else:
                matrix[affected_service]["affects"].append(check_service)

    return matrix


def _calculate_isolation_score(matrix: dict, total_services: int) -> float:
    """격리 점수 계산 (백분율)."""
    total_checks = total_services * (total_services - 1)
    if total_checks == 0:
        return 100.0
    isolated_count = sum(len(m["does_not_affect"]) for m in matrix.values())
    return isolated_count / total_checks * 100


class MultiServiceBlastRadiusView(XTestModeMixin, APIView):
    """
    Stage 51: 다중 서비스 Blast Radius 격리 매트릭스 테스트.

    POST /api/baldur/xtest/multi-blast-radius/
    Body: {"test_services": ["service_a", "service_b", "service_c"]}

    각 서비스 장애가 다른 서비스에 미치는 영향을 매트릭스로 분석합니다.

    - test_services: 테스트할 서비스 목록 (생략 시 CB에 등록된 모든 서비스)
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        requested_services = request.data.get("test_services", [])
        failure_count = int(request.data.get("failure_count", 5))

        from baldur.services.circuit_breaker import (
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()

        # 테스트할 서비스 목록 조회
        test_services = _get_test_services(cb_service, requested_services)

        if len(test_services) < 2:
            return Response(
                {"error": "At least 2 services required for matrix test"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 매트릭스 구성
        matrix = _build_isolation_matrix(cb_service, test_services, failure_count)

        # 모든 서비스 복구
        _reset_all_services(cb_service, test_services, "matrix test cleanup")

        # 격리 점수 계산
        isolation_score = _calculate_isolation_score(matrix, len(test_services))

        logger.info(
            "stage.multi_blast_radius",
            isolation_score=isolation_score,
        )

        # WAL Audit 기록
        self.log_xtest_audit(
            request=request,
            action="multi_blast_radius_test",
            component="observability",
            details={
                "test_services": test_services,
                "isolation_score_percent": round(isolation_score, 1),
                "total_services_tested": len(test_services),
            },
            result="success",
        )

        return Response(
            {
                "status": "success",
                "matrix": matrix,
                "isolation_score_percent": round(isolation_score, 1),
                "total_services_tested": len(test_services),
                "timestamp": timezone.now().isoformat(),
            }
        )


# =============================================================================
# Postmortem Generation Helpers (Complexity Reduction)
# - Moved to services/postmortem_store.py for reusability
# - Re-exported here for backward compatibility
# =============================================================================

# Import from centralized postmortem_store

# Re-export from utils for backward compatibility
from baldur.utils.duration import (
    IncidentDurationResult,
    calculate_incident_duration,
)


def _calculate_incident_duration(
    timeline: list,
) -> tuple[str | None, str | None, float | None]:
    """
    타임라인에서 인시던트 시작/종료 시점 및 지속 시간 계산.

    Returns:
        tuple: (started_at, resolved_at, duration_seconds)
    """
    result = calculate_incident_duration_detailed(timeline)
    return result.started_at, result.resolved_at, result.duration_seconds


def calculate_incident_duration_detailed(timeline: list) -> IncidentDurationResult:
    """
    타임라인에서 인시던트 지속 시간 세부 정보 계산.

    CB 상태별 시간 세분화:
    - duration_seconds: 전체 소요 시간 (OPEN → CLOSED)
    - downtime_seconds: 실제 서비스 중단 시간 (OPEN → HALF_OPEN)
    - validation_seconds: 복구 검증 시간 (HALF_OPEN → CLOSED)

    Returns:
        IncidentDurationResult: 시작/종료 시각 및 세분화된 duration 정보
    """
    current_time = timezone.now().isoformat()
    return calculate_incident_duration(timeline, current_time)


def _generate_dynamic_actions(
    timeline: list,
    affected_services: list,
    duration_seconds: float | None,
) -> tuple[list, list]:
    """
    타임라인과 분석 결과를 기반으로 동적 action items 및 recommendations 생성.

    순수 함수 generate_dynamic_actions를 래핑하여 Django timezone을 사용.

    Returns:
        tuple: (auto_actions, recommendations)
    """
    from baldur.utils.postmortem_actions import generate_dynamic_actions

    return generate_dynamic_actions(
        timeline=timeline,
        affected_services=affected_services,
        duration_seconds=duration_seconds,
        current_timestamp=timezone.now().isoformat(),
    )


class RecordHealingEventView(XTestModeMixin, APIView):
    """
    Stage 51: 힐링 이벤트 기록 API.

    POST /api/baldur/xtest/record-healing-event/
    Body: {"event_type": "cb_opened", "service": "my_service", "details": {...}}

    커스텀 힐링 이벤트를 기록합니다.
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        event_type = request.data.get("event_type", "custom_event")
        service = request.data.get("service")
        details = request.data.get("details", {})

        event = {
            "event_type": event_type,
            "service": service,
            "details": details,
            "source": "xtest-api",
            "timestamp": timezone.now().isoformat(),
        }

        # 스냅샷 추가 (옵션)
        if request.data.get("include_snapshot", False):
            event["snapshot"] = collect_system_snapshot()

        add_healing_event(event)

        logger.info(
            "stage.healing_event_recorded",
            event_type=event_type,
            service=service,
        )

        # WAL Audit 기록
        self.log_xtest_audit(
            request=request,
            action="record_healing_event",
            component="observability",
            details={
                "event_type": event_type,
                "service": service,
            },
            result="success",
        )

        return Response(
            {
                "status": "success",
                "event": event,
                "total_events": get_healing_events_count(),
                "timestamp": timezone.now().isoformat(),
            }
        )


__all__ = [
    "HealingTimelineView",
    "BlastRadiusTestView",
    "MultiServiceBlastRadiusView",
    "RecordHealingEventView",
]
