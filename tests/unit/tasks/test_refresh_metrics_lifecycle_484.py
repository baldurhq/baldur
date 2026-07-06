"""Periodic refresh task tests for 484 D11.

Two refresh tasks publish lifecycle hygiene gauges from authoritative
service state on a Celery Beat cadence:

- ``refresh_audit_wal_metrics`` (hourly :05) — reads ``WriteAheadLog.get_stats()``
  and writes ``baldur_wal_total_files`` + ``baldur_wal_current_size_bytes``.
  Skipped (no-op success) when WAL is disabled (``_get_wal()`` returns None).

- ``refresh_governance_approval_metrics`` (every 5 minutes) — reads
  ``RuntimeConfigManager.get_approval_requests(status="PENDING")`` and writes
  the count + oldest-PENDING age in seconds. Tolerates malformed
  ``requested_at`` strings by skipping them in the min() reduction.

References:
- ``484`` D11
- ``src/baldur/tasks/cleanup_tasks.py::refresh_audit_wal_metrics``
- ``src/baldur/tasks/governance.py::refresh_governance_approval_metrics``
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from baldur.utils.time import utc_now

# =============================================================================
# D11 — refresh_audit_wal_metrics
# =============================================================================


class TestRefreshAuditWalMetricsBehavior:
    """484 D11: WAL gauges refresh task happy path + None-WAL guard."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_wal_disabled_returns_skipped_success(self):
        """``_get_wal()`` returns None → skipped: True, success: True (no gauge writes)."""
        from baldur.tasks.cleanup_tasks import refresh_audit_wal_metrics

        with (
            patch("baldur_pro.services.audit._get_wal", return_value=None),
            patch("baldur.metrics.drift_metrics.update_wal_total_files") as mock_files,
            patch(
                "baldur.metrics.drift_metrics.update_wal_current_size_bytes"
            ) as mock_bytes,
        ):
            result = refresh_audit_wal_metrics()

        assert result == {"success": True, "skipped": True}
        mock_files.assert_not_called()
        mock_bytes.assert_not_called()

    def test_wal_enabled_publishes_both_gauges_from_stats(self):
        """Happy path: both gauges set from ``wal.get_stats()`` totals."""
        from baldur.tasks.cleanup_tasks import refresh_audit_wal_metrics

        wal = MagicMock()
        stats = MagicMock()
        stats.total_files = 12
        stats.current_size_bytes = 4096 * 1024
        wal.get_stats.return_value = stats

        with (
            patch("baldur_pro.services.audit._get_wal", return_value=wal),
            patch("baldur.metrics.drift_metrics.update_wal_total_files") as mock_files,
            patch(
                "baldur.metrics.drift_metrics.update_wal_current_size_bytes"
            ) as mock_bytes,
        ):
            result = refresh_audit_wal_metrics()

        assert result["success"] is True
        assert result["skipped"] is False
        assert result["total_files"] == 12
        assert result["current_size_bytes"] == 4096 * 1024
        mock_files.assert_called_once_with(12)
        mock_bytes.assert_called_once_with(4096 * 1024)

    def test_wal_enabled_with_zero_stats_still_publishes(self):
        """Empty WAL → publish 0/0 (alerting needs the explicit current value)."""
        from baldur.tasks.cleanup_tasks import refresh_audit_wal_metrics

        wal = MagicMock()
        stats = MagicMock()
        stats.total_files = 0
        stats.current_size_bytes = 0
        wal.get_stats.return_value = stats

        with (
            patch("baldur_pro.services.audit._get_wal", return_value=wal),
            patch("baldur.metrics.drift_metrics.update_wal_total_files") as mock_files,
            patch(
                "baldur.metrics.drift_metrics.update_wal_current_size_bytes"
            ) as mock_bytes,
        ):
            result = refresh_audit_wal_metrics()

        assert result == {
            "success": True,
            "skipped": False,
            "total_files": 0,
            "current_size_bytes": 0,
        }
        mock_files.assert_called_once_with(0)
        mock_bytes.assert_called_once_with(0)

    def test_get_stats_exception_propagates_for_celery_retry(self):
        """``get_stats()`` raise → re-raised so Celery records and retries."""
        from baldur.tasks.cleanup_tasks import refresh_audit_wal_metrics

        wal = MagicMock()
        wal.get_stats.side_effect = OSError("disk gone")

        with patch("baldur_pro.services.audit._get_wal", return_value=wal):
            with pytest.raises(OSError, match="disk gone"):
                refresh_audit_wal_metrics()


# =============================================================================
# D11 — refresh_governance_approval_metrics
# =============================================================================


