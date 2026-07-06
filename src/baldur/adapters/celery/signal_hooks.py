"""
Celery Signal Hooks for Baldur System — Thin Orchestrator.

Connects/disconnects handler classes to Celery signals and manages the
signal lifecycle.  All business logic lives in handlers/ and integrations/.

Usage:
    from baldur.adapters.celery.signal_hooks import setup_baldur_signals
    setup_baldur_signals()
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from baldur.adapters.celery.handlers.actor_context_handler import (
        ActorContextHandler,
    )
from celery.signals import (
    before_task_publish,
    task_failure,
    task_postrun,
    task_prerun,
    task_retry,
    task_success,
)

from baldur.adapters.celery.handlers.causation_handler import (
    CausationHandler,
)
from baldur.adapters.celery.handlers.failure_handler import FailureHandler
from baldur.adapters.celery.handlers.retry_handler import RetryHandler
from baldur.adapters.celery.handlers.success_handler import SuccessHandler
from baldur.adapters.celery.handlers.trace_context_handler import (
    TraceContextHandler,
)
from baldur.adapters.celery.signal_config import (
    SignalHooksSettings,
    get_signal_hooks_settings,
    reset_signal_hooks_settings,
)
from baldur.adapters.celery.signal_handlers import (
    connect_setup_logging_handler,
    disconnect_setup_logging_handler,
)

__all__ = [
    "SignalHooksSettings",
    "disconnect_baldur_signals",
    "get_signal_hooks_settings",
    "is_signals_connected",
    "reset_signal_hooks_settings",
    "setup_baldur_signals",
]

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
_signals_connected: bool = False
_setup_lock = threading.Lock()

# Handler instances (created on setup, cleared on disconnect)
_failure_handler: FailureHandler | None = None
_success_handler: SuccessHandler | None = None
_retry_handler: RetryHandler | None = None
_causation_handler: CausationHandler | None = None
_actor_context_handler: ActorContextHandler | None = None
_trace_handler: TraceContextHandler | None = None


# ---------------------------------------------------------------------------
# Setup / Teardown
# ---------------------------------------------------------------------------


def setup_baldur_signals(  # noqa: C901
    app: Any = None,
    enabled: bool | None = None,
    cb_enabled: bool | None = None,
    dlq_enabled: bool | None = None,
    metrics_enabled: bool | None = None,
    forensics_enabled: bool | None = None,
    excluded_tasks: list[str] | None = None,
    task_domain_mapping: dict[str, str] | None = None,
) -> None:
    """
    Setup baldur signal hooks for Celery.

    Creates handler instances and connects them to Celery signals.
    Call this once during application initialization.

    Args:
        app: Celery application instance (optional). When provided, registers
            BaldurContextError in dont_autoretry_for to prevent infinite retries.
        enabled: Master switch for all hooks (default: True)
        cb_enabled: Enable circuit breaker recording
        dlq_enabled: Enable DLQ storage
        metrics_enabled: Enable metrics recording
        forensics_enabled: Enable forensic context capture
        excluded_tasks: List of task names to exclude from processing
        task_domain_mapping: Dict mapping task names to domains

    Example:
        from celery import Celery
        from baldur.adapters.celery.signal_hooks import setup_baldur_signals

        app = Celery('myapp')
        setup_baldur_signals(
            app=app,
            task_domain_mapping={
                'myapp.tasks.process_payment': 'payment',
            },
        )
    """
    with _setup_lock:
        global _signals_connected
        global _failure_handler, _success_handler, _retry_handler
        global _causation_handler, _actor_context_handler, _trace_handler

        if _signals_connected:
            logger.warning("baldur.signal_hooks_already_connected")
            return

        # Build / retrieve config and apply overrides
        config = get_signal_hooks_settings()

        if enabled is not None:
            config = config.model_copy(update={"enabled": enabled})
        if cb_enabled is not None:
            config = config.model_copy(update={"cb_enabled": cb_enabled})
        if dlq_enabled is not None:
            config = config.model_copy(update={"dlq_enabled": dlq_enabled})
        if metrics_enabled is not None:
            config = config.model_copy(update={"metrics_enabled": metrics_enabled})
        if forensics_enabled is not None:
            config = config.model_copy(update={"forensics_enabled": forensics_enabled})
        if excluded_tasks:
            new_excluded = config.excluded_tasks | set(excluded_tasks)
            config = config.model_copy(update={"excluded_tasks": new_excluded})
        if task_domain_mapping:
            new_mapping = {**config.task_domain_mapping, **task_domain_mapping}
            config = config.model_copy(update={"task_domain_mapping": new_mapping})

        # CRITICAL context restore failure -> prevent retries via dont_autoretry_for
        try:
            from baldur.context.celery_context_utils import BaldurContextError

            if app is not None:
                base_task = app.Task
                existing = getattr(base_task, "dont_autoretry_for", ()) or ()
                if BaldurContextError not in existing:
                    base_task.dont_autoretry_for = (*existing, BaldurContextError)
        except Exception as e:
            logger.debug(
                "baldur.register_failed",
                error=e,
            )

        # Create handler instances
        from baldur.adapters.celery.handlers.actor_context_handler import (
            ActorContextHandler,
        )

        _failure_handler = FailureHandler(config)
        _success_handler = SuccessHandler(config)
        _retry_handler = RetryHandler(config)
        _causation_handler = CausationHandler(config)
        _actor_context_handler = ActorContextHandler(config)
        _trace_handler = TraceContextHandler(config)

        # Connect signals
        task_failure.connect(_failure_handler.handle)
        task_success.connect(_success_handler.handle)
        task_retry.connect(_retry_handler.handle)
        before_task_publish.connect(_causation_handler.handle)
        before_task_publish.connect(_actor_context_handler.handle)
        task_prerun.connect(_trace_handler.on_prerun)
        task_postrun.connect(_trace_handler.on_postrun)

        # Block Celery worker boot from overriding configure_structlog()
        connect_setup_logging_handler()

        _signals_connected = True

        logger.info(
            "baldur.signal_hooks_configured",
            enabled=config.enabled,
            cb_enabled=config.cb_enabled,
            dlq_enabled=config.dlq_enabled,
            metrics_enabled=config.metrics_enabled,
            forensics_enabled=config.forensics_enabled,
        )


def disconnect_baldur_signals() -> None:
    """
    Disconnect baldur signal hooks.

    Useful for testing or when you need to temporarily disable hooks.
    """
    with _setup_lock:
        global _signals_connected
        global _failure_handler, _success_handler, _retry_handler
        global _causation_handler, _actor_context_handler, _trace_handler

        try:
            if _failure_handler is not None:
                task_failure.disconnect(_failure_handler.handle)
            if _success_handler is not None:
                task_success.disconnect(_success_handler.handle)
            if _retry_handler is not None:
                task_retry.disconnect(_retry_handler.handle)
            if _causation_handler is not None:
                before_task_publish.disconnect(_causation_handler.handle)
            if _actor_context_handler is not None:
                before_task_publish.disconnect(_actor_context_handler.handle)
            if _trace_handler is not None:
                task_prerun.disconnect(_trace_handler.on_prerun)
                task_postrun.disconnect(_trace_handler.on_postrun)

            disconnect_setup_logging_handler()

            _failure_handler = None
            _success_handler = None
            _retry_handler = None
            _causation_handler = None
            _actor_context_handler = None
            _trace_handler = None

            _signals_connected = False
            logger.info("baldur.signal_hooks_disconnected")
        except Exception as e:
            logger.exception(
                "baldur.error_disconnecting_signals",
                error=e,
            )


def is_signals_connected() -> bool:
    """Check if signal hooks are currently connected."""
    return _signals_connected
