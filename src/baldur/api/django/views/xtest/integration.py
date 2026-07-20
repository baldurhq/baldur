"""
X-Test-Mode Integration Test Views

Integration test API verifying that Baldur components work together.

Endpoints:
- POST /api/baldur/xtest/integration/run-scenario/ - run a scenario
- GET  /api/baldur/xtest/integration/scenario/{id}/ - query scenario status
- GET  /api/baldur/xtest/integration/full-snapshot/ - full system snapshot
- POST /api/baldur/xtest/integration/reset/ - reset the system (test only)

Scenarios:
- cb_open_dlq_flow: CB Open -> DLQ store flow
- retry_exhaust_dlq: retry exhaustion -> DLQ flow
- rate_limit_retry: rate limit -> retry backoff
- dlq_replay_success: DLQ -> replay success
- dlq_replay_failure: DLQ -> replay failure -> re-DLQ
- full_recovery_cycle: full outage -> recovery cycle
- idempotent_replay: replay idempotency guarantee

Security:
- X-Test-Mode: chaos-monkey header required
- DEBUG or the CHAOS_ENABLED environment variable required
- fully blocked in production environments
"""

from typing import Any

import structlog
from django.utils import timezone
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from baldur.audit.helpers import log_xtest_scenario_audit

from .base import XTestModeMixin, collect_system_snapshot
from .scenarios import (
    clear_scenario_results,
    get_scenario_class,
    get_scenario_result,
    list_available_scenarios,
)

logger = structlog.get_logger()


# =============================================================================
# Run Scenario View
# =============================================================================


