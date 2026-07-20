"""
X-Test-Mode Observability & Blast Radius Views

Test-only Observability APIs:
- HealingTimelineView: query the healing timeline
- BlastRadiusTestView: single-service Blast Radius isolation test
- MultiServiceBlastRadiusView: multi-service isolation matrix test
- RecordHealingEventView: record a healing event

For the production Post-mortem API, see views/postmortem.py:
- POST /postmortem/generate/ - generate a post-mortem
- GET /postmortem/incidents/ - list incidents
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
    Stage 51: Baldur timeline query API.

    GET /api/baldur/xtest/healing-timeline/?service=database&limit=50

    Returns the event timeline for failure detection, CB state changes,
    recovery, and so on.
    """

    @staticmethod
    def _get_timeline_default_limit() -> int:
        """Look up timeline_default_limit from Settings."""
        try:
            from baldur.settings.api_view import get_api_view_settings

            return get_api_view_settings().xtest_timeline_default_limit
        except Exception:
            return 50  # default

    def get(self, request: Request) -> Response:
        denied = self.check_chaos_permission(request)
        if denied:
            return denied

        service_filter = request.query_params.get("service")
        default_limit = self._get_timeline_default_limit()
        limit = int(request.query_params.get("limit", default_limit))

        # Query the history from the event bus
        from baldur.services.event_bus import get_event_bus

        bus = get_event_bus()
        history = bus.get_history(event_type=None, limit=limit)

        # Add local events
        local_events = get_healing_events(limit)

        # Filtering
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

        # Add CB state information
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
    Stage 51: Blast Radius (impact scope) isolation test API.

    POST /api/baldur/xtest/blast-radius-test/
    Body: {"affected_service": "service_a", "check_services": ["service_b", "service_c"]}

    Injects a failure into a specific service and checks that other services are
    unaffected.

    - affected_service: service to inject the failure into (required)
    - check_services: services to check for impact (defaults to every service
      registered with the CB)
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

        # If check_services is empty, query every service registered with the CB
        if not check_services:
            all_states = cb_service.repository.get_all_states()
            check_services = [
                s.service_name for s in all_states if s.service_name != affected_service
            ]

        # Step 1: inject the failure into the target service
        for _ in range(failure_count):
            cb_service.record_failure(
                affected_service, error_context={"source": "blast-radius-test"}
            )

        affected_state = cb_service.get_state(affected_service)
        results["affected_service_state"] = affected_state
        results["affected_services"].append(affected_service)

        # Record the event
        add_healing_event(
            {
                "event_type": "blast_radius_test_started",
                "service": affected_service,
                "failure_count": failure_count,
                "check_services": check_services,
            }
        )

        # Step 2: check the state of the other services
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

        # Step 3: save a snapshot of the result
        snapshot = collect_system_snapshot()

        # Record the event
        add_healing_event(
            {
                "event_type": "blast_radius_test_completed",
                "service": affected_service,
                "isolation_verified": results["isolation_verified"],
                "affected_count": len(results["affected_services"]),
                "unaffected_count": len(results["unaffected_services"]),
            }
        )

        # Step 4: recover the target service (test teardown)
        cb_service.force_close(affected_service, reason="Blast radius test cleanup")

        logger.info(
            "stage.blast_radius_test",
            affected_service=affected_service,
            results=results["isolation_verified"],
            items_count=len(results["unaffected_services"]),
        )

        # WAL Audit record
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
    """Query the services to test. If empty, return every service on the CB."""
    if requested_services:
        return requested_services
    all_states = cb_service.repository.get_all_states()
    return [s.service_name for s in all_states]


def _reset_all_services(cb_service, services: list, reason: str) -> None:
    """Reset every service to the CLOSED state."""
    for svc in services:
        cb_service.force_close(svc, reason=reason)


def _inject_failures(cb_service, service: str, failure_count: int, source: str) -> None:
    """Inject failures into a specific service."""
    for _ in range(failure_count):
        cb_service.record_failure(service, error_context={"source": source})


