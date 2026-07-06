"""
X-Test-Mode Replay Views

DLQ Replay 동작을 X-Test-Mode 환경에서 테스트하기 위한 API.

Endpoints:
- POST /api/baldur/xtest/replay/single/ - 단일 DLQ 항목 재생
- POST /api/baldur/xtest/replay/batch/ - 다수 항목 배치 재생
- POST /api/baldur/xtest/replay/trigger-on-cb-close/ - CB 복구 시 자동 재생 시뮬레이션
- GET  /api/baldur/xtest/replay/status/ - 재생 가능 항목 및 상태 조회

Security:
- X-Test-Mode: chaos-monkey 헤더 필수
- DEBUG 또는 CHAOS_ENABLED 환경 변수 필요
- production 환경에서는 완전 차단
"""

import time
from typing import Any

import structlog
from django.utils import timezone
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .base import XTestModeMixin, collect_system_snapshot

logger = structlog.get_logger()


# =============================================================================
# 단일 항목 재생 View
# =============================================================================


class ReplaySingleView(XTestModeMixin, APIView):
    """
    단일 DLQ 항목 재생 API.

    POST /api/baldur/xtest/replay/single/

    Request:
        {
            "dlq_id": 123,               // 재생할 DLQ 항목 ID (필수)
            "dry_run": false,            // 실제 실행 없이 검증만 (선택, 기본 false)
            "skip_governance": false     // 거버넌스 체크 스킵 (선택, 기본 false)
        }

    Response:
        {
            "status": "success",
            "success": true,
            "dlq_id": 123,
            "message": "Replay completed successfully",
            "governance_result": {
                "allowed": true,
                "checks_passed": ["kill_switch", "emergency_mode", "error_budget"],
                "checks_failed": [],
                "block_reason": null
            },
            "replay_duration_ms": 150,
            "snapshot": {...}
        }
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        dlq_id = request.data.get("dlq_id")
        if not dlq_id:
            return Response(
                {
                    "status": "error",
                    "error": "missing_required_field",
                    "message": "dlq_id is required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 538 D1: dlq_id is an opaque string (composite token for the Redis
        # adapter); no numeric coercion. Normalize to str so a JSON-number
        # body still flows through the str contract.
        dlq_id = str(dlq_id)

        dry_run = request.data.get("dry_run", False)
        skip_governance = request.data.get("skip_governance", False)

        # 거버넌스 체크 수행 (skip_governance=False인 경우)
        governance_result = self._check_governance(skip_governance)

        if not governance_result["allowed"]:
            snapshot = collect_system_snapshot()
            return Response(
                {
                    "status": "blocked",
                    "success": False,
                    "dlq_id": dlq_id,
                    "message": "Replay blocked by governance",
                    "governance_result": governance_result,
                    "replay_duration_ms": 0,
                    "snapshot": snapshot,
                },
                status=status.HTTP_200_OK,
            )

        if dry_run:
            # dry_run 모드: 실제 실행 없이 검증만 수행
            snapshot = collect_system_snapshot()
            validation_result = self._validate_replay(dlq_id)

            return Response(
                {
                    "status": "dry_run",
                    "success": validation_result["valid"],
                    "dlq_id": dlq_id,
                    "message": validation_result["message"],
                    "governance_result": governance_result,
                    "validation": validation_result,
                    "replay_duration_ms": 0,
                    "snapshot": snapshot,
                },
                status=status.HTTP_200_OK,
            )

        # 실제 재생 수행
        start_time = time.time()
        result = self._execute_replay(dlq_id)
        duration_ms = int((time.time() - start_time) * 1000)

        snapshot = collect_system_snapshot()

        logger.info(
            "test.mode_replay_single",
            dlq_id=dlq_id,
            result=result["success"],
            duration_ms=duration_ms,
        )

        response_data = {
            "status": "success" if result["success"] else "failed",
            "success": result["success"],
            "dlq_id": dlq_id,
            "message": result["message"],
            "error": result.get("error"),
            "governance_result": governance_result,
            "replay_duration_ms": duration_ms,
            "snapshot": snapshot,
        }

        # WAL Audit 기록
        self.log_xtest_audit(
            request=request,
            action="replay_single",
            component="replay",
            details={"dlq_id": dlq_id, "duration_ms": duration_ms},
            result="success" if result["success"] else "failed",
            error_message=result.get("error"),
        )

        return Response(response_data, status=status.HTTP_200_OK)

    def _check_governance(self, skip: bool) -> dict[str, Any]:
        """거버넌스 체크 수행."""
        if skip:
            return {
                "allowed": True,
                "checks_passed": [],
                "checks_failed": [],
                "block_reason": None,
                "skipped": True,
            }

        try:
            from baldur.factory.registry import ProviderRegistry

            result = ProviderRegistry.governance.get().check_all_governance(
                check_kill_switch=True,
                check_emergency=True,
                emergency_min_level=2,
                check_error_budget=True,
                operation_name="xtest_replay_single",
                service_name="XTestReplayService",
                domain="dlq",
                audit_on_block=False,  # X-Test-Mode에서는 audit 비활성화
            )

            checks_passed = []
            checks_failed = []

            if result.allowed:
                checks_passed = ["kill_switch", "emergency_mode", "error_budget"]
            else:
                if result.block_reason:
                    checks_failed.append(result.block_reason.value)
                    # 나머지는 passed로 처리 (첫 번째 실패에서 중단되므로)
                    if result.block_reason.value == "kill_switch":
                        pass
                    elif result.block_reason.value == "emergency_mode":
                        checks_passed.append("kill_switch")
                    elif result.block_reason.value == "error_budget":
                        checks_passed.extend(["kill_switch", "emergency_mode"])

            return {
                "allowed": result.allowed,
                "checks_passed": checks_passed,
                "checks_failed": checks_failed,
                "block_reason": (
                    result.block_reason.value if result.block_reason else None
                ),
                "block_message": result.block_message if not result.allowed else None,
            }
        except Exception as e:
            logger.warning(
                "test.mode_governance_check",
                error=e,
            )
            # fail-open: 체크 실패 시 허용
            return {
                "allowed": True,
                "checks_passed": [],
                "checks_failed": [],
                "block_reason": None,
                "error": str(e),
            }

    def _validate_replay(self, dlq_id: str) -> dict[str, Any]:
        """재생 가능 여부 검증 (dry_run용)."""
        try:
            from baldur.services.replay_service import get_replay_service

            service = get_replay_service()
            entry = service.repository.get_by_id(dlq_id)

            if entry is None:
                return {
                    "valid": False,
                    "message": "DLQ entry not found",
                    "reason": "not_found",
                }

            if entry.status != "pending":
                return {
                    "valid": False,
                    "message": f"Cannot replay: status is '{entry.status}'",
                    "reason": "invalid_status",
                    "current_status": entry.status,
                }

            max_replays = service.config.get("max_replay_attempts", 2)
            if entry.retry_count >= max_replays:
                return {
                    "valid": False,
                    "message": "Maximum replay attempts exceeded",
                    "reason": "max_replays_exceeded",
                    "retry_count": entry.retry_count,
                    "max_replays": max_replays,
                }

            return {
                "valid": True,
                "message": "Entry is eligible for replay",
                "entry_domain": entry.domain,
                "entry_status": entry.status,
                "retry_count": entry.retry_count,
            }
        except Exception as e:
            return {
                "valid": False,
                "message": f"Validation error: {str(e)}",
                "reason": "validation_error",
            }

    def _execute_replay(self, dlq_id: str) -> dict[str, Any]:
        """실제 재생 수행."""
        try:
            from baldur.services.replay_service import get_replay_service

            service = get_replay_service()
            result = service.replay_single(dlq_id)

            return {
                "success": result.success,
                "message": result.message
                or ("Replay completed" if result.success else "Replay failed"),
                "error": result.error,
                "data": result.data,
            }
        except Exception as e:
            logger.exception(
                "test.mode_replay_execution",
                error=e,
            )
            return {
                "success": False,
                "message": "Replay execution failed",
                "error": str(e),
            }


# =============================================================================
# 배치 재생 View
# =============================================================================


class ReplayBatchView(XTestModeMixin, APIView):
    """
    다수 DLQ 항목 배치 재생 API.

    POST /api/baldur/xtest/replay/batch/

    Request:
        {
            "domain": "external_service",  // 도메인 필터 (선택)
            "status": "pending",           // 상태 필터 (선택, 기본 pending)
            "batch_size": 10,              // 배치 크기 (선택, 기본 10, 최대 50)
            "dry_run": false               // 검증만 (선택, 기본 false)
        }

    Response:
        {
            "status": "success",
            "total": 10,
            "success_count": 8,
            "failed_count": 2,
            "skipped_count": 0,
            "governance_blocked": false,
            "results": [
                {"dlq_id": 1, "success": true, "message": "..."},
                ...
            ],
            "snapshot": {...}
        }
    """

    MAX_BATCH_SIZE = 50
    DEFAULT_BATCH_SIZE = 10

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        domain = request.data.get("domain")
        request.data.get("status", "pending")
        batch_size = int(request.data.get("batch_size", self.DEFAULT_BATCH_SIZE))
        dry_run = request.data.get("dry_run", False)

        # 배치 크기 제한
        if batch_size > self.MAX_BATCH_SIZE:
            return Response(
                {
                    "status": "error",
                    "error": "batch_size_exceeded",
                    "message": f"Maximum batch size is {self.MAX_BATCH_SIZE}",
                    "requested": batch_size,
                    "max_allowed": self.MAX_BATCH_SIZE,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if batch_size < 1:
            batch_size = 1

        if dry_run:
            # dry_run 모드: 재생 대상 항목만 조회
            result = self._get_eligible_entries(domain, batch_size)
            snapshot = collect_system_snapshot()

            return Response(
                {
                    "status": "dry_run",
                    "total": result["count"],
                    "eligible_entries": result["entries"],
                    "governance_status": self._get_governance_status(),
                    "snapshot": snapshot,
                },
                status=status.HTTP_200_OK,
            )

        # 실제 배치 재생 수행
        result = self._execute_batch_replay(domain, batch_size)
        snapshot = collect_system_snapshot()

        logger.info(
            "test.mode_replay_batch",
            healing_domain=domain,
            result=result["total"],
            success_count=result["success_count"],
            failed_count=result["failed_count"],
        )

        response_data = {
            "status": "success",
            "total": result["total"],
            "success_count": result["success_count"],
            "failed_count": result["failed_count"],
            "skipped_count": result["skipped_count"],
            "governance_blocked": result["governance_blocked"],
            "governance_block_reason": result.get("governance_block_reason"),
            "results": result["results"],
            "snapshot": snapshot,
        }

        # WAL Audit 기록
        self.log_xtest_audit(
            request=request,
            action="replay_batch",
            component="replay",
            details={
                "total": result["total"],
                "success_count": result["success_count"],
                "failed_count": result["failed_count"],
            },
            result="success" if result["failed_count"] == 0 else "partial",
        )

        return Response(response_data, status=status.HTTP_200_OK)

    def _get_eligible_entries(self, domain: str | None, limit: int) -> dict[str, Any]:
        """재생 가능한 항목 목록 조회."""
        try:
            from baldur.services.replay_service import get_replay_service

            service = get_replay_service()
            max_replays = service.config.get("max_replay_attempts", 2)

            # Note: replayability filtering (retry_count < max_replay_attempts)
            # is enforced inside ``replay_batch`` via ``config.max_replay_attempts``;
            # ``find_by_status`` only filters by status/domain/failure_type so the
            # dry-run probe shows what the live path will see.
            del max_replays  # signature carries it for parity, but not consumed here
            entries = service.repository.find_by_status(
                status="pending",
                domain=domain,
                failure_type=None,
                limit=limit,
            )

            return {
                "count": len(entries),
                "entries": [
                    {
                        "id": e.id,
                        "domain": e.domain,
                        "failure_type": e.failure_type,
                        "retry_count": e.retry_count,
                    }
                    for e in entries
                ],
            }
        except Exception as e:
            logger.warning(
                "test.mode_failed_get",
                error=e,
            )
            return {"count": 0, "entries": [], "error": str(e)}

    def _get_governance_status(self) -> dict[str, Any]:
        """현재 거버넌스 상태 조회."""
        try:
            from baldur.factory.registry import ProviderRegistry

            gov = ProviderRegistry.governance.get()
            system_enabled = gov.is_system_enabled()
            emergency_blocked, emergency_level = gov.is_emergency_blocking(min_level=2)
            budget_blocked, budget_pct, threshold_pct = gov.is_error_budget_blocking()

            return {
                "system_enabled": system_enabled,
                "emergency_blocking": emergency_blocked,
                "emergency_level": emergency_level,
                "error_budget_blocking": budget_blocked,
                "error_budget_percent": budget_pct,
            }
        except Exception as e:
            return {"error": str(e)}

    def _execute_batch_replay(
        self, domain: str | None, batch_size: int
    ) -> dict[str, Any]:
        """배치 재생 실행."""
        try:
            from baldur.services.replay_service import get_replay_service

            service = get_replay_service()
            result = service.replay_batch(
                domain=domain,
                failure_type=None,
                max_items=batch_size,
            )

            # 결과 요약 생성
            results_summary = []
            if result.results:
                results_summary = [
                    {
                        "dlq_id": r.dlq_id,
                        "success": r.success,
                        "message": r.message or r.error or "",
                    }
                    for r in result.results[:20]  # 최대 20개만 포함
                ]

            return {
                "total": result.total,
                "success_count": result.success_count,
                "failed_count": result.failed_count,
                "skipped_count": result.skipped_count,
                "governance_blocked": result.governance_blocked,
                "governance_block_reason": result.governance_block_reason,
                "results": results_summary,
            }
        except Exception as e:
            logger.exception(
                "test.mode_batch_replay",
                error=e,
            )
            return {
                "total": 0,
                "success_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "governance_blocked": False,
                "results": [],
                "error": str(e),
            }


# =============================================================================
# CB 복구 시 자동 재생 트리거 View
# =============================================================================


class TriggerReplayOnCBCloseView(XTestModeMixin, APIView):
    """
    CB 복구 시 자동 재생 동작 시뮬레이션 API.

    POST /api/baldur/xtest/replay/trigger-on-cb-close/

    Request:
        {
            "service_name": "database",      // CB 서비스 이름 (필수)
            "simulate_close": true,          // CB CLOSE 시뮬레이션 (선택, 기본 true)
            "max_items": 50                  // 최대 재생 항목 수 (선택, 기본 50)
        }

    Response:
        {
            "status": "success",
            "triggered": true,
            "eligible_count": 5,
            "replayed_count": 5,
            "cb_previous_state": "OPEN",
            "cb_current_state": "CLOSED",
            "replay_results": {...},
            "snapshot": {...}
        }
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        service_name = request.data.get("service_name")
        if not service_name:
            return Response(
                {
                    "status": "error",
                    "error": "missing_required_field",
                    "message": "service_name is required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        simulate_close = request.data.get("simulate_close", True)
        max_items = int(request.data.get("max_items", 50))

        # CB 이전 상태 조회
        cb_previous_state = self._get_cb_state(service_name)

        # simulate_close가 true면 CB 상태를 CLOSED로 전환 시뮬레이션
        if simulate_close:
            self._simulate_cb_close(service_name)

        cb_current_state = self._get_cb_state(service_name)

        # 재생 대상 항목 수 조회
        eligible_count = self._get_eligible_count(service_name)

        # 조건부 재생 실행
        replay_result = self._execute_conditional_replay(service_name, max_items)

        snapshot = collect_system_snapshot()

        logger.info(
            "test.mode_trigger_replay",
            service_name=service_name,
            eligible_count=eligible_count,
            replay_result=replay_result.get("success_count", 0),
        )

        response_data = {
            "status": "success",
            "triggered": replay_result.get("total", 0) > 0,
            "eligible_count": eligible_count,
            "replayed_count": replay_result.get("success_count", 0),
            "failed_count": replay_result.get("failed_count", 0),
            "cb_previous_state": cb_previous_state,
            "cb_current_state": cb_current_state,
            "replay_results": replay_result,
            "snapshot": snapshot,
        }

        # WAL Audit 기록
        self.log_xtest_audit(
            request=request,
            action="trigger_cb_close_replay",
            component="replay",
            details={
                "service_name": service_name,
                "eligible_count": eligible_count,
                "replayed_count": replay_result.get("success_count", 0),
            },
            result="success",
        )

        return Response(response_data, status=status.HTTP_200_OK)

    def _get_cb_state(self, service_name: str) -> str:
        """CB 상태 조회."""
        try:
            from baldur.services.circuit_breaker import (
                get_circuit_breaker_service,
            )

            cb_service = get_circuit_breaker_service()
            state = cb_service.get_state(service_name)
            return state or "UNKNOWN"
        except Exception as e:
            logger.warning(
                "test.mode_failed_get",
                error=e,
            )
            return "UNKNOWN"

    def _simulate_cb_close(self, service_name: str) -> bool:
        """CB CLOSE 시뮬레이션."""
        try:
            from baldur.services.circuit_breaker import (
                get_circuit_breaker_service,
            )

            cb_service = get_circuit_breaker_service()
            # force_close 메서드가 있으면 사용
            if hasattr(cb_service, "force_close"):
                cb_service.force_close(service_name, trigger_replay=False)
                return True
            if hasattr(cb_service, "reset"):
                cb_service.reset(service_name)
                return True
            return False
        except Exception as e:
            logger.warning(
                "test.mode_failed_simulate",
                error=e,
            )
            return False

    def _get_eligible_count(self, service_name: str) -> int:
        """재생 대상 항목 수 조회."""
        try:
            from baldur.services.replay_service import get_replay_service

            service = get_replay_service()
            # 서비스 이름으로 도메인 매핑
            entries = service.repository.find_replayable(
                max_retries=service.config.get("max_replay_attempts", 2),
                domain=None,
                failure_type=None,
                limit=100,
            )
            return len(entries)
        except Exception as e:
            logger.warning(
                "test.mode_failed_get",
                error=e,
            )
            return 0

    def _execute_conditional_replay(
        self, service_name: str, max_items: int
    ) -> dict[str, Any]:
        """조건부 재생 실행."""
        try:
            from baldur.services.replay_service import get_replay_service

            service = get_replay_service()
            result = service.replay_on_circuit_close(
                service_name=service_name,
                max_items=max_items,
                escalate_failures=False,  # X-Test-Mode에서는 escalate 비활성화
            )

            return {
                "total": result.total,
                "success_count": result.success_count,
                "failed_count": result.failed_count,
                "skipped_count": result.skipped_count,
            }
        except Exception as e:
            logger.exception(
                "test.mode_conditional_replay",
                error=e,
            )
            return {
                "total": 0,
                "success_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "error": str(e),
            }


# =============================================================================
# 재생 상태 조회 View
# =============================================================================


class ReplayStatusView(XTestModeMixin, APIView):
    """
    재생 가능 항목 및 상태 조회 API.

    GET /api/baldur/xtest/replay/status/

    Query Parameters:
        - domain: 도메인 필터 (선택)

    Response:
        {
            "status": "success",
            "pending_count": 100,
            "by_domain": {
                "external_service": 50,
                "internal_process": 30,
                "other": 20
            },
            "governance_status": {
                "system_enabled": true,
                "emergency_blocking": false,
                "error_budget_blocking": false
            },
            "cb_states": {
                "database": "CLOSED",
                "external_api": "OPEN"
            },
            "snapshot": {...}
        }
    """

    def get(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        domain_filter = request.query_params.get("domain")

        # 대기 중인 항목 수 조회
        pending_stats = self._get_pending_stats(domain_filter)

        # 거버넌스 상태 조회
        governance_status = self._get_governance_status()

        # CB 상태 목록 조회
        cb_states = self._get_cb_states()

        snapshot = collect_system_snapshot()

        response_data = {
            "status": "success",
            "pending_count": pending_stats["total"],
            "by_domain": pending_stats["by_domain"],
            "by_status": pending_stats.get("by_status", {}),
            "governance_status": governance_status,
            "cb_states": cb_states,
            "timestamp": timezone.now().isoformat(),
            "snapshot": snapshot,
        }

        # WAL Audit 기록
        self.log_xtest_audit(
            request=request,
            action="query_status",
            component="replay",
            details={"pending_count": pending_stats["total"]},
            result="success",
        )

        return Response(response_data, status=status.HTTP_200_OK)

    def _get_pending_stats(self, domain: str | None) -> dict[str, Any]:
        """대기 중인 DLQ 항목 통계 조회."""
        try:
            from baldur.factory.registry import ProviderRegistry

            service = ProviderRegistry.dlq_service.safe_get()
            if service is None:
                raise RuntimeError("baldur_pro DLQService not registered")
            stats = service.get_stats()

            by_domain = stats.get("by_domain", {})
            by_status = stats.get("by_status", {})

            # 도메인 필터 적용
            if domain:
                filtered_count = by_domain.get(domain, 0)
                by_domain = {domain: filtered_count}
                total = filtered_count
            else:
                total = by_status.get("pending", 0)

            return {
                "total": total,
                "by_domain": by_domain,
                "by_status": by_status,
            }
        except Exception as e:
            logger.warning(
                "test.mode_failed_get",
                error=e,
            )
            return {"total": 0, "by_domain": {}, "error": str(e)}

    def _get_governance_status(self) -> dict[str, Any]:
        """거버넌스 상태 조회."""
        try:
            from baldur.factory.registry import ProviderRegistry

            gov = ProviderRegistry.governance.get()
            system_enabled = gov.is_system_enabled()
            emergency_blocked, emergency_level = gov.is_emergency_blocking(min_level=2)
            budget_blocked, budget_pct, threshold_pct = gov.is_error_budget_blocking()

            return {
                "system_enabled": system_enabled,
                "emergency_blocking": emergency_blocked,
                "emergency_level": emergency_level,
                "error_budget_blocking": budget_blocked,
                "error_budget_percent": budget_pct,
                "replay_allowed": system_enabled
                and not emergency_blocked
                and not budget_blocked,
            }
        except Exception as e:
            return {"error": str(e), "replay_allowed": True}

    def _get_cb_states(self) -> dict[str, str]:
        """등록된 CB 상태 목록 조회."""
        try:
            from baldur.services.circuit_breaker import (
                get_circuit_breaker_service,
            )

            cb_service = get_circuit_breaker_service()
            # get_all_status 메서드가 있으면 사용
            if hasattr(cb_service, "get_all_status"):
                all_status = cb_service.get_all_status()
                return {
                    name: status.get("state", "UNKNOWN")
                    for name, status in all_status.items()
                }
            return {}
        except Exception as e:
            logger.warning(
                "test.mode_failed_get",
                error=e,
            )
            return {"error": str(e)}