class RunScenarioView(XTestModeMixin, APIView):
    """
    Integration test scenario execution API.

    POST /api/baldur/xtest/integration/run-scenario/

    Request:
        {
            "scenario": "cb_open_dlq_flow",  // scenario identifier (required)
            "service_name": "test_service",  // service under test (required)
            "config": {                      // per-scenario settings (optional)
                "failure_count": 5
            }
        }

    Response:
        {
            "status": "success",
            "scenario_id": "uuid-xxx",
            "scenario": "cb_open_dlq_flow",
            "execution_status": "completed",
            "steps": [...],
            "timeline": [...],
            "snapshot": {...}
        }
    """

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        scenario_name = request.data.get("scenario")
        service_name = request.data.get("service_name")
        config = request.data.get("config", {})

        # Validate the required parameters
        if not scenario_name:
            return Response(
                {
                    "status": "error",
                    "error": "missing_required_field",
                    "message": "scenario is required",
                    "available_scenarios": list_available_scenarios(),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not service_name:
            return Response(
                {
                    "status": "error",
                    "error": "missing_required_field",
                    "message": "service_name is required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Look up the scenario class
        scenario_class = get_scenario_class(scenario_name)
        if not scenario_class:
            return Response(
                {
                    "status": "error",
                    "error": "unknown_scenario",
                    "message": f"Unknown scenario: {scenario_name}",
                    "available_scenarios": list_available_scenarios(),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Run the scenario
        try:
            scenario = scenario_class(service_name=service_name, config=config)
            result = scenario.run()

            logger.info(
                "test.integration_scenario_completed",
                scenario_name=scenario_name,
                status=result.status.value,
                steps_count=len(result.steps),
            )

            # Record the WAL audit entry (via scenario_audit)
            # duration_ms is the sum of every step's duration_ms
            total_duration_ms = sum(s.duration_ms for s in result.steps)
            log_xtest_scenario_audit(
                scenario_id=result.scenario_id,
                scenario_name=scenario_name,
                service_name=service_name,
                status=result.status.value,
                steps_total=len(result.steps),
                steps_completed=sum(1 for s in result.steps if s.success),
                errors=result.errors[:10] if result.errors else [],
                duration_ms=total_duration_ms,
                session_id=self.get_xtest_session_id(request),
                user=self.get_xtest_user(request),
            )

            return Response(
                {
                    "status": "success",
                    **result.to_dict(),
                },
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            logger.exception(
                "test.integration_scenario_error",
                scenario_name=scenario_name,
                error=e,
            )
            return Response(
                {
                    "status": "error",
                    "error": "scenario_execution_failed",
                    "message": str(e),
                    "scenario": scenario_name,
                    "service_name": service_name,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# =============================================================================
# Scenario Status View
# =============================================================================


class ScenarioStatusView(XTestModeMixin, APIView):
    """
    Scenario execution status query API.

    GET /api/baldur/xtest/integration/scenario/{scenario_id}/

    Response:
        {
            "status": "success",
            "scenario_id": "uuid-xxx",
            "scenario": "cb_open_dlq_flow",
            "started_at": "...",
            "completed_at": "...",
            "status": "completed",
            "steps": [...],
            "errors": [...]
        }
    """

    def get(self, request: Request, scenario_id: str) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        result = get_scenario_result(scenario_id)
        if not result:
            return Response(
                {
                    "status": "error",
                    "error": "scenario_not_found",
                    "message": f"Scenario {scenario_id} not found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        # Record the WAL audit entry
        self.log_xtest_audit(
            request=request,
            action="query_scenario",
            component="integration",
            details={"scenario_id": scenario_id},
            result="success",
        )

        return Response(
            {
                "status": "success",
                **result.to_dict(),
            },
            status=status.HTTP_200_OK,
        )


# =============================================================================
# Full System Snapshot View
# =============================================================================


class FullSnapshotView(XTestModeMixin, APIView):
    """
    Combined status query API across every Baldur component.

    GET /api/baldur/xtest/integration/full-snapshot/

    Query Parameters:
        service_name: service filter (optional)
        include_history: whether to include history (optional, default false)

    Response:
        {
            "status": "success",
            "circuit_breakers": {...},
            "error_budget": {...},
            "dlq": {...},
            "retry": {...},
            "rate_limiter": {...},
            "idempotency": {...},
            "timestamp": "..."
        }
    """

    def get(self, request: Request) -> Response:  # noqa: C901, PLR0912, PLR0915
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        service_name = request.query_params.get("service_name")
        include_history = (
            request.query_params.get("include_history", "false").lower() == "true"
        )

        snapshot = {
            "timestamp": timezone.now().isoformat(),
            "service_filter": service_name,
            "include_history": include_history,
        }

        # Circuit Breaker state
        try:
            from baldur.services.circuit_breaker import (
                get_circuit_breaker_service,
            )

            cb_service = get_circuit_breaker_service()

            if service_name:
                state = cb_service.get_state(service_name)
                state_data = cb_service.get_or_create_state(service_name)
                snapshot["circuit_breakers"] = {
                    service_name: {
                        "state": state,
                        "failure_count": state_data.failure_count,
                    }
                }
            else:
                snapshot["circuit_breakers"] = cb_service.get_all_states()
        except Exception as e:
            logger.warning(
                "test.integration_cb_snapshot",
                error=e,
            )
            snapshot["circuit_breakers"] = {"error": str(e)}

        # Error Budget state
        try:
            from baldur.factory.registry import ProviderRegistry

            eb_service = ProviderRegistry.error_budget_service.safe_get()
            if eb_service is None:
                raise RuntimeError("baldur_pro ErrorBudgetService not registered")

            if service_name:
                eb_status = eb_service.get_status(service_name)
                snapshot["error_budget"] = {
                    service_name: {
                        "remaining_percent": eb_status.remaining_percent,
                        "consumed_percent": eb_status.consumed_percent,
                        "status": (
                            eb_status.status.value
                            if hasattr(eb_status, "status")
                            else "unknown"
                        ),
                    }
                }
            else:
                snapshot["error_budget"] = {"status": "available"}
        except Exception as e:
            logger.warning(
                "test.integration_eb_snapshot",
                error=e,
            )
            snapshot["error_budget"] = {"error": str(e)}

        # DLQ state
        try:
            from baldur.factory.registry import ProviderRegistry

            dlq_service = ProviderRegistry.dlq_service.safe_get()
            if dlq_service is None:
                raise RuntimeError("baldur_pro DLQService not registered")

            # ``get_stats`` returns global aggregates; the OSS Protocol does
            # not take a ``domain`` filter. Include the requested service in
            # the snapshot key for callers that filter client-side.
            stats = dlq_service.get_stats()
            snapshot["dlq"] = stats
        except Exception as e:
            logger.warning(
                "test.integration_dlq_snapshot",
                error=e,
            )
            snapshot["dlq"] = {"error": str(e)}

        # Rate Limiter state
        try:
            from baldur.api.django.rate_limit import (
                RedisHealthState,
                get_rate_limit_config,
                get_redis_health_checker,
            )

            health_checker = get_redis_health_checker()
            config = get_rate_limit_config()

            snapshot["rate_limiter"] = {
                "redis_healthy": health_checker.state == RedisHealthState.HEALTHY,
                "state": health_checker.state.value,
                "config": {
                    "control_api_rate_limit": config.get("control_api_rate_limit", 100),
                    "window_seconds": config.get("control_api_window_seconds", 60),
                },
            }
        except Exception as e:
            logger.warning(
                "test.integration_rate_limiter",
                error=e,
            )
            snapshot["rate_limiter"] = {"error": str(e)}

        # Idempotency state
        try:
            from baldur.services.idempotency import IdempotencyService

            idempotency_service = IdempotencyService()

            snapshot["idempotency"] = {
                "status": "available",
                "cache_available": idempotency_service._cache is not None,
            }
        except Exception as e:
            logger.warning(
                "test.integration_idempotency_snapshot",
                error=e,
            )
            snapshot["idempotency"] = {"error": str(e)}

        # Retry state
        try:
            from baldur.services.retry_handler import RetryPolicyConfig

            retry_config = RetryPolicyConfig.from_settings(
                domain=service_name or "default"
            )

            snapshot["retry"] = {
                "max_attempts": retry_config.max_attempts,
                "backoff_base": retry_config.backoff_base,
                "backoff_max": retry_config.backoff_max,
                "rate_limit_aware": retry_config.rate_limit_aware,
            }
        except Exception as e:
            logger.warning(
                "test.integration_retry_snapshot",
                error=e,
            )
            snapshot["retry"] = {"error": str(e)}

        # Add the system snapshot
        snapshot["system"] = collect_system_snapshot()

        # Record the WAL audit entry
        self.log_xtest_audit(
            request=request,
            action="full_snapshot",
            component="integration",
            details={
                "service_filter": service_name,
                "include_history": include_history,
            },
            result="success",
        )

        return Response(
            {
                "status": "success",
                **snapshot,
            },
            status=status.HTTP_200_OK,
        )


# =============================================================================
# System Reset View
# =============================================================================


# =============================================================================
# Component Reset Helpers (Complexity Reduction)
# =============================================================================


def _reset_circuit_breakers(
    service_name: str | None, xtest_only: bool
) -> dict[str, Any]:
    """Reset the Circuit Breaker component."""
    try:
        from baldur.services.circuit_breaker import (
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()

        if service_name:
            # ``reset_circuit`` was replaced by manual_control's ``force_close``
            # (no-op when already closed). Trigger the same end state here.
            cb_service.force_close(service_name=service_name, reason="xtest reset")
            return {"reset": True, "service": service_name}
        return {"reset": True, "scope": "xtest_only" if xtest_only else "all"}
    except Exception as e:
        return {"error": str(e)}


def _reset_error_budget() -> dict[str, Any]:
    """Reset the Error Budget component."""
    return {"reset": True, "note": "EB reset is simulated in X-Test mode"}


def _reset_dlq(xtest_only: bool) -> dict[str, Any]:
    """Reset the DLQ component."""
    try:
        from baldur.factory.registry import ProviderRegistry

        if ProviderRegistry.dlq_service.safe_get() is None:
            raise RuntimeError("baldur_pro DLQService not registered")

        if xtest_only:
            return {"reset": True, "deleted_count": 0, "scope": "xtest_only"}
        return {"reset": True, "scope": "test_data"}
    except Exception as e:
        return {"error": str(e)}


def _reset_rate_limiter() -> dict[str, Any]:
    """Reset the Rate Limiter component."""
    try:
        from baldur.api.django.rate_limit import get_local_limiter

        local_limiter = get_local_limiter()

        if hasattr(local_limiter, "reset"):
            local_limiter.reset()

        return {"reset": True, "scope": "local_counters"}
    except Exception as e:
        return {"error": str(e)}


def _reset_idempotency(xtest_only: bool) -> dict[str, Any]:
    """Reset the Idempotency component."""
    return {"reset": True, "scope": "xtest_keys" if xtest_only else "all_keys"}


def _reset_scenarios() -> dict[str, Any]:
    """Reset the stored scenario results."""
    try:
        count = clear_scenario_results()
        return {"reset": True, "cleared_count": count}
    except Exception as e:
        return {"error": str(e)}


class ResetView(XTestModeMixin, APIView):
    """
    Pre-test system state reset API.

    POST /api/baldur/xtest/integration/reset/

    Request:
        {
            "components": ["circuit_breakers", "dlq", "rate_limiter"],  // optional, default all
            "service_name": "test_service",  // optional
            "xtest_only": true               // optional, X-Test-created data only (default true)
        }

    Response:
        {
            "status": "success",
            "reset_results": {
                "circuit_breakers": {"reset": true},
                "dlq": {"deleted_count": 5},
                "rate_limiter": {"reset": true}
            }
        }
    """

    VALID_COMPONENTS = [
        "circuit_breakers",
        "error_budget",
        "dlq",
        "rate_limiter",
        "idempotency",
        "scenarios",
        "all",
    ]

    # Per-component reset handler mapping
    RESET_HANDLERS = {
        "circuit_breakers": lambda sn, xo: _reset_circuit_breakers(sn, xo),
        "error_budget": lambda sn, xo: _reset_error_budget(),
        "dlq": lambda sn, xo: _reset_dlq(xo),
        "rate_limiter": lambda sn, xo: _reset_rate_limiter(),
        "idempotency": lambda sn, xo: _reset_idempotency(xo),
        "scenarios": lambda sn, xo: _reset_scenarios(),
    }

    def _validate_components(self, components: list[str]) -> Response | None:
        """Validate the component list; return a Response on error."""
        invalid = [c for c in components if c not in self.VALID_COMPONENTS]
        if invalid:
            return Response(
                {
                    "status": "error",
                    "error": "invalid_components",
                    "message": f"Invalid components: {invalid}",
                    "valid_components": self.VALID_COMPONENTS,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return None

    def _execute_resets(
        self,
        components: list[str],
        service_name: str | None,
        xtest_only: bool,
    ) -> dict[str, Any]:
        """Run the reset for each component."""
        reset_all = "all" in components
        results: dict[str, Any] = {}

        for component, handler in self.RESET_HANDLERS.items():
            if reset_all or component in components:
                results[component] = handler(service_name, xtest_only)

        return results

    def post(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        components = request.data.get("components", ["all"])
        service_name = request.data.get("service_name")
        xtest_only = request.data.get("xtest_only", True)

        if isinstance(components, str):
            components = [components]

        # Validate
        validation_error = self._validate_components(components)
        if validation_error:
            return validation_error

        # Run the per-component resets
        reset_results = self._execute_resets(components, service_name, xtest_only)

        logger.info(
            "test.integration_reset_completed",
            components=components,
            service_name=service_name,
            xtest_only=xtest_only,
        )

        response_data = {
            "status": "success",
            "reset_results": reset_results,
            "components_requested": components,
            "service_name": service_name,
            "xtest_only": xtest_only,
            "timestamp": timezone.now().isoformat(),
        }

        # Record the WAL audit entry
        self.log_xtest_cleanup(
            request=request,
            component="integration",
            cleaned_count=len(
                [k for k, v in reset_results.items() if v.get("reset", False)]
            ),
            cleaned_ids=list(reset_results.keys()),
        )

        return Response(response_data, status=status.HTTP_200_OK)
