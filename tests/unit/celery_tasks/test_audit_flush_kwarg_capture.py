"""audit_flush_tasks kwarg vocabulary sanity tests (511 D9 + D12, 600 D2/D3).

Verifies that:
  1. Each rewritten emit lands the expected kwarg keys in the captured
     ``event_dict`` (typo regression catch).
  2. ``logger.bind(task_id=...)`` discipline holds across consecutive task
     invocations on the same worker (D12 isolation guarantee).

Mock points (600 D2): buffer acquisition patches the module-level accessor
``audit_flush_tasks.get_redis_audit_buffer``; the flush target patches
``ProviderRegistry.get_audit_adapter`` (the registry sink replaced the old
``_get_target_adapter`` helper). The effective drain gate is forced ON so the
tasks run their body instead of early-exiting.

Scope: 1 success-path emit per task. NOT exhaustive — sanity-only.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.settings.audit import override_audit_settings


def _entries(logs: list[dict], event_name: str) -> list[dict]:
    return [e for e in logs if e.get("event") == event_name]


@pytest.fixture(autouse=True)
def _drain_gate_on():
    """Force the effective drain gate ON for every test in this module."""
    with override_audit_settings(enabled=True, buffer_redis_enabled=True):
        yield


def _non_null_adapter() -> MagicMock:
    """A registry adapter that is NOT a NullAuditLogAdapter (passes the guard)."""
    return MagicMock()


class TestAuditFlushKwargCaptureBehavior:
    """Sanity check: rewritten audit_flush_tasks emits carry typed kwargs."""

    def test_flush_redis_audit_buffer_completed_kwargs(self):
        """audit_flush.redis_buffer_flushed lands flushed_count + duration_ms."""
        pytest.importorskip("baldur_pro")
        import structlog

        from baldur.celery_tasks import audit_flush_tasks

        mock_buffer = MagicMock()
        mock_buffer.flush_to_external_safe.return_value = 42

        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock_cls = MagicMock(return_value=mock_lock)

        with (
            patch(
                "baldur_pro.services.coordination.distributed_recovery_lock.DistributedRecoveryLock",
                mock_lock_cls,
            ),
            patch.object(
                audit_flush_tasks, "get_redis_audit_buffer", return_value=mock_buffer
            ),
            patch(
                "baldur.factory.ProviderRegistry.get_audit_adapter",
                return_value=_non_null_adapter(),
            ),
            structlog.testing.capture_logs() as logs,
        ):
            eager = audit_flush_tasks.flush_redis_audit_buffer.apply(
                kwargs={"batch_size": 100}, task_id="task-xyz"
            )
        result = eager.get()

        assert result["status"] == "success"
        entries = _entries(logs, "audit_flush.redis_buffer_flushed")
        assert len(entries) == 1, f"Expected one redis_buffer_flushed log, got: {logs}"
        entry = entries[0]
        assert entry["flushed_count"] == 42
        assert "duration_ms" in entry
        # D12 bind: task_id is implicit on every bound_logger emit
        assert entry["task_id"] == "task-xyz"

    def test_recover_orphaned_processing_queues_kwargs(self):
        """audit_flush.orphaned_queues_recovered lands recovered_total."""
        import structlog

        from baldur.celery_tasks import audit_flush_tasks

        mock_buffer = MagicMock()
        mock_buffer.recover_orphaned_processing_queues.return_value = 7

        with (
            patch.object(
                audit_flush_tasks, "get_redis_audit_buffer", return_value=mock_buffer
            ),
            structlog.testing.capture_logs() as logs,
        ):
            eager = audit_flush_tasks.recover_orphaned_processing_queues.apply(
                kwargs={"timeout_seconds": 300}, task_id="task-abc"
            )
        result = eager.get()

        assert result["status"] == "success"
        entries = _entries(logs, "audit_flush.orphaned_queues_recovered")
        assert len(entries) == 1
        entry = entries[0]
        assert entry["recovered_total"] == 7
        assert entry["task_id"] == "task-abc"

    def test_apply_audit_buffer_safety_ltrim_kwargs(self):
        """audit_flush.safety_ltrim_applied lands trimmed_domains + total_trimmed."""
        import structlog

        from baldur.celery_tasks import audit_flush_tasks

        mock_buffer = MagicMock()
        mock_buffer.apply_safety_ltrim.return_value = {"payment": 5, "webhook": 3}

        with (
            patch.object(
                audit_flush_tasks, "get_redis_audit_buffer", return_value=mock_buffer
            ),
            structlog.testing.capture_logs() as logs,
        ):
            eager = audit_flush_tasks.apply_audit_buffer_safety_ltrim.apply(
                task_id="task-trim"
            )
        result = eager.get()

        assert result["status"] == "success"
        entries = _entries(logs, "audit_flush.safety_ltrim_applied")
        assert len(entries) == 1
        entry = entries[0]
        assert entry["trimmed_domains"] == {"payment": 5, "webhook": 3}
        assert entry["total_trimmed"] == 8
        assert entry["task_id"] == "task-trim"


class TestAuditFlushBindIsolationBehavior:
    """D12 bind discipline: consecutive invocations must not leak task_id."""

    def test_consecutive_task_invocations_have_independent_task_id(self):
        """Each invocation's bound_logger only sees its own self.request.id."""
        import structlog

        from baldur.celery_tasks import audit_flush_tasks

        mock_buffer = MagicMock()
        mock_buffer.apply_safety_ltrim.return_value = {}

        with (
            patch.object(
                audit_flush_tasks, "get_redis_audit_buffer", return_value=mock_buffer
            ),
            structlog.testing.capture_logs() as logs_a,
        ):
            audit_flush_tasks.apply_audit_buffer_safety_ltrim.apply(task_id="task-A")

        with (
            patch.object(
                audit_flush_tasks, "get_redis_audit_buffer", return_value=mock_buffer
            ),
            structlog.testing.capture_logs() as logs_b,
        ):
            audit_flush_tasks.apply_audit_buffer_safety_ltrim.apply(task_id="task-B")

        a_entries = _entries(logs_a, "audit_flush.safety_ltrim_applied")
        b_entries = _entries(logs_b, "audit_flush.safety_ltrim_applied")
        assert a_entries
        assert a_entries[0]["task_id"] == "task-A"
        assert b_entries
        assert b_entries[0]["task_id"] == "task-B"
        # No cross-contamination: A's logs do not see task-B, vice versa.
        assert all(e.get("task_id") == "task-A" for e in a_entries)
        assert all(e.get("task_id") == "task-B" for e in b_entries)
