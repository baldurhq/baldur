"""BaldurEventBus — pure in-memory pub/sub engine."""

from __future__ import annotations

import contextvars
import threading
import traceback
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, Literal

import structlog

from baldur.core.shutdown_coordinator import ShutdownHandler, TrackedRequest

from .event_types import EventPriority, EventType
from .models import BaldurEvent, EventSubscription

logger = structlog.get_logger()

__all__ = [
    "BaldurEventBus",
    "BaldurEventBusDispatchShutdownHandler",
    "integrate_dispatch_with_shutdown_coordinator",
    "shutdown_dispatch_executor",
]

_FALLBACK_MAX_HISTORY = 1000
_FALLBACK_HANDLER_TIMEOUT = 5.0
_FALLBACK_DISPATCH_MODE: Literal["async_pool"] = "async_pool"
_FALLBACK_DISPATCH_WORKERS = 32

_EXECUTOR_NAME = "baldur-eventbus-dispatch"


class BaldurEventBus:
    """
    Baldur Event Bus — loose coupling between components.

    Thread-safe implementation. Singleton lifecycle managed by get_event_bus()/reset_event_bus().

    Dispatch (487):
        Handler dispatch is governed by ``BALDUR_EVENT_BUS_DISPATCH_MODE``.
        ``async_pool`` (default) submits each handler to a process-shared
        ``ThreadPoolExecutor`` and waits with ``future.result(timeout=)``,
        eliminating the per-emit ``threading.Thread.start()`` cost while
        preserving sync-blocking + sequential per-handler ordering and the
        per-handler timeout contract. ``thread_per_emit`` retains the
        legacy per-call thread path. ``sync`` runs handlers inline on the
        caller thread without timeout enforcement.

    Usage:
        from baldur.services.event_bus.bus import get_event_bus

        bus = get_event_bus()
        bus.subscribe(EventType.EMERGENCY_LEVEL_CHANGED, my_handler)
        bus.publish(BaldurEvent(
            event_type=EventType.EMERGENCY_LEVEL_CHANGED,
            data={"level": 3},
            source="emergency_manager",
        ))
    """

    # Process-shared dispatch executor (DCL singleton — see _get_executor).
    # Mirrors TimeoutPolicy._executor pattern (resilience/policies/timeout.py),
    # itself ported from baldur_pro.services.hedging.executor — Python
    # attribute lookup gives subclasses their own slot per #479's
    # subclass-safety contract.
    _executor: ThreadPoolExecutor | None = None
    _executor_lock = threading.Lock()

    def __init__(self) -> None:
        self._subscriptions: dict[EventType, list[EventSubscription]] = {}
        self._subscription_lock = threading.RLock()
        self._max_history = self._load_max_history()
        self._event_history: deque[dict[str, Any]] = deque(maxlen=self._max_history)
        self._handler_timeout = self._load_handler_timeout()
        self._dispatch_mode = self._load_dispatch_mode()
        self._timeout_count = 0
        self._history_lock = threading.Lock()
        self._enabled = True
        self._handlers_registered = False

    @staticmethod
    def _load_max_history() -> int:
        """Load max history from audit settings, fallback to 1000."""
        try:
            from baldur.settings.audit import get_audit_settings

            return get_audit_settings().event_history_max
        except Exception:
            return _FALLBACK_MAX_HISTORY

    @staticmethod
    def _load_handler_timeout() -> float:
        """Load handler timeout from event bus settings, fallback to 5.0s."""
        try:
            from baldur.settings.event_bus import get_event_bus_settings

            return get_event_bus_settings().handler_timeout_seconds
        except Exception:
            return _FALLBACK_HANDLER_TIMEOUT

    @staticmethod
    def _load_dispatch_mode() -> Literal["sync", "thread_per_emit", "async_pool"]:
        """Load dispatch mode from event bus settings, fallback to async_pool."""
        try:
            from baldur.settings.event_bus import get_event_bus_settings

            return get_event_bus_settings().dispatch_mode
        except Exception:
            return _FALLBACK_DISPATCH_MODE

    @classmethod
    def _get_executor(cls) -> ThreadPoolExecutor:
        """Return the process-shared dispatch executor, lazily constructing on first call.

        Double-checked locking — fast path is an unlocked classvar read.
        Concurrent first-call from N threads results in exactly one
        ``ThreadPoolExecutor`` constructor invocation; the late arrivals
        see the cached instance via the second check inside the lock.
        ``dispatch_workers`` is read once inside the lock and frozen for
        the lifetime of the executor; change requires
        ``shutdown_dispatch_executor()`` (drained by
        ``reset_event_bus_settings()`` and ``reset_protect_caches()``).
        """
        if cls._executor is None:
            with cls._executor_lock:
                if cls._executor is None:
                    try:
                        from baldur.settings.event_bus import (
                            get_event_bus_settings,
                        )

                        max_workers = get_event_bus_settings().dispatch_workers
                    except Exception:
                        max_workers = _FALLBACK_DISPATCH_WORKERS
                    cls._executor = ThreadPoolExecutor(
                        max_workers=max_workers,
                        thread_name_prefix=_EXECUTOR_NAME,
                    )
                    try:
                        from baldur.metrics.recorders.executor import (
                            register_executor,
                        )

                        register_executor(_EXECUTOR_NAME, cls._executor)
                    except Exception:
                        pass
        return cls._executor

    @classmethod
    def shutdown_dispatch_executor(cls) -> None:
        """Drain and clear the process-shared dispatch executor.

        Used by ``reset_protect_caches()`` and ``reset_event_bus_settings()``
        for test isolation, and by ``BaldurEventBusDispatchShutdownHandler``
        for graceful shutdown. ``wait=True`` ensures in-flight handler
        futures complete before the next test starts, preventing
        cross-test races on shared subscribers (Prometheus counters,
        audit hash chain, structured-log binding).

        Idempotent — second call against an already-cleared slot is a
        silent no-op so dual invocation from D3's two reset paths is
        harmless. After shutdown, the next ``_get_executor()`` call
        rebuilds with the current settings.
        """
        with cls._executor_lock:
            if cls._executor is not None:
                cls._executor.shutdown(wait=True)
                try:
                    from baldur.metrics.recorders.executor import (
                        unregister_executor,
                    )

                    unregister_executor(_EXECUTOR_NAME)
                except Exception:
                    pass
                cls._executor = None

    def _execute_handler_with_timeout(
        self,
        handler: Callable[[BaldurEvent], None],
        event: BaldurEvent,
        timeout: float,
        await_result: bool = True,
    ) -> bool:
        """Execute handler with timeout. Returns True if handler completed.

        When ``await_result`` is False the handler is dispatched
        fire-and-forget — the publisher thread never waits on the handler
        body — via ``_dispatch_fire_and_forget`` (see its docstring for the
        per-mode behavior). Otherwise the (awaited) path branches on
        ``self._dispatch_mode``:

        - ``sync``: inline on caller thread, no timeout enforcement.
        - ``thread_per_emit``: legacy per-call ``threading.Thread.start()``
          path. Retained as an escape hatch for environments that need
          bit-identical pre-487 dispatch behavior.
        - ``async_pool``: submit to the shared ``ThreadPoolExecutor``,
          wait with ``future.result(timeout=)``. Preserves
          contextvars / structlog binding via ``contextvars.copy_context``.
          Sequential per-handler iteration in ``publish()`` keeps
          priority-sorted ordering intact.
        """
        if not await_result:
            return self._dispatch_fire_and_forget(handler, event)

        if timeout <= 0 or self._dispatch_mode == "sync":
            handler(event)
            return True

        if self._dispatch_mode == "thread_per_emit":
            return self._execute_handler_thread_per_emit(handler, event, timeout)

        return self._execute_handler_async_pool(handler, event, timeout)

    def _dispatch_fire_and_forget(
        self,
        handler: Callable[[BaldurEvent], None],
        event: BaldurEvent,
    ) -> bool:
        """Dispatch a best-effort handler without awaiting its completion.

        Used for subscriptions registered with ``await_result=False``. The
        publisher (request) thread never blocks on the handler body — even
        when the handler delegates to an inline-executing Celery task
        (``task_always_eager`` / no broker). Branches on
        ``self._dispatch_mode``:

        - ``async_pool`` (default): ``executor.submit(...)`` and return
          immediately without ``future.result()``. ``RuntimeError``
          (executor shut down — graceful-shutdown window) falls back to
          inline execution, mirroring ``_execute_handler_async_pool``.
        - ``thread_per_emit``: start a daemon thread and do not ``join()``.
        - ``sync``: run inline on the caller thread (preserves deterministic
          single-thread behavior for tests that select sync mode).

        Always returns ``True`` — a fire-and-forget handler is counted as
        dispatched and cannot "time out" from the publisher's view, so
        ``_timeout_count`` is never incremented here. Uncaught handler
        exceptions are logged by ``_run_handler_logging_exceptions`` (the
        discarded future cannot surface them otherwise).
        """
        if self._dispatch_mode == "sync":
            self._run_handler_logging_exceptions(handler, event)
            return True

        ctx = contextvars.copy_context()

        if self._dispatch_mode == "thread_per_emit":
            thread = threading.Thread(
                target=ctx.run,
                args=(self._run_handler_logging_exceptions, handler, event),
                daemon=True,
            )
            thread.start()
            return True

        executor = self._get_executor()
        try:
            executor.submit(
                ctx.run, self._run_handler_logging_exceptions, handler, event
            )
        except RuntimeError:
            self._run_handler_logging_exceptions(handler, event)
        return True

    @staticmethod
    def _run_handler_logging_exceptions(
        handler: Callable[[BaldurEvent], None],
        event: BaldurEvent,
    ) -> None:
        """Run a fire-and-forget handler, logging any uncaught exception.

        The fire-and-forget dispatch path discards the future, so a handler
        exception has no ``future.result()`` to surface it. This wrapper
        logs it as ``event_bus.fire_and_forget_handler_failed`` so a failing
        best-effort handler stays observable. Handlers also keep their own
        try/except for graceful degradation.
        """
        try:
            handler(event)
        except Exception as e:
            logger.exception(
                "event_bus.fire_and_forget_handler_failed",
                handler=getattr(handler, "__name__", str(handler)),
                event_type=event.event_type.value,
                event_id=event.event_id,
                error=e,
            )

    def _execute_handler_thread_per_emit(
        self,
        handler: Callable[[BaldurEvent], None],
        event: BaldurEvent,
        timeout: float,
    ) -> bool:
        """Legacy per-call ``threading.Thread.start()`` dispatch (pre-487 body)."""
        exc_holder: list[BaseException | None] = [None]
        ctx = contextvars.copy_context()

        def _run() -> None:
            try:
                ctx.run(handler, event)
            except BaseException as e:
                exc_holder[0] = e

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=timeout)

        if thread.is_alive():
            self._timeout_count += 1
            logger.warning(
                "event_bus.handler_timeout",
                handler=getattr(handler, "__name__", str(handler)),
                event_type=event.event_type.value,
                event_id=event.event_id,
                timeout_seconds=timeout,
            )
            return False

        if exc_holder[0] is not None:
            raise exc_holder[0]

        return True

    def _execute_handler_async_pool(
        self,
        handler: Callable[[BaldurEvent], None],
        event: BaldurEvent,
        timeout: float,
    ) -> bool:
        """Shared ``ThreadPoolExecutor`` dispatch with ``future.result(timeout=)``.

        On ``RuntimeError`` (executor shut down between submit attempts —
        graceful-shutdown window), falls back to inline execution so the
        in-flight emit completes without log noise. The post-shutdown
        caller is past the deadline, so the timeout policy is moot in
        that path.
        """
        executor = self._get_executor()
        ctx = contextvars.copy_context()
        try:
            future = executor.submit(ctx.run, handler, event)
        except RuntimeError:
            handler(event)
            return True

        try:
            future.result(timeout=timeout)
            return True
        except FuturesTimeoutError:
            future.cancel()
            self._timeout_count += 1
            logger.warning(
                "event_bus.handler_timeout",
                handler=getattr(handler, "__name__", str(handler)),
                event_type=event.event_type.value,
                event_id=event.event_id,
                timeout_seconds=timeout,
            )
            return False

    # -------------------------------------------------------------------------
    # Subscription Management
    # -------------------------------------------------------------------------

    def subscribe(
        self,
        event_type: EventType,
        handler: Callable[[BaldurEvent], None],
        priority: EventPriority = EventPriority.NORMAL,
        *,
        await_result: bool = True,
    ) -> EventSubscription:
        """
        Subscribe a handler to an event type.

        Args:
            event_type: Event type to subscribe to
            handler: Event handler function
            priority: Handler priority (higher executes first)
            await_result: When False, the handler is dispatched
                fire-and-forget — ``publish()`` returns without waiting on
                the handler body, so the publisher thread is never blocked.
                Use for best-effort side effects (notification, snapshot,
                replay, post-mortem). Keep the default True for handlers
                that write ``event.data`` consumed by later handlers.

        Returns:
            EventSubscription: Subscription info
        """
        handler_name = getattr(handler, "__name__", str(handler))

        subscription = EventSubscription(
            event_type=event_type,
            handler=handler,
            handler_name=handler_name,
            priority=priority,
            await_result=await_result,
        )

        with self._subscription_lock:
            if event_type not in self._subscriptions:
                self._subscriptions[event_type] = []

            # Prevent duplicate subscriptions
            existing = [
                s
                for s in self._subscriptions[event_type]
                if s.handler_name == handler_name
            ]
            if not existing:
                self._subscriptions[event_type].append(subscription)
                # Sort by priority (highest first)
                self._subscriptions[event_type].sort(
                    key=lambda s: s.priority.value,
                    reverse=True,
                )
                logger.debug(
                    "event_bus.handler_subscribed",
                    handler_name=handler_name,
                    event_type=event_type.value,
                    priority=priority.name,
                )
            else:
                logger.debug(
                    "event_bus.handler_already_subscribed",
                    handler_name=handler_name,
                    event_type=event_type.value,
                )
                return existing[0]

        return subscription

    def unsubscribe(
        self,
        event_type: EventType,
        handler: Callable[[BaldurEvent], None],
    ) -> bool:
        """
        Unsubscribe a handler from an event type.

        Args:
            event_type: Event type to unsubscribe from
            handler: Handler function

        Returns:
            bool: Whether unsubscription was successful
        """
        handler_name = getattr(handler, "__name__", str(handler))

        with self._subscription_lock:
            if event_type not in self._subscriptions:
                return False

            original_count = len(self._subscriptions[event_type])
            self._subscriptions[event_type] = [
                s
                for s in self._subscriptions[event_type]
                if s.handler_name != handler_name
            ]

            removed = original_count > len(self._subscriptions[event_type])
            if removed:
                logger.debug(
                    "event_bus.unsubscribed",
                    handler_name=handler_name,
                    event_type=event_type.value,
                )

            return removed

    def subscribe_all(
        self,
        handler: Callable[[BaldurEvent], None],
        priority: EventPriority = EventPriority.LOW,
    ) -> list[EventSubscription]:
        """Wildcard subscription for all EventType members.

        Used by modules that need to observe all events, such as Correlation Engine.
        Automatically enumerates all EventType enum members and subscribes individually.

        Args:
            handler: Event handler function
            priority: Handler priority (default LOW — runs after existing handlers)

        Returns:
            list[EventSubscription]: List of created subscriptions
        """
        subscriptions = []
        for event_type in EventType:
            sub = self.subscribe(event_type, handler, priority)
            subscriptions.append(sub)
        logger.debug(
            "event_bus.wildcard_subscription_all_event",
            handler_name=getattr(handler, "__name__", str(handler)),
            subscriptions_count=len(subscriptions),
            priority=priority.name,
        )
        return subscriptions

    def unsubscribe_all(self, event_type: EventType | None = None):
        """
        Unsubscribe all handlers.

        Args:
            event_type: Specific event type to clear (None clears all)
        """
        with self._subscription_lock:
            if event_type is None:
                self._subscriptions.clear()
                logger.info("event_bus.all_subscriptions_cleared")
            elif event_type in self._subscriptions:
                del self._subscriptions[event_type]
                logger.info(
                    "event_bus.subscriptions_cleared",
                    event_type=event_type.value,
                )

    # -------------------------------------------------------------------------
    # Event Publishing
    # -------------------------------------------------------------------------

    def publish(self, event: BaldurEvent) -> int:
        """
        Publish an event.

        Args:
            event: Event to publish

        Returns:
            int: Number of handlers called
        """
        if not self._enabled:
            logger.debug(
                "event_bus.event_bus_disabled_ignoring",
                event_type=event.event_type.value,
            )
            return 0

        # Record to history
        self._record_event(event)

        handlers_called = 0

        with self._subscription_lock:
            subscriptions = self._subscriptions.get(event.event_type, [])
            if not subscriptions:
                logger.debug(
                    "event_bus.no_subscribers",
                    event_type=event.event_type.value,
                )
                return 0

            # Work with a copy (prevent subscription changes during execution)
            subscriptions = list(subscriptions)

        for subscription in subscriptions:
            if not subscription.enabled:
                continue

            try:
                completed = self._execute_handler_with_timeout(
                    subscription.handler,
                    event,
                    self._handler_timeout,
                    await_result=subscription.await_result,
                )
                if completed:
                    handlers_called += 1
                    logger.debug(
                        "event_bus.handler_executed",
                        subscription=subscription.handler_name,
                        event_type=event.event_type.value,
                    )
            except Exception as e:
                logger.exception(
                    "event_bus.handler_failed",
                    subscription=subscription.handler_name,
                    event_type=event.event_type.value,
                    error=e,
                    traceback=traceback.format_exc(),
                )

        logger.debug(
            "event_bus.event_published",
            event_type=event.event_type.value,
            event_source=event.source,
            handlers_called=handlers_called,
        )

        return handlers_called

    def emit(
        self,
        event_type: EventType,
        data: dict[str, Any],
        source: str = "unknown",
        priority: EventPriority = EventPriority.NORMAL,
        correlation_id: str | None = None,
    ) -> int:
        """
        Convenience event publishing.

        Args:
            event_type: Event type
            data: Event data
            source: Event source
            priority: Priority
            correlation_id: Correlation ID

        Returns:
            int: Number of handlers called
        """
        from .models import create_event

        event = create_event(event_type, data, source, priority, correlation_id)
        return self.publish(event)

    # -------------------------------------------------------------------------
    # Event History
    # -------------------------------------------------------------------------

    def _record_event(self, event: BaldurEvent):
        """Record event to history."""
        with self._history_lock:
            self._event_history.append(event.to_dict())

    def get_history(
        self,
        event_type: EventType | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        Retrieve event history.

        Args:
            event_type: Event type to filter by
            limit: Maximum number of entries to retrieve

        Returns:
            List[Dict]: Event history
        """
        with self._history_lock:
            history = list(self._event_history)

        if event_type:
            history = [e for e in history if e["event_type"] == event_type.value]

        return history[-limit:]

    def clear_history(self):
        """Clear event history."""
        with self._history_lock:
            self._event_history.clear()

    # -------------------------------------------------------------------------
    # Control
    # -------------------------------------------------------------------------

    def enable(self):
        """Enable the event bus."""
        self._enabled = True
        logger.info("event_bus.enabled")

    def disable(self):
        """Disable the event bus."""
        self._enabled = False
        logger.info("event_bus.disabled")

    def is_enabled(self) -> bool:
        """Check whether the event bus is enabled."""
        return self._enabled

    # -------------------------------------------------------------------------
    # Statistics
    # -------------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Get event bus statistics."""
        with self._subscription_lock:
            subscriptions_count = sum(
                len(subs) for subs in self._subscriptions.values()
            )
            event_types_with_subs = len(self._subscriptions)

        with self._history_lock:
            history_count = len(self._event_history)

        return {
            "enabled": self._enabled,
            "subscriptions_count": subscriptions_count,
            "event_types_with_subscribers": event_types_with_subs,
            "history_count": history_count,
            "max_history": self._max_history,
            "handler_timeouts": self._timeout_count,
        }

    def get_subscriptions(
        self,
        event_type: EventType | None = None,
    ) -> list[dict[str, Any]]:
        """Get subscription information."""
        with self._subscription_lock:
            if event_type:
                subs = self._subscriptions.get(event_type, [])
            else:
                subs = [s for subs in self._subscriptions.values() for s in subs]

            return [
                {
                    "event_type": s.event_type.value,
                    "handler_name": s.handler_name,
                    "priority": s.priority.name,
                    "enabled": s.enabled,
                    "await_result": s.await_result,
                }
                for s in subs
            ]

    # -------------------------------------------------------------------------
    # Reset (Testing)
    # -------------------------------------------------------------------------

    def reset(self):
        """Reset state (for testing)."""
        with self._subscription_lock:
            self._subscriptions.clear()
        with self._history_lock:
            self._event_history.clear()
        self._enabled = True
        self._handlers_registered = False
        self._timeout_count = 0
        logger.info("event_bus.reset_defaults")


# =============================================================================
# Module-level shim for re-export through bus/__init__.py
# =============================================================================


def shutdown_dispatch_executor() -> None:
    """Module-level alias for ``BaldurEventBus.shutdown_dispatch_executor``.

    Re-exported through ``bus/__init__.py`` so ``protect.py`` and
    ``bootstrap.py`` consumers can drain the dispatch executor without
    crossing the ``bus/`` private boundary.
    """
    BaldurEventBus.shutdown_dispatch_executor()


# =============================================================================
# Graceful Shutdown Integration (487 D4)
# =============================================================================


class BaldurEventBusDispatchShutdownHandler(ShutdownHandler):
    """Drain the ``BaldurEventBus`` dispatch executor on graceful shutdown.

    Mirrors the minimalist ``AuditShutdownHandler`` pattern: ``on_shutdown_start``
    is a pass (the executor keeps accepting submits — the brief shutdown
    window where ``ThreadPoolExecutor.submit`` raises ``RuntimeError`` is
    handled by ``BaldurEventBus._execute_handler_async_pool``'s
    ``RuntimeError`` fallback to inline execution); ``is_drain_complete``
    inherits the ABC default ``True``; ``on_drain_complete`` synchronously
    drains via ``shutdown(wait=True)``; ``on_force_shutdown`` calls
    ``shutdown(wait=False)`` for the timeout-bypass path.
    """

    def on_shutdown_start(self) -> None:
        pass

    def on_drain_complete(self) -> None:
        try:
            BaldurEventBus.shutdown_dispatch_executor()
            logger.info("event_bus.dispatch_shutdown_drained")
        except Exception as e:
            logger.warning("event_bus.dispatch_shutdown_drain_failed", error=e)

    def on_force_shutdown(self, pending_requests: list[TrackedRequest]) -> None:
        try:
            with BaldurEventBus._executor_lock:
                executor = BaldurEventBus._executor
                if executor is not None:
                    executor.shutdown(wait=False)
                    try:
                        from baldur.metrics.recorders.executor import (
                            unregister_executor,
                        )

                        unregister_executor(_EXECUTOR_NAME)
                    except Exception:
                        pass
                    BaldurEventBus._executor = None
            logger.warning("event_bus.dispatch_shutdown_forced")
        except Exception as e:
            logger.warning("event_bus.dispatch_shutdown_force_failed", error=e)


def integrate_dispatch_with_shutdown_coordinator() -> (
    BaldurEventBusDispatchShutdownHandler
):
    """Factory for shutdown integration (pattern: redis_bus.integrate_with_shutdown_coordinator).

    Returns the handler unconditionally. The dispatch executor is a
    process-shared class-level singleton, so the handler does not need
    an instance to bind to (unlike ``RedisEventBusShutdownHandler``,
    which holds a per-bus listener thread reference).

    Usage (application bootstrap — see ``bootstrap.py:_register_shutdown_handlers``):
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator

        coordinator = get_shutdown_coordinator()
        handler = integrate_dispatch_with_shutdown_coordinator()
        coordinator.register_handler(handler)
    """
    return BaldurEventBusDispatchShutdownHandler()