class TestRefreshGovernanceApprovalMetricsBehavior:
    """484 D11/D3: PENDING 4-eyes count + oldest-age refresh task."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    @pytest.fixture
    def patched_manager(self):
        """Patch ``get_runtime_config_manager`` and yield the mock manager."""
        manager = MagicMock()
        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            return_value=manager,
        ):
            yield manager

    @pytest.fixture
    def patched_recorder(self):
        """Patch the metrics facade so the test can inspect setter calls."""
        recorder = MagicMock()
        metrics = MagicMock()
        metrics.governance = recorder
        with patch(
            "baldur.metrics.prometheus.get_metrics",
            return_value=metrics,
        ):
            yield recorder

    def test_no_pending_publishes_zero_count_and_zero_age(
        self, patched_manager, patched_recorder
    ):
        """Empty PENDING list → count=0, oldest_age_seconds=0.0."""
        from baldur.tasks.governance import refresh_governance_approval_metrics

        patched_manager.get_approval_requests.return_value = []

        result = refresh_governance_approval_metrics()

        assert result["success"] is True
        assert result["pending_count"] == 0
        assert result["oldest_age_seconds"] == 0.0
        patched_recorder.set_pending_approval_count.assert_called_once_with(0)
        patched_recorder.set_oldest_pending_approval_age.assert_called_once_with(0.0)

    def test_single_pending_publishes_age_from_requested_at(
        self, patched_manager, patched_recorder
    ):
        """Single PENDING → age = utc_now - requested_at."""
        from baldur.tasks.governance import refresh_governance_approval_metrics

        requested_at = utc_now() - timedelta(seconds=30)
        patched_manager.get_approval_requests.return_value = [
            {"id": "r1", "requested_at": requested_at.isoformat()},
        ]

        result = refresh_governance_approval_metrics()

        assert result["pending_count"] == 1
        assert result["oldest_age_seconds"] >= 30.0
        assert result["oldest_age_seconds"] < 60.0  # allow scheduler skew
        patched_recorder.set_pending_approval_count.assert_called_once_with(1)
        # First positional arg of the age setter is in (30, 60) seconds.
        age_arg = patched_recorder.set_oldest_pending_approval_age.call_args.args[0]
        assert 30.0 <= age_arg < 60.0

    def test_oldest_among_many_is_chosen(self, patched_manager, patched_recorder):
        """``oldest_age_seconds`` reflects the earliest ``requested_at`` (largest age)."""
        from baldur.tasks.governance import refresh_governance_approval_metrics

        now = utc_now()
        patched_manager.get_approval_requests.return_value = [
            {"id": "young", "requested_at": (now - timedelta(seconds=5)).isoformat()},
            {"id": "oldest", "requested_at": (now - timedelta(hours=2)).isoformat()},
            {"id": "mid", "requested_at": (now - timedelta(minutes=10)).isoformat()},
        ]

        result = refresh_governance_approval_metrics()

        # Oldest = 2h ≈ 7200 seconds
        assert result["pending_count"] == 3
        assert 7195.0 <= result["oldest_age_seconds"] <= 7250.0

    @pytest.mark.parametrize(
        "bad_value",
        ["", "not-a-date", None, 12345],
        ids=["empty", "garbage", "none", "int"],
    )
    def test_malformed_requested_at_is_skipped_not_raised(
        self, patched_manager, patched_recorder, bad_value
    ):
        """Bad ``requested_at`` doesn't break the task — entry is skipped."""
        from baldur.tasks.governance import refresh_governance_approval_metrics

        good_at = utc_now() - timedelta(minutes=5)
        patched_manager.get_approval_requests.return_value = [
            {"id": "bad", "requested_at": bad_value},
            {"id": "good", "requested_at": good_at.isoformat()},
        ]

        result = refresh_governance_approval_metrics()

        # Both PENDING entries counted; only the parseable one drives the age.
        assert result["pending_count"] == 2
        assert 295.0 <= result["oldest_age_seconds"] <= 360.0

    def test_all_malformed_requested_at_yields_zero_age(
        self, patched_manager, patched_recorder
    ):
        """If no entry has a parseable timestamp, oldest_age_seconds = 0.0."""
        from baldur.tasks.governance import refresh_governance_approval_metrics

        patched_manager.get_approval_requests.return_value = [
            {"id": "a", "requested_at": ""},
            {"id": "b", "requested_at": "garbage"},
            {"id": "c"},  # missing key entirely
        ]

        result = refresh_governance_approval_metrics()

        assert result["pending_count"] == 3
        assert result["oldest_age_seconds"] == 0.0
        patched_recorder.set_pending_approval_count.assert_called_once_with(3)
        patched_recorder.set_oldest_pending_approval_age.assert_called_once_with(0.0)

    def test_metric_publish_failure_is_swallowed(self, patched_manager):
        """Metrics-side errors are logged but don't fail the task.

        The refresh task is observability-only; a broken Prometheus client
        must not raise back into Celery and trigger noisy retries.
        """
        from baldur.tasks.governance import refresh_governance_approval_metrics

        patched_manager.get_approval_requests.return_value = []

        with patch(
            "baldur.metrics.prometheus.get_metrics",
            side_effect=RuntimeError("metrics down"),
        ):
            result = refresh_governance_approval_metrics()

        # Outer try-block's success path still returns a populated dict.
        assert result["success"] is True
        assert result["pending_count"] == 0

    def test_get_approval_requests_filters_by_pending_status(
        self, patched_manager, patched_recorder
    ):
        """Task asks the manager for PENDING-only entries (label discipline)."""
        from baldur.tasks.governance import refresh_governance_approval_metrics

        patched_manager.get_approval_requests.return_value = []

        refresh_governance_approval_metrics()

        patched_manager.get_approval_requests.assert_called_once_with(status="PENDING")

    def test_manager_exception_propagates_for_celery_retry(self, patched_manager):
        """Manager failure re-raises so Celery's ``max_retries=1`` policy triggers."""
        from baldur.tasks.governance import refresh_governance_approval_metrics

        patched_manager.get_approval_requests.side_effect = ConnectionError("db down")

        with pytest.raises(ConnectionError, match="db down"):
            refresh_governance_approval_metrics()
