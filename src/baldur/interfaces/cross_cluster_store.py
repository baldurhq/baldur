"""
CrossClusterStore — Domain State Store for Cross-Cluster Operations.

Abstract interface for cross-cluster propagation requests and
governance policy synchronization.

Design:
    - Combines PropagationRequest storage and GovernancePolicySync storage
    - Both classes in cross_cluster.py share the same Redis access pattern
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

__all__ = ["CrossClusterStore"]


class CrossClusterStore(ABC):
    """Abstract store for cross-cluster propagation and governance policy state.

    Propagation request operations:
    - save_request / get_request: request CRUD
    - add_pending / remove_pending: per-cluster pending set management

    Governance policy operations:
    - save_policy / get_policy: policy CRUD
    """

    # -- Propagation requests -------------------------------------------------

    @abstractmethod
    def save_request(
        self,
        request_id: str,
        data: dict[str, Any],
        ttl_seconds: int,
    ) -> None:
        """Save a propagation request with TTL.

        Args:
            request_id: Request identifier
            data: Serialized request data
            ttl_seconds: Time-to-live in seconds
        """

    @abstractmethod
    def get_request(self, request_id: str) -> dict[str, Any] | None:
        """Get a propagation request by ID.

        Args:
            request_id: Request identifier

        Returns:
            Request data dict or None
        """

    @abstractmethod
    def add_pending(self, cluster: str, request_id: str) -> None:
        """Add a request to a cluster's pending set.

        Args:
            cluster: Target cluster name
            request_id: Request identifier
        """

    @abstractmethod
    def remove_pending(self, cluster: str, request_id: str) -> None:
        """Remove a request from a cluster's pending set.

        Args:
            cluster: Target cluster name
            request_id: Request identifier
        """

    # -- Governance policies --------------------------------------------------

    @abstractmethod
    def save_policy(self, policy_id: str, data: dict[str, Any]) -> None:
        """Save a governance policy.

        Args:
            policy_id: Policy identifier (typically config_type)
            data: Serialized policy data
        """

    @abstractmethod
    def get_policy(self, policy_id: str) -> dict[str, Any] | None:
        """Get a governance policy by ID.

        Args:
            policy_id: Policy identifier

        Returns:
            Policy data dict or None
        """
