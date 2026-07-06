"""
D13 verification: Celery setup_logging signal handler preserves structlog config.

Without the no-op ``setup_logging`` handler, Celery worker boot calls
``_setup_logging_subsystem()`` which invokes ``logging.config.dictConfig(...)``
and silently replaces the root logger's handlers — discarding whatever
``configure_structlog()`` installed.

This test verifies that:
  1. Connecting the handler swallows ``setup_logging`` signal sends
     without installing any new root handlers.
  2. After signal dispatch, the root logger's handler list is identical
     (same handler identities) to the pre-signal state.

Mock-based — no Docker required.
"""

from __future__ import annotations

import logging

from celery.signals import setup_logging

from baldur.adapters.celery.signal_handlers import (
    _preserve_structlog_config,
    connect_setup_logging_handler,
    disconnect_setup_logging_handler,
)
from baldur.observability.structlog_config import (
    configure_structlog,
    reset_structlog_config,
)


def _root_handler_ids() -> set[int]:
    return {id(h) for h in logging.getLogger().handlers}


class TestSetupLoggingSignalBehavior:
    """Celery setup_logging signal blocking via no-op handler."""

    def setup_method(self):
        reset_structlog_config()
        configure_structlog()

    def teardown_method(self):
        try:
            disconnect_setup_logging_handler()
        except Exception:
            pass
        reset_structlog_config()

    def test_signal_dispatch_preserves_root_handler_identities(self):
        """setup_logging.send must not add/replace root logger handlers."""
        before = _root_handler_ids()
        assert before, "configure_structlog() should install at least one root handler"

        connect_setup_logging_handler()

        # Simulate Celery worker boot dispatching setup_logging.
        responses = setup_logging.send_robust(sender=None, loglevel=logging.INFO)

        # The no-op handler was invoked and produced None (block signal).
        assert any(
            receiver is _preserve_structlog_config and response is None
            for receiver, response in responses
        )

        after = _root_handler_ids()
        assert after == before, (
            f"setup_logging dispatch must not replace root handlers; "
            f"before={before}, after={after}"
        )

    def test_handler_is_idempotent_on_repeat_connect(self):
        """Repeating connect_setup_logging_handler keeps no-op receiver attached."""
        connect_setup_logging_handler()
        connect_setup_logging_handler()

        responses = setup_logging.send_robust(sender=None, loglevel=logging.INFO)
        matches = [
            (r, resp) for r, resp in responses if r is _preserve_structlog_config
        ]
        # Connecting twice may or may not dedupe depending on dispatcher behavior;
        # the important contract is that *every* response is None (no override).
        assert matches, "no-op receiver must be connected"
        assert all(resp is None for _, resp in matches)
