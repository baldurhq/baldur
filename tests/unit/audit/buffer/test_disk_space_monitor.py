"""DiskSpaceMonitor unit tests.

Tests boundary analysis, exception handling, and dependency interactions
for disk space monitoring in the disk-persistent buffer.
"""

from __future__ import annotations

import collections
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from baldur.audit.persistence.config import DiskBufferSettings
from baldur.audit.persistence.disk_space_monitor import DiskSpaceMonitor

# Reusable namedtuple for shutil.disk_usage results
_Usage = collections.namedtuple("usage", ["total", "used", "free"])


# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture
def settings() -> DiskBufferSettings:
    """DiskBufferSettings with known thresholds."""
    return DiskBufferSettings(
        disk_full_threshold=0.05,
        disk_recovery_threshold=0.1,
    )


@pytest.fixture
def monitor(settings: DiskBufferSettings, tmp_path: Path) -> DiskSpaceMonitor:
    """DiskSpaceMonitor bound to a test path."""
    return DiskSpaceMonitor(path=tmp_path / "test", settings=settings)


# =============================================================================
# Behavior: check() boundary analysis
# =============================================================================


class TestDiskSpaceMonitorCheckBehavior:
    """check() method boundary and behavior tests."""

    @patch(
        "baldur.audit.persistence.disk_space_monitor.shutil.disk_usage",
        autospec=True,
    )
    def test_check_above_threshold_returns_ok_true(self, mock_usage, monitor, settings):
        """Free ratio above disk_full_threshold returns (True, ratio)."""
        # Given — 20% free, well above 5% threshold
        mock_usage.return_value = _Usage(total=100, used=80, free=20)

        # When
        ok, ratio = monitor.check()

        # Then
        assert ok is True
        assert ratio == pytest.approx(0.20)

    @patch(
        "baldur.audit.persistence.disk_space_monitor.shutil.disk_usage",
        autospec=True,
    )
    def test_check_below_threshold_returns_ok_false(
        self, mock_usage, monitor, settings
    ):
        """Free ratio below disk_full_threshold returns (False, ratio)."""
        # Given — 3% free, below 5% threshold
        mock_usage.return_value = _Usage(total=1000, used=970, free=30)

        # When
        ok, ratio = monitor.check()

        # Then
        assert ok is False
        assert ratio == pytest.approx(0.03)

    @patch(
        "baldur.audit.persistence.disk_space_monitor.shutil.disk_usage",
        autospec=True,
    )
    def test_check_exactly_at_threshold_returns_ok_true(
        self, mock_usage, monitor, settings
    ):
        """Free ratio exactly at disk_full_threshold returns (True, ratio)."""
        # Given — 5% free, exactly at the 0.05 threshold (>= check)
        mock_usage.return_value = _Usage(total=1000, used=950, free=50)

        # When
        ok, ratio = monitor.check()

        # Then
        assert ok is True
        assert ratio == pytest.approx(settings.disk_full_threshold)

    @patch(
        "baldur.audit.persistence.disk_space_monitor.shutil.disk_usage",
        autospec=True,
    )
    def test_check_just_below_threshold_returns_ok_false(
        self, mock_usage, monitor, settings
    ):
        """Free ratio just below disk_full_threshold returns (False, ratio)."""
        # Given — 4.9% free, just below the 5% threshold
        mock_usage.return_value = _Usage(total=1000, used=951, free=49)

        # When
        ok, ratio = monitor.check()

        # Then
        assert ok is False
        assert ratio == pytest.approx(0.049)


# =============================================================================
# Behavior: check() exception handling (fail-open)
# =============================================================================


class TestDiskSpaceMonitorCheckExceptionBehavior:
    """check() graceful failure (fail-open) tests."""

    @patch(
        "baldur.audit.persistence.disk_space_monitor.shutil.disk_usage",
        autospec=True,
    )
    def test_check_disk_usage_raises_returns_fail_open(self, mock_usage, monitor):
        """When shutil.disk_usage raises, returns (True, -1.0) for fail-open."""
        mock_usage.side_effect = OSError("Disk not accessible")

        ok, ratio = monitor.check()

        assert ok is True
        assert ratio == -1.0

    @patch(
        "baldur.audit.persistence.disk_space_monitor.shutil.disk_usage",
        autospec=True,
    )
    def test_check_unexpected_exception_returns_fail_open(self, mock_usage, monitor):
        """Any exception during disk check returns fail-open tuple."""
        mock_usage.side_effect = RuntimeError("Unexpected error")

        ok, ratio = monitor.check()

        assert ok is True
        assert ratio == -1.0


# =============================================================================
# Behavior: should_recover()
# =============================================================================


class TestDiskSpaceMonitorShouldRecoverBehavior:
    """should_recover() threshold comparison tests."""

    def test_should_recover_above_threshold_returns_true(self, monitor, settings):
        """Free ratio above recovery_threshold returns True."""
        ratio = settings.disk_recovery_threshold + 0.05
        assert monitor.should_recover(ratio) is True

    def test_should_recover_below_threshold_returns_false(self, monitor, settings):
        """Free ratio below recovery_threshold returns False."""
        ratio = settings.disk_recovery_threshold - 0.05
        assert monitor.should_recover(ratio) is False

    def test_should_recover_exactly_at_threshold_returns_false(self, monitor, settings):
        """Free ratio exactly at recovery_threshold returns False (strict >)."""
        assert monitor.should_recover(settings.disk_recovery_threshold) is False


# =============================================================================
# Behavior: should_fail_open()
# =============================================================================


