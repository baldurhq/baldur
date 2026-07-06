"""
Tests for ReplayService event emission (381).

DLQ_REPLAY_BLOCKED / COMPLETED / FAILED 이벤트 발행 및
_execute_replay() 공통 코어, EventBus fail-safe 동작을 검증합니다.
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.replay_service import (
    ReplayResult,
    ReplayService,
    _replay_handlers,
)
from baldur.services.replay_service.handlers import ReplayHandler
from baldur_pro.services.governance.checks import BlockReason

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


class FailHandler(ReplayHandler):
    """Always fails (handler-reported)."""

    @property
    def domain(self) -> str:
        return "payment"

    def can_replay(self, failed_op) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op) -> ReplayResult:
        return ReplayResult.failed(failed_op.id, "downstream_error")


class CrashHandler(ReplayHandler):
    """Raises exception on replay."""

    @property
    def domain(self) -> str:
        return "payment"

    def can_replay(self, failed_op) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op) -> ReplayResult:
        raise RuntimeError("Connection lost")


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
    """ReplayService with injected mock repository and mock event bus."""
    svc = ReplayService(repository=mock_repository)
    svc._event_bus = mock_event_bus
    return svc


def _register_handler(handler: ReplayHandler):
    _replay_handlers[handler.domain] = handler


# =============================================================================
# EventBus Fail-Safe (§2)
# =============================================================================


class TestReplayServiceEventBusFailSafeBehavior:
    """EventBus lazy getter and _emit_event fail-safe behavior."""

    def test_get_event_bus_returns_none_on_import_failure(self):
        """EventBus import failure returns None without crashing."""
        svc = ReplayService(repository=MagicMock())

        with (
            patch(
                "baldur.services.replay_service.service.get_event_bus",
                side_effect=ImportError("no module"),
                create=True,
            ),
            patch(
                "baldur.services.event_bus.get_event_bus",
                side_effect=ImportError("no module"),
            ),
        ):
            result = svc._get_event_bus()

        assert result is None

    def test_emit_event_silently_skips_when_bus_is_none(self):
        """_emit_event does nothing when EventBus is unavailable."""
        svc = ReplayService(repository=MagicMock())
        svc._event_bus = None

        with patch.object(svc, "_get_event_bus", return_value=None):
            # Should not raise
            svc._emit_event("test_event", {"key": "value"})

    def test_emit_event_catches_bus_emit_exception(self, mock_event_bus):
        """_emit_event logs warning but does not propagate bus.emit exceptions."""
        mock_event_bus.emit.side_effect = RuntimeError("bus down")
        svc = ReplayService(repository=MagicMock())
        svc._event_bus = mock_event_bus

        # Should not raise
        svc._emit_event("test_event", {"key": "value"})

    def test_emit_event_passes_source_replay_service(self, mock_event_bus):
        """_emit_event passes _event_source to bus.emit."""
        svc = ReplayService(repository=MagicMock())
        svc._event_bus = mock_event_bus

        svc._emit_event("test_event", {"key": "value"})

        mock_event_bus.emit.assert_called_once_with(
            "test_event", data={"key": "value"}, source=ReplayService._event_source
        )


# =============================================================================
# _execute_replay() Event Emissions (§3)
# =============================================================================


class TestExecuteReplayEventsBehavior:
    """_execute_replay per-item event emissions."""

    @patch("baldur.services.replay_service.service.log_dlq_replay_audit")
    def test_successful_replay_emits_completed_event(
        self, mock_audit, replay_service, mock_event_bus
    ):
        """Successful replay emits DLQ_REPLAY_COMPLETED with success=True."""
        _register_handler(SuccessHandler())

        replay_service._execute_replay(1)

        # Find the DLQ_REPLAY_COMPLETED call
        completed_calls = [
            c
            for c in mock_event_bus.emit.call_args_list
            if c[0][0] == EventType.DLQ_REPLAY_COMPLETED
        ]
        assert len(completed_calls) == 1
        data = completed_calls[0][1]["data"]
        assert data["dlq_id"] == 1
        assert data["domain"] == "payment"
        assert data["success"] is True
        assert data["replay_attempt"] == 1

    @patch("baldur.services.replay_service.service.log_dlq_replay_audit")
    def test_failed_replay_emits_completed_event_with_false(
        self, mock_audit, replay_service, mock_event_bus
    ):
        """Handler-reported failure emits DLQ_REPLAY_COMPLETED with success=False."""
        _register_handler(FailHandler())

        replay_service._execute_replay(1)

        completed_calls = [
            c
            for c in mock_event_bus.emit.call_args_list
            if c[0][0] == EventType.DLQ_REPLAY_COMPLETED
        ]
        assert len(completed_calls) == 1
        assert completed_calls[0][1]["data"]["success"] is False

    @patch("baldur.services.replay_service.service.log_dlq_replay_audit")
    def test_handler_crash_emits_failed_event(
        self, mock_audit, replay_service, mock_event_bus
    ):
        """Handler exception emits DLQ_REPLAY_FAILED (not COMPLETED)."""
        _register_handler(CrashHandler())

        result = replay_service._execute_replay(1)

        assert result.success is False
        assert "RuntimeError" in result.error

        failed_calls = [
            c
            for c in mock_event_bus.emit.call_args_list
            if c[0][0] == EventType.DLQ_REPLAY_FAILED
        ]
        assert len(failed_calls) == 1
        data = failed_calls[0][1]["data"]
        assert data["dlq_id"] == 1
        assert data["domain"] == "payment"
        assert data["error_type"] == "RuntimeError"
        assert "Connection lost" in data["error_message"]

        # Should NOT emit COMPLETED
        completed_calls = [
            c
            for c in mock_event_bus.emit.call_args_list
            if c[0][0] == EventType.DLQ_REPLAY_COMPLETED
        ]
        assert len(completed_calls) == 0

    def test_max_attempts_exceeded_emits_blocked_event(
        self, replay_service, mock_repository, mock_event_bus
    ):
        """Entry with pending status but max retries exceeded emits DLQ_REPLAY_BLOCKED."""
        # Simulate: acquire returns None, entry exists as pending (max exceeded)
        mock_repository.try_acquire_for_replay.return_value = None
        mock_repository.get_by_id.return_value = FakeFailedOperationData(
            id=1, status="pending"
        )

        result = replay_service._execute_replay(1)

        assert result.success is False
        assert "max_replays_exceeded" in result.error

        blocked_calls = [
            c
            for c in mock_event_bus.emit.call_args_list
            if c[0][0] == EventType.DLQ_REPLAY_BLOCKED
        ]
        assert len(blocked_calls) == 1
        data = blocked_calls[0][1]["data"]
        assert data["dlq_id"] == 1
        assert data["block_reason"] == "max_replay_attempts_exceeded"

    def test_entry_not_found_does_not_emit_event(
        self, replay_service, mock_repository, mock_event_bus
    ):
        """Entry not found scenario should not emit any event."""
        mock_repository.try_acquire_for_replay.return_value = None
        mock_repository.get_by_id.return_value = None

        replay_service._execute_replay(1)

        mock_event_bus.emit.assert_not_called()

    def test_already_processed_does_not_emit_event(
        self, replay_service, mock_repository, mock_event_bus
    ):
        """Entry with non-pending status should not emit any event."""
        mock_repository.try_acquire_for_replay.return_value = None
        mock_repository.get_by_id.return_value = FakeFailedOperationData(
            id=1, status="completed"
        )

        replay_service._execute_replay(1)

        mock_event_bus.emit.assert_not_called()

    @patch("baldur.services.replay_service.service.log_dlq_replay_audit")
    def test_audit_called_on_success(self, mock_audit, replay_service):
        """Audit logging is called after successful replay."""
        _register_handler(SuccessHandler())

        replay_service._execute_replay(1)

        mock_audit.assert_called_once_with(
            dlq_id=1,
            domain="payment",
            success=True,
            actor_id="system",
            error_message=None,
            origin_trace_id=None,
        )


# =============================================================================
# replay_single() BLOCKED Event (§4)
# =============================================================================


class TestReplaySingleBlockedEventBehavior:
    """replay_single governance-blocked event emission."""

    @patch(
        "baldur_pro.services.governance.checks.check_all_governance",
    )
    def test_governance_blocked_emits_blocked_event(
        self, mock_governance, replay_service, mock_event_bus
    ):
        """Governance block emits DLQ_REPLAY_BLOCKED with block_reason."""
        mock_governance.return_value = MagicMock(
            allowed=False,
            block_reason=BlockReason.KILL_SWITCH,
            block_message="Kill Switch active",
        )

        result = replay_service.replay_single(dlq_id=42)

        assert result.success is False
        mock_event_bus.emit.assert_called_once()
        call_args = mock_event_bus.emit.call_args
        assert call_args[0][0] == EventType.DLQ_REPLAY_BLOCKED
        data = call_args[1]["data"]
        assert data["dlq_id"] == 42
        assert data["block_reason"] == BlockReason.KILL_SWITCH.value
        assert data["block_message"] == "Kill Switch active"

    @patch(
        "baldur_pro.services.governance.checks.check_all_governance",
    )
    def test_governance_blocked_with_none_block_reason(
        self, mock_governance, replay_service, mock_event_bus
    ):
        """Governance block with block_reason=None emits None in data."""
        mock_governance.return_value = MagicMock(
            allowed=False,
            block_reason=None,
            block_message="Unknown block",
        )

        replay_service.replay_single(dlq_id=1)

        data = mock_event_bus.emit.call_args[1]["data"]
        assert data["block_reason"] is None


# =============================================================================
# replay_batch() BLOCKED + Summary Events (§4, §5)
# =============================================================================


class TestReplayBatchEventsBehavior:
    """replay_batch governance-blocked and summary event emissions."""

    @patch(
        "baldur_pro.services.governance.checks.check_all_governance",
    )
    def test_governance_blocked_emits_blocked_event_with_domain(
        self, mock_governance, replay_service, mock_event_bus
    ):
        """Batch governance block emits DLQ_REPLAY_BLOCKED with domain filter."""
        mock_governance.return_value = MagicMock(
            allowed=False,
            block_reason=BlockReason.EMERGENCY_MODE,
            block_message="Emergency active",
        )

        replay_service.replay_batch(domain="payment")

        call_args = mock_event_bus.emit.call_args
        assert call_args[0][0] == EventType.DLQ_REPLAY_BLOCKED
        data = call_args[1]["data"]
        assert data["domain"] == "payment"
        assert data["block_reason"] == BlockReason.EMERGENCY_MODE.value

    @patch(
        "baldur_pro.services.governance.checks.check_all_governance",
    )
    def test_governance_blocked_without_domain_uses_all(
        self, mock_governance, replay_service, mock_event_bus
    ):
        """Batch block with domain=None emits domain='all'."""
        mock_governance.return_value = MagicMock(
            allowed=False,
            block_reason=BlockReason.ERROR_BUDGET,
            block_message="Budget exhausted",
        )

        replay_service.replay_batch(domain=None)

        data = mock_event_bus.emit.call_args[1]["data"]
        assert data["domain"] == "all"

    @patch(
        "baldur_pro.services.governance.checks.check_all_governance",
    )
    @patch("baldur.services.replay_service.service.log_dlq_replay_audit")
    def test_batch_summary_emitted_after_processing(
        self,
        mock_audit,
        mock_governance,
        replay_service,
        mock_repository,
        mock_event_bus,
    ):
        """Batch summary DLQ_REPLAY_BATCH_COMPLETED emitted."""
        mock_governance.return_value = MagicMock(allowed=True)
        _register_handler(SuccessHandler())

        mock_repository.find_replayable.return_value = [
            FakeFailedOperationData(id=1),
            FakeFailedOperationData(id=2),
        ]

        replay_service.replay_batch(domain="payment")

        # Collect batch summary events
        summary_calls = [
            c
            for c in mock_event_bus.emit.call_args_list
            if c[0][0] == EventType.DLQ_REPLAY_BATCH_COMPLETED
        ]
        assert len(summary_calls) == 1
        data = summary_calls[0][1]["data"]
        assert data["domain"] == "payment"
        assert data["total"] == 2
        assert data["success_count"] == 2
        assert data["failed_count"] == 0

    @patch(
        "baldur_pro.services.governance.checks.check_all_governance",
    )
    def test_empty_batch_does_not_emit_summary(
        self, mock_governance, replay_service, mock_repository, mock_event_bus
    ):
        """Empty batch (0 entries) should not emit summary event."""
        mock_governance.return_value = MagicMock(allowed=True)
        mock_repository.find_replayable.return_value = []

        replay_service.replay_batch()

        # No batch summary event
        summary_calls = [
            c
            for c in mock_event_bus.emit.call_args_list
            if c[0][0] == EventType.DLQ_REPLAY_BATCH_COMPLETED
        ]
        assert len(summary_calls) == 0


# =============================================================================
# replay_on_circuit_close() Refactoring (§12)
# =============================================================================


class TestReplayOnCircuitCloseEventsBehavior:
    """replay_on_circuit_close refactored with batch governance + summary event."""

    @patch(
        "baldur_pro.services.governance.checks.check_all_governance",
    )
    def test_governance_blocked_emits_blocked_event_with_trigger(
        self, mock_governance, replay_service, mock_event_bus
    ):
        """Circuit-close governance block emits BLOCKED with trigger=circuit_close."""
        mock_governance.return_value = MagicMock(
            allowed=False,
            block_reason=BlockReason.KILL_SWITCH,
            block_message="Kill Switch active",
        )

        result = replay_service.replay_on_circuit_close(
            service_name="payment_api",
            service_failure_type_map={"payment_api": ["PG_TIMEOUT"]},
        )

        assert result.governance_blocked is True
        call_args = mock_event_bus.emit.call_args
        assert call_args[0][0] == EventType.DLQ_REPLAY_BLOCKED
        data = call_args[1]["data"]
        assert data["trigger"] == "circuit_close"
        assert data["service_name"] == "payment_api"
        assert data["block_reason"] == BlockReason.KILL_SWITCH.value

    @patch(
        "baldur_pro.services.governance.checks.check_all_governance",
    )
    @patch("baldur.services.replay_service.service.log_dlq_replay_audit")
    def test_summary_event_emitted_with_circuit_close_trigger(
        self,
        mock_audit,
        mock_governance,
        replay_service,
        mock_repository,
        mock_event_bus,
    ):
        """Circuit-close batch summary includes trigger=circuit_close."""
        mock_governance.return_value = MagicMock(allowed=True)
        _register_handler(SuccessHandler())

        mock_repository.find_replayable.return_value = [
            FakeFailedOperationData(id=10),
        ]

        replay_service.replay_on_circuit_close(
            service_name="payment_api",
            service_failure_type_map={"payment_api": ["PG_TIMEOUT"]},
        )

        summary_calls = [
            c
            for c in mock_event_bus.emit.call_args_list
            if c[0][0] == EventType.DLQ_REPLAY_BATCH_COMPLETED
        ]
        assert len(summary_calls) == 1
        data = summary_calls[0][1]["data"]
        assert data["trigger"] == "circuit_close"
        assert data["service_name"] == "payment_api"
        assert data["total"] == 1
        assert data["success_count"] == 1

    def test_no_failure_types_emits_blocked_event(self, replay_service, mock_event_bus):
        """No failure types mapped emits DLQ_REPLAY_BLOCKED (#496 observability).

        Operator misconfig (missing service_failure_type_map entry) must
        surface as a blocked event, not a silent no-op.
        """
        result = replay_service.replay_on_circuit_close(
            service_name="unknown_service",
            service_failure_type_map={},
        )

        assert result.total == 0
        blocked_calls = [
            c
            for c in mock_event_bus.emit.call_args_list
            if c[0][0] == EventType.DLQ_REPLAY_BLOCKED
        ]
        assert len(blocked_calls) == 1
        data = blocked_calls[0][1]["data"]
        assert data["trigger"] == "circuit_close"
        assert data["service_name"] == "unknown_service"
        assert data["block_reason"] == "service_failure_type_map_unconfigured"
        assert data["config_path"] == "replay_automation.service_failure_type_map"

    @patch(
        "baldur_pro.services.governance.checks.check_all_governance",
    )
    def test_empty_entries_does_not_emit_summary(
        self, mock_governance, replay_service, mock_repository, mock_event_bus
    ):
        """0 entries from repository should not emit summary event."""
        mock_governance.return_value = MagicMock(allowed=True)
        mock_repository.find_replayable.return_value = []

        replay_service.replay_on_circuit_close(
            service_name="payment_api",
            service_failure_type_map={"payment_api": ["PG_TIMEOUT"]},
        )

        summary_calls = [
            c
            for c in mock_event_bus.emit.call_args_list
            if c[0][0] == EventType.DLQ_REPLAY_BATCH_COMPLETED
        ]
        assert len(summary_calls) == 0


class TestLoadFailureTypeMapBehavior:
    """_load_failure_type_map RuntimeConfig fallback behavior."""

    def test_returns_empty_dict_on_runtime_config_failure(self):
        """RuntimeConfig failure returns empty dict (fail-open)."""
        svc = ReplayService(repository=MagicMock())

        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            side_effect=RuntimeError("unavailable"),
        ):
            result = svc._load_failure_type_map()

        assert result == {}

    def test_returns_mapping_from_runtime_config(self):
        """RuntimeConfig returns configured service_failure_type_map."""
        svc = ReplayService(repository=MagicMock())

        mock_manager = MagicMock()
        mock_manager.get_config.return_value = {
            "service_failure_type_map": {"svc_a": ["TIMEOUT"]}
        }

        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            return_value=mock_manager,
        ):
            result = svc._load_failure_type_map()

        assert result == {"svc_a": ["TIMEOUT"]}

    @patch(
        "baldur_pro.services.governance.checks.check_all_governance",
    )
    @patch("baldur.services.replay_service.service.log_dlq_replay_audit")
    def test_explicit_map_takes_precedence_over_runtime_config(
        self, mock_audit, mock_governance
    ):
        """Explicit service_failure_type_map takes precedence over RuntimeConfig."""
        mock_governance.return_value = MagicMock(allowed=True)
        repo = MagicMock()
        repo.find_replayable.return_value = []
        svc = ReplayService(repository=repo)
        svc._event_bus = MagicMock()

        with patch.object(svc, "_load_failure_type_map") as mock_load:
            svc.replay_on_circuit_close(
                service_name="svc",
                service_failure_type_map={"svc": ["ERR"]},
            )
            # Should NOT call _load_failure_type_map since explicit map provided
            mock_load.assert_not_called()