def _check_service_isolation(
    cb_service, affected_service: str, check_service: str
) -> bool:
    """Check whether another service was impacted. True means isolated (no impact)."""
    state = cb_service.get_state(check_service)
    allowed = cb_service.should_allow(check_service)
    return bool(state != "open" and allowed)


def _build_isolation_matrix(
    cb_service,
    test_services: list,
    failure_count: int,
) -> dict:
    """Build the per-service impact matrix."""
    matrix: dict[str, dict[str, list[str]]] = {}

    for affected_service in test_services:
        matrix[affected_service] = {"affects": [], "does_not_affect": []}

        # Reset every service
        _reset_all_services(cb_service, test_services, "matrix test reset")

        # Inject the failure into the target service
        _inject_failures(
            cb_service, affected_service, failure_count, "multi-blast-radius-test"
        )

        # Check the other services
        for check_service in test_services:
            if check_service == affected_service:
                continue

            if _check_service_isolation(cb_service, affected_service, check_service):
                matrix[affected_service]["does_not_affect"].append(check_service)
            else:
                matrix[affected_service]["affects"].append(check_service)

    return matrix


def _calculate_isolation_score(matrix: dict, total_services: int) -> float:
    """Calculate the isolation score (percentage)."""
    total_checks = total_services * (total_services - 1)
    if total_checks == 0:
        return 100.0
    isolated_count = sum(len(m["does_not_affect"]) for m in matrix.values())
    return isolated_count / total_checks * 100


class MultiServiceBlastRadiusView(XTestModeMixin, APIView):
    """
    Stage 51: multi-service Blast Radius isolation matrix test.

    POST /api/baldur/xtest/multi-blast-radius/
    Body: {"test_services": ["service_a", "service_b", "service_c"]}

    Analyzes, as a matrix, the impact each service failure has on the others.

    - test_services: services to test (defaults to every service registered
      with the CB)
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

        # Query the services to test
        test_services = _get_test_services(cb_service, requested_services)

        if len(test_services) < 2:
            return Response(
                {"error": "At least 2 services required for matrix test"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Build the matrix
        matrix = _build_isolation_matrix(cb_service, test_services, failure_count)

        # Recover every service
        _reset_all_services(cb_service, test_services, "matrix test cleanup")

        # Calculate the isolation score
        isolation_score = _calculate_isolation_score(matrix, len(test_services))

        logger.info(
            "stage.multi_blast_radius",
            isolation_score=isolation_score,
        )

        # WAL Audit record
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
    Calculate the incident start/end points and duration from the timeline.

    Returns:
        tuple: (started_at, resolved_at, duration_seconds)
    """
    result = calculate_incident_duration_detailed(timeline)
    return result.started_at, result.resolved_at, result.duration_seconds


def calculate_incident_duration_detailed(timeline: list) -> IncidentDurationResult:
    """
    Calculate detailed incident duration information from the timeline.

    Time broken down by CB state:
    - duration_seconds: total elapsed time (OPEN → CLOSED)
    - downtime_seconds: actual service outage time (OPEN → HALF_OPEN)
    - validation_seconds: recovery validation time (HALF_OPEN → CLOSED)

    Returns:
        IncidentDurationResult: start/end times and the broken-down duration info
    """
    current_time = timezone.now().isoformat()
    return calculate_incident_duration(timeline, current_time)


def _generate_dynamic_actions(
    timeline: list,
    affected_services: list,
    duration_seconds: float | None,
) -> tuple[list, list]:
    """
    Generate dynamic action items and recommendations from the timeline and
    analysis results.

    Wraps the pure function generate_dynamic_actions to use the Django timezone.

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
    Stage 51: healing event recording API.

    POST /api/baldur/xtest/record-healing-event/
    Body: {"event_type": "cb_opened", "service": "my_service", "details": {...}}

    Records a custom healing event.
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

        # Add a snapshot (optional)
        if request.data.get("include_snapshot", False):
            event["snapshot"] = collect_system_snapshot()

        add_healing_event(event)

        logger.info(
            "stage.healing_event_recorded",
            event_type=event_type,
            service=service,
        )

        # WAL Audit record
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
