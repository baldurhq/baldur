"""Replay safety gate unit tests (#502 D7).

Test targets:
    - baldur.services.replay_service.handlers._truncate_gate (pure function)
    - ReplayService._execute_replay wiring of the gate into the replay path

Test Categories:
    A. Behavior: _truncate_gate decisions
    B. Behavior: ReplayService wires the gate before handler.replay and emits
       DLQ_REPLAY_BLOCKED on block (entry stays PENDING — no complete_replay)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.interfaces.repositories import (
    FailedOperationData,
    FailedOperationStatus,
)
from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.handlers import _truncate_gate
from baldur.services.replay_service.models import ReplayResult

# =============================================================================
# Helpers
# =============================================================================


def _make_failed_op(request_data: dict | None = None, domain: str = "payment"):
    return FailedOperationData(
        id=1,
        domain=domain,
        failure_type="t",
        status=FailedOperationStatus.PENDING.value,
        retry_count=0,
        max_retries=3,
        request_data=request_data or {},
    )


def _settings_double(blocks: bool):
    s = MagicMock()
    s.truncate_blocks_replay = blocks
    return s


# =============================================================================
# A. _truncate_gate decisions
# =============================================================================


class TestTruncateGateBehavior:
    """_truncate_gate return values for the three decision branches."""

    def test_not_truncated_is_allowed(self):
        op = _make_failed_op(request_data={"order_id": 1})
        with patch(
            "baldur.settings.dlq.get_dlq_settings",
            return_value=_settings_double(blocks=True),
        ):
            allowed, reason = _truncate_gate(op)
        assert allowed is True
        assert reason == ""

    def test_truncated_blocked_by_default(self):
        op = _make_failed_op(request_data={"_truncated": True, "original_size": 9000})
        with patch(
            "baldur.settings.dlq.get_dlq_settings",
            return_value=_settings_double(blocks=True),
        ):
            allowed, reason = _truncate_gate(op)
        assert allowed is False
        assert reason == "request_data_truncated"

    def test_truncated_allowed_when_setting_off(self):
        """Opt-out: BALDUR_DLQ_TRUNCATE_BLOCKS_REPLAY=False lets replay proceed."""
        op = _make_failed_op(request_data={"_truncated": True})
        with patch(
            "baldur.settings.dlq.get_dlq_settings",
            return_value=_settings_double(blocks=False),
        ):
            allowed, reason = _truncate_gate(op)
        assert allowed is True
        assert reason == ""

    def test_non_dict_request_data_is_allowed(self):
        """Edge case: malformed request_data (non-dict) is not a truncation marker."""
        op = _make_failed_op()
        op.request_data = "not-a-dict"  # type: ignore[assignment]
        allowed, reason = _truncate_gate(op)
        assert allowed is True
        assert reason == ""

    def test_settings_failure_defaults_to_deny(self):
        """Conservative default-deny when settings cannot be read."""
        op = _make_failed_op(request_data={"_truncated": True})
        with patch(
            "baldur.settings.dlq.get_dlq_settings",
            side_effect=RuntimeError("settings boom"),
        ):
            allowed, reason = _truncate_gate(op)
        assert allowed is False
        assert reason == "request_data_truncated"


# =============================================================================
# B. ReplayService._execute_replay wires the gate into the replay path
# =============================================================================


class TestExecuteReplayTruncateGate:
    """The gate runs BEFORE handler.replay and emits DLQ_REPLAY_BLOCKED on block."""

    def _build_service(self, acquired_op: FailedOperationData):
        svc = ReplayService(repository=MagicMock())
        svc.repository.try_acquire_for_replay.return_value = acquired_op
        svc._event_bus = MagicMock()
        return svc

    def test_truncated_entry_skips_handler_and_emits_blocked_event(self):
        """Truncated request_data → handler not invoked, DLQ_REPLAY_BLOCKED emitted."""
        op = _make_failed_op(request_data={"_truncated": True, "original_size": 9000})
        svc = self._build_service(op)

        handler = MagicMock()
        with (
            patch(
                "baldur.services.replay_service.service.get_replay_handler",
                return_value=handler,
            ),
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=_settings_double(blocks=True),
            ),
        ):
            result = svc._execute_replay(dlq_id=op.id)

        # Customer handler never invoked.
        handler.replay.assert_not_called()
        handler.can_replay.assert_not_called()

        # Result is a skip with the gate reason.
        assert isinstance(result, ReplayResult)
        assert result.skipped is True
        assert result.data == {"skip_reason": "request_data_truncated"}

        # DLQ_REPLAY_BLOCKED emitted with the gate reason.
        blocked = [
            c
            for c in svc._event_bus.emit.call_args_list
            if c[0][0] == EventType.DLQ_REPLAY_BLOCKED
        ]
        assert blocked
        data = blocked[0][1]["data"]
        assert data["dlq_id"] == op.id
        assert data["block_reason"] == "request_data_truncated"

    def test_truncated_entry_does_not_call_complete_replay(self):
        """Entry stays PENDING — complete_replay must not be called on gate block."""
        op = _make_failed_op(request_data={"_truncated": True})
        svc = self._build_service(op)

        with (
            patch(
                "baldur.services.replay_service.service.get_replay_handler",
                return_value=MagicMock(),
            ),
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=_settings_double(blocks=True),
            ),
        ):
            svc._execute_replay(dlq_id=op.id)

        svc.repository.complete_replay.assert_not_called()

    def test_non_truncated_entry_passes_through_to_handler(self):
        """Untruncated request_data → handler.replay is invoked normally."""
        op = _make_failed_op(request_data={"order_id": 1})
        svc = self._build_service(op)

        handler = MagicMock()
        handler.replay.return_value = ReplayResult.succeeded(op.id, "ok")
        with (
            patch(
                "baldur.services.replay_service.service.get_replay_handler",
                return_value=handler,
            ),
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=_settings_double(blocks=True),
            ),
        ):
            svc._execute_replay(dlq_id=op.id)

        handler.replay.assert_called_once_with(op)

    def test_truncated_entry_calls_on_replay_blocked_metric(self):
        op = _make_failed_op(request_data={"_truncated": True})
        svc = self._build_service(op)

        with (
            patch(
                "baldur.metrics.event_handlers.ReplayEventHandler.on_replay_blocked"
            ) as on_blocked,
            patch(
                "baldur.services.replay_service.service.get_replay_handler",
                return_value=MagicMock(),
            ),
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=_settings_double(blocks=True),
            ),
        ):
            svc._execute_replay(dlq_id=op.id)

        on_blocked.assert_called_once_with(op.domain, "request_data_truncated")
