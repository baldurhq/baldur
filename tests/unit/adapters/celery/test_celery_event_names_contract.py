"""
Contract tests for Celery handler and integration event names.

Verifies that all event names follow the logging standard:
- Format: {component}.{entity}_{action} (dot separator required)
- Semantic correctness: _unavailable in ImportError, _failed on errors

Uses two verification approaches:
- Side-effect: trigger the code path and assert the log event name
- Source inspection: verify the event name string exists in source code
  (for code paths that are impractical to trigger in unit tests)
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

from baldur.adapters.celery.signal_config import SignalHooksSettings

# =========================================================================
# Handler Event Names — Side-Effect Verification
# =========================================================================


class TestCeleryHandlerEventNameContract:
    """Celery signal handler event names follow dot-separated logging standard."""

    def test_failure_handler_task_failed_event_name(self) -> None:
        """FailureHandler logs 'baldur_signal.task_failed' on task failure."""
        from baldur.adapters.celery.handlers.failure_handler import FailureHandler

        config = SignalHooksSettings()
        handler = FailureHandler(config)

        with patch(
            "baldur.adapters.celery.handlers.failure_handler.logger",
        ) as mock_logger:
            handler.handle(
                sender=type("Task", (), {"name": "test.task"})(),
                task_id="t1",
                exception=ValueError("test"),
                einfo=None,
            )

        mock_logger.info.assert_called_once()
        assert mock_logger.info.call_args[0][0] == "baldur_signal.task_failed"

    def test_failure_handler_error_event_name(self) -> None:
        """FailureHandler logs 'baldur_signal.failure_handler_error' on internal error."""
        from baldur.adapters.celery.handlers.failure_handler import FailureHandler

        config = SignalHooksSettings()
        handler = FailureHandler(config)

        with (
            patch.object(
                handler, "_handle_internal", side_effect=RuntimeError("internal")
            ),
            patch(
                "baldur.adapters.celery.handlers.failure_handler.logger",
            ) as mock_logger,
        ):
            handler.handle(
                sender=type("Task", (), {"name": "test.task"})(),
                task_id="t1",
                exception=ValueError("test"),
                einfo=None,
            )

        mock_logger.exception.assert_called_once()
        assert (
            mock_logger.exception.call_args[0][0]
            == "baldur_signal.failure_handler_error"
        )

    def test_retry_handler_error_event_name(self) -> None:
        """RetryHandler logs 'baldur_signal.retry_handler_error' on internal error."""
        from baldur.adapters.celery.handlers.retry_handler import RetryHandler

        config = SignalHooksSettings()
        handler = RetryHandler(config)

        with (
            patch.object(
                handler._metrics, "record_retry", side_effect=RuntimeError("internal")
            ),
            patch(
                "baldur.adapters.celery.handlers.retry_handler.logger",
            ) as mock_logger,
        ):
            handler.handle(
                sender=type("Task", (), {"name": "test.task"})(),
                reason="retry",
                einfo=None,
            )

        mock_logger.exception.assert_called_once()
        assert (
            mock_logger.exception.call_args[0][0] == "baldur_signal.retry_handler_error"
        )

    def test_success_handler_error_event_name(self) -> None:
        """SuccessHandler logs 'baldur_signal.success_handler_error' on internal error."""
        from baldur.adapters.celery.handlers.success_handler import SuccessHandler

        config = SignalHooksSettings()
        handler = SuccessHandler(config)

        with (
            patch.object(
                handler._cb, "record_success", side_effect=RuntimeError("internal")
            ),
            patch(
                "baldur.adapters.celery.handlers.success_handler.logger",
            ) as mock_logger,
        ):
            handler.handle(
                sender=type("Task", (), {"name": "test.task"})(),
                task_id="t1",
                retval="ok",
            )

        mock_logger.exception.assert_called_once()
        assert (
            mock_logger.exception.call_args[0][0]
            == "baldur_signal.success_handler_error"
        )


# =========================================================================
# Handler Event Names — Source Inspection
# =========================================================================


class TestCeleryHandlerEventNameSourceContract:
    """Verify event name strings in handler source code (dot-separated convention)."""

    def test_causation_handler_has_dot_separated_event_names(self) -> None:
        """CausationHandler event names use dot separator."""
        from baldur.adapters.celery.handlers import causation_handler

        source = inspect.getsource(causation_handler)
        assert '"baldur_signal.causation_inject_failed"' in source

    def test_trace_handler_has_dot_separated_event_names(self) -> None:
        """TraceContextHandler event names use dot separator."""
        from baldur.adapters.celery.handlers import trace_context_handler

        source = inspect.getsource(trace_context_handler)
        assert '"baldur_signal.task_prerun"' in source
        assert '"baldur_signal.prerun_error"' in source
        assert '"baldur_signal.task_postrun"' in source
        assert '"baldur_signal.postrun_error"' in source


# =========================================================================
# Integration Event Names — Source Inspection (Semantic Inversion Fixes)
# =========================================================================


class TestCeleryIntegrationEventNameContract:
    """CB/DLQ/Forensics/Metrics event names: _unavailable on ImportError (not _available)."""

    def test_cb_recorder_uses_unavailable_on_import_error(self) -> None:
        """CB recorder uses 'baldur_cb.service_unavailable' (not _available)."""
        from baldur.adapters.celery.integrations import cb_recorder

        source = inspect.getsource(cb_recorder)
        # New correct names present
        assert '"baldur_cb.service_unavailable"' in source
        assert '"baldur_cb.failure_recorded"' in source
        assert '"baldur_cb.success_recorded"' in source
        assert '"baldur_cb.record_failed"' in source
        # Old incorrect names absent
        assert '"baldur_cb_service_available"' not in source
        assert '"baldur_cb_recorded_failure"' not in source

    def test_dlq_recorder_uses_unavailable_on_import_error(self) -> None:
        """DLQ recorder uses 'baldur_dlq.service_unavailable' (not _available)."""
        from baldur.adapters.celery.integrations import dlq_recorder

        source = inspect.getsource(dlq_recorder)
        assert '"baldur_dlq.service_unavailable"' in source
        assert '"baldur_dlq.entry_stored"' in source
        assert '"baldur_dlq.store_failed"' in source
        # Old incorrect names absent
        assert '"baldur_dlq_service_available"' not in source

    def test_forensic_capture_uses_dot_separator(self) -> None:
        """ForensicCapture uses dot-separated event names."""
        from baldur.adapters.celery.integrations import forensic_capture

        source = inspect.getsource(forensic_capture)
        assert '"baldur_forensics.context_captured"' in source
        assert '"baldur_forensics.capture_failed"' in source
        # Old names absent
        assert '"baldur_forensics_captured_context"' not in source

    def test_metric_recorder_uses_dot_separator(self) -> None:
        """MetricRecorder uses dot-separated event names."""
        from baldur.adapters.celery.integrations import metric_recorder

        source = inspect.getsource(metric_recorder)
        assert '"baldur_metrics.record_failed"' in source
        # Old name absent
        assert '"baldur_metrics_failed_record"' not in source
