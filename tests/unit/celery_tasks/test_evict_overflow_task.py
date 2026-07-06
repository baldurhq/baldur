"""
evict_overflow_dlq_entries Celery Task Unit Tests (329_DLQ_SIZE_LIMIT).

Test targets:
    - baldur.celery_tasks.dlq_tasks.evict_overflow_dlq_entries

Test Categories:
    A. Contract: Task decorator metadata (name, queue, time_limit, etc.)
    B. Behavior: Execution flow (success, exception handling)
"""

from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# A. Contract Tests — Task metadata
# =============================================================================


class TestEvictOverflowTaskMetadataContract:
    """evict_overflow_dlq_entries task decorator contract values."""

    def test_task_name_matches_contract(self):
        """Task name: baldur.celery_tasks.evict_overflow_dlq_entries."""
        from baldur.celery_tasks.dlq_tasks import evict_overflow_dlq_entries

        assert (
            evict_overflow_dlq_entries.name
            == "baldur.celery_tasks.evict_overflow_dlq_entries"
        )

    def test_task_queue_is_maintenance(self):
        """Task queue: maintenance."""
        from baldur.celery_tasks.dlq_tasks import evict_overflow_dlq_entries

        assert evict_overflow_dlq_entries.queue == "maintenance"

    def test_task_max_retries_is_zero(self):
        """Max retries: 0 (no automatic retries)."""
        from baldur.celery_tasks.dlq_tasks import evict_overflow_dlq_entries

        assert evict_overflow_dlq_entries.max_retries == 0

    def test_task_time_limit_is_120(self):
        """Hard timeout: 120 seconds."""
        from baldur.celery_tasks.dlq_tasks import evict_overflow_dlq_entries

        assert evict_overflow_dlq_entries.time_limit == 120

    def test_task_soft_time_limit_is_110(self):
        """Soft timeout: 110 seconds."""
        from baldur.celery_tasks.dlq_tasks import evict_overflow_dlq_entries

        assert evict_overflow_dlq_entries.soft_time_limit == 110


# =============================================================================
# B. Behavior Tests — Execution flow
# =============================================================================


class TestEvictOverflowTaskBehavior:
    """evict_overflow_dlq_entries execution behavior."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def _mock_drl(self):
        """Create a mock DistributedRecoveryLock that always acquires."""
        return patch(
            "baldur_pro.services.coordination.distributed_recovery_lock.DistributedRecoveryLock",
            return_value=MagicMock(acquire=MagicMock(return_value=True)),
        )

    def test_success_returns_eviction_result(self):
        """Successful execution returns run_background_eviction result."""
        eviction_result = {"evicted": 150, "reason": "above_target"}

        with (
            patch(
                "baldur_pro.services.dlq.overflow.run_background_eviction",
                return_value=eviction_result,
            ),
            self._mock_drl(),
        ):
            from baldur.celery_tasks.dlq_tasks import evict_overflow_dlq_entries

            result = evict_overflow_dlq_entries()

        assert result == eviction_result

    def test_exception_returns_error_dict(self):
        """Exception during eviction returns error dict with success=False."""
        with (
            patch(
                "baldur_pro.services.dlq.overflow.run_background_eviction",
                side_effect=RuntimeError("Redis connection lost"),
            ),
            patch("baldur.celery_tasks.dlq_tasks.logger"),
            self._mock_drl(),
        ):
            from baldur.celery_tasks.dlq_tasks import evict_overflow_dlq_entries

            result = evict_overflow_dlq_entries()

        assert result["success"] is False
        assert "Redis connection lost" in result["error"]

    def test_calls_run_background_eviction(self):
        """Task delegates to run_background_eviction."""
        mock_eviction = MagicMock(return_value={"evicted": 0, "reason": "below_target"})

        with (
            patch(
                "baldur_pro.services.dlq.overflow.run_background_eviction",
                mock_eviction,
            ),
            self._mock_drl(),
        ):
            from baldur.celery_tasks.dlq_tasks import evict_overflow_dlq_entries

            evict_overflow_dlq_entries()

        mock_eviction.assert_called_once()
