"""
Common Messaging Types 단위 테스트.

테스트 대상: interfaces/messaging_common.py
- MessageSeverity: 통합 심각도 열거형
- MessageChannel: 공유 채널 열거형

테스트 대상: interfaces/notification.py
- NotificationSeverity/NotificationChannel: 하위 호환 별칭

테스트 대상: interfaces/alert_adapter.py
- AlertSeverity: MessageSeverity 하위 호환 별칭
"""

from __future__ import annotations

from baldur.interfaces.alert_adapter import AlertSeverity
from baldur.interfaces.messaging_common import MessageChannel, MessageSeverity
from baldur.interfaces.notification import (
    NotificationChannel,
    NotificationSeverity,
)

# =============================================================================
# MessageSeverity — 계약 검증
# =============================================================================


class TestMessageSeverityContract:
    """MessageSeverity 열거형 멤버 계약 검증."""

    def test_has_six_members(self):
        """MessageSeverity는 6개 멤버를 가진다."""
        assert len(MessageSeverity) == 6

    def test_critical_value(self):
        """CRITICAL 값: 'critical'."""
        assert MessageSeverity.CRITICAL.value == "critical"

    def test_high_value(self):
        """HIGH 값: 'high'."""
        assert MessageSeverity.HIGH.value == "high"

    def test_warning_value(self):
        """WARNING 값: 'warning'."""
        assert MessageSeverity.WARNING.value == "warning"

    def test_medium_value(self):
        """MEDIUM 값: 'medium'."""
        assert MessageSeverity.MEDIUM.value == "medium"

    def test_low_value(self):
        """LOW 값: 'low'."""
        assert MessageSeverity.LOW.value == "low"

    def test_info_value(self):
        """INFO 값: 'info'."""
        assert MessageSeverity.INFO.value == "info"

    def test_is_str_enum(self):
        """MessageSeverity는 str을 상속한다."""
        assert isinstance(MessageSeverity.CRITICAL, str)


# =============================================================================
# MessageChannel — 계약 검증
# =============================================================================


class TestMessageChannelContract:
    """MessageChannel 열거형 멤버 계약 검증."""

    def test_has_seven_members(self):
        """MessageChannel has seven members (email/sms removed in 657)."""
        assert len(MessageChannel) == 7

    def test_slack_value(self):
        """SLACK 값: 'slack'."""
        assert MessageChannel.SLACK.value == "slack"

    def test_teams_value(self):
        """TEAMS 값: 'teams'."""
        assert MessageChannel.TEAMS.value == "teams"

    def test_pagerduty_value(self):
        """PAGERDUTY 값: 'pagerduty'."""
        assert MessageChannel.PAGERDUTY.value == "pagerduty"

    def test_webhook_value(self):
        """WEBHOOK 값: 'webhook'."""
        assert MessageChannel.WEBHOOK.value == "webhook"

    def test_stdout_value(self):
        """STDOUT 값: 'stdout'."""
        assert MessageChannel.STDOUT.value == "stdout"

    def test_file_value(self):
        """FILE 값: 'file'."""
        assert MessageChannel.FILE.value == "file"

    def test_log_value(self):
        """LOG value: 'log' (OSS logging-adapter channel token)."""
        assert MessageChannel.LOG.value == "log"

    def test_is_str_enum(self):
        """MessageChannel은 str을 상속한다."""
        assert isinstance(MessageChannel.SLACK, str)


# =============================================================================
# Backward-compatible Aliases — 동작 검증
# =============================================================================


class TestNotificationAliasesBehavior:
    """NotificationSeverity/NotificationChannel 하위 호환 별칭 검증."""

    def test_notification_severity_is_message_severity(self):
        """NotificationSeverity는 MessageSeverity와 동일하다."""
        assert NotificationSeverity is MessageSeverity

    def test_notification_channel_is_message_channel(self):
        """NotificationChannel은 MessageChannel과 동일하다."""
        assert NotificationChannel is MessageChannel

    def test_notification_severity_members_accessible(self):
        """NotificationSeverity로 모든 멤버에 접근 가능하다."""
        assert NotificationSeverity.CRITICAL.value == "critical"
        assert NotificationSeverity.HIGH.value == "high"
        assert NotificationSeverity.MEDIUM.value == "medium"
        assert NotificationSeverity.LOW.value == "low"
        assert NotificationSeverity.INFO.value == "info"


class TestAlertSeverityAliasBehavior:
    """AlertSeverity 하위 호환 별칭 동작 검증."""

    def test_alert_severity_is_message_severity(self):
        """AlertSeverity는 MessageSeverity와 동일하다."""
        assert AlertSeverity is MessageSeverity

    def test_alert_severity_backward_compat_members(self):
        """기존 AlertSeverity 멤버(CRITICAL, WARNING, INFO)에 접근 가능하다."""
        assert AlertSeverity.CRITICAL.value == "critical"
        assert AlertSeverity.WARNING.value == "warning"
        assert AlertSeverity.INFO.value == "info"
