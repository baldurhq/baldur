"""
Tests for ReplayService metrics wiring and ReplayEventHandler new methods (394).

Test targets:
    - ReplayEventHandler.on_replay_blocked (A1)
    - ReplayEventHandler.on_batch_completed (A1)
    - ReplayService._record_batch_completion (A4/DD-8)
    - ReplayService._execute_replay replay_type parameter (A2/A3)

Test Categories:
    A. Behavior: Side effects, dependency interaction

Reference:
    394 §A
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from baldur.services.replay_service import (
    ReplayResult,
    ReplayService,
    _replay_handlers,
)
from baldur.services.replay_service.handlers import ReplayHandler
from baldur.services.replay_service.models import BatchReplayResult

# =============================================================================
# Fixtures
# =============================================================================


@dataclass
class FakeFailedOperationData:
    """Test substitute for FailedOperationData."""

    id: int
    domain: str = "payment"
    status: str = "pending"
    failure_type: str = "PG_TIMEOUT"
    retry_count: int = 1
    error_code: str = ""
    error_message: str = ""
    snapshot_data: dict = None
    request_data: dict = None
    response_data: dict = None
    metadata: dict = None

    def __post_init__(self):
        self.snapshot_data = self.snapshot_data or {}
        self.request_data = self.request_data or {}
        self.response_data = self.response_data or {}
        self.metadata = self.metadata or {}


class SuccessHandler(ReplayHandler):
    """Always succeeds."""

    @property
    def domain(self) -> str:
        return "payment"

    def can_replay(self, failed_op) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op) -> ReplayResult:
        return ReplayResult.succeeded(failed_op.id, "OK")


@pytest.fixture(autouse=True)
def _clear_handler_registry():
    _replay_handlers.clear()
    yield
    _replay_handlers.clear()


@pytest.fixture
def mock_repository():
    repo = MagicMock()
    repo.try_acquire_for_replay.return_value = FakeFailedOperationData(id=1)
    repo.get_by_id.return_value = FakeFailedOperationData(id=1)
    repo.complete_replay.return_value = None
    repo.find_replayable.return_value = []
    return repo


@pytest.fixture
def mock_event_bus():
    return MagicMock()


@pytest.fixture
def replay_service(mock_repository, mock_event_bus):
    """ReplayService with injected mocks."""
    svc = ReplayService(repository=mock_repository)
    svc._event_bus = mock_event_bus
    return svc


def _register_handler(handler: ReplayHandler):
    _replay_handlers[handler.domain] = handler


# =============================================================================
# A. ReplayEventHandler New Methods (R5/A1)
# =============================================================================


class TestReplayEventHandlerNewMethodsBehavior:
    """R5: on_replay_blocked and on_batch_completed behavior."""

    @patch("baldur.metrics.event_handlers._get_metrics", autospec=True)
    def test_on_replay_blocked_records_blocked_outcome(self, mock_get_metrics):
        """on_replay_blocked records 'blocked' outcome via replay recorder."""
        from baldur.metrics.event_handlers import ReplayEventHandler

        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        ReplayEventHandler.on_replay_blocked("payment", "kill_switch")

        mock_metrics.replay.record_replay.assert_called_once()
        args = mock_metrics.replay.record_replay.call_args[0]
        assert args[1] == "blocked"

    @patch("baldur.metrics.event_handlers._get_metrics", autospec=True)
    def test_on_replay_blocked_handles_none_metrics(self, mock_get_metrics):
        """on_replay_blocked is no-op when metrics is None."""
        from baldur.metrics.event_handlers import ReplayEventHandler

        mock_get_metrics.return_value = None
        ReplayEventHandler.on_replay_blocked("payment", "kill_switch")

    @patch("baldur.metrics.event_handlers._get_metrics", autospec=True)
    def test_on_batch_completed_records_batch_outcome(self, mock_get_metrics):
        """on_batch_completed records 'batch_completed' with duration."""
        from baldur.metrics.event_handlers import ReplayEventHandler

        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        ReplayEventHandler.on_batch_completed("all", 10, 8, 2, 1.5)

        mock_metrics.replay.record_replay.assert_called_once()
        args = mock_metrics.replay.record_replay.call_args[0]
        assert args[1] == "batch_completed"
        assert args[2] == 1.5

    @patch("baldur.metrics.event_handlers._get_metrics", autospec=True)
    def test_on_batch_completed_handles_exception_gracefully(self, mock_get_metrics):
        """on_batch_completed catches exceptions (fail-open)."""
        from baldur.metrics.event_handlers import ReplayEventHandler

        mock_get_metrics.side_effect = Exception("boom")
        ReplayEventHandler.on_batch_completed("all", 10, 8, 2, 1.5)


# =============================================================================
# B. ReplayService._record_batch_completion (DD-8)
# =============================================================================


class TestRecordBatchCompletionBehavior:
    """DD-8: _record_batch_completion helper encapsulates EventBus + metrics."""

    def test_skips_when_total_is_zero(self, replay_service, mock_event_bus):
        """Does not emit or record when batch_result.total == 0."""
        batch_result = BatchReplayResult(total=0)
        replay_service._record_batch_completion("all", batch_result, 1.0)
        mock_event_bus.emit.assert_not_called()

    @patch("baldur.metrics.event_handlers.ReplayEventHandler", autospec=True)
    def test_emits_event_and_records_metric(
        self, mock_handler_cls, replay_service, mock_event_bus
    ):
        """Emits DLQ_REPLAY_BATCH_COMPLETED and calls ReplayEventHandler."""
        batch_result = BatchReplayResult(
            total=5, success_count=3, failed_count=2, results=[]
        )
        replay_service._record_batch_completion("payment", batch_result, 2.5)

        # EventBus emit
        mock_event_bus.emit.assert_called_once()
        emit_args = mock_event_bus.emit.call_args
        assert emit_args[1]["data"]["domain"] == "payment"
        assert emit_args[1]["data"]["total"] == 5

        # MetricEventHandler
        mock_handler_cls.on_batch_completed.assert_called_once_with(
            "payment", 5, 3, 2, 2.5
        )

    @patch("baldur.metrics.event_handlers.ReplayEventHandler", autospec=True)
    def test_merges_extra_event_data(
        self, mock_handler_cls, replay_service, mock_event_bus
    ):
        """extra_event_data is merged into the event data dict."""
        batch_result = BatchReplayResult(
            total=1, success_count=1, failed_count=0, results=[]
        )
        replay_service._record_batch_completion(
            "svc-a",
            batch_result,
            0.5,
            extra_event_data={"trigger": "circuit_close", "service_name": "svc-a"},
        )

        emit_data = mock_event_bus.emit.call_args[1]["data"]
        assert emit_data["trigger"] == "circuit_close"
        assert emit_data["service_name"] == "svc-a"


# =============================================================================
# C. _execute_replay replay_type parameter (A2/A3)
# =============================================================================


class TestExecuteReplayTypeBehavior:
    """A2/A3: _execute_replay passes replay_type to ReplayEventHandler."""

    @patch("baldur.metrics.event_handlers.ReplayEventHandler", autospec=True)
    def test_default_replay_type_is_single(
        self, mock_handler_cls, replay_service, mock_repository
    ):
        """_execute_replay defaults replay_type to 'single'."""
        _register_handler(SuccessHandler())

        replay_service._execute_replay(1)

        mock_handler_cls.on_replay_started.assert_called_once()
        args = mock_handler_cls.on_replay_started.call_args[0]
        assert args[1] == "single"

    @patch("baldur.metrics.event_handlers.ReplayEventHandler", autospec=True)
    def test_explicit_replay_type_batch(
        self, mock_handler_cls, replay_service, mock_repository
    ):
        """_execute_replay passes explicit replay_type='batch'."""
        _register_handler(SuccessHandler())

        replay_service._execute_replay(1, replay_type="batch")

        args = mock_handler_cls.on_replay_started.call_args[0]
        assert args[1] == "batch"

    @patch("baldur.metrics.event_handlers.ReplayEventHandler", autospec=True)
    def test_on_replay_completed_called_on_success(
        self, mock_handler_cls, replay_service, mock_repository
    ):
        """on_replay_completed is called with success=True on handler success."""
        _register_handler(SuccessHandler())

        replay_service._execute_replay(1)

        mock_handler_cls.on_replay_completed.assert_called_once()
        args = mock_handler_cls.on_replay_completed.call_args[0]
        assert args[1] is True  # success
        assert args[2] >= 0  # duration >= 0 (mock handler returns instantly)

    @patch("baldur.metrics.event_handlers.ReplayEventHandler", autospec=True)
    def test_on_replay_blocked_on_max_attempts_exceeded(
        self, mock_handler_cls, replay_service, mock_repository
    ):
        """on_replay_blocked called when max attempts exceeded."""
        mock_repository.try_acquire_for_replay.return_value = None
        existing = FakeFailedOperationData(id=1, status="pending")
        mock_repository.get_by_id.return_value = existing

        replay_service._execute_replay(1)

        mock_handler_cls.on_replay_blocked.assert_called_once()
        args = mock_handler_cls.on_replay_blocked.call_args[0]
        assert args[0] == "payment"  # existing.domain
        assert args[1] == "max_replay_attempts_exceeded"
