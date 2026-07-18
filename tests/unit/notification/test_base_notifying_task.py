"""
BaseNotifyingTask 단위 테스트

기반 구조 테스트:
- NotificationPolicy 기본값 및 설정
- NotificationTiming 동작
- NotificationThreshold 임계값 체크
- BaseNotifyingTask.should_notify() 검증
- BaseNotifyingTask._get_effective_timing() 검증
- BaseNotifyingTask._record_audit_trail() 검증
- DailyAutonomousReport 집계
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, Mock, patch

import pytest

from baldur.models.emergency import EmergencyLevel
from baldur.tasks.base import (
    BaseNotifyingTask,
    get_cooldown_status,
    reset_cooldowns,
)
from baldur.tasks.daily_report import DailyAutonomousReport
from baldur.tasks.notification_policy import (
    NotificationPolicy,
    NotificationThreshold,
    NotificationTiming,
)

# =============================================================================
# NotificationTiming 테스트
# =============================================================================


class TestNotificationTiming:
    """NotificationTiming enum 테스트."""

    def test_timing_values(self):
        """모든 타이밍 값 확인."""
        assert NotificationTiming.BEFORE.value == "before"
        assert NotificationTiming.AFTER.value == "after"
        assert NotificationTiming.REALTIME.value == "realtime"
        assert NotificationTiming.AGGREGATED.value == "aggregated"

    def test_timing_is_string_enum(self):
        """문자열 비교 가능 확인."""
        assert NotificationTiming.BEFORE == "before"
        assert NotificationTiming.AFTER == "after"


# =============================================================================
# NotificationThreshold 테스트
# =============================================================================


class TestNotificationThreshold:
    """NotificationThreshold 데이터클래스 테스트."""

    def test_default_values(self):
        """기본값 확인."""
        threshold = NotificationThreshold()
        assert threshold.log_only == 5.0
        assert threshold.warning == 20.0
        assert threshold.critical == 50.0

    def test_get_severity_critical(self):
        """임계치 초과 시 critical 반환."""
        threshold = NotificationThreshold()
        assert threshold.get_severity(100) == "critical"
        assert threshold.get_severity(50) == "critical"

    def test_get_severity_warning(self):
        """warning 임계치 초과 시 warning 반환."""
        threshold = NotificationThreshold()
        assert threshold.get_severity(30) == "warning"
        assert threshold.get_severity(20) == "warning"

    def test_get_severity_info(self):
        """log_only 이상 warning 미만 시 info 반환."""
        threshold = NotificationThreshold()
        assert threshold.get_severity(10) == "info"
        assert threshold.get_severity(5) == "info"

    def test_get_severity_none_below_log_only(self):
        """log_only 미만 시 None 반환 (알림 안함)."""
        threshold = NotificationThreshold()
        assert threshold.get_severity(4) is None
        assert threshold.get_severity(0) is None

    def test_custom_thresholds(self):
        """커스텀 임계값 설정."""
        threshold = NotificationThreshold(
            log_only=1.0,
            warning=10.0,
            critical=100.0,
        )
        assert threshold.get_severity(0.5) is None  # Below log_only
        assert threshold.get_severity(5) == "info"  # Between log_only and warning
        assert threshold.get_severity(50) == "warning"  # Between warning and critical
        assert threshold.get_severity(150) == "critical"  # Above critical


# =============================================================================
# NotificationPolicy 테스트
# =============================================================================


class TestNotificationPolicy:
    """NotificationPolicy 데이터클래스 테스트."""

    def test_default_values(self):
        """기본값 확인."""
        policy = NotificationPolicy()
        assert policy.timing == NotificationTiming.AFTER
        assert policy.aggregate is False
        assert policy.threshold is None
        assert policy.threshold_field == ""
        assert policy.cooldown_seconds == 300
        assert policy.default_severity == "info"
        assert policy.channels == ["slack"]
        assert policy.requires_approval is False
        assert policy.escalate_on_emergency is True

    def test_custom_policy(self):
        """커스텀 정책 설정."""
        policy = NotificationPolicy(
            timing=NotificationTiming.BEFORE,
            aggregate=True,
            threshold=10,
            threshold_field="count",
            cooldown_seconds=3600,
            default_severity="warning",
            channels=["slack", "email"],
            requires_approval=True,
            escalate_on_emergency=False,
        )
        assert policy.timing == NotificationTiming.BEFORE
        assert policy.aggregate is True
        assert policy.threshold == 10
        assert policy.threshold_field == "count"
        assert policy.cooldown_seconds == 3600
        assert policy.default_severity == "warning"
        assert policy.channels == ["slack", "email"]
        assert policy.requires_approval is True
        assert policy.escalate_on_emergency is False


# =============================================================================
# DailyAutonomousReport 테스트
# =============================================================================


class TestDailyAutonomousReport:
    """DailyAutonomousReport 데이터클래스 테스트."""

    def test_default_values(self):
        """기본값 확인."""
        report = DailyAutonomousReport()
        assert report.archived_count == 0
        assert report.expired_count == 0
        assert report.purged_count == 0
        assert report.recovered_count == 0
        assert report.drift_warnings_count == 0
        assert report.custom_counts == {}

    def test_to_dict(self):
        """딕셔너리 변환 확인."""
        report = DailyAutonomousReport(
            archived_count=10,
            expired_count=5,
        )
        data = report.to_dict()
        assert data["archived_count"] == 10
        assert data["expired_count"] == 5
        assert "date" in data

    def test_merge_reports(self):
        """리포트 병합 확인 - to_slack_message가 제거되었으므로 대체 테스트."""
        report1 = DailyAutonomousReport(
            archived_count=10,
            expired_count=5,
            purged_count=2,
        )
        report2 = DailyAutonomousReport(
            archived_count=5,
            expired_count=3,
            recovered_count=7,
        )
        report1.merge(report2)

        assert report1.archived_count == 15
        assert report1.expired_count == 8
        assert report1.purged_count == 2
        assert report1.recovered_count == 7

    def test_custom_counts_in_to_dict(self):
        """커스텀 카운트가 to_dict에 포함되는지 확인."""
        report = DailyAutonomousReport(
            custom_counts={"특수작업": 42},
        )
        data = report.to_dict()
        assert data["custom_counts"]["특수작업"] == 42


# =============================================================================
# BaseNotifyingTask 테스트
# =============================================================================


class TestBaseNotifyingTask:
    """BaseNotifyingTask 클래스 테스트."""

    def setup_method(self):
        """테스트 전 초기화."""
        reset_cooldowns()

    def teardown_method(self):
        """테스트 후 정리."""
        reset_cooldowns()

    def test_default_policy(self):
        """기본 정책 확인."""
        task = BaseNotifyingTask()
        assert task.notification_policy.timing == NotificationTiming.AFTER
        assert task.notification_policy.default_severity == "info"

    def test_run_not_implemented(self):
        """run() 미구현 시 에러 발생."""
        task = BaseNotifyingTask()
        with pytest.raises(NotImplementedError):
            task.run()

    def test_should_notify_with_no_threshold(self):
        """임계값 없을 때 알림 여부."""
        task = BaseNotifyingTask()
        task.name = "test_task"
        result = {"count": 5}

        assert task._should_notify(result) is True

    def test_should_notify_below_threshold(self):
        """임계값 미달 시 알림 안함."""
        task = BaseNotifyingTask()
        task.name = "test_task"
        task.notification_policy = NotificationPolicy(
            threshold=10,
            threshold_field="count",
        )
        result = {"count": 5}  # Below threshold

        assert task._should_notify(result) is False

    def test_should_notify_above_threshold(self):
        """임계값 초과 시 알림."""
        task = BaseNotifyingTask()
        task.name = "test_task"
        task.notification_policy = NotificationPolicy(
            threshold=10,
            threshold_field="count",
        )
        result = {"count": 15}  # Above threshold

        assert task._should_notify(result) is True

    def test_should_notify_cooldown(self):
        """Alert is suppressed during the cooldown."""
        task = BaseNotifyingTask()
        task.name = "test_task"
        task.notification_policy = NotificationPolicy(
            cooldown_seconds=300,
        )

        # First alert
        result = {"count": 5}
        assert task._should_notify(result) is True

        # Reserve the cooldown slot
        BaseNotifyingTask._alert_gate.try_reserve("test_task:default", 300.0)

        # Second alert (within cooldown)
        assert task._should_notify(result) is False

    def test_cooldown_expired(self):
        """Alert is allowed once the cooldown has expired."""
        task = BaseNotifyingTask()
        task.name = "test_task"
        task.notification_policy = NotificationPolicy(
            cooldown_seconds=1,  # 1 second
        )

        # Seed a reservation aged past the 1s cooldown.
        with patch("time.time", return_value=time.time() - 10):
            BaseNotifyingTask._alert_gate.try_reserve("test_task:default", 1.0)

        result = {"count": 5}
        assert task._should_notify(result) is True

    def test_get_effective_timing_normal(self):
        """일반 상태에서 원래 타이밍 반환."""
        task = BaseNotifyingTask()
        task.notification_policy = NotificationPolicy(
            timing=NotificationTiming.AGGREGATED,
        )

        # emergency_mode가 없으면 기본 타이밍 반환
        timing = task._get_effective_timing()
        assert timing == NotificationTiming.AGGREGATED

    def test_get_effective_timing_emergency_level_3(self):
        """Emergency Level 3에서 REALTIME으로 에스컬레이션 (모듈 있을 때)."""
        task = BaseNotifyingTask()
        task.notification_policy = NotificationPolicy(
            timing=NotificationTiming.AGGREGATED,
            escalate_on_emergency=True,
        )

        # emergency_mode 모듈이 없는 경우 기본 타이밍 반환
        # 실제 모듈이 있으면 Level 3+에서 REALTIME으로 에스컬레이션
        timing = task._get_effective_timing()
        # 모듈이 없으면 기본값, 있으면 레벨에 따라 변경
        assert timing in [NotificationTiming.AGGREGATED, NotificationTiming.REALTIME]

    def test_get_effective_timing_no_escalation(self):
        """에스컬레이션 비활성화 시 원래 타이밍 유지."""
        task = BaseNotifyingTask()
        task.notification_policy = NotificationPolicy(
            timing=NotificationTiming.AGGREGATED,
            escalate_on_emergency=False,
        )

        timing = task._get_effective_timing()
        assert timing == NotificationTiming.AGGREGATED

    def test_get_severity_error(self):
        """에러 결과는 항상 critical."""
        task = BaseNotifyingTask()
        result = {"error": "Something went wrong"}

        assert task._get_severity(result) == "critical"

    def test_get_severity_success_false(self):
        """success=False는 critical."""
        task = BaseNotifyingTask()
        result = {"success": False}

        assert task._get_severity(result) == "critical"

    def test_get_severity_default(self):
        """성공 결과는 기본 severity."""
        task = BaseNotifyingTask()
        task.notification_policy = NotificationPolicy(default_severity="info")
        result = {"success": True, "count": 5}

        assert task._get_severity(result) == "info"

    def test_get_summary_message_error(self):
        """Error result message (428 Phase 5.1: Korean → English)."""
        task = BaseNotifyingTask()
        result = {"error": "Test error"}

        message = task._get_summary_message(result)
        assert "failed" in message.lower()
        assert "Test error" in message

    def test_get_summary_message_count(self):
        """카운트 결과 메시지."""
        task = BaseNotifyingTask()
        result = {"archived_count": 42}

        message = task._get_summary_message(result)
        assert "42" in message

    def test_has_meaningful_result_with_error(self):
        """에러 결과는 의미있음."""
        task = BaseNotifyingTask()
        result = {"error": "Test"}

        assert task._has_meaningful_result(result) is True

    def test_has_meaningful_result_with_zero_count(self):
        """0건 결과는 의미없음."""
        task = BaseNotifyingTask()
        result = {"count": 0}

        assert task._has_meaningful_result(result) is False

    def test_has_meaningful_result_with_positive_count(self):
        """양수 카운트는 의미있음."""
        task = BaseNotifyingTask()
        result = {"count": 5}

        assert task._has_meaningful_result(result) is True

    def test_get_alert_key_with_domain(self):
        """도메인 기반 알림 키."""
        task = BaseNotifyingTask()
        result = {"domain": "payment"}

        key = task._get_alert_key(result)
        assert key == "payment"

    def test_get_alert_key_default(self):
        """기본 알림 키."""
        task = BaseNotifyingTask()
        result = {"count": 5}

        key = task._get_alert_key(result)
        assert key == "default"

    @patch("baldur.factory.ProviderRegistry.get_audit_adapter")
    def test_record_audit_trail(self, mock_get_adapter):
        """notification_sent is emitted via the canonical AuditLogAdapter (G5)."""
        from baldur.interfaces.audit_adapter import AuditEntry, AuditLogAdapter

        # spec'd spy: a reintroduced phantom log_event would raise AttributeError.
        spy = MagicMock(spec=AuditLogAdapter)
        mock_get_adapter.return_value = spy

        task = BaseNotifyingTask()
        task.name = "test_task"
        task.request = Mock(id="task-123")
        result = {"count": 5}

        task._record_audit_trail(result)

        spy.log.assert_called_once()
        entry = spy.log.call_args.args[0]
        assert isinstance(entry, AuditEntry)
        assert entry.action == "notification_sent"
        assert entry.target_type == "celery_task"
        assert entry.target_id == "task-123"
        assert entry.details["task_name"] == "test_task"

    @patch("baldur.factory.ProviderRegistry.get_audit_adapter")
    def test_record_audit_trail_adapter_raises_fails_open(self, mock_get_adapter):
        """D8: a raising adapter does not propagate (best-effort, fail-open)."""
        from baldur.interfaces.audit_adapter import AuditLogAdapter

        spy = MagicMock(spec=AuditLogAdapter)
        spy.log.side_effect = RuntimeError("audit backend unavailable")
        mock_get_adapter.return_value = spy

        task = BaseNotifyingTask()
        task.name = "test_task"
        task.request = Mock(id="task-123")

        # Must not raise.
        task._record_audit_trail({"count": 5})

    @patch("baldur.factory.ProviderRegistry.get_audit_adapter")
    def test_record_audit_trail_adapter_raises_increments_drop_counter(
        self, mock_get_adapter
    ):
        """D8: a raising adapter increments audit_emit_dropped_total{site=...}."""
        from baldur.interfaces.audit_adapter import AuditLogAdapter
        from baldur.metrics.audit_emit_metrics import (
            METRICS_AVAILABLE,
            audit_emit_dropped_total,
        )

        if not METRICS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

        spy = MagicMock(spec=AuditLogAdapter)
        spy.log.side_effect = RuntimeError("audit backend unavailable")
        mock_get_adapter.return_value = spy
        before = audit_emit_dropped_total.labels(
            site="celery_notifying_task"
        )._value.get()

        task = BaseNotifyingTask()
        task.name = "test_task"
        task.request = Mock(id="task-123")
        task._record_audit_trail({"count": 5})

        after = audit_emit_dropped_total.labels(
            site="celery_notifying_task"
        )._value.get()
        assert after - before == 1.0


# =============================================================================
# Emergency Level escalation (real EmergencyLevel enum — regression guard)
# =============================================================================


class TestEmergencyLevelEscalation:
    """Regression: emergency-level gates compare against EmergencyLevel.severity.

    EmergencyManager.get_current_level() returns an EmergencyLevel ((str, Enum)),
    not an int, so a bare ``level >= 3`` comparison raised TypeError at runtime —
    swallowed by the surrounding try/except, leaving the Level 3 escalation dead
    (surfaced by the Cat 3.9 E2E scenario). These tests drive the real enum through
    ProviderRegistry.emergency_manager so the .severity comparison is exercised and
    cannot silently regress to the int-mock fiction that hid the bug.
    """

    @staticmethod
    def _provider_with_level(level: EmergencyLevel):
        manager = MagicMock()
        manager.get_current_level.return_value = level
        provider = MagicMock()
        provider.safe_get.return_value = manager
        return provider

    def test_effective_timing_realtime_at_level_3(self):
        task = BaseNotifyingTask()
        task.notification_policy = NotificationPolicy(
            timing=NotificationTiming.AGGREGATED,
            escalate_on_emergency=True,
        )
        with patch(
            "baldur.factory.registry.ProviderRegistry.emergency_manager",
            self._provider_with_level(EmergencyLevel.LEVEL_3),
        ):
            assert task._get_effective_timing() == NotificationTiming.REALTIME

    def test_effective_timing_not_escalated_below_level_3(self):
        task = BaseNotifyingTask()
        task.notification_policy = NotificationPolicy(
            timing=NotificationTiming.AGGREGATED,
            escalate_on_emergency=True,
        )
        with patch(
            "baldur.factory.registry.ProviderRegistry.emergency_manager",
            self._provider_with_level(EmergencyLevel.LEVEL_2),
        ):
            assert task._get_effective_timing() == NotificationTiming.AGGREGATED

    def test_should_skip_approval_at_level_3(self):
        task = BaseNotifyingTask()
        with patch(
            "baldur.factory.registry.ProviderRegistry.emergency_manager",
            self._provider_with_level(EmergencyLevel.LEVEL_3),
        ):
            assert task._should_skip_approval() is True

    def test_should_skip_approval_false_below_level_3(self):
        task = BaseNotifyingTask()
        with patch(
            "baldur.factory.registry.ProviderRegistry.emergency_manager",
            self._provider_with_level(EmergencyLevel.LEVEL_2),
        ):
            assert task._should_skip_approval() is False


# =============================================================================
# Custom Task 구현 테스트
# =============================================================================


class TestCustomTaskImplementation:
    """커스텀 태스크 구현 테스트."""

    def setup_method(self):
        """테스트 전 초기화."""
        reset_cooldowns()

    def test_custom_task_with_policy(self):
        """커스텀 정책 태스크."""

        class MyTask(BaseNotifyingTask):
            notification_policy = NotificationPolicy(
                timing=NotificationTiming.AGGREGATED,
                aggregate=True,
                threshold=5,
                threshold_field="archived_count",
                default_severity="info",
            )

            def run(self, days=30):
                return {"archived_count": 10, "days": days}

            def _get_summary_message(self, result):
                return f"📦 아카이브: {result['archived_count']}건"

        task = MyTask()
        task.name = "test_archive_task"

        # 정책 확인
        assert task.notification_policy.timing == NotificationTiming.AGGREGATED
        assert task.notification_policy.threshold == 5

        # 실행
        result = task.run(days=30)
        assert result["archived_count"] == 10

        # 메시지 확인
        message = task._get_summary_message(result)
        assert "아카이브: 10건" in message

    def test_high_risk_task_requires_approval(self):
        """고위험 태스크 승인 요구."""

        class HighRiskTask(BaseNotifyingTask):
            notification_policy = NotificationPolicy(
                timing=NotificationTiming.BEFORE,
                requires_approval=True,
                default_severity="critical",
            )

            def run(self, days=90):
                return {"purged_count": 100}

        task = HighRiskTask()
        task.name = "purge_task"

        assert task.notification_policy.requires_approval is True
        assert task.notification_policy.timing == NotificationTiming.BEFORE


# =============================================================================
# 헬퍼 함수 테스트
# =============================================================================


class TestHelperFunctions:
    """모듈 레벨 헬퍼 함수 테스트."""

    def setup_method(self):
        """테스트 전 초기화."""
        reset_cooldowns()

    def test_reset_cooldowns(self):
        """Cooldown reset clears every reservation."""
        # Reserve a cooldown slot
        BaseNotifyingTask._alert_gate.try_reserve("test:key", 300.0)
        assert BaseNotifyingTask._alert_gate.keys()

        # Reset
        reset_cooldowns()
        assert BaseNotifyingTask._alert_gate.keys() == []

    def test_get_cooldown_status(self):
        """Cooldown status query returns an isoformat timestamp per key."""
        reserved, token = BaseNotifyingTask._alert_gate.try_reserve("test:key", 300.0)
        assert reserved is True

        status = get_cooldown_status()
        assert "test:key" in status
        assert status["test:key"] == datetime.fromtimestamp(token, tz=UTC).isoformat()


# =============================================================================
# 통합 시나리오 테스트
# =============================================================================


class TestIntegrationScenarios:
    """통합 시나리오 테스트."""

    def setup_method(self):
        """테스트 전 초기화."""
        reset_cooldowns()

    @patch("baldur.tasks.base.notify")
    @patch("baldur.audit.get_audit_logger")
    def test_full_task_execution_flow(self, mock_audit, mock_notify):
        """Full task execution flow."""
        mock_audit.return_value = Mock()
        mock_notify.return_value = Mock()

        class TestTask(BaseNotifyingTask):
            notification_policy = NotificationPolicy(
                timing=NotificationTiming.AFTER,
                default_severity="info",
            )

            def run(self, count=5):
                return {"success": True, "count": count}

        task = TestTask()
        task.name = "test_full_flow"

        # __call__로 실행 (pre/post 훅 포함)
        result = task(count=10)

        assert result["success"] is True
        assert result["count"] == 10

    def test_threshold_based_notification(self):
        """임계값 기반 알림 테스트."""

        class ThresholdTask(BaseNotifyingTask):
            notification_policy = NotificationPolicy(
                timing=NotificationTiming.AFTER,
                threshold=10,
                threshold_field="suspicious_count",
                default_severity="warning",
            )

            def run(self):
                return {"suspicious_count": 5}  # Below threshold

            def _has_meaningful_result(self, result):
                # suspicious_count가 있으면 의미있는 결과
                return result.get("suspicious_count", 0) > 0

        task = ThresholdTask()
        task.name = "threshold_task"

        result = task.run()

        # 임계값 미달로 알림 안함
        assert task._should_notify(result) is False

        # 임계값 초과 결과
        result_high = {"suspicious_count": 15}
        assert task._should_notify(result_high) is True

    def test_cooldown_prevents_spam(self):
        """쿨다운으로 알림 스팸 방지."""

        class FrequentTask(BaseNotifyingTask):
            notification_policy = NotificationPolicy(
                cooldown_seconds=60,  # 1분
            )

            def run(self):
                return {"count": 1}

        task = FrequentTask()
        task.name = "frequent_task"

        result = task.run()

        # First alert allowed
        assert task._should_notify(result) is True
        BaseNotifyingTask._alert_gate.try_reserve("frequent_task:default", 60.0)

        # Second alert suppressed (within cooldown)
        assert task._should_notify(result) is False
