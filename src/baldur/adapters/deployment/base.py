"""
Deployment Adapter Base Interface and Data Models.

Defines the abstract interface and data models for integrating with external
deployment systems.

Data Models:
- DeploymentEvent: deployment event information
- DeploymentConfigChange: configuration change event information

Interfaces:
- ExternalDeploymentAdapter: external deployment system adapter protocol
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from baldur.core.serializable import SerializableMixin


class DeploymentType(str, Enum):
    """Deployment strategy type."""

    ROLLING = "rolling"
    """Rolling update deployment."""

    CANARY = "canary"
    """Canary deployment."""

    BLUE_GREEN = "blue-green"
    """Blue-green deployment."""

    RECREATE = "recreate"
    """Recreate deployment."""

    UNKNOWN = "unknown"
    """Unknown deployment strategy."""


class DeploymentSource(str, Enum):
    """Source of the deployment information."""

    KUBERNETES = "kubernetes"
    """Collected from the Kubernetes API."""

    ARGOCD = "argocd"
    """Collected from ArgoCD."""

    HELM = "helm"
    """Collected from a Helm release."""

    MANUAL = "manual"
    """Entered manually."""

    MOCK = "mock"
    """Mock data for testing."""


@dataclass
class DeploymentEvent(SerializableMixin):
    """
    Deployment event information.

    Data model for tracking deployment history around an incident.

    Attributes:
        deployment_id: unique deployment ID
        service_name: target service name
        version_from: previous version
        version_to: new version
        deployed_at: deployment time (ISO 8601)
        deployed_by: deployer (user or system)
        deployment_type: deployment strategy
        source: source of the deployment information
        namespace: namespace (Kubernetes)
        is_rollback: whether this deployment is a rollback
        metadata: additional metadata
    """

    deployment_id: str
    """Unique deployment ID."""

    service_name: str
    """Target service name."""

    version_from: str
    """Previous version."""

    version_to: str
    """New version."""

    deployed_at: str
    """Deployment time (ISO 8601 format)."""

    deployed_by: str = "system"
    """Deployer (user or system)."""

    deployment_type: DeploymentType = DeploymentType.ROLLING
    """Deployment strategy."""

    source: DeploymentSource = DeploymentSource.KUBERNETES
    """Source of the deployment information."""

    namespace: str = "default"
    """Namespace."""

    is_rollback: bool = False
    """Whether this deployment is a rollback."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Additional metadata."""

    def to_timeline_event(self) -> dict[str, Any]:
        """Convert to timeline event format."""
        event_type = "ROLLBACK" if self.is_rollback else "DEPLOY"
        return {
            "timestamp": self.deployed_at,
            "event_type": f"[{event_type}] {self.version_from} → {self.version_to}",
            "details": {
                "deployment_id": self.deployment_id,
                "service_name": self.service_name,
                "deployed_by": self.deployed_by,
                "deployment_type": self.deployment_type.value,
                "source": self.source.value,
            },
        }


@dataclass
class DeploymentConfigChange(SerializableMixin):
    """
    Configuration change event information.

    Data model for tracking configuration change history around an incident.

    Attributes:
        change_id: unique change ID
        config_key: changed configuration key
        old_value: previous value (sensitive data masked)
        new_value: new value (sensitive data masked)
        changed_at: change time (ISO 8601)
        changed_by: who made the change
        service_name: target service name
        namespace: namespace
    """

    change_id: str
    """Unique change ID."""

    config_key: str
    """Changed configuration key."""

    old_value: str
    """Previous value (sensitive data masked)."""

    new_value: str
    """New value (sensitive data masked)."""

    changed_at: str
    """Change time (ISO 8601 format)."""

    changed_by: str = "system"
    """Who made the change."""

    service_name: str = ""
    """Target service name."""

    namespace: str = "default"
    """Namespace."""

    def to_timeline_event(self) -> dict[str, Any]:
        """Convert to timeline event format."""
        return {
            "timestamp": self.changed_at,
            "event_type": f"[CONFIG] {self.config_key}: {self.old_value} → {self.new_value}",
            "details": {
                "change_id": self.change_id,
                "service_name": self.service_name,
                "changed_by": self.changed_by,
            },
        }


@runtime_checkable
class ExternalDeploymentAdapter(Protocol):
    """
    External deployment system adapter interface.

    Defines the protocol for adapters that collect deployment history by
    integrating with external deployment systems such as Kubernetes, ArgoCD
    and Helm.

    Implementations:
    - MockDeploymentAdapter: returns static data for testing
    - KubernetesDeploymentAdapter: Kubernetes API integration

    Example:
        >>> adapter = MockDeploymentAdapter()
        >>> deployments = adapter.get_deployments_in_range(
        ...     service_name="payment-service",
        ...     start_time=datetime(2025, 1, 1, 10, 0),
        ...     end_time=datetime(2025, 1, 1, 12, 0)
        ... )
        >>> for deploy in deployments:
        ...     print(f"{deploy.version_from} -> {deploy.version_to}")
    """

    def get_deployments_in_range(
        self,
        service_name: str,
        start_time: datetime,
        end_time: datetime,
        namespace: str = "default",
    ) -> list[DeploymentEvent]:
        """
        Look up deployment history within the given time range.

        Args:
            service_name: service name
            start_time: start of the query range
            end_time: end of the query range
            namespace: namespace

        Returns:
            List of deployment events (sorted chronologically)
        """
        ...

    def get_deployment_by_version(
        self,
        service_name: str,
        version: str,
        namespace: str = "default",
    ) -> DeploymentEvent | None:
        """
        Look up deployment details for a specific version.

        Args:
            service_name: service name
            version: deployed version
            namespace: namespace

        Returns:
            Deployment event, or None
        """
        ...

    def get_current_version(
        self,
        service_name: str,
        namespace: str = "default",
    ) -> str | None:
        """
        Look up the currently deployed version of a service.

        Args:
            service_name: service name
            namespace: namespace

        Returns:
            Current version string, or None
        """
        ...

    def get_rollback_history(
        self,
        service_name: str,
        namespace: str = "default",
        limit: int = 10,
    ) -> list[DeploymentEvent]:
        """
        Look up rollback history.

        Args:
            service_name: service name
            namespace: namespace
            limit: maximum number of entries to return

        Returns:
            List of rollback events (newest first)
        """
        ...

    def get_config_changes_in_range(
        self,
        service_name: str,
        start_time: datetime,
        end_time: datetime,
        namespace: str = "default",
    ) -> list[DeploymentConfigChange]:
        """
        Look up configuration change history within the given time range.

        Args:
            service_name: service name
            start_time: start of the query range
            end_time: end of the query range
            namespace: namespace

        Returns:
            List of configuration change events (sorted chronologically)
        """
        ...

    def is_available(self) -> bool:
        """
        Check whether the adapter is available.

        Returns:
            Whether the adapter is available
        """
        ...
