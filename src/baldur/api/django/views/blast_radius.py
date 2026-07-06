"""
Blast Radius DNA API Views.

Thin HandlerAPIView wrappers. Business logic extracted to
api/handlers/blast_radius.py (Phase 2b -- 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.blast_radius import (
    blast_radius_dna_assessment_create,
    blast_radius_dna_assessment_list,
    blast_radius_dna_dependency_create,
    blast_radius_dna_dependency_get,
    blast_radius_dna_graph,
    blast_radius_dna_isolation_create,
    blast_radius_dna_isolation_delete,
    blast_radius_dna_isolation_list,
    blast_radius_dna_policy_create,
    blast_radius_dna_policy_get,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

__all__ = [
    "BlastRadiusDNAPolicyView",
    "BlastRadiusDependencyView",
    "BlastRadiusAssessmentView",
    "BlastRadiusIsolationView",
    "BlastRadiusGraphView",
]


class BlastRadiusDNAPolicyView(HandlerAPIView):
    """Blast radius DNA policy management endpoint."""

    handler_map = {
        HttpMethod.GET: blast_radius_dna_policy_get,
        HttpMethod.POST: blast_radius_dna_policy_create,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.POST: PermissionLevel.ADMIN,
    }


class BlastRadiusDependencyView(HandlerAPIView):
    """Service dependency management endpoint."""

    handler_map = {
        HttpMethod.GET: blast_radius_dna_dependency_get,
        HttpMethod.POST: blast_radius_dna_dependency_create,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.POST: PermissionLevel.ADMIN,
    }


class BlastRadiusAssessmentView(HandlerAPIView):
    """Impact assessment endpoint."""

    handler_map = {
        HttpMethod.GET: blast_radius_dna_assessment_list,
        HttpMethod.POST: blast_radius_dna_assessment_create,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.POST: PermissionLevel.OPERATOR,
    }


class BlastRadiusIsolationView(HandlerAPIView):
    """Service isolation management endpoint."""

    handler_map = {
        HttpMethod.GET: blast_radius_dna_isolation_list,
        HttpMethod.POST: blast_radius_dna_isolation_create,
        HttpMethod.DELETE: blast_radius_dna_isolation_delete,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.POST: PermissionLevel.ADMIN,
        HttpMethod.DELETE: PermissionLevel.ADMIN,
    }


class BlastRadiusGraphView(HandlerAPIView):
    """Dependency graph data endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = blast_radius_dna_graph
