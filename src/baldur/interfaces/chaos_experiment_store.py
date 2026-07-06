"""
ChaosExperimentStore — Domain State Store for Chaos Experiments.

Abstract interface for chaos experiment storage and querying.
Separates chaos domain data from canary rollout domain.

Design:
    - Data owned by chaos domain (ChaosExperimentContext)
    - Consumed by ChaosGuard for conflict detection
    - Separate from CanaryRolloutStore to maintain domain boundaries
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

__all__ = ["ChaosExperimentStore"]


class ChaosExperimentStore(ABC):
    """Abstract store for chaos experiment data.

    Write operations (chaos service):
    - save / delete: experiment lifecycle

    Read operations (ChaosGuard + chaos service):
    - get: single experiment lookup
    - find_active: list all active experiments
    """

    @abstractmethod
    def save(
        self,
        experiment_id: str,
        data: dict[str, Any],
        ttl_seconds: int,
    ) -> None:
        """Save an experiment with TTL.

        Args:
            experiment_id: Experiment identifier
            data: Serialized experiment data
            ttl_seconds: Time-to-live in seconds
        """

    @abstractmethod
    def get(self, experiment_id: str) -> dict[str, Any] | None:
        """Get experiment data by ID.

        Args:
            experiment_id: Experiment identifier

        Returns:
            Experiment data dict or None
        """

    @abstractmethod
    def delete(self, experiment_id: str) -> None:
        """Delete an experiment.

        Args:
            experiment_id: Experiment identifier
        """

    @abstractmethod
    def find_active(self) -> list[dict[str, Any]]:
        """Find all active experiments.

        Returns experiments where status == 'active' and not expired.

        Returns:
            List of experiment data dicts
        """
