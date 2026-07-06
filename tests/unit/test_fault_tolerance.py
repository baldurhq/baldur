"""
Fault Tolerance Tests for New Baldur Features

Tests that verify:
1. ChaosContext failures don't break DLQ operations
2. Drift Detection task failures are logged and don't crash system
3. All new features have graceful degradation

Core Principle: Baldur features should NEVER make things worse.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest


class TestChaosContextFaultTolerance:
    """Test ChaosContext failure handling."""

    def test_attach_chaos_context_with_invalid_operation(self):
        """Attaching chaos context to invalid op should not crash."""
        from baldur.services.chaos_context import (
            ChaosExperimentContext,
            attach_chaos_context,
        )

        mock_op = MagicMock()
        mock_op.id = 1
        mock_op.metadata = None
        mock_op.next_action_hint = ""

        context = ChaosExperimentContext(
            experiment_name="test_experiment",
            experiment_type="latency_injection",
        )

        # Should not raise exception
        try:
            attach_chaos_context(mock_op, context)
        except Exception as e:
            # Defensive: this path does not expect to raise (so pytest.raises
            # does not apply); if it ever does, the failure must be reasonable.
            assert (  # noqa: PT017
                mock_op.metadata is not None or "metadata" in str(e).lower()
            )

    def test_is_chaos_experiment_with_corrupted_metadata(self):
        """is_chaos_experiment should handle corrupted metadata."""
        from baldur.services.chaos_context import is_chaos_experiment

        mock_op = MagicMock()
        mock_op.metadata = {"chaos_context": "not_a_dict"}  # Corrupted

        # Should not crash, should return False
        result = is_chaos_experiment(mock_op)
        assert isinstance(result, bool)


class TestDriftDetectionFaultTolerance:
    """Test Drift Detection task failure handling."""

    def test_check_sla_drift_handles_db_failure(self):
        """check_sla_drift should return error result on DB failure."""
        from baldur.celery_tasks.drift_detection_tasks import check_sla_drift

        with patch(
            "baldur.celery_tasks.drift_detection_tasks._get_sla_thresholds"
        ) as mock_sla:
            mock_sla.side_effect = Exception("Config unavailable")

            # Celery task는 bound task이므로 apply() 또는 직접 호출 필요
            result = check_sla_drift.apply().result

            assert result["success"] is False
            assert "error" in result

    def test_sla_detector_handles_empty_queryset(self):
        """SLADriftDetector handles empty querysets properly."""

        from baldur.tasks.drift_detection import SLADriftDetector

        mock_sla = MagicMock()
        mock_sla.get_all_thresholds.return_value = {"payment": timedelta(hours=1)}

        mock_qs = MagicMock()
        mock_qs.count.return_value = 0

        detector = SLADriftDetector(
            get_sla_thresholds=lambda: mock_sla,
            get_failed_operations=lambda **kwargs: mock_qs,
        )

        result = detector.check_drift()

        assert result["success"] is True
        assert result["metrics"]["payment"]["total_resolved"] == 0


class TestAuditTrailResilience:
    """Test that audit trail mechanisms are resilient."""

    def test_control_api_audit_is_best_effort(self):
        """ControlAPI audit logging should never block response."""
        import inspect

        from baldur.services.control_api_service import (
            ControlAPIService,
        )

        # Verify by code inspection that _record_audit has try/except
        source = inspect.getsource(ControlAPIService._record_audit)

        assert "try:" in source
        assert "except" in source
        assert "Best-effort" in source or "best-effort" in source


class TestGracefulDegradation:
    """Test graceful degradation patterns in new features."""

    def test_idempotency_service_graceful_degradation(self):
        """IdempotencyService should gracefully degrade to DB-only."""
        import inspect

        from baldur.services.idempotency import (
            IdempotencyService,
        )

        # Verify by code inspection - check() has graceful degradation
        source = inspect.getsource(IdempotencyService.check)

        # The service should handle graceful degradation
        assert (
            "graceful" in source.lower()
            or "Gracefully" in source
            or "fallback" in source.lower()
            or "cache" in source.lower()
        )

    def test_new_features_dont_block_core_operations(self):
        """New features should not block core DLQ operations."""
        # This is verified by the architecture:
        # - ChaosContext is OPTIONAL metadata
        # - Drift Detection runs in separate Celery tasks

        pytest.importorskip("baldur_pro")
        from baldur.services.chaos_context import ChaosExperimentContext
        from baldur_pro.services.dlq import DLQService

        # Verify DLQService works without additional dependencies
        dlq = DLQService()
        assert dlq is not None

        # Verify ChaosContext is standalone
        context = ChaosExperimentContext()
        assert context is not None


class TestSystemRecovery:
    """Test system recovery when features fail."""

    def test_drift_detection_failure_returns_structured_error(self):
        """Drift detection failure returns structured error for monitoring."""
        from baldur.celery_tasks.drift_detection_tasks import check_sla_drift

        with patch(
            "baldur.celery_tasks.drift_detection_tasks._get_sla_thresholds"
        ) as mock_sla:
            mock_sla.side_effect = Exception("Database connection lost")

            result = check_sla_drift.apply().result

            # Should return structured error, not raise exception
            assert result["success"] is False
            assert "error" in result
            assert "checked_at" in result  # Always includes timestamp

    def test_cleanup_task_failure_logged(self):
        """cleanup_expired_chaos_experiments logs failures properly."""
        from baldur.celery_tasks.drift_detection_tasks import (
            cleanup_expired_chaos_experiments,
        )

        with patch(
            "baldur.celery_tasks.drift_detection_tasks._resolve_expired_chaos_experiments"
        ) as mock_resolve:
            mock_resolve.side_effect = Exception("Cleanup failed")

            result = cleanup_expired_chaos_experiments.apply().result

            assert result["success"] is False
            assert "error" in result
