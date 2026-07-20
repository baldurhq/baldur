"""
X-Test-Mode Replay Views

API for testing DLQ replay behavior under X-Test-Mode.

Endpoints:
- POST /api/baldur/xtest/replay/single/ - Replay a single DLQ entry
- POST /api/baldur/xtest/replay/batch/ - Replay multiple entries as a batch
- POST /api/baldur/xtest/replay/trigger-on-cb-close/ - Simulate auto-replay on CB close
- GET  /api/baldur/xtest/replay/status/ - Query replayable entries and status

Security:
- X-Test-Mode: chaos-monkey header required
- DEBUG or CHAOS_ENABLED environment variable required
- Fully blocked in production environments
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
# Single-entry replay view
# =============================================================================


class ReplaySingleView(XTestModeMixin, APIView):
    """
    Single DLQ entry replay API.

    POST /api/baldur/xtest/replay/single/

    Request:
        {
            "dlq_id": 123,  // ID of the DLQ entry to replay (required)
            "dry_run": false,  // Validate only, no execution (optional, default false)
            "skip_governance": false  // Skip governance check (optional, default false)
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

        # Run the governance check (when skip_governance=False)
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
            # dry_run mode: validate only, without executing
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

        # Perform the actual replay
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

        # WAL audit record
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
        """Run the governance check."""
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
                audit_on_block=False,  # audit disabled under X-Test-Mode
            )

            checks_passed = []
            checks_failed = []

            if result.allowed:
                checks_passed = ["kill_switch", "emergency_mode", "error_budget"]
            else:
                if result.block_reason:
                    checks_failed.append(result.block_reason.value)
                    # Treat the rest as passed (checks stop at the first failure)
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
            # fail-open: allow when the check itself fails
            return {
                "allowed": True,
                "checks_passed": [],
                "checks_failed": [],
                "block_reason": None,
                "error": str(e),
            }

    def _validate_replay(self, dlq_id: str) -> dict[str, Any]:
        """Validate replay eligibility (for dry_run)."""
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
        """Perform the actual replay."""
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
# Batch replay view
# =============================================================================


class ReplayBatchView(XTestModeMixin, APIView):
    """
    Batch replay API for multiple DLQ entries.

    POST /api/baldur/xtest/replay/batch/

    Request:
        {
            "domain": "external_service",  // Domain filter (optional)
            "status": "pending",           // Status filter (optional, default pending)
            "batch_size": 10,              // Batch size (optional, default 10, max 50)
            "dry_run": false               // Validate only (optional, default false)
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

        # Batch size limit
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
            # dry_run mode: only look up the entries eligible for replay
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

        # Perform the actual batch replay
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

        # WAL audit record
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
        """Look up the list of entries eligible for replay."""
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
        """Look up the current governance status."""
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
        """Execute the batch replay."""
        try:
            from baldur.services.replay_service import get_replay_service

            service = get_replay_service()
            result = service.replay_batch(
                domain=domain,
                failure_type=None,
                max_items=batch_size,
            )

            # Build the result summary
            results_summary = []
            if result.results:
                results_summary = [
                    {
                        "dlq_id": r.dlq_id,
                        "success": r.success,
                        "message": r.message or r.error or "",
                    }
                    for r in result.results[:20]  # include at most 20
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
# Auto-replay-on-CB-recovery trigger view
# =============================================================================


class TriggerReplayOnCBCloseView(XTestModeMixin, APIView):
    """
    API that simulates automatic replay on CB recovery.

    POST /api/baldur/xtest/replay/trigger-on-cb-close/

    Request:
        {
            "service_name": "database",  // CB service name (required)
            "simulate_close": true,      // Simulate CB CLOSE (optional, default true)
            "max_items": 50              // Max entries to replay (optional, default 50)
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

        # Look up the previous CB state
        cb_previous_state = self._get_cb_state(service_name)

        # If simulate_close is true, simulate a CB transition to CLOSED
        if simulate_close:
            self._simulate_cb_close(service_name)

        cb_current_state = self._get_cb_state(service_name)

        # Look up the number of entries eligible for replay
        eligible_count = self._get_eligible_count(service_name)

        # Run the conditional replay
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

        # WAL audit record
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
        """Look up the CB state."""
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
        """Simulate a CB CLOSE."""
        try:
            from baldur.services.circuit_breaker import (
                get_circuit_breaker_service,
            )

            cb_service = get_circuit_breaker_service()
            # Use the force_close method if available
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
        """Look up the number of entries eligible for replay."""
        try:
            from baldur.services.replay_service import get_replay_service

            service = get_replay_service()
            # Map the service name to a domain
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
        """Run the conditional replay."""
        try:
            from baldur.services.replay_service import get_replay_service

            service = get_replay_service()
            result = service.replay_on_circuit_close(
                service_name=service_name,
                max_items=max_items,
                escalate_failures=False,  # escalation disabled under X-Test-Mode
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
# Replay status view
# =============================================================================


class ReplayStatusView(XTestModeMixin, APIView):
    """
    API for querying replayable entries and their status.

    GET /api/baldur/xtest/replay/status/

    Query Parameters:
        - domain: Domain filter (optional)

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

        # Look up the number of pending entries
        pending_stats = self._get_pending_stats(domain_filter)

        # Look up the governance status
        governance_status = self._get_governance_status()

        # Look up the list of CB states
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

        # WAL audit record
        self.log_xtest_audit(
            request=request,
            action="query_status",
            component="replay",
            details={"pending_count": pending_stats["total"]},
            result="success",
        )

        return Response(response_data, status=status.HTTP_200_OK)

    def _get_pending_stats(self, domain: str | None) -> dict[str, Any]:
        """Look up statistics for pending DLQ entries."""
        try:
            from baldur.factory.registry import ProviderRegistry

            service = ProviderRegistry.dlq_service.safe_get()
            if service is None:
                raise RuntimeError("baldur_pro DLQService not registered")
            stats = service.get_stats()

            by_domain = stats.get("by_domain", {})
            by_status = stats.get("by_status", {})

            # Apply the domain filter
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
        """Look up the governance status."""
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
        """Look up the list of registered CB states."""
        try:
            from baldur.services.circuit_breaker import (
                get_circuit_breaker_service,
            )

            cb_service = get_circuit_breaker_service()
            # Use the get_all_status method if available
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
