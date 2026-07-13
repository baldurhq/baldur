"""
Freeze Mode for Circuit Breaker

Freezes the current CB states as-is in the LOCKDOWN state.

Freeze Mode behavior:
- Automatic OPEN   → ❌ forbidden
- Automatic CLOSE  → ❌ forbidden
- Canary Recovery → ❌ forbidden
- Manual OPEN   → ✅ allowed (explicit operator intervention)
- Manual CLOSE  → ✅ allowed (explicit operator intervention)
- Currently OPEN   → stays OPEN
- Currently CLOSED → stays CLOSED

Design decisions:
- Full disable: ❌ (if it never CLOSEs, blocking is permanent)
- Forbid OPEN only: ❌ (automatic recovery may induce load)
- Freeze Mode: ✅ (keep current state, maximum stability)
"""

from __future__ import annotations

import threading

import structlog

from baldur.audit.helpers import log_freeze_mode_audit
from baldur.services.circuit_breaker.models import FreezeModeState
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# =============================================================================
# Freeze Mode State Change Reasons
# =============================================================================


class FreezeReason:
    """Freeze Mode activation/deactivation reason constants."""

    LOCKDOWN_ENTRY = "Freeze Mode activated due to LOCKDOWN entry"
    LOCKDOWN_EXIT = "Freeze Mode deactivated due to LOCKDOWN exit"
    PANIC_THRESHOLD = "Freeze Mode activated due to Panic Threshold trigger"
    MANUAL_ACTIVATION = "Manual activation by operator"
    MANUAL_DEACTIVATION = "Manual deactivation by operator"
    EMERGENCY_LEVEL_3 = "Freeze Mode activated due to Emergency Level 3 entry"


# =============================================================================
# Freeze Mode Manager
# =============================================================================


