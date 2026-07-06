"""
Load Shedding Dashboard API.

Provides an API for operators to look up and control the Shedding state.

Usage:
    dashboard = LoadSheddingDashboard(manager)

    # Look up the current state
    status = dashboard.get_status()

    # Manual activation
    dashboard.activate(level=1, reason="Planned maintenance")

    # Manual deactivation
    dashboard.deactivate(reason="Recovery confirmed")
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.services.circuit_breaker.load_shedding.manager import (
        LoadSheddingManager,
    )

logger = structlog.get_logger()


class LoadSheddingDashboard:
    """
    Load Shedding dashboard API.

    Provides an API for operators to look up and control the Shedding state.
    """

    def __init__(self, manager: LoadSheddingManager | None = None):
        """Initialize."""
        self._manager = manager

    @property
    def manager(self) -> LoadSheddingManager:
        """Manager instance."""
        if self._manager is None:
            from . import (
                get_load_shedding_manager,
            )

            self._manager = get_load_shedding_manager()
        return self._manager

    def get_status(self) -> dict[str, Any]:
        """
        Look up the current Shedding state.

        Returns:
            Status dictionary
        """
        return self.manager.get_status().to_dict()

    def get_service_status(self, service_id: str) -> dict[str, Any]:
        """
        Look up the Shedding state of a specific service.

        Args:
            service_id: Service ID

        Returns:
            Per-service status dictionary
        """
        allowed_percent = self.manager.evaluate_shedding(service_id)
        config = self.manager.get_service_config(service_id)

        return {
            "service_id": service_id,
            "allowed_traffic_percent": allowed_percent,
            "is_shed": allowed_percent < 100.0,
            "criticality": config.criticality if config else "unknown",
            "shed_priority": config.shed_priority if config else 0,
            "min_traffic_percentage": config.min_traffic_percentage if config else 0.0,
            "timestamp": utc_now().isoformat(),
        }

    def get_all_services_status(self) -> list[dict[str, Any]]:
        """
        Look up the Shedding state of all services.

        Returns:
            List of per-service statuses
        """
        return [
            self.get_service_status(service_id)
            for service_id in self.manager._service_configs
        ]

    def activate(
        self,
        level: int = 0,
        reason: str = "manual_activation",
        operator: str = "unknown",
    ) -> dict[str, Any]:
        """
        Manually activate Shedding.

        Args:
            level: Level to activate (0-based)
            reason: Activation reason
            operator: Operator ID

        Returns:
            Result dictionary
        """
        success = self.manager.force_activate(level, f"{reason} (by {operator})")

        return {
            "success": success,
            "action": "activate",
            "level": level,
            "reason": reason,
            "operator": operator,
            "timestamp": utc_now().isoformat(),
            "current_status": self.get_status(),
        }

    def deactivate(
        self,
        reason: str = "manual_deactivation",
        operator: str = "unknown",
    ) -> dict[str, Any]:
        """
        Manually deactivate Shedding.

        Args:
            reason: Deactivation reason
            operator: Operator ID

        Returns:
            Result dictionary
        """
        success = self.manager.force_deactivate(f"{reason} (by {operator})")

        return {
            "success": success,
            "action": "deactivate",
            "reason": reason,
            "operator": operator,
            "timestamp": utc_now().isoformat(),
            "current_status": self.get_status(),
        }

    def get_policy(self) -> dict[str, Any]:
        """
        Look up the current policy.

        Returns:
            Policy dictionary
        """
        policy = self.manager.policy
        return {
            "enabled": policy.enabled,
            "trigger_threshold": policy.trigger_threshold,
            "levels": [
                {
                    "error_rate": level.error_rate,
                    "shed_criticality": level.shed_criticality,
                    "traffic_limit": level.traffic_limit,
                    "description": level.description,
                }
                for level in policy.levels
            ],
        }
