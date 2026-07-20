"""
LoadSheddingGuard — priority-based Load Shedding Guard.

Extracts TrafficGate's load-shedding check
(CascadeLoadShedding.should_accept) into a PolicyComposer Guard.

ThrottlePolicy has no need to know a request's priority. Priority-based
rejection is decided here, reading context.extra["priority"].

Fail-open principle:
    If CascadeLoadShedding fails to import or call, the request passes.

Usage::

    from baldur.resilience.policies.guards.load_shedding import (
        LoadSheddingGuard,
    )
    policy.add_guard(LoadSheddingGuard())
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.interfaces.resilience_policy import (
    GuardResult,
    PolicyContext,
)

logger = structlog.get_logger()


class LoadSheddingGuard:
    """
    Priority-based Load Shedding Guard.

    Wraps CascadeLoadShedding.should_accept(priority=priority) to decide whether
    a request of that priority is admitted at the current shedding level.

    Reads the request priority from context.extra["priority"]. If context is
    None or no priority is given, the request passes.
    """

    def __init__(self, load_shedding: Any | None = None) -> None:
        """
        Initialize.

        Args:
            load_shedding: a CascadeLoadShedding instance. If None, the global
                instance is obtained via lazy import.
        """
        self._load_shedding = load_shedding
        self._initialized = load_shedding is not None

    @property
    def name(self) -> str:
        """Guard identifier."""
        return "load_shedding"

    def check(self, context: PolicyContext | None = None) -> GuardResult:
        """
        Priority-based load shedding check.

        Reads the request priority from context.extra["priority"]. If context is
        None the check is global (priority=0, request passes).

        Returns:
            GuardResult(allowed=True) — passed
            GuardResult(allowed=False, reason=...) — shedding rejection
        """
        shedding = self._get_load_shedding()
        if shedding is None:
            return GuardResult(allowed=True)

        priority = 0
        if context and context.extra:
            priority = context.extra.get("priority", 0)

        try:
            result = shedding.should_accept(priority=priority)
            if isinstance(result, dict) and not result.get("accepted", True):
                return GuardResult(
                    allowed=False,
                    reason=f"load_shedding_rejected:priority={priority}",
                    metadata={"priority": priority},
                )
        except Exception as e:
            logger.warning(
                "guard.check_failed_fail_open",
                guard_name="load_shedding",
                check="should_accept",
                error=str(e),
                exc_info=True,
            )

        return GuardResult(allowed=True)

    def _get_load_shedding(self) -> Any | None:
        """Obtain the CascadeLoadShedding instance (lazy import, fail-open)."""
        if self._initialized:
            return self._load_shedding

        try:
            from baldur.audit.cascade_load_shedding import (
                get_cascade_load_shedding,
            )

            self._load_shedding = get_cascade_load_shedding()
            self._initialized = True
            return self._load_shedding
        except ImportError:
            logger.debug(
                "guard.dependency_missing",
                guard_name="load_shedding",
                dependency="baldur.audit.cascade_load_shedding",
            )
            self._initialized = True
            return None
        except Exception as e:
            logger.warning(
                "guard.check_failed_fail_open",
                guard_name="load_shedding",
                check="controller_init",
                error=str(e),
                exc_info=True,
            )
            self._initialized = True
            return None
