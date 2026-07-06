"""
Load Shedding Manager.

Monitors core-service error rates and, when a threshold is exceeded,
progressively limits non-core service traffic.

Usage:
    manager = LoadSheddingManager()

    # Register a service
    manager.register_service(ServiceConfig(
        service_id="review-api",
        criticality="low",
        shed_priority=10,
    ))

    # Set the error rate (for tests or external metric integration)
    manager.set_error_rate("payment-api", 45.0)

    # Evaluate Shedding
    allowed = manager.evaluate_shedding("review-api")  # Returns 50.0
"""

from __future__ import annotations

import random
from collections.abc import Callable

import structlog

from baldur.services.circuit_breaker.load_shedding.error_rate import (
    ErrorRateProvider,
)
from baldur.services.circuit_breaker.load_shedding.shedding_models import (
    SheddingAuditEntry,
    SheddingDecision,
    SheddingState,
    SheddingStatus,
)
from baldur.services.circuit_breaker.models import (
    LoadSheddingPolicy,
    ServiceConfig,
    SheddingLevel,
)
from baldur.utils.time import utc_now

logger = structlog.get_logger()


class LoadSheddingManager:
    """
    Load Shedding manager.

    Monitors core-service error rates and, when a threshold is exceeded,
    progressively limits non-core service traffic.
    """

    _instance: LoadSheddingManager | None = None

    def __new__(cls, *args, **kwargs):
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        policy: LoadSheddingPolicy | None = None,
        error_rate_provider: ErrorRateProvider | None = None,
    ):
        if getattr(self, "_initialized", False):
            return

        self._policy = policy or LoadSheddingPolicy()
        self._error_rate_provider = error_rate_provider or ErrorRateProvider()
        self._service_configs: dict[str, ServiceConfig] = {}
        self._current_level_index: int = -1  # -1 = inactive
        self._activated_at: str | None = None
        self._audit_callback: Callable[[SheddingAuditEntry], None] | None = None
        self._initialized = True

        logger.debug("load_shedding_manager.initialized")

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton instance (for tests)."""
        cls._instance = None

    # =========================================================================
    # Configuration
    # =========================================================================

    @property
    def policy(self) -> LoadSheddingPolicy:
        """Current policy."""
        return self._policy

    def set_policy(self, policy: LoadSheddingPolicy) -> None:
        """Set the policy."""
        self._policy = policy
        logger.info(
            "load_shedding_manager.policy_updated",
            policy=policy.enabled,
        )

    def set_audit_callback(
        self,
        callback: Callable[[SheddingAuditEntry], None],
    ) -> None:
        """
        Set the Audit callback.

        Args:
            callback: Callback function that receives Audit entries
        """
        self._audit_callback = callback

    # =========================================================================
    # Service Registration
    # =========================================================================

    def register_service(self, config: ServiceConfig) -> bool:
        """
        Register a service.

        Args:
            config: Service configuration

        Returns:
            bool: Whether registration succeeded
        """
        self._service_configs[config.service_id] = config
        logger.debug(
            "load_shedding_manager.service_registered",
            target_service_id=config.service_id,
            criticality=config.criticality,
        )
        return True

    def register_services(self, configs: list[ServiceConfig]) -> int:
        """Register multiple services in bulk."""
        count = 0
        for config in configs:
            if self.register_service(config):
                count += 1
        return count

    def unregister_service(self, service_id: str) -> bool:
        """Unregister a service."""
        if service_id in self._service_configs:
            del self._service_configs[service_id]
            return True
        return False

    def clear_services(self) -> None:
        """Unregister all services."""
        self._service_configs.clear()

    def get_service_config(self, service_id: str) -> ServiceConfig | None:
        """Look up a service configuration."""
        return self._service_configs.get(service_id)

    # =========================================================================
    # Error Rate Management
    # =========================================================================

    def set_error_rate(self, service_id: str, error_rate: float) -> None:
        """Set a service's error rate."""
        self._error_rate_provider.set_error_rate(service_id, error_rate)

    def get_error_rate(self, service_id: str) -> float:
        """Look up a service's error rate."""
        return self._error_rate_provider.get_error_rate(service_id)

    def record_success(self, service_id: str) -> None:
        """Record a success."""
        self._error_rate_provider.record_success(service_id)

    def record_failure(self, service_id: str) -> None:
        """Record a failure."""
        self._error_rate_provider.record_failure(service_id)

    # =========================================================================
    # Critical Services Error Rate
    # =========================================================================

    def _get_critical_services(self) -> list[ServiceConfig]:
        """Look up the list of critical services."""
        return [
            config
            for config in self._service_configs.values()
            if config.criticality == "critical"
        ]

    def get_critical_services_error_rate(self) -> float:
        """Compute the average error rate of critical services."""
        critical_services = self._get_critical_services()
        if not critical_services:
            return 0.0

        total_error_rate = sum(
            self._error_rate_provider.get_error_rate(s.service_id)
            for s in critical_services
        )
        return total_error_rate / len(critical_services)

    # =========================================================================
    # Shedding Evaluation
    # =========================================================================

    def evaluate_shedding(self, service_id: str) -> float:
        """
        Compute the current allowed traffic ratio for a service.

        Args:
            service_id: Service ID

        Returns:
            float: Allowed traffic ratio (0.0 ~ 100.0)
        """
        if not self._policy.enabled:
            return 100.0

        service_config = self.get_service_config(service_id)
        if service_config is None:
            return 100.0

        if service_config.criticality == "critical":
            return 100.0

        critical_error_rate = self.get_critical_services_error_rate()

        applicable_level = self._find_applicable_level(
            critical_error_rate,
            service_config.criticality,
        )

        if applicable_level is None:
            return 100.0

        return max(
            applicable_level.traffic_limit, service_config.min_traffic_percentage
        )

    def _find_applicable_level(
        self,
        critical_error_rate: float,
        service_criticality: str,
    ) -> SheddingLevel | None:
        """Find the Shedding level matching the current error rate and service criticality."""
        for level in sorted(
            self._policy.levels, key=lambda lv: lv.error_rate, reverse=True
        ):
            if (
                critical_error_rate >= level.error_rate
                and service_criticality in level.shed_criticality
            ):
                return level
        return None

    def should_allow_request(self, service_id: str) -> SheddingDecision:
        """
        Decide whether to allow a request.

        Limits traffic probabilistically. E.g., 50% allowed means allowed with 50% probability.
        """
        allowed_percent = self.evaluate_shedding(service_id)
        service_config = self.get_service_config(service_id)

        if allowed_percent >= 100.0:
            return SheddingDecision(
                allow_request=True,
                allowed_traffic_percent=100.0,
                is_shed=False,
                reason="No shedding applied",
                service_criticality=(
                    service_config.criticality if service_config else None
                ),
            )

        if allowed_percent <= 0.0:
            current_level = self._get_current_level_description()
            return SheddingDecision(
                allow_request=False,
                allowed_traffic_percent=0.0,
                is_shed=True,
                reason=f"Fully shed - {current_level}",
                current_level=current_level,
                service_criticality=(
                    service_config.criticality if service_config else None
                ),
            )

        allow = random.random() * 100 < allowed_percent
        current_level = self._get_current_level_description()

        return SheddingDecision(
            allow_request=allow,
            allowed_traffic_percent=allowed_percent,
            is_shed=True,
            reason=(
                f"Probabilistic shedding - {current_level}"
                if not allow
                else "Request allowed"
            ),
            current_level=current_level,
            service_criticality=service_config.criticality if service_config else None,
        )

    def _get_current_level_description(self) -> str:
        """Look up the current Shedding level description."""
        critical_error_rate = self.get_critical_services_error_rate()

        for i, level in enumerate(
            sorted(self._policy.levels, key=lambda lv: lv.error_rate, reverse=True)
        ):
            if critical_error_rate >= level.error_rate:
                return level.description or f"Level {len(self._policy.levels) - i}"

        return "No shedding"

    # =========================================================================
    # Level Management
    # =========================================================================

    def get_current_level_index(self) -> int:
        """Look up the current Shedding level index."""
        critical_error_rate = self.get_critical_services_error_rate()

        for i, level in enumerate(self._policy.levels):
            if critical_error_rate >= level.error_rate:
                highest_index = i
                for j, lvl in enumerate(self._policy.levels[i:], start=i):
                    if critical_error_rate >= lvl.error_rate:
                        highest_index = j
                return highest_index

        return -1

    def update_shedding_state(self) -> SheddingAuditEntry | None:
        """Update the Shedding state and detect level changes."""
        new_level_index = self.get_current_level_index()
        previous_level_index = self._current_level_index

        if new_level_index == previous_level_index:
            return None

        self._current_level_index = new_level_index
        timestamp = utc_now().isoformat()

        if previous_level_index == -1 and new_level_index >= 0:
            event_type = "SHEDDING_ACTIVATED"
            self._activated_at = timestamp
        elif previous_level_index >= 0 and new_level_index == -1:
            event_type = "SHEDDING_DEACTIVATED"
            self._activated_at = None
        else:
            event_type = "SHEDDING_LEVEL_CHANGED"

        affected_services = self._get_affected_services(new_level_index)

        audit_entry = SheddingAuditEntry(
            event_type=event_type,
            timestamp=timestamp,
            previous_level=previous_level_index,
            new_level=new_level_index,
            critical_error_rate=self.get_critical_services_error_rate(),
            affected_services=[s.service_id for s in affected_services],
            reason=f"Critical error rate: {self.get_critical_services_error_rate():.1f}%",
        )

        if self._audit_callback:
            try:
                self._audit_callback(audit_entry)
            except Exception as e:
                logger.exception(
                    "load_shedding_manager.audit_callback_failed",
                    error=e,
                )

        # Publish a Shedding state-change event over the EventBus (Fail-Open)
        self._publish_shedding_event(
            new_level_index=new_level_index,
            previous_level_index=previous_level_index,
            affected_service_ids=audit_entry.affected_services,
        )

        logger.info(
            "load_shedding_manager.level_affected_services",
            event_type=event_type,
            previous_level_index=previous_level_index,
            new_level_index=new_level_index,
            affected_services_count=len(affected_services),
        )

        return audit_entry

    def _publish_shedding_event(
        self,
        new_level_index: int,
        previous_level_index: int,
        affected_service_ids: list[str],
    ) -> None:
        """Publish a Load Shedding state-change event over the EventBus (Fail-Open)."""
        try:
            from baldur.services.event_bus import (
                BaldurEvent,
                EventPriority,
                EventType,
                get_event_bus,
            )

            bus = get_event_bus()

            # Compute traffic_limit: 100.0 when inactive, the level's traffic_limit when active
            if new_level_index < 0 or new_level_index >= len(self._policy.levels):
                traffic_limit = 100.0
            else:
                traffic_limit = self._policy.levels[new_level_index].traffic_limit

            bus.publish(
                BaldurEvent(
                    event_type=EventType.LOAD_SHEDDING_LEVEL_CHANGED,
                    data={
                        "new_level": new_level_index,
                        "previous_level": previous_level_index,
                        "traffic_limit": traffic_limit,
                        "affected_services": affected_service_ids,
                        "critical_error_rate": self.get_critical_services_error_rate(),
                    },
                    source="load_shedding_manager",
                    priority=EventPriority.HIGH,
                )
            )
        except ImportError:
            logger.debug("load_shedding_manager.eventbus_available_shedding_event")
        except Exception as e:
            logger.warning(
                "load_shedding_manager.publish_shedding_event_failed",
                error=e,
            )

    def _get_affected_services(self, level_index: int) -> list[ServiceConfig]:
        """List of services affected at the current level."""
        if level_index < 0 or level_index >= len(self._policy.levels):
            return []

        level = self._policy.levels[level_index]
        return [
            config
            for config in self._service_configs.values()
            if config.criticality in level.shed_criticality and config.shed_priority > 0
        ]

    # =========================================================================
    # Status
    # =========================================================================

    def is_shedding_active(self) -> bool:
        """Whether Shedding is active."""
        return self.get_current_level_index() >= 0

    def get_status(self) -> SheddingStatus:
        """Look up the current Shedding state."""
        level_index = self.get_current_level_index()
        active = level_index >= 0

        if not active:
            state = SheddingState.INACTIVE
        elif level_index == 0:
            state = SheddingState.LEVEL_1
        elif level_index == 1:
            state = SheddingState.LEVEL_2
        elif level_index == 2:
            state = SheddingState.LEVEL_3
        else:
            state = SheddingState.CUSTOM

        level_description = ""
        shed_criticality: list[str] = []
        traffic_limit = 100.0
        if active and level_index < len(self._policy.levels):
            level = self._policy.levels[level_index]
            level_description = level.description
            shed_criticality = level.shed_criticality
            traffic_limit = level.traffic_limit

        shed_services = [
            config.service_id
            for config in self._service_configs.values()
            if config.criticality in shed_criticality and config.shed_priority > 0
        ]

        return SheddingStatus(
            active=active,
            current_state=state,
            current_level_index=level_index,
            current_level_description=level_description,
            critical_error_rate=self.get_critical_services_error_rate(),
            shed_services=shed_services,
            shed_criticality=shed_criticality,
            traffic_limit=traffic_limit,
            timestamp=utc_now().isoformat(),
            activated_at=self._activated_at,
        )

    # =========================================================================
    # Manual Control
    # =========================================================================

    def force_activate(
        self,
        level_index: int = 0,
        reason: str = "manual_activation",
    ) -> bool:
        """Force-activate Shedding (for tests/operations)."""
        if level_index < 0 or level_index >= len(self._policy.levels):
            logger.warning(
                "load_shedding_manager.invalid_level_index",
                level_index=level_index,
            )
            return False

        target_level = self._policy.levels[level_index]
        critical_services = self._get_critical_services()

        for service in critical_services:
            self.set_error_rate(service.service_id, target_level.error_rate + 1.0)

        self.update_shedding_state()

        logger.info(
            "load_shedding_manager.force_activated_level",
            level_index=level_index,
            reason=reason,
        )
        return True

    def force_deactivate(self, reason: str = "manual_deactivation") -> bool:
        """Force-deactivate Shedding."""
        critical_services = self._get_critical_services()

        for service in critical_services:
            self.set_error_rate(service.service_id, 0.0)

        self.update_shedding_state()

        logger.info(
            "load_shedding_manager.force_deactivated",
            reason=reason,
        )
        return True

    def reset(self) -> None:
        """Reset all state (for tests)."""
        self._error_rate_provider.reset()
        self._current_level_index = -1
        self._activated_at = None
        logger.debug("load_shedding_manager.reset_complete")
