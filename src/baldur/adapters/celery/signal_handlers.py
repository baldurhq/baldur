"""
Celery signal handlers that preserve Baldur's structlog configuration.

By default Celery worker boot calls ``_setup_logging_subsystem()`` which
invokes ``logging.config.dictConfig(...)`` and replaces all root handlers —
silently discarding the ``ProcessorFormatter`` ``StreamHandler`` that
``configure_structlog()`` installs. Connecting a no-op handler to the
``setup_logging`` signal tells Celery "the application owns logging setup;
do not touch it."

Any future need for Celery-specific logging features (per-task formatter,
task-routing-based log routing) must be channeled through
``configure_structlog()`` or explicitly override this handler.
"""

from __future__ import annotations

from typing import Any

from celery.signals import setup_logging

__all__ = ["connect_setup_logging_handler", "disconnect_setup_logging_handler"]


def _preserve_structlog_config(**kwargs: Any) -> None:
    """No-op setup_logging receiver — blocks Celery worker boot override."""
    return


def connect_setup_logging_handler() -> None:
    """Attach the no-op setup_logging receiver."""
    setup_logging.connect(_preserve_structlog_config, weak=False)


def disconnect_setup_logging_handler() -> None:
    """Detach the no-op setup_logging receiver (used in tests)."""
    setup_logging.disconnect(_preserve_structlog_config)
