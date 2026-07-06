"""
dlq_tasks kwarg vocabulary sanity tests (511 D3/D10 + D9 + D12).

Verifies that the rewritten ``conditional_replay_on_circuit_close`` state-machine
emits land the expected typed kwargs in the captured ``event_dict``:

  - dlq.circuit_recovery_started   (entry log)
  - dlq.circuit_recovery_blocked   (governance-blocked branch)
  - dlq.circuit_recovery_completed (success branch)
  - dlq.circuit_recovery_failed    (exception branch)

Scope: 1 emit per state. NOT exhaustive — sanity-only.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import structlog


def _entries(logs: list[dict], event_name: str) -> list[dict]:
    return [e for e in logs if e.get("event") == event_name]


class TestDLQCircuitRecoveryKwargCaptureBehavior:
    """Sanity check: dlq.circuit_recovery_* state-machine emits."""

    def test_circuit_recovery_started_kwargs(self):
        """dlq.circuit_recovery_started lands service_name + max_items."""
        from baldur.celery_tasks import dlq_tasks

        mock_replay = MagicMock()
        mock_replay.replay_on_circuit_close.return_value = SimpleNamespace(
            governance_blocked=False,
            governance_block_reason=None,
            total=0,
            success_count=0,
            failed_count=0,
        )

        with (
            patch(
                "baldur.services.get_replay_service",
                return_value=mock_replay,
            ),
            structlog.testing.capture_logs() as logs,
        ):
            dlq_tasks.conditional_replay_on_circuit_close.apply(
                kwargs={"service_name": "payment-api", "max_items": 25},
                task_id="task-xyz",
            )

        started = _entries(logs, "dlq.circuit_recovery_started")
        assert len(started) == 1, f"expected one started event, got {logs}"
        assert started[0]["service_name"] == "payment-api"
        assert started[0]["max_items"] == 25
        assert started[0]["task_id"] == "task-xyz"

    def test_circuit_recovery_completed_kwargs(self):
        """dlq.circuit_recovery_completed lands dlq_total + success/failed counts."""
        from baldur.celery_tasks import dlq_tasks

        mock_replay = MagicMock()
        mock_replay.replay_on_circuit_close.return_value = SimpleNamespace(
            governance_blocked=False,
            governance_block_reason=None,
            total=10,
            success_count=8,
            failed_count=2,
            capped=False,
        )

        with (
            patch(
                "baldur.services.get_replay_service",
                return_value=mock_replay,
            ),
            structlog.testing.capture_logs() as logs,
        ):
            dlq_tasks.conditional_replay_on_circuit_close.apply(
                kwargs={"service_name": "payment-api", "max_items": 50},
                task_id="task-c",
            )

        completed = _entries(logs, "dlq.circuit_recovery_completed")
        assert len(completed) == 1, f"expected one completed event, got {logs}"
        entry = completed[0]
        assert entry["service_name"] == "payment-api"
        assert entry["dlq_total"] == 10
        assert entry["success_count"] == 8
        assert entry["failed_count"] == 2
        assert entry["capped"] is False

    def test_circuit_recovery_blocked_kwargs(self):
        """dlq.circuit_recovery_blocked lands service_name + reason."""
        from baldur.celery_tasks import dlq_tasks

        mock_replay = MagicMock()
        mock_replay.replay_on_circuit_close.return_value = SimpleNamespace(
            governance_blocked=True,
            governance_block_reason="emergency_mode_active",
            total=0,
            success_count=0,
            failed_count=0,
        )

        with (
            patch(
                "baldur.services.get_replay_service",
                return_value=mock_replay,
            ),
            structlog.testing.capture_logs() as logs,
        ):
            eager = dlq_tasks.conditional_replay_on_circuit_close.apply(
                kwargs={"service_name": "payment-api", "max_items": 50},
                task_id="task-b",
            )
        result = eager.get()

        assert result["success"] is False
        blocked = _entries(logs, "dlq.circuit_recovery_blocked")
        assert len(blocked) == 1, f"expected one blocked event, got {logs}"
        entry = blocked[0]
        assert entry["service_name"] == "payment-api"
        assert entry["reason"] == "emergency_mode_active"

    def test_circuit_recovery_failed_kwargs(self):
        """dlq.circuit_recovery_failed lands service_name + error str."""
        from baldur.celery_tasks import dlq_tasks

        mock_replay = MagicMock()
        mock_replay.replay_on_circuit_close.side_effect = RuntimeError("replay boom")

        with (
            patch(
                "baldur.services.get_replay_service",
                return_value=mock_replay,
            ),
            structlog.testing.capture_logs() as logs,
        ):
            eager = dlq_tasks.conditional_replay_on_circuit_close.apply(
                kwargs={"service_name": "payment-api", "max_items": 50},
                task_id="task-f",
            )
        result = eager.get()

        assert result["success"] is False
        failed = _entries(logs, "dlq.circuit_recovery_failed")
        assert len(failed) == 1, f"expected one failed event, got {logs}"
        entry = failed[0]
        assert entry["service_name"] == "payment-api"
        assert "replay boom" in entry["error"]
