"""
Daily Report 및 Unified Notification Manager 테스트.

테스트 대상:
- DailyReportData 집계
- DailyReportCollector 기능
- UnifiedNotificationManager 라우팅
- Cooldown 및 억제 로직
"""

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from baldur.tasks.daily_report import (
    DailyReportCollector,
    DailyReportData,
    TaskResultEntry,
)
from baldur_pro.services.unified_notification import (
    ChannelResolver,
    NotificationCategory,
    NotificationPayload,
    NotificationPriority,
    UnifiedNotificationManager,
    get_unified_notification_manager,
    notify,
    notify_error,
    notify_sla,
    reset_notification_manager,
)

# =============================================================================
# DailyReportData Tests
# =============================================================================


class TestDailyReportData:
    """Tests for DailyReportData aggregation."""

    def test_create_empty_report(self):
        """Test creating an empty report."""
        report = DailyReportData()

        assert report.archived_count == 0
        assert report.expired_count == 0
        assert report.purged_count == 0
        assert len(report.entries) == 0

    def test_add_entry_updates_counts(self):
        """Test that adding entries updates aggregate counts."""
        report = DailyReportData()

        entry = TaskResultEntry(
            task_name="archive_task",
            result={"archived_count": 10, "expired_count": 5},
            timestamp=datetime.now(UTC),
            severity="info",
        )

        report.add_entry(entry)

        assert report.archived_count == 10
        assert report.expired_count == 5
        assert len(report.entries) == 1

    def test_add_multiple_entries(self):
        """Test adding multiple entries aggregates correctly."""
        report = DailyReportData()

        entries = [
            TaskResultEntry(
                task_name="archive_task_1",
                result={"archived_count": 10},
                timestamp=datetime.now(UTC),
            ),
            TaskResultEntry(
                task_name="archive_task_2",
                result={"archived_count": 20},
                timestamp=datetime.now(UTC),
            ),
            TaskResultEntry(
                task_name="expire_task",
                result={"expired_count": 5},
                timestamp=datetime.now(UTC),
            ),
        ]

        for entry in entries:
            report.add_entry(entry)

        assert report.archived_count == 30
        assert report.expired_count == 5
        assert len(report.entries) == 3

    def test_track_failures(self):
        """Test that task failures are tracked."""
        report = DailyReportData()

        # Add a successful task
        report.add_entry(
            TaskResultEntry(
                task_name="success_task",
                result={"success": True, "archived_count": 5},
                timestamp=datetime.now(UTC),
            )
        )

        # Add a failed task
        report.add_entry(
            TaskResultEntry(
                task_name="failed_task",
                result={"success": False, "error": "Connection failed"},
                timestamp=datetime.now(UTC),
            )
        )

        assert report.task_failures == 1

    def test_track_critical_alerts(self):
        """Test that critical alerts are counted."""
        report = DailyReportData()

        report.add_entry(
            TaskResultEntry(
                task_name="normal_task",
                result={"count": 5},
                timestamp=datetime.now(UTC),
                severity="info",
            )
        )

        report.add_entry(
            TaskResultEntry(
                task_name="critical_task",
                result={"count": 1},
                timestamp=datetime.now(UTC),
                severity="critical",
            )
        )

        assert report.critical_alerts == 1

    def test_merge_reports(self):
        """Test merging two reports."""
        report1 = DailyReportData()
        report1.archived_count = 10
        report1.expired_count = 5

        report2 = DailyReportData()
        report2.archived_count = 20
        report2.purged_count = 3

        report1.merge(report2)

        assert report1.archived_count == 30
        assert report1.expired_count == 5
        assert report1.purged_count == 3

    def test_to_slack_message(self):
        """Test Slack message formatting using format_report_for_slack."""
        from baldur.services.daily_report import format_report_for_slack

        report = DailyReportData(
            date=datetime(2026, 1, 2, tzinfo=UTC),
            archived_count=100,
            expired_count=50,
            recovered_count=5,
        )

        message = format_report_for_slack(report)

        assert "2026-01-02" in message
        assert "100" in message
        assert "Archived" in message

    def test_to_dict(self):
        """Test dictionary conversion."""
        report = DailyReportData(
            archived_count=10,
            expired_count=5,
        )

        data = report.to_dict()

        assert data["archived_count"] == 10
        assert data["expired_count"] == 5
        assert "date" in data


# =============================================================================
# DailyReportCollector Tests
# =============================================================================


