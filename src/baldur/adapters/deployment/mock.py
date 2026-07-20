"""
Mock Deployment Adapter.

Mock adapter for test and development environments.
Returns static data so DeploymentCorrelator can be tested without Kubernetes.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import structlog

from baldur.utils.time import utc_now

from .base import (
    DeploymentConfigChange,
    DeploymentEvent,
    DeploymentSource,
    DeploymentType,
)

logger = structlog.get_logger()


class MockDeploymentAdapter:
    """
    Mock deployment adapter for testing.

    Returns static data so DeploymentCorrelator can be tested without a real
    Kubernetes integration.

    Configuration:
        DEPLOYMENT_ADAPTER=mock

    Example:
        >>> adapter = MockDeploymentAdapter()
        >>> deployments = adapter.get_deployments_in_range(
        ...     service_name="payment-service",
        ...     start_time=datetime(2025, 1, 1, 10, 0),
        ...     end_time=datetime(2025, 1, 1, 12, 0)
        ... )
    """

    def __init__(
        self,
        mock_deployments: list[DeploymentEvent] | None = None,
        mock_config_changes: list[DeploymentConfigChange] | None = None,
    ):
        """
        Initialize the mock adapter.

        Args:
            mock_deployments: deployment events for testing (default data is
                generated when None)
            mock_config_changes: configuration change events for testing
        """
        self._mock_deployments = mock_deployments or []
        self._mock_config_changes = mock_config_changes or []
        self._is_available = True
        logger.debug("mock_deployment_adapter.initialized_mock_data")

    def set_mock_deployments(self, deployments: list[DeploymentEvent]) -> None:
        """Set the deployment data used for testing."""
        self._mock_deployments = deployments

    def set_mock_config_changes(self, changes: list[DeploymentConfigChange]) -> None:
        """Set the configuration change data used for testing."""
        self._mock_config_changes = changes

    def set_availability(self, available: bool) -> None:
        """Set adapter availability (for fallback testing)."""
        self._is_available = available

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
        if not self._is_available:
            logger.warning("mock_deployment_adapter.adapter_available")
            return []

        result = []
        for deploy in self._mock_deployments:
            # Service filter
            if deploy.service_name != service_name:
                continue
            # Namespace filter
            if deploy.namespace != namespace:
                continue
            # Time range filter
            try:
                deploy_time = datetime.fromisoformat(
                    deploy.deployed_at.replace("Z", "+00:00")
                )
                if start_time <= deploy_time <= end_time:
                    result.append(deploy)
            except (ValueError, TypeError):
                continue

        # Sort chronologically
        result.sort(key=lambda d: d.deployed_at)

        logger.debug(
            "mock_deployment_adapter.found_deployments_range",
            result_count=len(result),
            service_name=service_name,
        )
        return result

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
        if not self._is_available:
            return None

        for deploy in self._mock_deployments:
            if (
                deploy.service_name == service_name
                and deploy.version_to == version
                and deploy.namespace == namespace
            ):
                return deploy

        return None

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
        if not self._is_available:
            return None

        # Return version_to of the most recent deployment
        matching = [
            d
            for d in self._mock_deployments
            if d.service_name == service_name and d.namespace == namespace
        ]

        if not matching:
            return None

        # Sort chronologically and take the last entry
        matching.sort(key=lambda d: d.deployed_at)
        return matching[-1].version_to

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
        if not self._is_available:
            return []

        rollbacks = [
            d
            for d in self._mock_deployments
            if (
                d.service_name == service_name
                and d.namespace == namespace
                and d.is_rollback
            )
        ]

        # Sort newest first
        rollbacks.sort(key=lambda d: d.deployed_at, reverse=True)

        return rollbacks[:limit]

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
        if not self._is_available:
            return []

        result = []
        for change in self._mock_config_changes:
            # Service filter
            if change.service_name and change.service_name != service_name:
                continue
            # Namespace filter
            if change.namespace != namespace:
                continue
            # Time range filter
            try:
                change_time = datetime.fromisoformat(
                    change.changed_at.replace("Z", "+00:00")
                )
                if start_time <= change_time <= end_time:
                    result.append(change)
            except (ValueError, TypeError):
                continue

        # Sort chronologically
        result.sort(key=lambda c: c.changed_at)

        logger.debug(
            "mock_deployment_adapter.found_config_changes_range",
            result_count=len(result),
            service_name=service_name,
        )
        return result

    def is_available(self) -> bool:
        """
        Check whether the adapter is available.

        Returns:
            Whether the adapter is available
        """
        return self._is_available


def create_sample_deployment(
    service_name: str,
    version_from: str,
    version_to: str,
    minutes_ago: int,
    is_rollback: bool = False,
    namespace: str = "default",
) -> DeploymentEvent:
    """
    Create a sample deployment event for testing.

    Args:
        service_name: service name
        version_from: previous version
        version_to: new version
        minutes_ago: how many minutes ago the deployment happened
        is_rollback: whether this deployment is a rollback
        namespace: namespace

    Returns:
        DeploymentEvent instance
    """
    deployed_at = utc_now() - timedelta(minutes=minutes_ago)

    return DeploymentEvent(
        deployment_id=f"deploy-{uuid4().hex[:8]}",
        service_name=service_name,
        version_from=version_from,
        version_to=version_to,
        deployed_at=deployed_at.isoformat(),
        deployed_by="ci/cd-pipeline",
        deployment_type=DeploymentType.ROLLING,
        source=DeploymentSource.MOCK,
        namespace=namespace,
        is_rollback=is_rollback,
    )


def create_sample_config_change(
    config_key: str,
    old_value: str,
    new_value: str,
    minutes_ago: int,
    service_name: str = "",
    namespace: str = "default",
) -> DeploymentConfigChange:
    """
    Create a sample configuration change event for testing.

    Args:
        config_key: configuration key
        old_value: previous value
        new_value: new value
        minutes_ago: how many minutes ago the change happened
        service_name: service name
        namespace: namespace

    Returns:
        DeploymentConfigChange instance
    """
    changed_at = utc_now() - timedelta(minutes=minutes_ago)

    return DeploymentConfigChange(
        change_id=f"config-{uuid4().hex[:8]}",
        config_key=config_key,
        old_value=old_value,
        new_value=new_value,
        changed_at=changed_at.isoformat(),
        changed_by="admin",
        service_name=service_name,
        namespace=namespace,
    )
