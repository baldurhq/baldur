"""
Deployment Adapters Package (OSS tier).

Provides adapters that collect deployment history from external deployment
systems (Kubernetes, ArgoCD, Helm, etc.) — OSS surface keeps the
domain models, interfaces, and the mock adapter for tests.

Components:
- DeploymentEvent: deployment event data model
- DeploymentConfigChange: config change event data model
- ExternalDeploymentAdapter: external deployment system adapter interface
- MockDeploymentAdapter: mock adapter for tests

Dormant tier (relocated to ``baldur_dormant.adapters.deployment.kubernetes``
per doc 528 D10-v2 / D16):
- KubernetesDeploymentAdapter: live Kubernetes API integration. Access
  via ``from baldur_dormant.adapters.deployment.kubernetes import
  KubernetesDeploymentAdapter`` when ``baldur-pro[kubernetes]`` is
  installed.
"""

from __future__ import annotations

from .base import (
    DeploymentConfigChange,
    DeploymentEvent,
    DeploymentSource,
    DeploymentType,
    ExternalDeploymentAdapter,
)
from .mock import MockDeploymentAdapter

__all__ = [
    # Models
    "DeploymentEvent",
    "DeploymentConfigChange",
    "DeploymentType",
    "DeploymentSource",
    # Interfaces
    "ExternalDeploymentAdapter",
    # Adapters (OSS-only — Kubernetes adapter relocated to baldur_dormant)
    "MockDeploymentAdapter",
]

# Backward-compat alias (deprecated).
ConfigChangeEvent = DeploymentConfigChange
