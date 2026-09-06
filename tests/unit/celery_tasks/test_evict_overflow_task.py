"""
evict_overflow_dlq_entries Celery Task Unit Tests (329_DLQ_SIZE_LIMIT).

Test targets:
    - baldur.celery_tasks.dlq_tasks.evict_overflow_dlq_entries

Test Categories:
    A. Contract: Task decorator metadata (name, queue, time_limit, etc.)
    B. Behavior: Execution flow (success, exception handling)
    C. Behavior: Entitlement gate (refusal vocabulary, ordering before the lock)
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from baldur.core.entitlement import EntitlementResult, EntitlementStatus

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


# =============================================================================
# C. Behavior Tests — Entitlement gate
# =============================================================================


class _AlwaysAcquiredLock:
    """Distributed-recovery-lock double that always grants the lock.

    A real double rather than a spec-less mock: the task only ever calls
    ``acquire`` and ``release`` on it, and neither needs recording here.
    """

    def acquire(self, **kwargs) -> bool:
        return True

    def release(self, **kwargs) -> None:
        return None


class TestDlqOverflowEvictionEntitlementBehavior:
    """The lazy overflow sweep is PRO behaviour and needs an ACTIVE verdict.

    Refusing defers nothing: without a licence the DLQ store backing resolves
    to the OSS capture service, which enforces its overflow bound synchronously
    at store time, so this lane has no backlog to work on.

    The two refusals are deliberately distinct answers. ``not_entitled`` names
    a licensing condition an operator can fix; ``pro_not_installed`` names a
    tier that never had the lane. Collapsing them would tell an OSS-only
    deployment its licence is the problem.
    """

    _EVICTION = "baldur_pro.services.dlq.overflow.run_background_eviction"
    _LOCK = (
        "baldur_pro.services.coordination.distributed_recovery_lock."
        "DistributedRecoveryLock"
    )
    _OVERFLOW_MODULE = "baldur_pro.services.dlq.overflow"

    @staticmethod
    def _verdict(status):
        return patch(
            "baldur.core.entitlement.get_entitlement_status",
            return_value=EntitlementResult(status=status),
        )

    @pytest.mark.parametrize(
        "status",
        [EntitlementStatus.INVALID, EntitlementStatus.MISSING],
        ids=["invalid_licence", "no_licence"],
    )
    def test_lapsed_worker_returns_the_not_entitled_refusal(
        self, mock_pro_tier, status
    ):
        """A PRO install without an ACTIVE verdict refuses by name."""
        from baldur.celery_tasks.dlq_tasks import evict_overflow_dlq_entries

        with self._verdict(status):
            result = evict_overflow_dlq_entries()

        assert result == {"status": "skipped", "reason": "not_entitled"}

    def test_lapsed_worker_refuses_before_touching_the_lock_or_the_sweep(
        self, mock_pro_tier
    ):
        """The verdict is read ahead of the distributed lock, so no worker
        coordination happens for a sweep that will not run."""
        pytest.importorskip("baldur_pro")
        from baldur.celery_tasks.dlq_tasks import evict_overflow_dlq_entries

        with (
            self._verdict(EntitlementStatus.MISSING),
            patch(self._LOCK) as mock_lock,
            patch(self._EVICTION) as mock_eviction,
        ):
            evict_overflow_dlq_entries()

        mock_lock.assert_not_called()
        mock_eviction.assert_not_called()

    def test_oss_only_worker_returns_the_pro_absent_refusal(self, mock_oss_tier):
        """Presence is answered first and by its own name.

        The PRO import is failed by pinning ``None`` into ``sys.modules`` — the
        import system's own "halted" marker — so the arm is the same in a
        PRO-present and a PRO-absent checkout.
        """
        from baldur.celery_tasks.dlq_tasks import evict_overflow_dlq_entries

        with (
            patch("baldur.core.entitlement.get_entitlement_status") as mock_verdict,
            patch.dict(sys.modules, {self._OVERFLOW_MODULE: None}),
        ):
            result = evict_overflow_dlq_entries()

        assert result == {"status": "skipped", "reason": "pro_not_installed"}
        mock_verdict.assert_not_called()

    def test_entitled_worker_runs_the_sweep(self, mock_pro_tier):
        """The gate is a refusal, not a rewrite: an entitled worker still sweeps."""
        pytest.importorskip("baldur_pro")
        from baldur.celery_tasks.dlq_tasks import evict_overflow_dlq_entries

        eviction_result = {"evicted": 7, "reason": "above_target"}
        with (
            self._verdict(EntitlementStatus.ACTIVE),
            patch(self._LOCK, return_value=_AlwaysAcquiredLock()),
            patch(self._EVICTION, return_value=eviction_result) as mock_eviction,
        ):
            result = evict_overflow_dlq_entries()

        assert result == eviction_result
        mock_eviction.assert_called_once()
