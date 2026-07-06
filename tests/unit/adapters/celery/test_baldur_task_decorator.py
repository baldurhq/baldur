"""
Unit tests for baldur_task decorator.

Tests success/failure recording, custom domain/service parameters,
and feature toggle suppression (track_cb, track_dlq).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.adapters.celery.baldur_task import baldur_task
from baldur.adapters.celery.signal_config import (
    SignalHooksSettings,
    reset_signal_hooks_settings,
)

# =========================================================================
# Behavior Tests
# =========================================================================


class TestBaldurTaskDecoratorBehavior:
    """baldur_task decorator success/failure recording behavior."""

    def setup_method(self) -> None:
        """Clear singleton settings before each test."""
        reset_signal_hooks_settings()

    def teardown_method(self) -> None:
        """Clear singleton settings after each test."""
        reset_signal_hooks_settings()

    @pytest.fixture
    def _patch_deps(self):
        """Patch CB recorder, DLQ recorder, and settings singleton."""
        with (
            patch(
                "baldur.adapters.celery.baldur_task.CircuitBreakerRecorder",
                autospec=True,
            ) as mock_cb_cls,
            patch(
                "baldur.adapters.celery.baldur_task.DLQRecorder",
                autospec=True,
            ) as mock_dlq_cls,
            patch(
                "baldur.adapters.celery.baldur_task.get_signal_hooks_settings",
                return_value=SignalHooksSettings(),
            ),
        ):
            yield {
                "cb_cls": mock_cb_cls,
                "dlq_cls": mock_dlq_cls,
            }

    def test_successful_call_records_cb_success(self, _patch_deps: dict) -> None:
        """Successful function call records CB success."""

        @baldur_task()
        def my_task(x: int) -> int:
            return x * 2

        result = my_task(5)

        assert result == 10
        _patch_deps["cb_cls"].return_value.record_success.assert_called_once()

    def test_failed_call_records_cb_failure_and_dlq_then_reraises(
        self,
        _patch_deps: dict,
    ) -> None:
        """Failed function call records CB failure, stores to DLQ, then re-raises."""

        @baldur_task()
        def failing_task() -> None:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            failing_task()

        _patch_deps["cb_cls"].return_value.record_failure.assert_called_once()
        _patch_deps["dlq_cls"].return_value.store.assert_called_once()

    def test_custom_domain_parameter_is_used(self, _patch_deps: dict) -> None:
        """Custom domain parameter is passed to DLQ recorder on failure."""

        @baldur_task(domain="billing")
        def billing_task() -> None:
            raise RuntimeError("billing error")

        with pytest.raises(RuntimeError):
            billing_task()

        # DLQ store should receive domain="billing"
        store_call = _patch_deps["dlq_cls"].return_value.store
        store_call.assert_called_once()
        assert store_call.call_args.kwargs["domain"] == "billing"

    def test_custom_service_name_parameter_is_used(self, _patch_deps: dict) -> None:
        """Custom service_name is passed to CB recorder on success."""

        @baldur_task(service_name="payment_gw")
        def payment_task() -> str:
            return "ok"

        payment_task()

        cb_call = _patch_deps["cb_cls"].return_value.record_success
        cb_call.assert_called_once()
        # First positional arg should be the service name
        assert cb_call.call_args[0][0] == "payment_gw"

    def test_track_cb_false_suppresses_cb_recording_on_success(
        self,
        _patch_deps: dict,
    ) -> None:
        """track_cb=False suppresses CB recording on success."""

        @baldur_task(track_cb=False)
        def my_task() -> str:
            return "ok"

        my_task()

        _patch_deps["cb_cls"].return_value.record_success.assert_not_called()

    def test_track_cb_false_suppresses_cb_recording_on_failure(
        self,
        _patch_deps: dict,
    ) -> None:
        """track_cb=False suppresses CB recording on failure."""

        @baldur_task(track_cb=False)
        def failing_task() -> None:
            raise RuntimeError("fail")

        with pytest.raises(RuntimeError):
            failing_task()

        _patch_deps["cb_cls"].return_value.record_failure.assert_not_called()

    def test_track_dlq_false_suppresses_dlq_storage(self, _patch_deps: dict) -> None:
        """track_dlq=False suppresses DLQ storage on failure."""

        @baldur_task(track_dlq=False)
        def failing_task() -> None:
            raise RuntimeError("fail")

        with pytest.raises(RuntimeError):
            failing_task()

        _patch_deps["dlq_cls"].return_value.store.assert_not_called()

    def test_successful_call_does_not_store_to_dlq(self, _patch_deps: dict) -> None:
        """Successful call does not trigger DLQ storage."""

        @baldur_task()
        def my_task() -> str:
            return "ok"

        my_task()

        _patch_deps["dlq_cls"].return_value.store.assert_not_called()

    def test_decorator_preserves_function_metadata(self) -> None:
        """Decorator preserves original function name via functools.wraps."""
        with (
            patch(
                "baldur.adapters.celery.baldur_task.CircuitBreakerRecorder",
                autospec=True,
            ),
            patch(
                "baldur.adapters.celery.baldur_task.DLQRecorder",
                autospec=True,
            ),
            patch(
                "baldur.adapters.celery.baldur_task.get_signal_hooks_settings",
                return_value=SignalHooksSettings(),
            ),
        ):

            @baldur_task()
            def original_name() -> None:
                pass

            assert original_name.__name__ == "original_name"

    def test_kwargs_forwarded_to_original_function(self, _patch_deps: dict) -> None:
        """Keyword arguments are correctly forwarded to the wrapped function."""

        @baldur_task()
        def task_with_kwargs(a: int, b: int = 10) -> int:
            return a + b

        result = task_with_kwargs(1, b=20)
        assert result == 21
