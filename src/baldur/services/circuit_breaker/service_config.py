"""
Service Configuration Manager for Circuit Breaker

Manages service configuration and provides criticality-based lookup and Load
Shedding target selection.

Usage example:
    manager = ServiceConfigManager()

    # Register a service
    manager.register_service(ServiceConfig(
        service_id="payment-api",
        criticality="critical",
        shed_priority=0,  # never shed
    ))

    # Look up by criticality
    critical_services = manager.get_services_by_criticality("critical")

    # Look up Load Shedding targets
    targets = manager.get_shedding_targets(["low", "medium"])
"""

from __future__ import annotations

import threading
from typing import Any

import structlog

from baldur.services.circuit_breaker.models import (
    CircuitBreakerAdvancedConfig,
    RecoveryStrategy,
    ServiceConfig,
)
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# =============================================================================
# Service Config Manager
# =============================================================================


class ServiceConfigManager:
    """
    Service configuration manager.

    Responsible for service registration, criticality-based lookup, Load
    Shedding target selection, and so on.

    Attributes:
        _services: Registered service configs (service_id -> ServiceConfig)
        _initialized: Whether initialized

    Usage:
        manager = ServiceConfigManager()

        # Register a service
        manager.register_service(ServiceConfig(
            service_id="payment-api",
            criticality="critical",
        ))

        # Look up a service
        config = manager.get_service_config("payment-api")

        # Look up services by criticality
        low_services = manager.get_services_by_criticality("low")

    Reference:
        docs/baldur/middleware_system/21_CB_ADVANCED_PROTECTION.md
        Section 2 - Service Criticality configuration
    """

    _instance: ServiceConfigManager | None = None

    def __new__(cls):
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return

        self._services: dict[str, ServiceConfig] = {}
        self._default_recovery: RecoveryStrategy = RecoveryStrategy()
        self._initialized = True

        logger.debug("service_config.initialized")

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton instance (for tests)."""
        cls._instance = None

    # =========================================================================
    # Service Registration
    # =========================================================================

    def register_service(self, config: ServiceConfig) -> bool:
        """
        Register a service.

        Args:
            config: Service configuration

        Returns:
            bool: Whether registration succeeded (updates if it already exists)

        Example:
            >>> manager.register_service(ServiceConfig(
            ...     service_id="payment-api",
            ...     criticality="critical",
            ...     shed_priority=0,
            ... ))
            True
        """
        service_id = config.service_id
        is_update = service_id in self._services

        self._services[service_id] = config

        action = "updated" if is_update else "registered"
        logger.info(
            "service_config_manager.service",
            config_action=action,
            service_id=service_id,
            service_criticality=config.criticality,
            shed_priority=config.shed_priority,
        )

        return True

    def register_services(self, configs: list[ServiceConfig]) -> int:
        """
        Register multiple services in bulk.

        Args:
            configs: List of service configurations

        Returns:
            int: Number of services registered
        """
        count = 0
        for config in configs:
            if self.register_service(config):
                count += 1
        return count

    def unregister_service(self, service_id: str) -> bool:
        """
        Unregister a service.

        Args:
            service_id: Service ID

        Returns:
            bool: Whether unregistration succeeded (False if absent)
        """
        if service_id not in self._services:
            logger.warning(
                "service_config_manager.service_found",
                service_id=service_id,
            )
            return False

        del self._services[service_id]
        logger.info(
            "service_config_manager.service_unregistered",
            service_id=service_id,
        )
        return True

    def clear_services(self) -> int:
        """
        Unregister all services.

        Returns:
            int: Number of services unregistered
        """
        count = len(self._services)
        self._services.clear()
        logger.info(
            "service_config_manager.all_services_cleared_services",
            cleared_services_count=count,
        )
        return count

    # =========================================================================
    # Service Retrieval
    # =========================================================================

    def get_service_config(self, service_id: str) -> ServiceConfig | None:
        """
        Look up configuration by service ID.

        Args:
            service_id: Service ID

        Returns:
            ServiceConfig or None if not found
        """
        return self._services.get(service_id)

    def get_all_services(self) -> list[ServiceConfig]:
        """
        Look up all registered services.

        Returns:
            List[ServiceConfig]: All service configurations
        """
        return list(self._services.values())

    def get_service_count(self) -> int:
        """
        Number of registered services.

        Returns:
            int: Number of services
        """
        return len(self._services)

    def is_service_registered(self, service_id: str) -> bool:
        """
        Check whether a service is registered.

        Args:
            service_id: Service ID

        Returns:
            bool: Whether registered
        """
        return service_id in self._services

    # =========================================================================
    # Criticality-based Retrieval
    # =========================================================================

    def get_services_by_criticality(self, criticality: str) -> list[ServiceConfig]:
        """
        Look up services by criticality.

        Args:
            criticality: Importance level ("critical", "high", "medium", "low")

        Returns:
            List[ServiceConfig]: Services with the given criticality

        Example:
            >>> critical_services = manager.get_services_by_criticality("critical")
            >>> for svc in critical_services:
            ...     print(svc.service_id)
            payment-api
        """
        return [
            config
            for config in self._services.values()
            if config.criticality == criticality
        ]

    def get_critical_services(self) -> list[ServiceConfig]:
        """
        Look up critical services.

        Returns:
            List[ServiceConfig]: List of critical services
        """
        return self.get_services_by_criticality("critical")

    def get_non_critical_services(self) -> list[ServiceConfig]:
        """
        Look up non-core services (high, medium, low).

        Returns:
            List[ServiceConfig]: List of non-core services
        """
        return [
            config
            for config in self._services.values()
            if config.criticality != "critical"
        ]

    # =========================================================================
    # Load Shedding Support
    # =========================================================================

    def get_shedding_targets(
        self,
        shed_criticality: list[str],
    ) -> list[ServiceConfig]:
        """
        Look up the list of Load Shedding target services.

        Among services with shed_priority greater than 0, returns those matching
        shed_criticality, sorted by shed_priority in descending order.
        (higher priority is shed first)

        Args:
            shed_criticality: List of criticality levels to shed (e.g., ["low", "medium"])

        Returns:
            List[ServiceConfig]: Target services to shed (descending shed_priority)

        Example:
            >>> targets = manager.get_shedding_targets(["low", "medium"])
            >>> # returned in descending shed_priority order (services to shed first)
        """
        targets = [
            config
            for config in self._services.values()
            if config.criticality in shed_criticality and config.shed_priority > 0
        ]
        return sorted(targets, key=lambda s: s.shed_priority, reverse=True)

    def get_shedding_order(self) -> list[ServiceConfig]:
        """
        Look up all services in Load Shedding order.

        Returns services with shed_priority > 0 in descending priority order.
        Services with priority 0 (never shed) are excluded.

        Returns:
            List[ServiceConfig]: Services sorted in shedding order
        """
        targets = [
            config for config in self._services.values() if config.shed_priority > 0
        ]
        return sorted(targets, key=lambda s: s.shed_priority, reverse=True)

    def is_sheddable(self, service_id: str) -> bool:
        """
        Check whether a service is a Load Shedding target.

        Args:
            service_id: Service ID

        Returns:
            bool: Whether it is a Shedding target (shed_priority > 0)
        """
        config = self.get_service_config(service_id)
        if config is None:
            return False
        return config.shed_priority > 0

    # =========================================================================
    # Recovery Strategy
    # =========================================================================

    def set_default_recovery_strategy(self, strategy: RecoveryStrategy) -> None:
        """
        Set the default Recovery strategy.

        Args:
            strategy: Default Recovery strategy
        """
        self._default_recovery = strategy
        logger.info(
            "service_config_manager.default_recovery_strategy_set",
            strategy=strategy.type,
        )

    def get_recovery_strategy(self, service_id: str) -> RecoveryStrategy:
        """
        Look up the Recovery strategy of a service.

        Returns the default strategy if there is no per-service strategy.

        Args:
            service_id: Service ID

        Returns:
            RecoveryStrategy: per-service or default strategy
        """
        config = self.get_service_config(service_id)
        if config is not None and config.recovery_strategy is not None:
            return config.recovery_strategy
        return self._default_recovery

    # =========================================================================
    # Service Threshold Override
    # =========================================================================

    def get_failure_threshold(
        self,
        service_id: str,
        default: int = 5,
    ) -> int:
        """
        Look up the service's failure threshold.

        Args:
            service_id: Service ID
            default: Default value (when there is no per-service setting)

        Returns:
            int: Failure threshold
        """
        config = self.get_service_config(service_id)
        if config is not None and config.failure_threshold is not None:
            return config.failure_threshold
        return default

    def get_window_seconds(
        self,
        service_id: str,
        default: int = 60,
    ) -> int:
        """
        Look up the service's observation window.

        Args:
            service_id: Service ID
            default: Default value (when there is no per-service setting)

        Returns:
            int: Observation window (seconds)
        """
        config = self.get_service_config(service_id)
        if config is not None and config.window_seconds is not None:
            return config.window_seconds
        return default

    # =========================================================================
    # Bulk Configuration
    # =========================================================================

    def configure_from_advanced_config(
        self,
        config: CircuitBreakerAdvancedConfig,
    ) -> int:
        """
        Load service configuration from CircuitBreakerAdvancedConfig.

        Args:
            config: Advanced configuration

        Returns:
            int: Number of services registered
        """
        # Clear existing services
        self.clear_services()

        # Register services
        count = self.register_services(config.services)

        self.set_default_recovery_strategy(config.default_recovery)

        logger.info(
            "service_config_manager.configured_advanced_config_services",
            registered_services_count=count,
        )

        return count

    # =========================================================================
    # Status
    # =========================================================================

    def get_status(self) -> dict[str, Any]:
        """
        Look up the current status.

        Returns:
            dict: Status information
        """
        services_by_criticality = {
            "critical": len(self.get_services_by_criticality("critical")),
            "high": len(self.get_services_by_criticality("high")),
            "medium": len(self.get_services_by_criticality("medium")),
            "low": len(self.get_services_by_criticality("low")),
        }

        sheddable_count = len(
            [s for s in self._services.values() if s.shed_priority > 0]
        )

        return {
            "total_services": len(self._services),
            "services_by_criticality": services_by_criticality,
            "sheddable_services": sheddable_count,
            "default_recovery_type": self._default_recovery.type,
            "timestamp": utc_now().isoformat(),
        }

    def to_dict(self) -> dict[str, Any]:
        """
        Convert the configuration to a dictionary.

        Returns:
            dict: Configuration dictionary
        """
        return {
            "services": [
                {
                    "service_id": s.service_id,
                    "criticality": s.criticality,
                    "shed_priority": s.shed_priority,
                    "min_traffic_percentage": s.min_traffic_percentage,
                    "failure_threshold": s.failure_threshold,
                    "window_seconds": s.window_seconds,
                }
                for s in self._services.values()
            ],
            "default_recovery_type": self._default_recovery.type,
        }


# =============================================================================
# Module-level Convenience Functions
# =============================================================================


_manager: ServiceConfigManager | None = None
_manager_lock = threading.Lock()


def get_service_config_manager() -> ServiceConfigManager:
    """
    Return the ServiceConfigManager singleton instance.

    Returns:
        ServiceConfigManager: singleton instance
    """
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = ServiceConfigManager()
    return _manager


def reset_service_config_manager() -> None:
    """Reset the singleton instance (for tests)."""
    global _manager
    _manager = None
    ServiceConfigManager.reset_instance()


def register_service(config: ServiceConfig) -> bool:
    """
    Register a service.

    Args:
        config: Service configuration

    Returns:
        bool: Whether registration succeeded
    """
    return get_service_config_manager().register_service(config)


def get_service_config(service_id: str) -> ServiceConfig | None:
    """
    Look up service configuration.

    Args:
        service_id: Service ID

    Returns:
        ServiceConfig or None
    """
    return get_service_config_manager().get_service_config(service_id)


def get_services_by_criticality(criticality: str) -> list[ServiceConfig]:
    """
    Look up services by criticality.

    Args:
        criticality: Importance level

    Returns:
        List[ServiceConfig]: List of services
    """
    return get_service_config_manager().get_services_by_criticality(criticality)


def get_shedding_targets(shed_criticality: list[str]) -> list[ServiceConfig]:
    """
    Look up Load Shedding targets.

    Args:
        shed_criticality: List of criticality levels to shed

    Returns:
        List[ServiceConfig]: Target services to shed
    """
    return get_service_config_manager().get_shedding_targets(shed_criticality)


def is_critical_service(service_id: str) -> bool:
    """
    Check whether a service is critical.

    Args:
        service_id: Service ID

    Returns:
        bool: Whether critical
    """
    config = get_service_config(service_id)
    if config is None:
        return False
    return config.criticality == "critical"
