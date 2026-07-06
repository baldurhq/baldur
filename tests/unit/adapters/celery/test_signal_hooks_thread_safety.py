"""
Unit tests for signal_hooks.py fix(356) — threading lock for setup/disconnect.

Tests:
C. Concurrent setup_baldur_signals calls connect only once (thread safety).
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import baldur.adapters.celery.signal_hooks as signal_hooks_module
from baldur.adapters.celery.signal_config import (
    SignalHooksSettings,
    reset_signal_hooks_settings,
)


class TestSignalHooksThreadSafetyBehavior:
    """Concurrent setup/disconnect should be serialized by _setup_lock."""

    def setup_method(self) -> None:
        reset_signal_hooks_settings()
        signal_hooks_module._signals_connected = False
        signal_hooks_module._failure_handler = None
        signal_hooks_module._success_handler = None
        signal_hooks_module._retry_handler = None
        signal_hooks_module._causation_handler = None
        signal_hooks_module._trace_handler = None

    def teardown_method(self) -> None:
        signal_hooks_module._signals_connected = False
        signal_hooks_module._failure_handler = None
        signal_hooks_module._success_handler = None
        signal_hooks_module._retry_handler = None
        signal_hooks_module._causation_handler = None
        signal_hooks_module._trace_handler = None
        reset_signal_hooks_settings()

    def test_concurrent_setup_connects_only_once(self) -> None:
        """Multiple threads calling setup_baldur_signals connect signals exactly once."""
        connect_counts = {"failure": 0}
        original_connect = None

        def counting_connect(handler, **kwargs):
            connect_counts["failure"] += 1
            if original_connect:
                return original_connect(handler, **kwargs)

        with (
            patch(
                "baldur.adapters.celery.signal_hooks.get_signal_hooks_settings",
                return_value=SignalHooksSettings(),
            ),
            patch(
                "baldur.adapters.celery.signal_hooks.FailureHandler",
                autospec=True,
            ),
            patch(
                "baldur.adapters.celery.signal_hooks.SuccessHandler",
                autospec=True,
            ),
            patch(
                "baldur.adapters.celery.signal_hooks.RetryHandler",
                autospec=True,
            ),
            patch(
                "baldur.adapters.celery.signal_hooks.CausationHandler",
                autospec=True,
            ),
            patch(
                "baldur.adapters.celery.signal_hooks.TraceContextHandler",
                autospec=True,
            ),
            patch(
                "baldur.adapters.celery.signal_hooks.task_failure",
            ) as mock_task_failure,
            patch(
                "baldur.adapters.celery.signal_hooks.task_success",
            ),
            patch(
                "baldur.adapters.celery.signal_hooks.task_retry",
            ),
            patch(
                "baldur.adapters.celery.signal_hooks.before_task_publish",
            ),
            patch(
                "baldur.adapters.celery.signal_hooks.task_prerun",
            ),
            patch(
                "baldur.adapters.celery.signal_hooks.task_postrun",
            ),
        ):
            original_connect = mock_task_failure.connect
            mock_task_failure.connect = counting_connect

            barrier = threading.Barrier(10)
            errors: list[Exception] = []

            def setup_worker():
                try:
                    barrier.wait(timeout=5)
                    signal_hooks_module.setup_baldur_signals()
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=setup_worker) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            assert not errors, f"Threads raised errors: {errors}"
            assert connect_counts["failure"] == 1
            assert signal_hooks_module.is_signals_connected() is True

    def test_concurrent_disconnect_clears_state_safely(self) -> None:
        """Multiple threads calling disconnect_baldur_signals are safe."""
        mock_handler = MagicMock()

        signal_hooks_module._signals_connected = True
        signal_hooks_module._failure_handler = mock_handler
        signal_hooks_module._success_handler = mock_handler
        signal_hooks_module._retry_handler = mock_handler
        signal_hooks_module._causation_handler = mock_handler
        signal_hooks_module._trace_handler = mock_handler

        with (
            patch(
                "baldur.adapters.celery.signal_hooks.task_failure",
            ),
            patch(
                "baldur.adapters.celery.signal_hooks.task_success",
            ),
            patch(
                "baldur.adapters.celery.signal_hooks.task_retry",
            ),
            patch(
                "baldur.adapters.celery.signal_hooks.before_task_publish",
            ),
            patch(
                "baldur.adapters.celery.signal_hooks.task_prerun",
            ),
            patch(
                "baldur.adapters.celery.signal_hooks.task_postrun",
            ),
        ):
            barrier = threading.Barrier(5)
            errors: list[Exception] = []

            def disconnect_worker():
                try:
                    barrier.wait(timeout=5)
                    signal_hooks_module.disconnect_baldur_signals()
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=disconnect_worker) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            assert not errors
            assert signal_hooks_module.is_signals_connected() is False
            assert signal_hooks_module._failure_handler is None
