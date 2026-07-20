"""
BackpressureGuard — RateController-based Backpressure Guard.

Extracts RateController, TrafficGate's third component, into a PolicyComposer
Guard.

Its algorithm and purpose differ entirely from ThrottlePolicy
(SlidingWindowThrottle):
    - SlidingWindowThrottle: limits outbound API calls (protects the service),
      sliding window
    - RateController: prevents internal queue overload (backpressure),
      token bucket + AIMD

Fail-open principle:
    If RateController is not installed or the call fails, the request passes.

Usage::

    from baldur.resilience.policies.guards.backpressure import (
        BackpressureGuard,
    )
    policy.add_guard(BackpressureGuard())
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.interfaces.resilience_policy import (
    GuardResult,
    PolicyContext,
)

logger = structlog.get_logger()


class BackpressureGuard:
    """
    RateController-based Backpressure Guard.

    Wraps token-bucket + AIMD queue-based backpressure as a Guard. Rejects when
    RateController.should_process() returns False.

    Independent of ThrottlePolicy; registered on PolicyComposer via add_guard().
    """

    def __init__(self, rate_controller: Any | None = None) -> None:
        """
        Initialize.

        Args:
            rate_controller: a RateController instance. If None, the global
                instance is obtained via lazy import.
        """
        self._controller = rate_controller
        self._initialized = rate_controller is not None

    @property
    def name(self) -> str:
        """Guard identifier."""
        return "backpressure"

    def check(self, context: PolicyContext | None = None) -> GuardResult:
        """
        Backpressure check.

        Returns a queue-overload rejection when
        RateController.should_process() is False.

        Returns:
            GuardResult(allowed=True) — processing allowed
            GuardResult(allowed=False, reason=...) — backpressure rejection
        """
        controller = self._get_rate_controller()
        if controller is None:
            return GuardResult(allowed=True)

        try:
            if not controller.should_process():
                state = controller.get_state()
                level_value = (
                    state.level.value
                    if hasattr(state.level, "value")
                    else str(state.level)
                )
                return GuardResult(
                    allowed=False,
                    reason=f"backpressure:level={level_value}",
                    metadata={
                        "backpressure_level": level_value,
                        "queue_size": state.queue_size,
                        "current_rate": state.current_rate,
                    },
                )
        except Exception as e:
            logger.warning(
                "guard.check_failed_fail_open",
                guard_name="backpressure",
                check="should_process",
                error=str(e),
                exc_info=True,
            )

        return GuardResult(allowed=True)

    def _get_rate_controller(self) -> Any | None:
        """Obtain the RateController instance (lazy import, fail-open)."""
        if self._initialized:
            return self._controller

        try:
            from baldur.scaling.rate_controller import get_rate_controller

            self._controller = get_rate_controller()
            self._initialized = True
            return self._controller
        except ImportError:
            logger.debug(
                "guard.dependency_missing",
                guard_name="backpressure",
                dependency="baldur.scaling.rate_controller",
            )
            self._initialized = True
            return None
        except Exception as e:
            logger.warning(
                "guard.check_failed_fail_open",
                guard_name="backpressure",
                check="controller_init",
                error=str(e),
                exc_info=True,
            )
            self._initialized = True
            return None
