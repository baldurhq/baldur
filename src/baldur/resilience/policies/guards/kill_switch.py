"""
Kill Switch Guard — system-wide enabled/disabled state check.

Wraps SystemControlManager's is_enabled() to test for a global block before the
PolicyComposer pipeline runs.

Fail-open principle: if SystemControlManager fails to import or call, the
request passes.
"""

from __future__ import annotations

import structlog

from baldur.interfaces.resilience_policy import (
    GuardResult,
    PolicyContext,
)

logger = structlog.get_logger()


class KillSwitchGuard:
    """Kill Switch guard — wraps SystemControlManager.

    Checks global state only; context is ignored. Rejects execution when
    SystemControlManager.is_enabled() is False.
    """

    @property
    def name(self) -> str:
        """Guard identifier."""
        return "kill_switch"

    def check(self, context: PolicyContext | None = None) -> GuardResult:
        """
        Check the global Kill Switch state.

        context is ignored (global on/off only). If SystemControlManager fails
        to import or call, this is fail-open (the request passes).
        """
        try:
            from baldur.services.system_control import get_system_control

            mgr = get_system_control()
            if not mgr.is_enabled():
                return GuardResult(
                    allowed=False,
                    reason="System kill switch is disabled",
                )
        except ImportError:
            logger.debug(
                "guard.dependency_missing",
                guard_name="kill_switch",
                dependency="baldur.services.system_control",
            )
        except Exception as e:
            logger.warning(
                "guard.check_failed_fail_open",
                guard_name="kill_switch",
                check="is_enabled",
                error=str(e),
                exc_info=True,
            )

        return GuardResult(allowed=True)
