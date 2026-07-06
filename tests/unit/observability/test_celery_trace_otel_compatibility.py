"""
Tests for Celery Trace ID OTEL Compatibility (Phase 4).

Tests for:
- generate_celery_trace_id() with OTEL enabled/disabled
- get_celery_trace_id_with_otel_context() function
- Celery context preservation
"""

import os
from unittest.mock import patch


class TestGenerateCeleryTraceIdOtelCompatibility:
    """Tests for generate_celery_trace_id OTEL compatibility."""

    def setup_method(self):
        """Reset OTEL state before each test."""
        from baldur.observability import reset_opentelemetry
        from baldur.settings.observability import reset_observability_settings
        from baldur.settings.otel import reset_otel_settings

        reset_opentelemetry()
        reset_observability_settings()
        reset_otel_settings()

    def teardown_method(self):
        """Clean up after each test."""
        from baldur.observability import reset_opentelemetry
        from baldur.settings.observability import reset_observability_settings
        from baldur.settings.otel import reset_otel_settings

        reset_opentelemetry()
        reset_observability_settings()
        reset_otel_settings()

    def test_generate_celery_trace_id_without_otel(self):
        """Returns CELERY_{task_id} format when OTEL is disabled."""
        from baldur.audit.trace import generate_celery_trace_id

        with patch.dict(
            os.environ, {"BALDUR_OBSERVABILITY_PROFILE": "local"}, clear=False
        ):
            task_id = "7483abc-1234-5678-90ab-cdef12345678"
            result = generate_celery_trace_id(task_id)

            assert result == f"CELERY_{task_id}"

    def test_generate_celery_trace_id_empty_task_id(self):
        """Returns CELERY_{uuid} format for empty task_id."""
        from baldur.audit.trace import generate_celery_trace_id

        with patch.dict(
            os.environ, {"BALDUR_OBSERVABILITY_PROFILE": "local"}, clear=False
        ):
            result = generate_celery_trace_id("")

            assert result.startswith("CELERY_req-")

    def test_generate_celery_trace_id_none_task_id(self):
        """Returns CELERY_{uuid} format for None task_id."""
        from baldur.audit.trace import generate_celery_trace_id

        with patch.dict(
            os.environ, {"BALDUR_OBSERVABILITY_PROFILE": "local"}, clear=False
        ):
            result = generate_celery_trace_id(None)

            assert result.startswith("CELERY_req-")


class TestGetCeleryTraceIdWithOtelContext:
    """Tests for get_celery_trace_id_with_otel_context function."""

    def setup_method(self):
        """Reset OTEL state before each test."""
        from baldur.observability import reset_opentelemetry
        from baldur.settings.observability import reset_observability_settings
        from baldur.settings.otel import reset_otel_settings

        reset_opentelemetry()
        reset_observability_settings()
        reset_otel_settings()

    def teardown_method(self):
        """Clean up after each test."""
        from baldur.observability import reset_opentelemetry
        from baldur.settings.observability import reset_observability_settings
        from baldur.settings.otel import reset_otel_settings

        reset_opentelemetry()
        reset_observability_settings()
        reset_otel_settings()

    def test_returns_dict_with_required_keys(self):
        """Returns dictionary with all required keys."""
        from baldur.audit.trace import get_celery_trace_id_with_otel_context

        with patch.dict(
            os.environ, {"BALDUR_OBSERVABILITY_PROFILE": "local"}, clear=False
        ):
            task_id = "test-task-123"
            result = get_celery_trace_id_with_otel_context(task_id)

            assert "trace_id" in result
            assert "trace_id_full" in result
            assert "span_id" in result
            assert "celery_task_id" in result

    def test_celery_task_id_preserved(self):
        """Original Celery task_id is preserved in result."""
        from baldur.audit.trace import get_celery_trace_id_with_otel_context

        task_id = "test-task-456"
        result = get_celery_trace_id_with_otel_context(task_id)

        assert result["celery_task_id"] == task_id

    def test_trace_id_full_none_when_otel_disabled(self):
        """trace_id_full is None when OTEL is disabled."""
        from baldur.audit.trace import get_celery_trace_id_with_otel_context

        with patch.dict(
            os.environ, {"BALDUR_OBSERVABILITY_PROFILE": "local"}, clear=False
        ):
            result = get_celery_trace_id_with_otel_context("task-789")

            assert result["trace_id_full"] is None
            assert result["span_id"] is None


class TestCeleryContextFunctions:
    """Tests for Celery context management functions."""

    def teardown_method(self):
        """Clean up Celery context after each test."""
        from baldur.audit.trace import clear_celery_context

        clear_celery_context()

    def test_set_and_get_celery_context(self):
        """set_celery_context and get_celery_context work correctly."""
        from baldur.audit.trace import (
            clear_celery_context,
            get_celery_context,
            set_celery_context,
        )

        set_celery_context(
            task_id="task-abc",
            task_name="test.task.name",
            retries=2,
        )

        context = get_celery_context()

        assert context is not None
        assert context["task_id"] == "task-abc"
        assert context["task_name"] == "test.task.name"
        assert context["retries"] == 2

        clear_celery_context()

    def test_is_celery_task_returns_true_in_context(self):
        """is_celery_task returns True when in Celery context."""
        from baldur.audit.trace import (
            clear_celery_context,
            is_celery_task,
            set_celery_context,
        )

        set_celery_context(task_id="task-xyz", task_name="test.task")

        assert is_celery_task() is True

        clear_celery_context()

    def test_is_celery_task_returns_false_outside_context(self):
        """is_celery_task returns False when not in Celery context."""
        from baldur.audit.trace import clear_celery_context, is_celery_task

        clear_celery_context()
        assert is_celery_task() is False


class TestRestoreTraceFromCelery:
    """Tests for restore_trace_from_celery context manager."""

    def teardown_method(self):
        """Clean up trace and Celery context after each test."""
        from baldur.audit.trace import clear_celery_context, clear_trace_id

        clear_trace_id()
        clear_celery_context()

    def test_restore_from_trace_info(self):
        """Restores trace_id from trace_info when provided."""
        from baldur.audit.trace import get_trace_id, restore_trace_from_celery

        trace_info = {"trace_id": "req-abc12345", "source": "celery_propagated"}

        with restore_trace_from_celery(trace_info=trace_info) as active_id:
            assert active_id == "req-abc12345"
            assert get_trace_id() == "req-abc12345"

    def test_restore_from_celery_task_id(self):
        """Generates CELERY_{task_id} when trace_info is empty."""
        from baldur.audit.trace import get_trace_id, restore_trace_from_celery

        with restore_trace_from_celery(celery_task_id="task-def") as active_id:
            assert active_id == "CELERY_task-def"
            assert get_trace_id() == "CELERY_task-def"

    def test_restore_fallback_generates_new_id(self):
        """Generates new trace_id when no info provided."""
        from baldur.audit.trace import restore_trace_from_celery

        with restore_trace_from_celery() as active_id:
            assert active_id.startswith("CELERY_req-")