class TestDiskSpaceMonitorShouldFailOpenBehavior:
    """should_fail_open() delegates to check()."""

    @patch(
        "baldur.audit.persistence.disk_space_monitor.shutil.disk_usage",
        autospec=True,
    )
    def test_should_fail_open_when_disk_full(self, mock_usage, monitor):
        """Returns True when check() says disk is full (ok=False)."""
        mock_usage.return_value = _Usage(total=1000, used=970, free=30)

        assert monitor.should_fail_open() is True

    @patch(
        "baldur.audit.persistence.disk_space_monitor.shutil.disk_usage",
        autospec=True,
    )
    def test_should_not_fail_open_when_space_available(self, mock_usage, monitor):
        """Returns False when check() says space is available (ok=True)."""
        mock_usage.return_value = _Usage(total=100, used=80, free=20)

        assert monitor.should_fail_open() is False


# =============================================================================
# Behavior: is_healthy()
# =============================================================================


class TestDiskSpaceMonitorIsHealthyBehavior:
    """is_healthy() health-check helper tests."""

    @patch(
        "baldur.audit.persistence.disk_space_monitor.shutil.disk_usage",
        autospec=True,
    )
    def test_is_healthy_above_recovery_threshold_returns_ok(self, mock_usage, monitor):
        """Returns ok=True when free ratio is above recovery threshold."""
        mock_usage.return_value = _Usage(total=100, used=80, free=20)

        ok, ratio, errors = monitor.is_healthy()

        assert ok is True
        assert ratio == pytest.approx(0.20)
        assert errors == []

    @patch(
        "baldur.audit.persistence.disk_space_monitor.shutil.disk_usage",
        autospec=True,
    )
    def test_is_healthy_below_recovery_threshold_adds_error(
        self, mock_usage, monitor, settings
    ):
        """Returns ok=False with error message when below recovery threshold."""
        # Given — 5% free, below 10% recovery threshold
        mock_usage.return_value = _Usage(total=1000, used=950, free=50)

        # When
        ok, ratio, errors = monitor.is_healthy()

        # Then
        assert ok is False
        assert ratio == pytest.approx(0.05)
        assert len(errors) == 1
        assert "Low disk space" in errors[0]

    @patch(
        "baldur.audit.persistence.disk_space_monitor.shutil.disk_usage",
        autospec=True,
    )
    def test_is_healthy_disk_usage_exception_adds_error(self, mock_usage, monitor):
        """Returns ok=False with error message when disk_usage raises."""
        mock_usage.side_effect = OSError("Permission denied")

        ok, ratio, errors = monitor.is_healthy()

        assert ok is False
        assert ratio == -1.0
        assert len(errors) == 1
        assert "Cannot check disk space" in errors[0]


# =============================================================================
# Behavior: execute_priority_purge()
# =============================================================================


class TestDiskSpaceMonitorPurgeBehavior:
    """execute_priority_purge() purge logic tests."""

    def test_purge_with_count_below_100_returns_zero(self, monitor):
        """No purge when fewer than 100 entries exist."""
        count_fn = MagicMock(return_value=99)
        iter_fn = MagicMock()
        delete_fn = MagicMock()

        result = monitor.execute_priority_purge(
            count_fn=count_fn,
            iter_fn=iter_fn,
            delete_batch_fn=delete_fn,
        )

        assert result == 0
        iter_fn.assert_not_called()
        delete_fn.assert_not_called()

    def test_purge_with_count_exactly_100_purges_oldest_10_percent(self, monitor):
        """Purges oldest 10% when exactly 100 entries exist."""
        # Given
        count_fn = MagicMock(return_value=100)
        entries = [MagicMock(key=f"key_{i}") for i in range(10)]
        iter_fn = MagicMock(return_value=entries)
        delete_fn = MagicMock(return_value=10)

        # When
        result = monitor.execute_priority_purge(
            count_fn=count_fn,
            iter_fn=iter_fn,
            delete_batch_fn=delete_fn,
        )

        # Then
        assert result == 10
        iter_fn.assert_called_once_with(limit=10)  # 100 // 10 = 10
        delete_fn.assert_called_once()

    def test_purge_with_1000_entries_deletes_100(self, monitor):
        """Purges 10% of 1000 entries (100 entries)."""
        # Given
        count_fn = MagicMock(return_value=1000)
        entries = [MagicMock(key=f"key_{i}") for i in range(100)]
        iter_fn = MagicMock(return_value=entries)
        delete_fn = MagicMock(return_value=100)

        # When
        result = monitor.execute_priority_purge(
            count_fn=count_fn,
            iter_fn=iter_fn,
            delete_batch_fn=delete_fn,
        )

        # Then
        assert result == 100
        iter_fn.assert_called_once_with(limit=100)


# =============================================================================
# Behavior: send_disk_full_alert()
# =============================================================================


class TestDiskSpaceMonitorAlertBehavior:
    """send_disk_full_alert() graceful degradation tests."""

    @patch(
        "baldur.audit.persistence.disk_space_monitor.UnifiedNotificationManager",
        create=True,
    )
    def test_send_disk_full_alert_handles_import_error(self, _mock_manager, monitor):
        """send_disk_full_alert does not crash when import fails."""
        with patch.dict(
            "sys.modules",
            {"baldur_pro.services.unified_notification": None},
        ):
            # Should not raise
            monitor.send_disk_full_alert()

    def test_send_disk_full_alert_handles_generic_exception(self, monitor):
        """send_disk_full_alert catches all exceptions gracefully."""
        with patch(
            "baldur.audit.persistence.disk_space_monitor.DiskSpaceMonitor.send_disk_full_alert",
            autospec=True,
            side_effect=None,
        ):
            # Direct call should not raise — best-effort
            monitor.send_disk_full_alert()