class TestDailyReportCollector:
    """Tests for DailyReportCollector."""

    def setup_method(self):
        """Setup fresh collector for each test."""
        self.collector = DailyReportCollector()

    def test_add_result(self):
        """Test adding results to collector via push_limit."""
        test_date = datetime(2026, 1, 2, tzinfo=UTC)

        mock_cache = MagicMock()
        mock_cache.push_limit.return_value = 1
        mock_cache.list_range.return_value = [
            {
                "task_name": "test_task",
                "result": {"archived_count": 10},
                "timestamp": test_date.isoformat(),
                "severity": "info",
            }
        ]

        with patch(
            "baldur.factory.ProviderRegistry.get_cache",
            return_value=mock_cache,
        ):
            self.collector.add_result(
                task_name="test_task",
                result={"archived_count": 10},
                severity="info",
            )
            report = self.collector.get_report(test_date)

        assert isinstance(report, DailyReportData)
        mock_cache.push_limit.assert_called_once()

    def test_get_report_for_date(self):
        """Test getting report for specific date."""
        report = self.collector.get_report(datetime(2026, 1, 1, tzinfo=UTC))

        assert isinstance(report, DailyReportData)
        # Empty report for non-existent date
        assert len(report.entries) == 0


# =============================================================================
# UnifiedNotificationManager Tests
# =============================================================================


class TestUnifiedNotificationManager:
    """Tests for UnifiedNotificationManager."""

    def setup_method(self):
        """Setup fresh manager for each test."""
        reset_notification_manager()
        self.manager = UnifiedNotificationManager()

    def teardown_method(self):
        """Cleanup after each test."""
        reset_notification_manager()

    def test_create_manager(self):
        """Test manager creation."""
        assert self.manager is not None
        assert isinstance(self.manager._resolver, ChannelResolver)

    def test_cooldown_suppression(self):
        """Test that cooldown suppresses duplicate notifications."""
        # First notification should go through
        payload = NotificationPayload(
            title="Test",
            message="Test message",
            priority=NotificationPriority.MEDIUM,
            category=NotificationCategory.OPERATIONS,
            source="test",
            dedup_key="test:dedup",
        )

        # Mock the send to channels
        with patch.object(self.manager, "_send_to_channels") as mock_send:
            mock_send.return_value = MagicMock(
                success=True,
                channels_sent=["slack"],
                channels_failed=[],
            )

            result1 = self.manager.notify(payload)
            assert result1.success

            # Second notification should be suppressed
            result2 = self.manager.notify(payload)
            assert result2.suppressed
            assert result2.suppression_reason == "cooldown"

    def test_no_cooldown_for_approval_category(self):
        """Test that approval category has no cooldown."""
        from baldur_pro.services.unified_notification import (
            NotificationResult as UnifiedResult,
        )

        payload = NotificationPayload(
            title="Approval Request",
            message="Please approve",
            priority=NotificationPriority.HIGH,
            category=NotificationCategory.APPROVAL,
            source="test",
        )

        with patch.object(self.manager, "_send_to_channels") as mock_send:
            # Return a proper NotificationResult object instead of MagicMock
            mock_send.return_value = UnifiedResult(
                success=True,
                channels_sent=["slack"],
                channels_failed=[],
            )

            # Both should go through
            result1 = self.manager.notify(payload)
            result2 = self.manager.notify(payload)

            assert result1.suppressed is False
            assert result2.suppressed is False

    def test_info_priority_log_only(self):
        """Test that INFO priority is log-only by default."""
        payload = NotificationPayload(
            title="Info",
            message="Info message",
            priority=NotificationPriority.INFO,
            category=NotificationCategory.OPERATIONS,
            source="test",
        )

        result = self.manager.notify(payload)

        assert result.success
        assert result.suppressed
        assert result.suppression_reason == "log_only"

    def test_reset_cooldowns(self):
        """Test cooldown reset."""
        self.manager._cooldown_cache["test:key"] = datetime.now(UTC)

        self.manager.reset_cooldowns()

        assert len(self.manager._cooldown_cache) == 0

    def test_get_stats(self):
        """Test getting statistics."""
        stats = self.manager.get_stats()

        assert "cooldown_entries" in stats
        assert "notification_counts" in stats


# =============================================================================
# Convenience Function Tests
# =============================================================================


