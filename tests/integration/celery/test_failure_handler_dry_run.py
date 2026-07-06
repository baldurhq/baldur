"""Integration: Celery FailureHandler observe-only orchestration (doc 603 / D5).

The Celery ``FailureHandler._handle_internal`` orchestrates four recorders and,
under observe-only, must gate exactly TWO of them — the state-mutating CB record
(step 1) and DLQ store (step 2) — while keeping the two observation steps
(metrics step 3, forensics step 4) LIVE. The assertion of interest is the
*combination*, driven through the real D1 bridge (``dry_run_active()``), which is
a multi-component orchestration check rather than single-method delegation —
hence integration-level.

Mock-based: a mock ``sender`` plus the four patched integration classes. No real
Celery worker, so no infra marker.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.celery.handlers.failure_handler import FailureHandler
from baldur.adapters.celery.signal_config import SignalHooksSettings
from tests.factories import dry_run_active


def _make_sender(
    name: str = "app.tasks.do_work", max_retries: int = 3, retries: int = 3
) -> MagicMock:
    """Mock Celery sender with retries exhausted (DLQ store would normally fire)."""
    sender = MagicMock()
    sender.name = name
    sender.max_retries = max_retries
    sender.request.retries = retries
    return sender


@pytest.fixture
def _patch_integrations():
    """Patch all four integration classes constructed by FailureHandler."""
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
            "cb": mock_cb_cls.return_value,
            "dlq": mock_dlq_cls.return_value,
            "metric": mock_metric_cls.return_value,
            "forensic": mock_forensic_cls.return_value,
        }


class TestCeleryFailureHandlerDryRun:
    """The two state-mutating steps are suppressed; observation stays live."""

    def test_cb_record_and_dlq_store_suppressed_under_dry_run(
        self, _patch_integrations: dict
    ) -> None:
        # Given the handler is constructed fresh so it builds the patched
        # recorders, and dry-run is active
        handler = FailureHandler(SignalHooksSettings())
        sender = _make_sender()
        # When a task-failure signal is handled under observe-only
        with dry_run_active():
            handler.handle(
                sender=sender,
                task_id="task-1",
                exception=RuntimeError("boom"),
                einfo="tb",
            )
        # Then the two state-mutating interventions are skipped
        _patch_integrations["cb"].record_failure.assert_not_called()
        _patch_integrations["dlq"].store.assert_not_called()

    def test_metrics_and_forensics_still_run_under_dry_run(
        self, _patch_integrations: dict
    ) -> None:
        # Observation steps (3-4) stay live even under observe-only.
        handler = FailureHandler(SignalHooksSettings())
        sender = _make_sender()
        with dry_run_active():
            handler.handle(
                sender=sender,
                task_id="task-1",
                exception=RuntimeError("boom"),
                einfo="tb",
            )
        _patch_integrations["metric"].record_failure.assert_called_once()
        _patch_integrations["forensic"].capture.assert_called_once()

    def test_all_four_run_when_not_dry_run(self, _patch_integrations: dict) -> None:
        # Control: without dry-run all four recorders fire — proving the gate is
        # what suppresses CB + DLQ, not the test wiring.
        handler = FailureHandler(SignalHooksSettings())
        sender = _make_sender()
        handler.handle(
            sender=sender,
            task_id="task-1",
            exception=RuntimeError("boom"),
            einfo="tb",
        )
        _patch_integrations["cb"].record_failure.assert_called_once()
        _patch_integrations["dlq"].store.assert_called_once()
        _patch_integrations["metric"].record_failure.assert_called_once()
        _patch_integrations["forensic"].capture.assert_called_once()
