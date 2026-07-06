"""
Framework-agnostic Blast Radius DNA handlers.

Extracted from api/django/views/blast_radius.py (Phase 2a).

Handler names use ``blast_radius_dna_*`` prefix to disambiguate from
chaos blast-radius handlers in ``chaos_config.py``.

Endpoints:
    GET  /blast-radius/policy/{service_name}/        Policy query
    POST /blast-radius/policy/{service_name}/        Policy creation
    GET  /blast-radius/dependency/{service_name}/    Dependency query
    POST /blast-radius/dependency/                   Dependency addition
    GET  /blast-radius/assessment/                   Assessment history
    POST /blast-radius/assessment/                   Impact assessment
    GET  /blast-radius/isolation/                    Isolated services list
    POST /blast-radius/isolation/{service_name}/     Service isolation
    DELETE /blast-radius/isolation/{service_name}/   Isolation release
    GET  /blast-radius/graph/                        Dependency graph
"""

from __future__ import annotations

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()

__all__ = [
    "blast_radius_dna_policy_get",
    "blast_radius_dna_policy_create",
    "blast_radius_dna_dependency_get",
    "blast_radius_dna_dependency_create",
    "blast_radius_dna_assessment_list",
    "blast_radius_dna_assessment_create",
    "blast_radius_dna_isolation_list",
    "blast_radius_dna_isolation_create",
    "blast_radius_dna_isolation_delete",
    "blast_radius_dna_graph",
]


def _get_blast_radius_service():
    """Acquire BlastRadiusService instance."""
    try:
        from baldur.services.blast_radius.service import BlastRadiusService

        return BlastRadiusService()
    except ImportError:
        return None


def blast_radius_dna_policy_get(ctx: RequestContext) -> ResponseContext:
    """GET /blast-radius/policy/{service_name}/ — policy query (viewer)."""
    service = _get_blast_radius_service()
    if not service:
        return ResponseContext.service_unavailable("Blast Radius service not available")

    service_name = ctx.get_path_param("service_name")
    if service_name:
        policy = service.get_policy(service_name)
        if policy:
            return ResponseContext.json(policy.to_dict())
        return ResponseContext.not_found("Policy not found")
    return ResponseContext.bad_request("Specify service_name")


def blast_radius_dna_policy_create(ctx: RequestContext) -> ResponseContext:
    """POST /blast-radius/policy/{service_name}/ — policy creation (admin)."""
    service = _get_blast_radius_service()
    if not service:
        return ResponseContext.service_unavailable("Blast Radius service not available")

    service_name = ctx.get_path_param("service_name", "")
    body = ctx.json_body or {}

    from baldur.models.blast_radius import BlastRadiusLevel

    level = BlastRadiusLevel(body.get("level", "isolated"))

    policy = service.set_policy(
        service_name=service_name,
        level=level,
        affected_services=body.get("affected_services", []),
        max_affected_percentage=body.get("max_affected_percentage", 10.0),
        auto_isolate=body.get("auto_isolate", True),
    )
    return ResponseContext.json(policy.to_dict(), status_code=201)


def blast_radius_dna_dependency_get(ctx: RequestContext) -> ResponseContext:
    """GET /blast-radius/dependency/{service_name}/ — dependency query (viewer)."""
    service = _get_blast_radius_service()
    if not service:
        return ResponseContext.service_unavailable("Blast Radius service not available")

    service_name = ctx.get_path_param("service_name", "")
    deps = service.get_dependencies(service_name)
    return ResponseContext.json(
        {
            "service": service_name,
            "upstream": [d.to_dict() for d in deps["upstream"]],
            "downstream": [d.to_dict() for d in deps["downstream"]],
        }
    )


def blast_radius_dna_dependency_create(ctx: RequestContext) -> ResponseContext:
    """POST /blast-radius/dependency/ — dependency addition (admin)."""
    service = _get_blast_radius_service()
    if not service:
        return ResponseContext.service_unavailable("Blast Radius service not available")

    body = ctx.json_body or {}
    dep = service.add_dependency(
        source_service=body.get("source_service"),
        target_service=body.get("target_service"),
        dependency_type=body.get("dependency_type", "sync"),
        criticality=body.get("criticality", "medium"),
    )
    return ResponseContext.json(dep.to_dict(), status_code=201)


def blast_radius_dna_assessment_list(ctx: RequestContext) -> ResponseContext:
    """GET /blast-radius/assessment/ — assessment history (viewer)."""
    service = _get_blast_radius_service()
    if not service:
        return ResponseContext.service_unavailable("Blast Radius service not available")

    service_name = ctx.get_query("service_name")
    limit = int(ctx.get_query("limit", "100"))

    assessments = service.get_assessments(service_name=service_name, limit=limit)
    return ResponseContext.json({"assessments": [a.to_dict() for a in assessments]})


def blast_radius_dna_assessment_create(ctx: RequestContext) -> ResponseContext:
    """POST /blast-radius/assessment/ — impact assessment (operator)."""
    service = _get_blast_radius_service()
    if not service:
        return ResponseContext.service_unavailable("Blast Radius service not available")

    body = ctx.json_body or {}
    assessment = service.assess_impact(
        service_name=body.get("service_name"),
        trigger_event=body.get("trigger_event"),
        failing_services=body.get("failing_services", []),
        total_users=body.get("total_users", 1000),
    )
    return ResponseContext.json(assessment.to_dict(), status_code=201)


def blast_radius_dna_isolation_list(ctx: RequestContext) -> ResponseContext:
    """GET /blast-radius/isolation/ — isolated services list (viewer)."""
    service = _get_blast_radius_service()
    if not service:
        return ResponseContext.service_unavailable("Blast Radius service not available")

    isolated = service.get_isolated_services()
    return ResponseContext.json({"isolated_services": isolated})


def blast_radius_dna_isolation_create(ctx: RequestContext) -> ResponseContext:
    """POST /blast-radius/isolation/{service_name}/ — service isolation (admin)."""
    service = _get_blast_radius_service()
    if not service:
        return ResponseContext.service_unavailable("Blast Radius service not available")

    service_name = ctx.get_path_param("service_name", "")
    if service.isolate_service(service_name):
        return ResponseContext.json({"message": f"Service {service_name} isolated"})
    return ResponseContext.json({"message": f"Service {service_name} already isolated"})


def blast_radius_dna_isolation_delete(ctx: RequestContext) -> ResponseContext:
    """DELETE /blast-radius/isolation/{service_name}/ — isolation release (admin)."""
    service = _get_blast_radius_service()
    if not service:
        return ResponseContext.service_unavailable("Blast Radius service not available")

    service_name = ctx.get_path_param("service_name", "")
    if service.release_isolation(service_name):
        return ResponseContext.json(
            {"message": f"Service {service_name} isolation released"}
        )
    return ResponseContext.not_found(f"Service {service_name} not isolated")


def blast_radius_dna_graph(ctx: RequestContext) -> ResponseContext:
    """GET /blast-radius/graph/ — dependency graph data (viewer)."""
    service = _get_blast_radius_service()
    if not service:
        return ResponseContext.service_unavailable("Blast Radius service not available")

    graph = service.build_dependency_graph()
    return ResponseContext.json(graph)
