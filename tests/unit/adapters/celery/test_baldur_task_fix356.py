"""
Unit tests for baldur_task fix(356) changes.

Tests:
A. DLQ task_id uses celery.current_task.request.id (not str(id(func)))
B. Recorder instances are created per-decorator (not per-call)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.celery.baldur_task import baldur_task
from baldur.adapters.celery.signal_config import (
    SignalHooksSettings,
    reset_signal_hooks_settings,
)


class TestBaldurTaskDlqTaskIdBehavior:
    """DLQ task_id should come from celery.current_task.request.id."""

    def setup_method(self) -> None:
        reset_signal_hooks_settings()

    def teardown_method(self) -> None:
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

    def test_dlq_task_id_uses_celery_current_task_request_id(
        self,
        _patch_deps: dict,
    ) -> None:
        """DLQ store receives celery current_task.request.id as task_id."""
        mock_current_task = MagicMock()
        mock_current_task.request.id = "abc-123-def"

        # Patch at the celery module level since the code does `from celery import current_task`
        with patch("celery.current_task", mock_current_task):

            @baldur_task()
            def failing_task() -> None:
                raise ValueError("boom")

            with pytest.raises(ValueError, match="boom"):
                failing_task()

        store_call = _patch_deps["dlq_cls"].return_value.store
        store_call.assert_called_once()
        assert store_call.call_args.kwargs["task_id"] == "abc-123-def"

    def test_dlq_task_id_empty_when_current_task_has_no_request(
        self,
        _patch_deps: dict,
    ) -> None:
        """DLQ task_id is empty string when current_task.request is None."""
        mock_current_task = MagicMock()
        mock_current_task.request = None

        with patch("celery.current_task", mock_current_task):

            @baldur_task()
            def failing_task() -> None:
                raise ValueError("boom")

            with pytest.raises(ValueError, match="boom"):
                failing_task()

        store_call = _patch_deps["dlq_cls"].return_value.store
        assert store_call.call_args.kwargs["task_id"] == ""

    def test_dlq_task_id_empty_when_request_id_is_none(
        self,
        _patch_deps: dict,
    ) -> None:
        """DLQ task_id is empty string when request.id is None."""
        mock_current_task = MagicMock()
        mock_current_task.request.id = None

        with patch("celery.current_task", mock_current_task):

            @baldur_task()
            def failing_task() -> None:
                raise ValueError("boom")

            with pytest.raises(ValueError, match="boom"):
                failing_task()

        store_call = _patch_deps["dlq_cls"].return_value.store
        assert store_call.call_args.kwargs["task_id"] == ""

    def test_dlq_task_id_empty_when_no_active_celery_task(
        self,
        _patch_deps: dict,
    ) -> None:
        """DLQ task_id is empty string when no active Celery task exists."""

        @baldur_task()
        def failing_task() -> None:
            raise ValueError("boom")

        # When called outside Celery worker context, current_task has no active
        # request, so request.id is None → task_id becomes empty string
        with pytest.raises(ValueError, match="boom"):
            failing_task()

        store_call = _patch_deps["dlq_cls"].return_value.store
        assert store_call.call_args.kwargs["task_id"] == ""


class TestBaldurTaskRecorderPerDecoratorBehavior:
    """Recorder instances should be created per-decorator, not per-call."""

    def setup_method(self) -> None:
        reset_signal_hooks_settings()

    def teardown_method(self) -> None:
        reset_signal_hooks_settings()

    def test_recorder_created_once_per_decorator_not_per_call(self) -> None:
        """CircuitBreakerRecorder and DLQRecorder are instantiated once at decoration time."""
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
            # Creating the decorator triggers recorder instantiation
            @baldur_task()
            def my_task(x: int) -> int:
                return x * 2

            assert mock_cb_cls.call_count == 1
            assert mock_dlq_cls.call_count == 1

            # Multiple calls should NOT create new recorder instances
            my_task(1)
            my_task(2)
            my_task(3)

            assert mock_cb_cls.call_count == 1
            assert mock_dlq_cls.call_count == 1

    def test_separate_decorators_get_separate_recorders(self) -> None:
        """Each decorated function gets its own recorder instances."""
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

            @baldur_task()
            def task_a() -> str:
                return "a"

            @baldur_task()
            def task_b() -> str:
                return "b"

            assert mock_cb_cls.call_count == 2
            assert mock_dlq_cls.call_count == 2
