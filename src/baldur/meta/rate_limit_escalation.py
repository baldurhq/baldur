"""
Rate Limit escalation handler.

Sends PagerDuty/Slack notifications when the consecutive-429 threshold is reached.

Features:
- Subscribes to RATE_LIMIT_429 events
- Decides escalation based on the consecutive-429 count
- Prevents duplicate escalation
- Resets state after recovery
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from baldur.meta.escalation import (
    EscalationEvent,
    EscalationLevel,
)

if TYPE_CHECKING:
    from baldur.meta.escalation import EscalationManager
    from baldur.services.event_bus.bus import BaldurEvent

logger = structlog.get_logger()

# Escalation trigger threshold (consecutive-429 count)
ESCALATION_THRESHOLD_CONSECUTIVE_429S = 10


class RateLimitEscalationHandler:
    """
    Rate Limit 429 escalation handler.

    Subscribes to RATE_LIMIT_429 events on the EventBus, and
    escalates to PagerDuty when consecutive_429s exceeds the threshold.

    Usage example:
        handler = RateLimitEscalationHandler()
        handler.subscribe()  # start EventBus subscription

        # reset state after recovery
        handler.reset_escalation("payment_api")
    """

    def __init__(
        self,
        escalation_manager: EscalationManager | None = None,
        threshold: int = ESCALATION_THRESHOLD_CONSECUTIVE_429S,
    ):
        """
        Initialize.

        Args:
            escalation_manager: an existing EscalationManager instance (created if None)
            threshold: escalation trigger threshold (consecutive-429 count)
        """
        self._escalation_manager = escalation_manager
        self._threshold = threshold
        self._escalated_keys: set[str] = set()  # prevent duplicate escalation
        self._subscribed = False

    def _get_escalation_manager(self) -> EscalationManager:
        """Lazy-initialize the EscalationManager."""
        if self._escalation_manager is None:
            from baldur.meta.escalation import get_escalation_manager

            self._escalation_manager = get_escalation_manager()
        return self._escalation_manager

    def subscribe(self) -> bool:
        """
        Subscribe to RATE_LIMIT_429 events on the EventBus.

        Returns:
            Whether the subscription succeeded
        """
        if self._subscribed:
            return True
        try:
            from baldur.services.event_bus import EventType, get_event_bus

            bus = get_event_bus()
            bus.subscribe(
                EventType.RATE_LIMIT_429,
                self._handle_rate_limit_429,
            )
            self._subscribed = True
            logger.info(
                "rate_limit_escalation_handler.subscribed",
                threshold=self._threshold,
            )
            return True
        except ImportError:
            logger.debug("rate_limit_escalation_handler.eventbus_available")
            return False
        except Exception as e:
            logger.warning(
                "rate_limit_escalation_handler.subscribe_failed",
                error=e,
            )
            return False

    def close(self) -> None:
        """Unsubscribe EventBus handler."""
        if not self._subscribed:
            return
        try:
            from baldur.services.event_bus import EventType, get_event_bus

            bus = get_event_bus()
            bus.unsubscribe(EventType.RATE_LIMIT_429, self._handle_rate_limit_429)
            self._subscribed = False
        except ImportError:
            pass
        except Exception:
            pass

    def _handle_rate_limit_429(self, event: BaldurEvent) -> None:
        """
        Handle a 429 event - escalate when the threshold is exceeded.

        Args:
            event: BaldurEvent with data dict (key, consecutive_429s, cooldown_until, etc.)
        """
        event_data = event.data if hasattr(event, "data") else {}
        key = event_data.get("key", "unknown")
        consecutive = event_data.get("consecutive_429s", 0)

        # Ignore if below the threshold
        if consecutive < self._threshold:
            logger.debug(
                "rate_limit_escalation_handler.skipping_escalation",
                escalation_key=key,
                consecutive=consecutive,
                threshold=self._threshold,
            )
            return

        # Prevent duplicates if already escalated
        if key in self._escalated_keys:
            logger.debug(
                "rate_limit_escalation_handler.already_escalated_skipping_duplicate",
                escalation_key=key,
            )
            return

        # Mark as escalated (prevent duplicates)
        self._escalated_keys.add(key)

        # Create the escalation event
        escalation_event = EscalationEvent(
            level=EscalationLevel.CRITICAL,
            title=f"Rate Limit Critical: {key}",
            description=(
                f"External API '{key}' returned {consecutive} consecutive 429 responses. "
                f"The system has switched to defensive mode. "
                f"Check the external API status and take action."
            ),
            component="rate_limit_coordinator",
            details={
                "key": key,
                "consecutive_429s": consecutive,
                "threshold": self._threshold,
                "cooldown_until": event_data.get("cooldown_until"),
                "calculated_delay": event_data.get("calculated_delay"),
            },
        )

        # Execute the escalation
        manager = self._get_escalation_manager()
        result = manager.escalate(escalation_event)

        if result.success:
            logger.critical(
                "rate_limit_escalation_handler.escalated",
                escalation_key=key,
                consecutive=consecutive,
                channels_sent=result.channels_sent,
            )
        else:
            logger.error(
                "rate_limit_escalation_handler.escalation_failed",
                escalation_key=key,
                result_error=result.error_message,
            )

    def reset_escalation(self, key: str) -> None:
        """
        Reset escalation state (call after recovery).

        Args:
            key: Rate limit key
        """
        if key in self._escalated_keys:
            self._escalated_keys.discard(key)
            logger.info(
                "rate_limit_escalation_handler.reset_escalation",
                escalation_key=key,
            )

    def reset_all_escalations(self) -> None:
        """Reset all escalation state."""
        count = len(self._escalated_keys)
        self._escalated_keys.clear()
        logger.info(
            "rate_limit_escalation_handler.reset_all_escalations_keys",
            escalated_keys_count=count,
        )

    @property
    def escalated_keys(self) -> frozenset[str]:
        """List of currently escalated keys (read-only)."""
        return frozenset(self._escalated_keys)

    @property
    def threshold(self) -> int:
        """Escalation threshold."""
        return self._threshold


from baldur.utils.singleton import CLEANUP_CLOSE, make_singleton_factory

(
    get_rate_limit_escalation_handler,
    configure_rate_limit_escalation_handler,
    reset_rate_limit_escalation_handler,
) = make_singleton_factory(
    "rate_limit_escalation_handler",
    RateLimitEscalationHandler,
    cleanup_fn=CLEANUP_CLOSE,
)

__all__ = [
    "ESCALATION_THRESHOLD_CONSECUTIVE_429S",
    "RateLimitEscalationHandler",
    "configure_rate_limit_escalation_handler",
    "get_rate_limit_escalation_handler",
    "reset_rate_limit_escalation_handler",
]
