"""
EventBus Hook — relays EventBus CONFIG_UPDATED events into Policy config
refresh.

Used alongside PolicyComposer to propagate runtime configuration changes to
Policies. In an environment without an EventBus it does nothing (fail-open).

HedgingConfigUpdateHook currently performs the same job; this module is placed
here as the general-purpose EventBus Hook extension point.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.resilience_policy import PolicyResult

if TYPE_CHECKING:
    from baldur.interfaces.resilience_policy import PolicyContext

logger = structlog.get_logger()


class EventBusHook:
    """General-purpose Hook observing EventBus events as a PolicyComposer Hook.

    Publishes pipeline success/failure/rejection events to the EventBus. In an
    environment without an EventBus it does nothing.
    """

    def __init__(self, event_prefix: str = "policy_pipeline") -> None:
        """
        Args:
            event_prefix: prefix for the EventBus event key.
        """
        self._event_prefix = event_prefix
        self._bus: Any = None
        self._initialized = False

    def _ensure_bus(self) -> bool:
        """Lazily initialize the EventBus. Returns False if unavailable."""
        if self._initialized:
            return self._bus is not None

        self._initialized = True
        try:
            from baldur.services.event_bus import get_event_bus

            self._bus = get_event_bus()
            return True
        except ImportError:
            logger.debug("event_bus.not_available")
            return False
        except Exception as e:
            logger.warning(
                "eventbus.initialization_failed",
                error=e,
            )
            return False

    def on_execute(
        self, policy_name: str, attempt: int, context: PolicyContext | None = None
    ) -> None:
        """Execution start."""

    def on_success(
        self,
        policy_name: str,
        result: PolicyResult,
        context: PolicyContext | None = None,
    ) -> None:
        """On pipeline success, publish an event to the EventBus."""
        if not self._ensure_bus():
            return

        try:
            self._bus.publish(
                f"{self._event_prefix}.success",
                {
                    "policies": result.executed_policies,
                    "attempts": result.total_attempts,
                    "duration_ms": result.total_duration_ms,
                },
            )
        except Exception as e:
            logger.debug(
                "eventbus.publish_failed_fail",
                error=e,
            )

    def on_failure(
        self,
        policy_name: str,
        error: Exception,
        attempt: int,
        context: PolicyContext | None = None,
    ) -> None:
        """On pipeline failure, publish an event to the EventBus."""
        if not self._ensure_bus():
            return

        try:
            self._bus.publish(
                f"{self._event_prefix}.failure",
                {
                    "error_type": type(error).__name__,
                    "error_message": str(error)[:500],
                    "attempts": attempt,
                },
            )
        except Exception as e:
            logger.debug(
                "eventbus.publish_failed_fail",
                error=e,
            )

    def on_retry(
        self,
        policy_name: str,
        attempt: int,
        delay: float,
        context: PolicyContext | None = None,
    ) -> None:
        """Retry — unused at the Composer level."""

    def on_reject(
        self, guard_name: str, reason: str, context: PolicyContext | None = None
    ) -> None:
        """On pipeline rejection, publish an event to the EventBus."""
        if not self._ensure_bus():
            return

        try:
            self._bus.publish(
                f"{self._event_prefix}.rejected",
                {
                    "guard": guard_name,
                    "reason": reason,
                },
            )
        except Exception as e:
            logger.debug(
                "eventbus.publish_failed_fail",
                error=e,
            )