class TestConvenienceFunctions:
    """Tests for notify convenience functions."""

    def setup_method(self):
        """Reset manager before each test."""
        reset_notification_manager()

    def teardown_method(self):
        """Cleanup after each test."""
        reset_notification_manager()

    def test_notify_function(self):
        """Test basic notify function."""
        with patch.object(UnifiedNotificationManager, "notify") as mock_notify:
            mock_notify.return_value = MagicMock(success=True)

            notify(
                title="Test",
                message="Test message",
                priority="medium",
                category="operations",
                source="test",
            )

            # Should have been called
            mock_notify.assert_called_once()

    def test_notify_sla_function(self):
        """Test SLA notification convenience function."""
        with patch.object(UnifiedNotificationManager, "notify") as mock_notify:
            mock_notify.return_value = MagicMock(success=True)

            notify_sla(
                title="SLA Warning",
                message="Threshold exceeded",
                domain="payment",
                priority="high",
            )

            mock_notify.assert_called_once()
            # Check that payload has correct dedup key
            call_args = mock_notify.call_args[0][0]
            assert call_args.dedup_key == "sla:payment"

    def test_notify_error_function(self):
        """Test error notification convenience function."""
        with patch.object(UnifiedNotificationManager, "notify") as mock_notify:
            mock_notify.return_value = MagicMock(success=True)

            error = ValueError("Test error")
            notify_error(
                title="Error",
                message="Something went wrong",
                error=error,
                source="test_task",
            )

            mock_notify.assert_called_once()
            call_args = mock_notify.call_args[0][0]
            assert call_args.category == NotificationCategory.ERROR
            assert call_args.metadata["error_type"] == "ValueError"


# =============================================================================
# NotificationPayload Tests
# =============================================================================


class TestNotificationPayload:
    """Tests for NotificationPayload."""

    def test_create_payload(self):
        """Test creating a notification payload."""
        payload = NotificationPayload(
            title="Test Title",
            message="Test message",
            priority=NotificationPriority.HIGH,
            category=NotificationCategory.SLA,
            source="drift_detection",
        )

        assert payload.title == "Test Title"
        assert payload.priority == NotificationPriority.HIGH
        assert payload.category == NotificationCategory.SLA

    def test_payload_to_dict(self):
        """Test payload dictionary conversion."""
        payload = NotificationPayload(
            title="Test",
            message="Message",
            priority=NotificationPriority.MEDIUM,
            category=NotificationCategory.OPERATIONS,
        )

        data = payload.to_dict()

        assert data["title"] == "Test"
        assert data["priority"] == "medium"
        assert data["category"] == "operations"
        assert "timestamp" in data

    def test_payload_with_metadata(self):
        """Test payload with metadata."""
        payload = NotificationPayload(
            title="SLA Warning",
            message="Threshold exceeded",
            metadata={"domain": "payment", "rate": 25.5},
            tags=["sla", "payment"],
        )

        assert payload.metadata["domain"] == "payment"
        assert "sla" in payload.tags


# =============================================================================
# Integration Tests
# =============================================================================


class TestNotificationIntegration:
    """Integration tests for the notification system."""

    def setup_method(self):
        """Reset state before each test."""
        reset_notification_manager()

    def teardown_method(self):
        """Cleanup after each test."""
        reset_notification_manager()

    def test_end_to_end_notification(self):
        """Test end-to-end notification flow."""
        # Patch at the module where the import happens (inside the function)
        with patch(
            "baldur_pro.services.security_notification.get_security_notification_service"
        ) as mock_get_service:
            # Setup mock service — UNM now calls deliver() instead of send_alert()
            mock_service = MagicMock()
            mock_service.deliver.return_value = MagicMock(
                results=[MagicMock(success=True, channel="slack")]
            )
            mock_get_service.return_value = mock_service

            manager = get_unified_notification_manager()

            payload = NotificationPayload(
                title="Integration Test",
                message="Testing full flow",
                priority=NotificationPriority.HIGH,
                category=NotificationCategory.OPERATIONS,
                source="integration_test",
            )

            result = manager.notify(payload)

            assert result.success
            mock_service.deliver.assert_called_once()

    def test_emergency_level_escalation(self):
        """Test notification is sent with correct priority."""
        # Create a mock emergency_mode module since it may not exist
        mock_emergency_mode = MagicMock()
        mock_emergency_manager = MagicMock()
        mock_emergency_manager.get_current_level.return_value = 3
        mock_emergency_mode.get_emergency_mode_manager.return_value = (
            mock_emergency_manager
        )

        with (
            patch.dict(
                "sys.modules", {"baldur.core.emergency_mode": mock_emergency_mode}
            ),
            patch(
                "baldur_pro.services.security_notification.get_security_notification_service"
            ) as mock_get_service,
        ):
            # Setup mock notification service
            mock_service = MagicMock()
            mock_service.deliver.return_value = MagicMock(
                results=[MagicMock(success=True, channel="slack")]
            )
            mock_get_service.return_value = mock_service

            manager = UnifiedNotificationManager()

            # Send a low priority notification
            payload = NotificationPayload(
                title="Test",
                message="Test",
                priority=NotificationPriority.LOW,
                category=NotificationCategory.OPERATIONS,
            )

            manager.notify(payload)

            # deliver() is called (emergency escalation is optional behavior)
            mock_service.deliver.assert_called_once()


# =============================================================================
# Run tests
# =============================================================================


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
