"""
Freeze Mode for Circuit Breaker

Freeze Mode is the circuit breaker's own view of Emergency Level 3: while the
system is in LOCKDOWN, breakers hold whatever state they are in and no
automatic transition is decided.

Freeze Mode behavior:
- Automatic OPEN   -> forbidden
- Automatic CLOSE  -> forbidden
- Canary Recovery  -> forbidden
- Manual OPEN      -> allowed (explicit operator intervention)
- Manual CLOSE     -> allowed (explicit operator intervention)
- Currently OPEN   -> stays OPEN
- Currently CLOSED -> stays CLOSED

Design decisions:
- Full disable: no (if it never CLOSEs, blocking is permanent)
- Forbid OPEN only: no (automatic recovery may induce load)
- Freeze Mode: yes (keep current state, maximum stability)

It holds no state of its own. There is nothing to activate or deactivate: the
emergency level is the single writer, it is shared across processes through
the state backend, and every worker derives the same verdict from it. A
process-local flag would have frozen exactly one worker.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import structlog

from baldur.models.emergency import EmergencyLevel
from baldur.services.circuit_breaker.models import FreezeModeState

if TYPE_CHECKING:
    from baldur.interfaces.emergency import EmergencyManager

logger = structlog.get_logger()

__all__ = [
    "FreezeModeManager",
    "FreezeReason",
    "get_freeze_mode_manager",
    "reset_freeze_mode_manager",
    "is_freeze_mode_active",
    "should_allow_cb_state_change",
]

# The level at and above which breakers hold their state.
FREEZE_LEVEL = EmergencyLevel.LEVEL_3


# =============================================================================
# Freeze Mode State Change Reasons
# =============================================================================


class FreezeReason:
    """Freeze Mode reason constants."""

    LOCKDOWN_ENTRY = "Freeze Mode activated due to LOCKDOWN entry"


# =============================================================================
# PRO presence probe - resolved once per process
# =============================================================================

_pro_installed: bool | None = None
_pro_installed_lock = threading.Lock()


def _pro_distribution_present() -> bool:
    """Whether the PRO distribution is importable, answered from a cache.

    The gate is consulted on the request path at every automatic transition
    site, and the underlying probe is an ``importlib.util.find_spec`` call --
    boot-only everywhere else in the tree. Packaging cannot change under a
    running process, so the verdict is resolved once and reused.
    """
    global _pro_installed
    if _pro_installed is None:
        with _pro_installed_lock:
            if _pro_installed is None:
                from baldur.utils.tier import is_pro_installed

                _pro_installed = is_pro_installed()
    return _pro_installed


# =============================================================================
# Freeze Mode Manager
# =============================================================================


class FreezeModeManager:
    """
    Circuit Breaker Freeze Mode manager.

    A derived view, not a store: Freeze Mode is active exactly while the
    registered emergency manager reports a level at or above LEVEL_3.

    Usage:
        manager = get_freeze_mode_manager()

        if manager.is_active():
            return  # automatic state change forbidden

        allowed, reason = manager.should_allow_state_change(
            service_id="payment-api",
            new_state="OPEN",
        )
    """

    def __init__(self, emergency_manager: EmergencyManager | None = None):
        """
        Initialize FreezeModeManager.

        Args:
            emergency_manager: Emergency manager (resolved from the provider
                registry when not injected)
        """
        self._emergency_manager: EmergencyManager | None = emergency_manager

    @property
    def emergency_manager(self) -> EmergencyManager | None:
        """Resolve the Emergency Manager, caching the first success.

        Two short-circuits sit in front of the registry lookup because this
        runs on the request path. An OSS-only install can never have the slot
        filled, and on a PRO install whose services have not registered
        ``safe_get()`` constructs and catches an exception per call -- the
        empty-slot listing answers the same question from a dict-keys copy.
        """
        if self._emergency_manager is not None:
            return self._emergency_manager

        if not _pro_distribution_present():
            return None

        from baldur.factory.registry import ProviderRegistry

        if not ProviderRegistry.emergency_manager.list_providers():
            return None

        self._emergency_manager = ProviderRegistry.emergency_manager.safe_get()
        if self._emergency_manager is None:
            logger.debug("freeze_mode.emergency_manager_unavailable")
        return self._emergency_manager

    def is_active(self) -> bool:
        """
        Whether Freeze Mode is active.

        Returns:
            bool: Freeze Mode active state
        """
        return self._is_lockdown()

    def _is_lockdown(self) -> bool:
        """Whether the current Emergency Level is LOCKDOWN (Level 3).

        Compares by the enum's own severity ordering. The level is a
        ``(str, Enum)`` whose value is ``"level_3"``, so any numeric reading
        of it is a bug, not a fallback.

        Every failure resolves to "not frozen", which is the safe direction
        for the circuit breaker: an unavailable breaker lets requests through.
        """
        manager = self.emergency_manager
        if manager is None:
            return False

        try:
            level = manager.get_current_level()
        except Exception as e:
            logger.warning("freeze_mode.lockdown_check_failed", error=str(e))
            return False

        # The Protocol types the return as Any; anything that is not the
        # ordered enum cannot be compared and is not evidence of a lockdown.
        return isinstance(level, EmergencyLevel) and level >= FREEZE_LEVEL

    def get_state(self) -> FreezeModeState:
        """
        Return the current Freeze Mode state.

        Returns:
            FreezeModeState: current state, derived from the emergency level
        """
        if not self._is_lockdown():
            return FreezeModeState()

        return FreezeModeState(
            active=True,
            activated_at=self._emergency_activated_at(),
            reason=FreezeReason.LOCKDOWN_ENTRY,
            activated_by="system",
        )

    def _emergency_activated_at(self) -> str | None:
        """Return the emergency state's activation timestamp, if it exposes one."""
        manager = self.emergency_manager
        if manager is None:
            return None
        try:
            state: Any = manager.get_state()
        except Exception as e:
            logger.warning("freeze_mode.lockdown_check_failed", error=str(e))
            return None
        return getattr(state, "activated_at", None)

    def should_allow_state_change(
        self,
        service_id: str,
        new_state: str,
    ) -> tuple[bool, str]:
        """
        Decide whether an automatic CB state change is allowed.

        Only automatic transitions consult this gate -- the manual paths
        (force open/close, manual control, reset) never reach it, because
        operator intent outranks the freeze by design.

        Nothing is logged on a block: one gate site re-consults on every
        request to a frozen OPEN circuit past its recovery timeout, so the
        caller owns the log level.

        Args:
            service_id: Target service ID
            new_state: New state (OPEN, CLOSED, HALF_OPEN)

        Returns:
            Tuple[bool, str]: (whether allowed, reason if denied)
        """
        if not self.is_active():
            return True, ""

        return False, (
            f"LOCKDOWN: Freeze Mode active - automatic state change to {new_state} "
            f"blocked for {service_id}. Use manual override."
        )


# =============================================================================
# Convenience Functions
# =============================================================================


_manager_instance: FreezeModeManager | None = None
_manager_instance_lock = threading.Lock()


def get_freeze_mode_manager() -> FreezeModeManager:
    """
    Return the global FreezeModeManager instance.
    """
    global _manager_instance
    if _manager_instance is None:
        with _manager_instance_lock:
            if _manager_instance is None:
                _manager_instance = FreezeModeManager()
    return _manager_instance


def reset_freeze_mode_manager() -> None:
    """Reset singleton instance for test isolation."""
    global _manager_instance
    _manager_instance = None


def is_freeze_mode_active() -> bool:
    """
    Convenience check for whether Freeze Mode is active.

    Returns:
        bool: Freeze Mode active state
    """
    return get_freeze_mode_manager().is_active()


def should_allow_cb_state_change(
    service_id: str,
    new_state: str,
) -> bool:
    """
    Convenience check for whether an automatic CB state change is allowed.

    Args:
        service_id: Target service ID
        new_state: New state

    Returns:
        bool: Whether allowed
    """
    allowed, _ = get_freeze_mode_manager().should_allow_state_change(
        service_id=service_id,
        new_state=new_state,
    )
    return allowed
