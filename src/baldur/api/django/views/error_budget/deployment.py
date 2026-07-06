"""
Deployment Policy API Endpoints.

Provides REST API for deployment policy management:
freeze/override/lift decision records and deployment verdict.

Handlers extracted to api/handlers/error_budget_deployment.py (Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.error_budget_deployment import (
    deployment_active_override,
    deployment_freeze_acknowledge,
    deployment_freeze_lift,
    deployment_override,
    deployment_verdict,
)
from baldur.interfaces.web_framework import PermissionLevel

__all__ = [
    "DeploymentVerdictView",
    "DeploymentFreezeAcknowledgeView",
    "DeploymentOverrideView",
    "DeploymentFreezeLiftView",
    "ActiveOverrideView",
]


class DeploymentVerdictView(HandlerAPIView):
    """Deployment readiness verdict endpoint (advisory only)."""

    permission_level = PermissionLevel.VIEWER
    handler = deployment_verdict


class DeploymentFreezeAcknowledgeView(HandlerAPIView):
    """Acknowledge deployment freeze recommendation."""

    permission_level = PermissionLevel.OPERATOR
    handler = deployment_freeze_acknowledge


class DeploymentOverrideView(HandlerAPIView):
    """Approve deployment freeze override."""

    permission_level = PermissionLevel.ADMIN
    handler = deployment_override


class DeploymentFreezeLiftView(HandlerAPIView):
    """Lift deployment freeze."""

    permission_level = PermissionLevel.OPERATOR
    handler = deployment_freeze_lift


class ActiveOverrideView(HandlerAPIView):
    """Check for currently active override."""

    permission_level = PermissionLevel.VIEWER
    handler = deployment_active_override
