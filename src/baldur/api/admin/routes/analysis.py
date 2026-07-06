"""Cascade + blast-radius + learning + postmortem analysis admin routes."""

from __future__ import annotations

from baldur.api.admin.registry import AdminRegistry, AdminRoute
from baldur.api.admin.routes._import_policy import handle_route_import_failure
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel


def _register_analysis_routes(registry: AdminRegistry) -> None:
    # --- cascade + blast_radius ---
    # Removed endpoints (no current handler — tracked in OOS_INDEX for v1.1
    # admin surface expansion): /cascade/active-chains, /cascade/stats,
    # /cascade/risk-assessment, /blast-radius/summary, /blast-radius/assess,
    # /blast-radius/analysis/{assessment_id}, /blast-radius/history,
    # /blast-radius/dependencies/{service_name}, /blast-radius/impact-prediction,
    # /blast-radius/pending-approvals.
    try:
        from baldur.api.handlers.blast_radius import (
            blast_radius_dna_graph,
            blast_radius_dna_policy_create,
            blast_radius_dna_policy_get,
        )
        from baldur.api.handlers.cascade import (
            cascade_chain_verify,
            cascade_event_list,
            cascade_load_shedding_status,
            causation_trace,
        )

        _cascade_blast_radius_available = True
    except Exception as exc:
        handle_route_import_failure(
            "admin.cascade_blast_radius_routes_unavailable", exc
        )
        _cascade_blast_radius_available = False

    # --- learning + postmortem (imports OK) ---
    try:
        from baldur.api.handlers.learning import (
            learning_insights,
            learning_metric_record,
            learning_pattern_create,
            learning_pattern_list,
            learning_session_action,
            learning_suggestion_apply,
            learning_suggestion_list,
        )
        from baldur.api.handlers.postmortem import (
            postmortem_generate,
            postmortem_incident_detail,
            postmortem_incidents_list,
        )
        from baldur.api.handlers.postmortem_revision import (
            postmortem_revision_compare,
            postmortem_revision_create,
            postmortem_revision_detail,
            postmortem_revision_list,
            postmortem_seal,
            postmortem_unseal,
        )

        _learning_postmortem_available = True
    except Exception as exc:
        handle_route_import_failure("admin.learning_postmortem_routes_unavailable", exc)
        _learning_postmortem_available = False

    if not _cascade_blast_radius_available and not _learning_postmortem_available:
        return

    if _cascade_blast_radius_available:
        for route in (
            AdminRoute(
                HttpMethod.GET,
                "/cascade/events",
                cascade_event_list,
                PermissionLevel.VIEWER,
            ),
            AdminRoute(
                HttpMethod.GET,
                "/cascade/causation/{event_id}",
                causation_trace,
                PermissionLevel.VIEWER,
            ),
            AdminRoute(
                HttpMethod.GET,
                "/cascade/hash-verify/{chain_id}",
                cascade_chain_verify,
                PermissionLevel.VIEWER,
            ),
            AdminRoute(
                HttpMethod.GET,
                "/cascade/load-shedding",
                cascade_load_shedding_status,
                PermissionLevel.VIEWER,
            ),
            AdminRoute(
                HttpMethod.GET,
                "/blast-radius/graph",
                blast_radius_dna_graph,
                PermissionLevel.VIEWER,
            ),
            AdminRoute(
                HttpMethod.GET,
                "/blast-radius/dna-policy",
                blast_radius_dna_policy_get,
                PermissionLevel.VIEWER,
            ),
            AdminRoute(
                HttpMethod.PUT,
                "/blast-radius/dna-policy",
                blast_radius_dna_policy_create,
                PermissionLevel.ADMIN,
            ),
        ):
            registry.register(route)

    if _learning_postmortem_available:
        for route in (
            AdminRoute(
                HttpMethod.POST,
                "/learning/session/{action}",
                learning_session_action,
                PermissionLevel.OPERATOR,
            ),
            AdminRoute(
                HttpMethod.GET,
                "/learning/pattern",
                learning_pattern_list,
                PermissionLevel.VIEWER,
            ),
            AdminRoute(
                HttpMethod.POST,
                "/learning/pattern",
                learning_pattern_create,
                PermissionLevel.OPERATOR,
            ),
            AdminRoute(
                HttpMethod.GET,
                "/learning/suggestion",
                learning_suggestion_list,
                PermissionLevel.VIEWER,
            ),
            AdminRoute(
                HttpMethod.POST,
                "/learning/suggestion/{suggestion_id}",
                learning_suggestion_apply,
                PermissionLevel.OPERATOR,
            ),
            AdminRoute(
                HttpMethod.POST,
                "/learning/metric",
                learning_metric_record,
                PermissionLevel.OPERATOR,
            ),
            AdminRoute(
                HttpMethod.GET,
                "/learning/insights",
                learning_insights,
                PermissionLevel.VIEWER,
            ),
            AdminRoute(
                HttpMethod.POST,
                "/postmortem/generate",
                postmortem_generate,
                PermissionLevel.OPERATOR,
            ),
            AdminRoute(
                HttpMethod.GET,
                "/postmortem/incidents",
                postmortem_incidents_list,
                PermissionLevel.VIEWER,
            ),
            AdminRoute(
                HttpMethod.GET,
                "/postmortem/incidents/{incident_id}",
                postmortem_incident_detail,
                PermissionLevel.VIEWER,
            ),
            AdminRoute(
                HttpMethod.GET,
                "/postmortem/{incident_id}/revisions",
                postmortem_revision_list,
                PermissionLevel.VIEWER,
            ),
            AdminRoute(
                HttpMethod.POST,
                "/postmortem/{incident_id}/revisions",
                postmortem_revision_create,
                PermissionLevel.OPERATOR,
            ),
            AdminRoute(
                HttpMethod.GET,
                "/postmortem/{incident_id}/revisions/{revision_number}",
                postmortem_revision_detail,
                PermissionLevel.VIEWER,
            ),
            AdminRoute(
                HttpMethod.GET,
                "/postmortem/{incident_id}/revisions/compare",
                postmortem_revision_compare,
                PermissionLevel.VIEWER,
            ),
            AdminRoute(
                HttpMethod.POST,
                "/postmortem/{incident_id}/seal",
                postmortem_seal,
                PermissionLevel.ADMIN,
            ),
            AdminRoute(
                HttpMethod.DELETE,
                "/postmortem/{incident_id}/seal",
                postmortem_unseal,
                PermissionLevel.ADMIN,
            ),
        ):
            registry.register(route)
