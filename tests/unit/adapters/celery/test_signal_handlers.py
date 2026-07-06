"""
Unit tests for Celery signal handler classes (Failure, Success, Retry).

Tests that handlers respect enabled/excluded config, call correct integrations,
and never crash on internal errors.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.celery.handlers.failure_handler import FailureHandler
from baldur.adapters.celery.handlers.retry_handler import RetryHandler
from baldur.adapters.celery.handlers.success_handler import SuccessHandler
from baldur.adapters.celery.signal_config import SignalHooksSettings

# =========================================================================
# Helpers
# =========================================================================


def _make_sender(
    name: str = "app.tasks.do_work", max_retries: int | None = 3, retries: int = 3
) -> MagicMock:
    """Create a mock Celery task sender with name, request.retries, and max_retries."""
    sender = MagicMock()
    sender.name = name
    sender.max_retries = max_retries
    sender.request.retries = retries
    return sender


# =========================================================================
# FailureHandler Behavior Tests
# =========================================================================


class TestFailureHandlerBehavior:
    """FailureHandler orchestration and guard behavior."""

    @pytest.fixture
    def _patch_integrations(self):
        """Patch all four integration classes used by FailureHandler."""
        with (
            patch(
                "baldur.adapters.celery.handlers.failure_handler.CircuitBreakerRecorder",
                autospec=True,
            ) as mock_cb_cls,
            patch(
                "baldur.adapters.celery.handlers.failure_handler.DLQRecorder",
                autospec=True,
            ) as mock_dlq_cls,
            patch(
                "baldur.adapters.celery.handlers.failure_handler.MetricRecorder",
                autospec=True,
            ) as mock_metric_cls,
            patch(
                "baldur.adapters.celery.handlers.failure_handler.ForensicCapture",
                autospec=True,
            ) as mock_forensic_cls,
        ):
            yield {
                "cb_cls": mock_cb_cls,
                "dlq_cls": mock_dlq_cls,
                "metric_cls": mock_metric_cls,
                "forensic_cls": mock_forensic_cls,
            }

    def test_disabled_config_returns_immediately(
        self, _patch_integrations: dict
    ) -> None:
        """When config.enabled=False, handler returns without side effects."""
        config = SignalHooksSettings(enabled=False)
        handler = FailureHandler(config)
        sender = _make_sender()

        handler.handle(sender=sender, exception=RuntimeError("fail"))

        _patch_integrations["cb_cls"].return_value.record_failure.assert_not_called()
        _patch_integrations["dlq_cls"].return_value.store.assert_not_called()

    def test_excluded_task_skips_processing(self, _patch_integrations: dict) -> None:
        """Tasks in excluded_tasks are skipped entirely."""
        config = SignalHooksSettings(excluded_tasks={"myapp.tasks.skip_me"})
        handler = FailureHandler(config)
        sender = _make_sender(name="myapp.tasks.skip_me")

        handler.handle(sender=sender, exception=RuntimeError("fail"))

        _patch_integrations["cb_cls"].return_value.record_failure.assert_not_called()
        _patch_integrations["dlq_cls"].return_value.store.assert_not_called()

    def test_all_enabled_calls_cb_dlq_metrics_forensics(
        self, _patch_integrations: dict
    ) -> None:
        """With all features enabled, CB, DLQ, metrics, and forensics are called."""
        config = SignalHooksSettings()
        handler = FailureHandler(config)
        sender = _make_sender()
        exc = RuntimeError("test failure")

        handler.handle(
            sender=sender,
            task_id="task-1",
            exception=exc,
            args=(1, 2),
            kwargs={"key": "val"},
            einfo="traceback info",
        )

        _patch_integrations["cb_cls"].return_value.record_failure.assert_called_once()
        _patch_integrations["dlq_cls"].return_value.store.assert_called_once()
        _patch_integrations[
            "metric_cls"
        ].return_value.record_failure.assert_called_once()
        _patch_integrations["forensic_cls"].return_value.capture.assert_called_once()

    def test_cb_disabled_skips_cb_recording(self, _patch_integrations: dict) -> None:
        """When cb_enabled=False, circuit breaker recording is skipped."""
        config = SignalHooksSettings(cb_enabled=False)
        handler = FailureHandler(config)
        sender = _make_sender()

        handler.handle(sender=sender, exception=RuntimeError("fail"))

        _patch_integrations["cb_cls"].return_value.record_failure.assert_not_called()

    def test_dlq_disabled_skips_dlq_store(self, _patch_integrations: dict) -> None:
        """When dlq_enabled=False, DLQ storage is skipped."""
        config = SignalHooksSettings(dlq_enabled=False)
        handler = FailureHandler(config)
        sender = _make_sender()

        handler.handle(sender=sender, exception=RuntimeError("fail"))

        _patch_integrations["dlq_cls"].return_value.store.assert_not_called()

    def test_metrics_disabled_skips_metric_recording(
        self, _patch_integrations: dict
    ) -> None:
        """When metrics_enabled=False, metric recording is skipped."""
        config = SignalHooksSettings(metrics_enabled=False)
        handler = FailureHandler(config)
        sender = _make_sender()

        handler.handle(sender=sender, exception=RuntimeError("fail"))

        _patch_integrations[
            "metric_cls"
        ].return_value.record_failure.assert_not_called()

    def test_forensics_disabled_skips_capture(self, _patch_integrations: dict) -> None:
        """When forensics_enabled=False, forensic capture is skipped."""
        config = SignalHooksSettings(forensics_enabled=False)
        handler = FailureHandler(config)
        sender = _make_sender()

        handler.handle(sender=sender, exception=RuntimeError("fail"))

        _patch_integrations["forensic_cls"].return_value.capture.assert_not_called()

    def test_internal_error_is_caught_and_logged(
        self, _patch_integrations: dict
    ) -> None:
        """Internal exception in handler does not propagate."""
        config = SignalHooksSettings()
        handler = FailureHandler(config)
        sender = _make_sender()

        # Make CB record_failure raise an exception
        _patch_integrations[
            "cb_cls"
        ].return_value.record_failure.side_effect = RuntimeError("internal boom")

        # Should not raise
        handler.handle(sender=sender, exception=RuntimeError("fail"))

    def test_none_exception_skips_cb_metrics_forensics(
        self, _patch_integrations: dict
    ) -> None:
        """When exception is None, CB/metrics/forensics are skipped (guarded by is not None)."""
        config = SignalHooksSettings()
        handler = FailureHandler(config)
        sender = _make_sender()

        handler.handle(sender=sender, exception=None)

        _patch_integrations["cb_cls"].return_value.record_failure.assert_not_called()
        _patch_integrations[
            "metric_cls"
        ].return_value.record_failure.assert_not_called()
        _patch_integrations["forensic_cls"].return_value.capture.assert_not_called()


# =========================================================================
# SuccessHandler Behavior Tests
# =========================================================================


class TestSuccessHandlerBehavior:
    """SuccessHandler orchestration and guard behavior."""

    @pytest.fixture
    def _patch_integrations(self):
        """Patch CB and metric recorder used by SuccessHandler."""
        with (
            patch(
                "baldur.adapters.celery.handlers.success_handler.CircuitBreakerRecorder",
                autospec=True,
            ) as mock_cb_cls,
            patch(
                "baldur.adapters.celery.handlers.success_handler.MetricRecorder",
                autospec=True,
            ) as mock_metric_cls,
        ):
            yield {
                "cb_cls": mock_cb_cls,
                "metric_cls": mock_metric_cls,
            }

    def test_disabled_config_returns_immediately(
        self, _patch_integrations: dict
    ) -> None:
        """When config.enabled=False, handler returns without side effects."""
        config = SignalHooksSettings(enabled=False)
        handler = SuccessHandler(config)
        sender = _make_sender()

        handler.handle(sender=sender, result="ok")

        _patch_integrations["cb_cls"].return_value.record_success.assert_not_called()
        _patch_integrations[
            "metric_cls"
        ].return_value.record_success.assert_not_called()

    def test_excluded_task_skips_processing(self, _patch_integrations: dict) -> None:
        """Tasks in excluded_tasks are skipped."""
        config = SignalHooksSettings(excluded_tasks={"myapp.tasks.skip_me"})
        handler = SuccessHandler(config)
        sender = _make_sender(name="myapp.tasks.skip_me")

        handler.handle(sender=sender, result="ok")

        _patch_integrations["cb_cls"].return_value.record_success.assert_not_called()

    def test_all_enabled_calls_cb_and_metrics(self, _patch_integrations: dict) -> None:
        """With all features enabled, CB success and metrics are recorded."""
        config = SignalHooksSettings()
        handler = SuccessHandler(config)
        sender = _make_sender()

        handler.handle(sender=sender, result="ok")

        _patch_integrations["cb_cls"].return_value.record_success.assert_called_once()
        _patch_integrations[
            "metric_cls"
        ].return_value.record_success.assert_called_once()

    def test_cb_disabled_skips_cb_recording(self, _patch_integrations: dict) -> None:
        """When cb_enabled=False, CB success recording is skipped."""
        config = SignalHooksSettings(cb_enabled=False)
        handler = SuccessHandler(config)
        sender = _make_sender()

        handler.handle(sender=sender, result="ok")

        _patch_integrations["cb_cls"].return_value.record_success.assert_not_called()

    def test_metrics_disabled_skips_metric_recording(
        self, _patch_integrations: dict
    ) -> None:
        """When metrics_enabled=False, metric recording is skipped."""
        config = SignalHooksSettings(metrics_enabled=False)
        handler = SuccessHandler(config)
        sender = _make_sender()

        handler.handle(sender=sender, result="ok")

        _patch_integrations[
            "metric_cls"
        ].return_value.record_success.assert_not_called()

    def test_internal_error_is_caught_and_logged(
        self, _patch_integrations: dict
    ) -> None:
        """Internal exception in handler does not propagate."""
        config = SignalHooksSettings()
        handler = SuccessHandler(config)
        sender = _make_sender()

        _patch_integrations[
            "cb_cls"
        ].return_value.record_success.side_effect = RuntimeError("internal boom")

        # Should not raise
        handler.handle(sender=sender, result="ok")


# =========================================================================
# RetryHandler Behavior Tests
# =========================================================================


class TestRetryHandlerBehavior:
    """RetryHandler orchestration and guard behavior."""

    @pytest.fixture
    def _patch_integrations(self):
        """Patch metric recorder used by RetryHandler."""
        with patch(
            "baldur.adapters.celery.handlers.retry_handler.MetricRecorder",
            autospec=True,
        ) as mock_metric_cls:
            yield {"metric_cls": mock_metric_cls}

    def test_disabled_config_returns_immediately(
        self, _patch_integrations: dict
    ) -> None:
        """When config.enabled=False, handler returns without side effects."""
        config = SignalHooksSettings(enabled=False)
        handler = RetryHandler(config)
        sender = _make_sender()

        handler.handle(sender=sender, reason="will retry")

        _patch_integrations["metric_cls"].return_value.record_retry.assert_not_called()

    def test_metrics_disabled_returns_immediately(
        self, _patch_integrations: dict
    ) -> None:
        """When metrics_enabled=False, handler returns without side effects."""
        config = SignalHooksSettings(metrics_enabled=False)
        handler = RetryHandler(config)
        sender = _make_sender()

        handler.handle(sender=sender, reason="will retry")

        _patch_integrations["metric_cls"].return_value.record_retry.assert_not_called()

    def test_excluded_task_skips_processing(self, _patch_integrations: dict) -> None:
        """Tasks in excluded_tasks are skipped."""
        config = SignalHooksSettings(excluded_tasks={"myapp.tasks.skip_me"})
        handler = RetryHandler(config)
        sender = _make_sender(name="myapp.tasks.skip_me")

        handler.handle(sender=sender, reason="will retry")

        _patch_integrations["metric_cls"].return_value.record_retry.assert_not_called()

    def test_enabled_calls_metrics_record_retry(
        self, _patch_integrations: dict
    ) -> None:
        """With metrics enabled, record_retry is called."""
        config = SignalHooksSettings()
        handler = RetryHandler(config)
        sender = _make_sender()

        handler.handle(sender=sender, reason="will retry")

        _patch_integrations["metric_cls"].return_value.record_retry.assert_called_once()

    def test_internal_error_is_caught_and_logged(
        self, _patch_integrations: dict
    ) -> None:
        """Internal exception in handler does not propagate."""
        config = SignalHooksSettings()
        handler = RetryHandler(config)
        sender = _make_sender()

        _patch_integrations[
            "metric_cls"
        ].return_value.record_retry.side_effect = RuntimeError("internal boom")

        # Should not raise
        handler.handle(sender=sender, reason="will retry")
