"""
System Control Service

Global kill-switch and system-state management for the Baldur system.

Features:
- Thread-safe state management
- Pluggable backends (File, Redis, Memory)
- Automatic state recovery on server restart
- State sharing across multiple servers (when using the Redis backend)

Configuration:
    # Django settings.py
    BALDUR_SYSTEM_CONTROL_BACKEND = "redis"  # or "file" (default)
    BALDUR_REDIS_URL = "redis://localhost:6379/0"

    # Or environment variables
    BALDUR_SYSTEM_CONTROL_BACKEND=redis
    BALDUR_REDIS_URL=redis://localhost:6379/0
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import structlog

from baldur.audit.helpers import log_system_control_audit
from baldur.core.serializable import SerializableMixin
from baldur.core.state_backend import StateBackend, get_state_backend
from baldur.utils.time import utc_now

try:
    from baldur.metrics.recorders.system_control import (
        record_sc_disabled,
        record_sc_disabled_duration,
        record_sc_state_change,
        set_sc_dry_run,
        set_sc_enabled,
    )
except ImportError:

    def set_sc_enabled(enabled: bool) -> None:
        return None

    def set_sc_dry_run(dry_run: bool) -> None:
        return None

    def record_sc_state_change(action: str) -> None:
        return None

    def record_sc_disabled_duration(duration: float) -> None:
        return None

    def record_sc_disabled() -> None:
        return None


logger = structlog.get_logger()


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class SystemState(SerializableMixin):
    """Baldur system state."""

    enabled: bool = True
    dry_run: bool = False  # Dry run mode: observe only, no actual actions
    disabled_at: str | None = None
    disabled_by: str | None = None
    disabled_reason: str | None = None
    enabled_at: str | None = None
    enabled_by: str | None = None
    dry_run_enabled_at: str | None = None
    dry_run_enabled_by: str | None = None


# State key for backend storage
STATE_KEY = "system_control"


# =============================================================================
# System Control Manager
# =============================================================================


class SystemControlManager:
    """
    Manages global baldur system state with pluggable backend.

    Features:
    - Thread-safe state management
    - Pluggable backends (File, Redis, Memory)
    - Automatic state recovery on restart
    - Shared state across servers (with Redis backend)

    Usage:
        manager = SystemControlManager()

        # Check if system is enabled
        if manager.is_enabled():
            do_healing()

        # Disable system (Kill Switch)
        manager.disable(reason="Emergency maintenance", actor="admin")

        # Re-enable system
        manager.enable(actor="admin")

    Configuration:
        # Django settings.py
        BALDUR_SYSTEM_CONTROL_BACKEND = "redis"  # or "file"
        BALDUR_REDIS_URL = "redis://localhost:6379/0"
    """

    _instance: SystemControlManager | None = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._state_lock = threading.Lock()
        self._backend: StateBackend = get_state_backend()
        self._load_state()
        self._initialized = True

    def _load_state(self) -> None:
        """Load state from backend."""
        try:
            data = self._backend.get(STATE_KEY)
            if data:
                self._cached_state = SystemState.from_dict(data)
                set_sc_enabled(self._cached_state.enabled)
                set_sc_dry_run(self._cached_state.dry_run)
                logger.info(
                    "system_control.loaded_state",
                    _self=self._cached_state.enabled,
                    value=type(self._backend).__name__,
                )
            else:
                self._cached_state = SystemState()
                logger.info("system_control.no_existing_state_using")
        except Exception as e:
            logger.warning(
                "system_control.load_state",
                error=e,
            )
            self._cached_state = SystemState()

    def _save_state(self) -> None:
        """Save state to backend."""
        try:
            self._backend.set(STATE_KEY, self._cached_state.to_dict())
            logger.debug("system_control.state_saved")
        except Exception as e:
            logger.exception(
                "system_control.save_state_failed",
                error=e,
            )

    def _refresh_state(self) -> SystemState:
        """Refresh state from backend (for multi-server sync)."""
        data = self._backend.get(STATE_KEY)
        if data:
            self._cached_state = SystemState.from_dict(data)
        return self._cached_state

    def _log_audit(
        self,
        action: str,
        actor: str,
        old_state: dict,
        new_state: dict,
        reason: str,
    ) -> None:
        """
        Record a system-control change in the Audit log.

        Fail-Open principle: an Audit failure does not stop system-control logic.
        """
        log_system_control_audit(
            action=action,
            actor=actor,
            old_state=old_state,
            new_state=new_state,
            reason=reason,
        )

    def is_enabled(self) -> bool:
        """
        Check if baldur system is enabled.

        Note: For Redis backend, this reads from cache for performance.
        Use get_state() for fresh read from backend.
        """
        with self._state_lock:
            return self._cached_state.enabled

    def get_state(self, refresh: bool = True) -> SystemState:
        """
        Get current system state.

        Args:
            refresh: If True, refresh from backend (for multi-server sync)
        """
        with self._state_lock:
            if refresh:
                self._refresh_state()
            return SystemState.from_dict(self._cached_state.to_dict())

    def enable(self, actor: str = "system", reason: str = "") -> SystemState:
        """Enable baldur system."""
        with self._state_lock:
            # Refresh first for multi-server consistency
            self._refresh_state()
            old_state = self._cached_state.to_dict()
            was_enabled = self._cached_state.enabled

            self._cached_state.enabled = True
            self._cached_state.enabled_at = utc_now().isoformat()
            self._cached_state.enabled_by = actor
            self._save_state()

            set_sc_enabled(True)
            record_sc_state_change("enable")
            if not was_enabled and self._cached_state.disabled_at:
                try:
                    from baldur.utils.time import from_iso_string

                    disabled_at = from_iso_string(self._cached_state.disabled_at)
                    record_sc_disabled_duration(
                        (utc_now() - disabled_at).total_seconds()
                    )
                except (ValueError, TypeError):
                    logger.warning(
                        "system_control.disabled_duration_parse_failed",
                        disabled_at=self._cached_state.disabled_at,
                    )

            new_state = self._cached_state.to_dict()

            if not was_enabled:
                logger.info(
                    "system_control.system_enabled_reason",
                    actor=actor,
                    value=reason or "N/A",
                )
                # Audit record
                self._log_audit("enable", actor, old_state, new_state, reason)

            return SystemState.from_dict(self._cached_state.to_dict())

    def disable(self, actor: str = "system", reason: str = "") -> SystemState:
        """
        Disable baldur system (Kill Switch).

        This immediately stops all baldur operations.
        State is persisted and shared across servers (with Redis backend).
        """
        with self._state_lock:
            # Refresh first for multi-server consistency
            self._refresh_state()
            old_state = self._cached_state.to_dict()
            was_enabled = self._cached_state.enabled

            self._cached_state.enabled = False
            self._cached_state.disabled_at = utc_now().isoformat()
            self._cached_state.disabled_by = actor
            self._cached_state.disabled_reason = reason
            self._save_state()

            set_sc_enabled(False)
            record_sc_state_change("disable")
            record_sc_disabled()

            new_state = self._cached_state.to_dict()

            if was_enabled:
                logger.warning(
                    "system_control.system_disabled_kill_switch",
                    actor=actor,
                    value=reason or "N/A",
                )
                # Audit record
                self._log_audit("disable", actor, old_state, new_state, reason)

            return SystemState.from_dict(self._cached_state.to_dict())

    def enable_dry_run(self, actor: str = "system") -> SystemState:
        """
        Enable dry run mode.

        In dry run mode:
        - All baldur logic executes normally
        - But actual actions (circuit breaking, retries, DLQ writes) are skipped
        - Actions that "would have been taken" are logged instead

        Use this to safely test baldur on production traffic.
        """
        with self._state_lock:
            self._refresh_state()
            old_state = self._cached_state.to_dict()
            was_dry_run = self._cached_state.dry_run

            self._cached_state.dry_run = True
            self._cached_state.dry_run_enabled_at = utc_now().isoformat()
            self._cached_state.dry_run_enabled_by = actor
            self._save_state()

            set_sc_dry_run(True)
            record_sc_state_change("enable_dry_run")

            new_state = self._cached_state.to_dict()

            if not was_dry_run:
                logger.info(
                    "system_control.dry_run_mode_enabled",
                    actor=actor,
                )
                # Audit record
                self._log_audit(
                    "enable_dry_run", actor, old_state, new_state, "dry_run_mode"
                )

            return SystemState.from_dict(self._cached_state.to_dict())

    def disable_dry_run(self, actor: str = "system") -> SystemState:
        """
        Disable dry run mode (go live).

        After disabling dry run, all baldur actions will be executed for real.
        """
        with self._state_lock:
            self._refresh_state()
            old_state = self._cached_state.to_dict()
            was_dry_run = self._cached_state.dry_run

            self._cached_state.dry_run = False
            self._save_state()

            set_sc_dry_run(False)
            record_sc_state_change("disable_dry_run")

            new_state = self._cached_state.to_dict()

            if was_dry_run:
                logger.warning(
                    "system_control.dry_run_mode_disabled",
                    actor=actor,
                )
                # Audit record
                self._log_audit(
                    "disable_dry_run", actor, old_state, new_state, "go_live"
                )

            return SystemState.from_dict(self._cached_state.to_dict())

    def is_dry_run(self) -> bool:
        """Check if dry run mode is enabled."""
        with self._state_lock:
            return self._cached_state.dry_run

    def reset(self) -> None:
        """Reset to default state (enabled)."""
        with self._state_lock:
            old_state = self._cached_state.to_dict()
            self._cached_state = SystemState()
            self._save_state()

            set_sc_enabled(True)
            set_sc_dry_run(False)
            record_sc_state_change("reset")

            new_state = self._cached_state.to_dict()
            logger.info("system_control.system_state_reset_defaults")
            # Audit record
            self._log_audit(
                "reset", "system", old_state, new_state, "reset_to_defaults"
            )

    def get_backend_info(self) -> dict[str, str]:
        """Get information about the current backend."""
        return {
            "backend_type": type(self._backend).__name__,
            "backend_class": f"{type(self._backend).__module__}.{type(self._backend).__name__}",
        }


# =============================================================================
# Singleton & Factory Functions
# =============================================================================


def _cleanup_system_control(ctrl: SystemControlManager) -> None:
    SystemControlManager._instance = None
    ctrl.reset()


from baldur.utils.singleton import make_singleton_factory

get_system_control, configure_system_control, reset_system_control = (
    make_singleton_factory(
        "system_control",
        SystemControlManager,
        cleanup_fn=_cleanup_system_control,
    )
)


def is_baldur_enabled() -> bool:
    """
    Quick check if baldur is enabled.

    Use this at the start of any baldur operation:

        from baldur.services.system_control import is_baldur_enabled

        def my_healing_function():
            if not is_baldur_enabled():
                return  # Kill switch is active

            # ... healing logic
    """
    return get_system_control().is_enabled()


def is_dry_run() -> bool:
    """
    Quick check if dry run mode is enabled.

    Use this before taking any action:

        from baldur.services.system_control import is_dry_run

        def trigger_circuit_breaker(service_name):
            if is_dry_run():
                logger.info(
                    "dry_run_open_circuit",
                    service_name=service_name,
                )
                return

            # Actually open the circuit breaker
            circuit_breaker.open(service_name)
    """
    return get_system_control().is_dry_run()


__all__ = [
    "SystemState",
    "SystemControlManager",
    "get_system_control",
    "configure_system_control",
    "reset_system_control",
    "is_baldur_enabled",
    "is_dry_run",
]