class FreezeModeManager:
    """
    Circuit Breaker Freeze Mode manager.

    Forbids all automatic CB state changes in the LOCKDOWN state and
    freezes the current state as-is.

    Usage:
        manager = FreezeModeManager()

        # Check Freeze Mode state
        if manager.is_active():
            return  # automatic state change forbidden

        # Activate Freeze Mode (on LOCKDOWN entry)
        manager.activate(reason="LOCKDOWN entry")

        # Check whether a state change is allowed
        allowed, reason = manager.should_allow_state_change(
            service_id="payment-api",
            new_state="OPEN",
            is_manual=False
        )

    Reference:
        docs/baldur/middleware_system/21_CB_ADVANCED_PROTECTION.md
        Section 6 - LOCKDOWN Freeze Mode
    """

    _instance: FreezeModeManager | None = None

    def __new__(cls):
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance for test isolation."""
        cls._instance = None

    def __init__(self):
        if getattr(self, "_initialized", False):
            return

        self._state = FreezeModeState()
        self._emergency_manager = None
        self._initialized = True

    @property
    def emergency_manager(self):
        """Lazy-load the Emergency Manager."""
        if self._emergency_manager is None:
            from baldur.factory.registry import ProviderRegistry

            self._emergency_manager = ProviderRegistry.emergency_manager.safe_get()
            if self._emergency_manager is None:
                logger.debug("freeze_mode.emergencymodemanager_available")
        return self._emergency_manager

    def is_active(self) -> bool:
        """
        Whether Freeze Mode is active.

        Returns:
            bool: Freeze Mode active state
        """
        # Automatically active when at Emergency Level 3 (LOCKDOWN)
        if self._is_lockdown():
            return True
        return self._state.active

    def _is_lockdown(self) -> bool:
        """Check whether the current Emergency Level is LOCKDOWN (Level 3)."""
        if self.emergency_manager is None:
            return False

        try:
            level = self.emergency_manager.get_current_level()
            level_value = level.value if hasattr(level, "value") else int(level)
            return level_value >= 3  # LEVEL_3 = LOCKDOWN
        except Exception:
            return False

    def get_state(self) -> FreezeModeState:
        """
        Return the current Freeze Mode state.

        Returns:
            FreezeModeState: current state
        """
        return FreezeModeState(
            active=self.is_active(),
            activated_at=self._state.activated_at,
            reason=self._state.reason
            or (FreezeReason.LOCKDOWN_ENTRY if self._is_lockdown() else ""),
            activated_by=self._state.activated_by
            or ("system" if self._is_lockdown() else ""),
        )

    def activate(
        self,
        reason: str = FreezeReason.MANUAL_ACTIVATION,
        activated_by: str = "system",
    ) -> bool:
        """
        Activate Freeze Mode.

        Args:
            reason: Activation reason
            activated_by: Who activates it ("system" or "operator:<username>")

        Returns:
            bool: Whether activation succeeded
        """
        previous_state = self._state.active

        self._state = FreezeModeState(
            active=True,
            activated_at=utc_now().isoformat(),
            reason=reason,
            activated_by=activated_by,
        )

        logger.warning(
            "freeze_mode.activated",
            activated_by=activated_by,
            reason=reason,
        )

        # Audit
        log_freeze_mode_audit(
            active=True,
            reason=reason,
            activated_by=activated_by,
            previous_state=previous_state,
            emergency_level=self._get_emergency_level_str(),
        )

        return True

    def deactivate(
        self,
        reason: str = FreezeReason.MANUAL_DEACTIVATION,
        deactivated_by: str = "system",
    ) -> bool:
        """
        Deactivate Freeze Mode.

        Note: Manual deactivation is not possible in the LOCKDOWN state.
              The Emergency Level must be lowered first.

        Args:
            reason: Deactivation reason
            deactivated_by: Who deactivates it

        Returns:
            bool: Whether deactivation succeeded
        """
        # Manual deactivation is not possible in the LOCKDOWN state
        if self._is_lockdown():
            logger.warning("freeze_mode.cannot_deactivate_during_lockdown")
            return False

        previous_state = self._state.active

        self._state = FreezeModeState(
            active=False,
            activated_at=None,
            reason="",
            activated_by="",
        )

        logger.info(
            "freeze_mode.deactivated",
            deactivated_by=deactivated_by,
            reason=reason,
        )

        # Audit
        log_freeze_mode_audit(
            active=False,
            reason=reason,
            activated_by=deactivated_by,
            previous_state=previous_state,
            emergency_level=self._get_emergency_level_str(),
        )

        return True

    def _get_emergency_level_str(self) -> str | None:
        """Return the current Emergency Level string."""
        if self.emergency_manager is None:
            return None
        try:
            level = self.emergency_manager.get_current_level()
            return level.value if hasattr(level, "value") else str(level)
        except Exception:
            return None

    def should_allow_state_change(
        self,
        service_id: str,
        new_state: str,
        is_manual: bool = False,
    ) -> tuple[bool, str]:
        """
        Decide whether a CB state change is allowed.

        Only manual operations are allowed in Freeze Mode.

        Args:
            service_id: Target service ID
            new_state: New state (OPEN, CLOSED, HALF_OPEN)
            is_manual: Whether this is a manual operation

        Returns:
            Tuple[bool, str]: (whether allowed, reason if denied)
        """
        if not self.is_active():
            return True, ""

        # Manual operations are allowed in Freeze Mode
        if is_manual:
            logger.info(
                "freeze_mode.manual_override_allowed",
                service_id=service_id,
                new_state=new_state,
            )
            return True, ""

        # Automatic operations are forbidden
        reason = (
            f"LOCKDOWN: Freeze Mode active - automatic state change to {new_state} "
            f"blocked for {service_id}. Use manual override."
        )
        logger.warning(
            "freeze_mode.event",
            reason=reason,
        )

        return False, reason

    def should_allow_auto_open(self, service_id: str) -> tuple[bool, str]:
        """
        Whether automatic OPEN is allowed.

        Args:
            service_id: Target service ID

        Returns:
            Tuple[bool, str]: (whether allowed, reason if denied)
        """
        return self.should_allow_state_change(
            service_id=service_id,
            new_state="OPEN",
            is_manual=False,
        )

    def should_allow_auto_close(self, service_id: str) -> tuple[bool, str]:
        """
        Whether automatic CLOSE is allowed.

        Args:
            service_id: Target service ID

        Returns:
            Tuple[bool, str]: (whether allowed, reason if denied)
        """
        return self.should_allow_state_change(
            service_id=service_id,
            new_state="CLOSED",
            is_manual=False,
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
    FreezeModeManager._instance = None


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
    is_manual: bool = False,
) -> bool:
    """
    Convenience check for whether a CB state change is allowed.

    Args:
        service_id: Target service ID
        new_state: New state
        is_manual: Whether this is a manual operation

    Returns:
        bool: Whether allowed
    """
    allowed, _ = get_freeze_mode_manager().should_allow_state_change(
        service_id=service_id,
        new_state=new_state,
        is_manual=is_manual,
    )
    return allowed
