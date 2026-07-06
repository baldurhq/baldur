"""
CapacityReservationService — Capacity Reservation service singleton.

A background scheduler periodically checks the EventCalendar and automatically
runs warm-up/cooldown. It checks the Safety Valve every cycle and immediately
transitions to CRITICAL when the hard limit is exceeded.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import structlog

from baldur.services.capacity_reservation.event_calendar import (
    EventCalendar,
    EventStatus,
    ScheduledEvent,
)
from baldur.services.capacity_reservation.pre_warmer import (
    PreWarmer,
    SafetyValveMetricsProvider,
)
from baldur.settings.capacity_reservation import (
    CapacityReservationSettings,
    get_capacity_reservation_settings,
)

if TYPE_CHECKING:
    from baldur.meta.daemon_worker import (  # noqa: F401
        DaemonWorkerHandle,
    )

logger = structlog.get_logger()


class CapacityReservationService:
    """Capacity Reservation service — singleton."""

    _instance: CapacityReservationService | None = None
    _initialized: bool = False
    _singleton_lock = threading.Lock()

    def __new__(cls) -> CapacityReservationService:
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def initialize(
        self,
        rate_controller: Any | None = None,
        pool_watchdog: Any | None = None,
        bulkhead: Any | None = None,
        graceful_degradation: Any | None = None,
        event_bus: Any | None = None,
        metrics_provider: SafetyValveMetricsProvider | None = None,
        recovery_gate: Any | None = None,
        state_backend: Any | None = None,
        settings: CapacityReservationSettings | None = None,
    ) -> None:
        """Initialize. Create EventCalendar + PreWarmer, restore from StateBackend."""
        if self._initialized:
            return

        self._settings = settings or get_capacity_reservation_settings()
        self._calendar = EventCalendar(
            state_backend=state_backend,
            settings=self._settings,
        )
        self._pre_warmer = PreWarmer(
            calendar=self._calendar,
            rate_controller=rate_controller,
            pool_watchdog=pool_watchdog,
            bulkhead=bulkhead,
            graceful_degradation=graceful_degradation,
            event_bus=event_bus,
            metrics_provider=metrics_provider,
            recovery_gate=recovery_gate,
            state_backend=state_backend,
            settings=self._settings,
        )
        self._scheduler_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._initialized = True
        self._handle: DaemonWorkerHandle | None = None  # impl 489 D9

        self._calendar.initialize()
        self._pre_warmer.initialize()

        logger.info(
            "capacity_reservation.service_initialized",
            enabled=self._settings.enabled,
            dry_run=self._settings.dry_run,
        )

    def register_event(self, event: ScheduledEvent) -> None:
        """Register an event + validate."""
        self._ensure_initialized()

        active_count = len(self._calendar.get_active())
        if active_count >= self._settings.max_concurrent_events:
            raise ValueError(
                f"Concurrent active event limit exceeded: "
                f"{active_count}/{self._settings.max_concurrent_events}"
            )

        self._calendar.register(event)

    def cancel_event(self, event_id: str) -> bool:
        """Cancel an event. Includes rollback if warm-up is in progress."""
        self._ensure_initialized()

        event = self._calendar.get_event(event_id)
        if event is None:
            return False

        if event.status in (EventStatus.WARMING, EventStatus.ACTIVE):
            self._pre_warmer.cool_down(event)

        return self._calendar.cancel(event_id)

    def get_status(self) -> dict:
        """Look up the current status."""
        self._ensure_initialized()

        return {
            "enabled": self._settings.enabled,
            "dry_run": self._settings.dry_run,
            "scheduler_running": (
                self._scheduler_thread is not None and self._scheduler_thread.is_alive()
            ),
            "active_events": [
                {
                    "event_id": e.event_id,
                    "name": e.name,
                    "status": e.status.value,
                    "start_time": e.start_time.isoformat(),
                    "end_time": e.end_time.isoformat(),
                }
                for e in self._calendar.get_active()
            ],
            "active_adjustments": self._pre_warmer.get_active_adjustments(),
            "safety_valve_active": self._pre_warmer.safety_valve_active,
        }

    @property
    def calendar(self) -> EventCalendar:
        """Access the EventCalendar."""
        self._ensure_initialized()
        return self._calendar

    @property
    def pre_warmer(self) -> PreWarmer:
        """Access the PreWarmer."""
        self._ensure_initialized()
        return self._pre_warmer

    def start(self) -> None:
        """Start the scheduler (background thread)."""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        self._ensure_initialized()

        if not self._settings.enabled:
            logger.info("capacity_reservation.service_disabled")
            return

        if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
            logger.warning("capacity_reservation.scheduler_already_running")
            return

        self._stop_event.clear()
        self._spawn_scheduler_thread()
        assert self._scheduler_thread is not None  # spawn always sets non-None
        self._handle = DaemonWorkerHandle(
            thread=self._scheduler_thread,
            tick_interval_seconds=self._settings.scheduler_interval_seconds,
            restart_callback=self._spawn_scheduler_thread,
        )
        register_daemon_worker("capacity-reservation-scheduler", self._handle)

        logger.info(
            "capacity_reservation.scheduler_started",
            interval_seconds=self._settings.scheduler_interval_seconds,
        )

    def _spawn_scheduler_thread(self) -> None:
        """Construct + start a fresh scheduler thread (impl 489 D9)."""
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop_with_crash_capture,
            name="capacity-reservation-scheduler",
            daemon=True,
        )
        self._scheduler_thread.start()
        if self._handle is not None:
            self._handle.thread = self._scheduler_thread

    def _scheduler_loop_with_crash_capture(self) -> None:
        try:
            self._scheduler_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def stop(self) -> None:
        """Stop the scheduler + cooldown in-progress events."""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        self._ensure_initialized()

        if self._handle is not None:
            self._handle.is_stopping = True
        self._stop_event.set()

        if self._scheduler_thread is not None:
            from baldur.settings.thread_management import (
                get_thread_management_settings,
            )

            timeout = get_thread_management_settings().join_timeout_long
            self._scheduler_thread.join(timeout=timeout)
            unregister_daemon_worker("capacity-reservation-scheduler")
            if self._scheduler_thread.is_alive():
                logger.critical(
                    "daemon_worker.stop_join_timeout",
                    worker_name="capacity-reservation-scheduler",
                    join_timeout_seconds=timeout,
                )
            self._scheduler_thread = None

        for event in self._calendar.get_active():
            self._pre_warmer.cool_down(event)
            self._calendar.update_status(event.event_id, EventStatus.COMPLETED)

        logger.info("capacity_reservation.scheduler_stopped")

    def _scheduler_loop(self) -> None:
        """Scheduler main loop."""
        import time as _time

        while not self._stop_event.is_set():
            iter_start = _time.monotonic()
            try:
                self._process_events()
                self._check_safety_valve()
            except Exception as exc:
                logger.exception(
                    "capacity_reservation.scheduler_error",
                    error=str(exc),
                )

            if self._handle is not None:
                self._handle.observe_iteration(_time.monotonic() - iter_start)
                self._handle.heartbeat()

            self._stop_event.wait(self._settings.scheduler_interval_seconds)

    def _process_events(self) -> None:
        """Process events that need warm-up/cooldown."""
        for event in self._calendar.get_needs_warmup():
            self._calendar.update_status(event.event_id, EventStatus.WARMING)
            result = self._pre_warmer.warm_up(event)
            if result.success:
                self._calendar.update_status(event.event_id, EventStatus.ACTIVE)
            else:
                self._calendar.update_status(event.event_id, EventStatus.CANCELLED)
                logger.error(
                    "capacity_reservation.warmup_failed",
                    event_id=event.event_id,
                    errors=result.errors,
                )

        for event in self._calendar.get_needs_cooldown():
            self._calendar.update_status(event.event_id, EventStatus.COOLING_DOWN)
            self._pre_warmer.cool_down(event)
            self._calendar.update_status(event.event_id, EventStatus.COMPLETED)

        self._calendar.remove_completed()

    def _check_safety_valve(self) -> None:
        """Check the Safety Valve every cycle."""
        if not self._calendar.is_event_period():
            return

        if self._pre_warmer.safety_valve_active:
            self._pre_warmer.check_safety_valve_recovery()
        elif self._pre_warmer.check_safety_valve():
            self._pre_warmer.emergency_override()

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError(
                "CapacityReservationService not initialized. Call initialize() first."
            )

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (for tests)."""
        with cls._singleton_lock:
            if cls._instance is not None and cls._instance._initialized:
                cls._instance._stop_event.set()
                if (
                    cls._instance._scheduler_thread is not None
                    and cls._instance._scheduler_thread.is_alive()
                ):
                    from baldur.settings.thread_management import (
                        get_thread_management_settings,
                    )

                    cls._instance._scheduler_thread.join(
                        timeout=get_thread_management_settings().join_timeout
                    )
            cls._instance = None
