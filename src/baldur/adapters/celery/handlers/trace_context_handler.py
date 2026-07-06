"""
Trace Context Handler — inject/cleanup trace context on task prerun/postrun.
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.adapters.celery.signal_config import SignalHooksSettings

__all__ = ["TraceContextHandler"]

logger = structlog.get_logger()


class TraceContextHandler:
    """Manage trace context lifecycle for Celery tasks (prerun / postrun)."""

    def __init__(self, config: SignalHooksSettings) -> None:
        self._config = config

    def on_prerun(
        self,
        sender: Any = None,
        task_id: str | None = None,
        task: Any = None,
        args: tuple | None = None,
        kwargs: dict | None = None,
        **kw: Any,
    ) -> None:
        """
        Inject TraceContext before task execution.

        Automatically restores trace_id, celery_context, and causation context
        so that all audit logs include the Celery task ID.
        """
        if not self._config.enabled:
            return

        task_name = sender.name if sender else "unknown"

        if task_name in self._config.excluded_tasks:
            return

        if task_id is None:
            # Celery prerun without a task_id has no context to restore.
            return

        try:
            from baldur.context.celery_context_utils import (
                BaldurContextError,
                restore_all_task_context,
            )

            restore_all_task_context(sender, task_id, task_name, kwargs)

            logger.debug(
                "baldur_signal.task_prerun",
                task_name=task_name,
                task_id=task_id,
            )

        except BaldurContextError:
            # CRITICAL context restore failure -> Fail-Fast.
            # Retry blocking is guaranteed by dont_autoretry_for in
            # setup_baldur_signals().
            raise
        except Exception as e:
            # Never let signal handler crash affect task execution
            logger.exception(
                "baldur_signal.prerun_error",
                error=e,
            )

    def on_postrun(
        self,
        sender: Any = None,
        task_id: str | None = None,
        task: Any = None,
        args: tuple | None = None,
        kwargs: dict | None = None,
        retval: Any = None,
        state: str | None = None,
        **kw: Any,
    ) -> None:
        """
        Clean up TraceContext after task execution.

        Prevents trace_id/celery_context leaking to subsequent tasks
        on the same worker process.
        """
        if not self._config.enabled:
            return

        task_name = sender.name if sender else "unknown"

        if task_name in self._config.excluded_tasks:
            return

        try:
            from baldur.context.celery_context_utils import (
                cleanup_all_task_context,
            )

            cleanup_all_task_context(sender)

            logger.debug(
                "baldur_signal.task_postrun",
                task_name=task_name,
                task_id=task_id,
                task_state=state,
            )

        except Exception as e:
            logger.exception(
                "baldur_signal.postrun_error",
                error=e,
            )
