"""
Tests for Phase 3 Settings extensions and source file refactoring.

File: tests/unit/settings/test_phase3_settings.py

Tests:
1. New Settings fields in audit_settings.py
2. New Settings fields in canary.py
3. New Settings fields in api_view.py
4. New Settings files: intelligence_task.py, drift_detection.py
5. Source file Settings integration
"""

import os
from unittest import mock


class TestAuditSettingsPhase3Extension:
    """Test AuditSettings Phase 3 extensions."""

    def setup_method(self):
        """Reset settings before each test."""
        from baldur.settings.audit import reset_audit_settings

        reset_audit_settings()

    def teardown_method(self):
        """Reset settings after each test."""
        from baldur.settings.audit import reset_audit_settings

        reset_audit_settings()

    def test_self_audit_default_values(self):
        """Test self_audit related default values."""
        from baldur.settings.audit import get_audit_settings

        settings = get_audit_settings()

        assert settings.self_audit_max_recent_events == 100
        assert settings.self_audit_default_limit == 20
        assert settings.self_audit_max_failure_rate == 0.1

    def test_cascade_rate_window_default(self):
        """Test cascade_rate_window_seconds default value."""
        from baldur.settings.audit import get_audit_settings

        settings = get_audit_settings()

        assert settings.cascade_rate_window_seconds == 1.0

    def test_self_audit_env_override(self):
        """Test environment variable override for self_audit settings."""
        from baldur.settings.audit import (
            get_audit_settings,
            reset_audit_settings,
        )

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_AUDIT_SELF_AUDIT_MAX_RECENT_EVENTS": "200",
                "BALDUR_AUDIT_SELF_AUDIT_DEFAULT_LIMIT": "50",
                "BALDUR_AUDIT_SELF_AUDIT_MAX_FAILURE_RATE": "0.2",
            },
        ):
            reset_audit_settings()
            settings = get_audit_settings()

            assert settings.self_audit_max_recent_events == 200
            assert settings.self_audit_default_limit == 50
            assert settings.self_audit_max_failure_rate == 0.2


class TestCanarySettingsPhase3Extension:
    """Test CanarySettings Phase 3 extensions."""

    def setup_method(self):
        """Reset settings before each test."""
        from baldur.settings.canary import reset_canary_settings

        reset_canary_settings()

    def teardown_method(self):
        """Reset settings after each test."""
        from baldur.settings.canary import reset_canary_settings

        reset_canary_settings()

    def test_api_view_defaults(self):
        """Test default_completed_rollouts_limit."""
        from baldur.settings.canary import get_canary_settings

        settings = get_canary_settings()

        assert settings.default_completed_rollouts_limit == 20

    def test_env_override(self):
        """Test environment variable override."""
        from baldur.settings.canary import (
            get_canary_settings,
            reset_canary_settings,
        )

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_CANARY_DEFAULT_COMPLETED_ROLLOUTS_LIMIT": "50",
            },
        ):
            reset_canary_settings()
            settings = get_canary_settings()

            assert settings.default_completed_rollouts_limit == 50


class TestApiViewSettingsPhase3Extension:
    """Test ApiViewSettings Phase 3 extensions."""

    def setup_method(self):
        """Reset settings before each test."""
        from baldur.settings.api_view import reset_api_view_settings

        reset_api_view_settings()

    def teardown_method(self):
        """Reset settings after each test."""
        from baldur.settings.api_view import reset_api_view_settings

        reset_api_view_settings()

    def test_auto_tuning_defaults(self):
        """Test auto_tuning related default values."""
        from baldur.settings.api_view import get_api_view_settings

        settings = get_api_view_settings()

        assert settings.auto_tuning_export_limit == 1000
        assert settings.auto_tuning_default_page_size == 20

    def test_xtest_observability_defaults(self):
        """Test XTest observability related default values."""
        from baldur.settings.api_view import get_api_view_settings

        settings = get_api_view_settings()

        assert settings.xtest_timeline_default_limit == 50
        assert settings.postmortem_history_limit == 100

    def test_env_override(self):
        """Test environment variable override."""
        from baldur.settings.api_view import (
            get_api_view_settings,
            reset_api_view_settings,
        )

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_API_VIEW_AUTO_TUNING_EXPORT_LIMIT": "5000",
                "BALDUR_API_VIEW_XTEST_TIMELINE_DEFAULT_LIMIT": "100",
            },
        ):
            reset_api_view_settings()
            settings = get_api_view_settings()

            assert settings.auto_tuning_export_limit == 5000
            assert settings.xtest_timeline_default_limit == 100


class TestIntelligenceTaskSettings:
    """Test IntelligenceTaskSettings."""

    def setup_method(self):
        """Reset settings before each test."""
        from baldur.settings.intelligence_task import (
            reset_intelligence_task_settings,
        )

        reset_intelligence_task_settings()

    def teardown_method(self):
        """Reset settings after each test."""
        from baldur.settings.intelligence_task import (
            reset_intelligence_task_settings,
        )

        reset_intelligence_task_settings()

    def test_default_values(self):
        """Test default values are set correctly."""
        from baldur.settings.intelligence_task import (
            get_intelligence_task_settings,
        )

        settings = get_intelligence_task_settings()

        assert settings.default_cooldown_seconds == 3600
        assert settings.recovery_check_cooldown_seconds == 120
        assert settings.execution_threshold == 10
        assert settings.analysis_threshold_minutes == 60
        assert settings.batch_size == 100
        assert settings.severity_high_threshold == 50
        assert settings.severity_medium_threshold == 10
        assert settings.reconciliation_cutoff_minutes == 30
        assert settings.insight_threshold == 3

    def test_env_override(self):
        """Test environment variable override."""
        from baldur.settings.intelligence_task import (
            get_intelligence_task_settings,
            reset_intelligence_task_settings,
        )

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_INTELLIGENCE_TASK_DEFAULT_COOLDOWN_SECONDS": "7200",
                "BALDUR_INTELLIGENCE_TASK_BATCH_SIZE": "200",
            },
        ):
            reset_intelligence_task_settings()
            settings = get_intelligence_task_settings()

            assert settings.default_cooldown_seconds == 7200
            assert settings.batch_size == 200

    def test_singleton_pattern(self):
        """Test singleton pattern works correctly."""
        from baldur.settings.intelligence_task import (
            get_intelligence_task_settings,
        )

        settings1 = get_intelligence_task_settings()
        settings2 = get_intelligence_task_settings()

        assert settings1 is settings2


class TestDriftDetectionSettings:
    """Test DriftDetectionSettings."""

    def setup_method(self):
        """Reset settings before each test."""
        from baldur.settings.drift_detection import reset_drift_detection_settings

        reset_drift_detection_settings()

    def teardown_method(self):
        """Reset settings after each test."""
        from baldur.settings.drift_detection import reset_drift_detection_settings

        reset_drift_detection_settings()

    def test_default_values(self):
        """Test default values are set correctly."""
        from baldur.settings.drift_detection import get_drift_detection_settings

        settings = get_drift_detection_settings()

        assert settings.analysis_window_hours == 24
        assert settings.sla_breach_rate_threshold == 10.0
        assert settings.sla_breach_rate_critical_threshold == 25.0
        assert settings.sla_approaching_threshold == 0.8
        assert settings.pending_at_risk_threshold == 5

    def test_env_override(self):
        """Test environment variable override."""
        from baldur.settings.drift_detection import (
            get_drift_detection_settings,
            reset_drift_detection_settings,
        )

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_DRIFT_DETECTION_ANALYSIS_WINDOW_HOURS": "48",
                "BALDUR_DRIFT_DETECTION_SLA_BREACH_RATE_THRESHOLD": "15.0",
            },
        ):
            reset_drift_detection_settings()
            settings = get_drift_detection_settings()

            assert settings.analysis_window_hours == 48
            assert settings.sla_breach_rate_threshold == 15.0

    def test_singleton_pattern(self):
        """Test singleton pattern works correctly."""
        from baldur.settings.drift_detection import get_drift_detection_settings

        settings1 = get_drift_detection_settings()
        settings2 = get_drift_detection_settings()

        assert settings1 is settings2


class TestSLOSettingsIntegration:
    """Test SLO module Settings integration."""

    def test_slo_uses_settings_defaults(self):
        """Test SLO dataclass uses Settings default values."""
        from baldur.settings.slo import get_slo_settings
        from baldur.slo import SLI, SLO

        settings = get_slo_settings()

        slo = SLO(
            name="test",
            sli=SLI.AVAILABILITY,
            target=0.999,
        )

        # fast_burn_rate와 slow_burn_rate가 settings에서 온 값인지 확인
        assert slo.fast_burn_rate == settings.default_fast_burn_rate
        assert slo.slow_burn_rate == settings.default_slow_burn_rate


class TestSelfAuditSettingsIntegration:
    """Test self_audit.py Settings integration."""

    def setup_method(self):
        """Reset settings before each test."""
        from baldur.audit.self_audit import SelfAuditLogger
        from baldur.settings.audit import reset_audit_settings

        SelfAuditLogger.reset_instance()
        reset_audit_settings()

    def teardown_method(self):
        """Reset settings after each test."""
        from baldur.audit.self_audit import SelfAuditLogger
        from baldur.settings.audit import reset_audit_settings

        SelfAuditLogger.reset_instance()
        reset_audit_settings()

    def test_uses_settings_defaults(self):
        """Test SelfAuditLogger uses Settings default values."""
        from baldur.audit.self_audit import self_audit

        logger = self_audit()

        # Settings에서 가져온 값이 적용되었는지 확인
        assert logger._max_recent_events == 100

    def test_is_healthy_uses_settings(self):
        """Test is_healthy method uses Settings default."""
        from baldur.audit.self_audit import self_audit

        logger = self_audit()

        # 기본 max_failure_rate가 0.1인지 확인
        # 실패 없이 시작하므로 healthy여야 함
        assert logger.is_healthy() is True


class TestCascadeLoadSheddingSettingsIntegration:
    """Test cascade_load_shedding.py Settings integration."""

    def setup_method(self):
        """Reset settings before each test."""
        from baldur.audit.cascade_load_shedding import reset_cascade_load_shedding
        from baldur.settings.audit import reset_audit_settings

        reset_cascade_load_shedding()
        reset_audit_settings()

    def teardown_method(self):
        """Reset settings after each test."""
        from baldur.audit.cascade_load_shedding import reset_cascade_load_shedding
        from baldur.settings.audit import reset_audit_settings

        reset_cascade_load_shedding()
        reset_audit_settings()

    def test_uses_settings_defaults(self):
        """Test CascadeLoadShedding uses Settings default values."""
        from baldur.audit.cascade_load_shedding import CascadeLoadShedding

        shedding = CascadeLoadShedding()

        # Settings에서 가져온 값이 적용되었는지 확인
        assert shedding._rate_window_seconds == 1.0

    def test_env_override_affects_instance(self):
        """Test environment variable override affects new instance."""
        from baldur.audit.cascade_load_shedding import (
            CascadeLoadShedding,
            reset_cascade_load_shedding,
        )
        from baldur.settings.audit import reset_audit_settings

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_AUDIT_CASCADE_RATE_WINDOW_SECONDS": "2.5",
            },
        ):
            reset_audit_settings()
            reset_cascade_load_shedding()
            shedding = CascadeLoadShedding()
            assert shedding._rate_window_seconds == 2.5


class TestDriftDetectionTaskSettingsIntegration:
    """Test tasks/drift_detection.py Settings integration."""

    def setup_method(self):
        """Reset settings before each test."""
        from baldur.settings.drift_detection import reset_drift_detection_settings

        reset_drift_detection_settings()

    def teardown_method(self):
        """Reset settings after each test."""
        from baldur.settings.drift_detection import reset_drift_detection_settings

        reset_drift_detection_settings()

    def test_detector_uses_settings_analysis_window(self):
        """Test SLADriftDetector._get_analysis_window_hours() uses Settings."""
        from baldur.tasks.drift_detection import SLADriftDetector

        # 정적 메서드 직접 테스트
        hours = SLADriftDetector._get_analysis_window_hours()
        assert hours == 24  # 기본값

    def test_env_override_analysis_window(self):
        """Test environment variable override for analysis_window_hours."""
        from baldur.settings.drift_detection import reset_drift_detection_settings
        from baldur.tasks.drift_detection import SLADriftDetector

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_DRIFT_DETECTION_ANALYSIS_WINDOW_HOURS": "48",
            },
        ):
            reset_drift_detection_settings()
            hours = SLADriftDetector._get_analysis_window_hours()
            assert hours == 48


class TestIntelligenceTasksSettingsIntegration:
    """Test tasks/intelligence_tasks.py Settings integration."""

    def setup_method(self):
        """Reset settings before each test."""
        from baldur.settings.intelligence_task import (
            reset_intelligence_task_settings,
        )

        reset_intelligence_task_settings()

    def teardown_method(self):
        """Reset settings after each test."""
        from baldur.settings.intelligence_task import (
            reset_intelligence_task_settings,
        )

        reset_intelligence_task_settings()

    def test_check_sla_drift_task_notification_policy(self):
        """Test CheckSLADriftTask uses Settings for notification_policy."""
        from baldur.tasks.intelligence_tasks import CheckSLADriftTask

        task = CheckSLADriftTask()
        policy = task.notification_policy

        # default_cooldown_seconds가 Settings에서 온 값인지 확인
        assert policy.cooldown_seconds == 3600  # 기본값

    def test_analyze_forensic_pending_task_notification_policy(self):
        """Test AnalyzeForensicPendingTask uses Settings for notification_policy."""
        from baldur.tasks.intelligence_tasks import AnalyzeForensicPendingTask

        task = AnalyzeForensicPendingTask()
        policy = task.notification_policy

        # execution_threshold가 Settings에서 온 값인지 확인
        assert policy.threshold == 10  # 기본값

    # AnalyzeCrossStageInsightsTask policy coverage moved with the task to
    # tests/dormant/unit/test_learning_insight_task.py (599 D10/D14).

    def test_check_recovery_transitions_task_notification_policy(self):
        """Test CheckRecoveryTransitionsTask uses Settings for notification_policy."""
        from baldur.tasks.intelligence_tasks import CheckRecoveryTransitionsTask

        task = CheckRecoveryTransitionsTask()
        policy = task.notification_policy

        # recovery_check_cooldown_seconds가 Settings에서 온 값인지 확인
        assert policy.cooldown_seconds == 120  # 기본값

    def test_get_intelligence_settings_fallback(self):
        """Test _get_intelligence_settings returns fallback on import error."""
        from baldur.tasks.intelligence_tasks import CheckSLADriftTask

        # 정적 메서드 직접 테스트 (정상 동작 확인)
        settings = CheckSLADriftTask._get_intelligence_settings()
        assert settings.default_cooldown_seconds == 3600
        assert settings.batch_size == 100

    def test_env_override_affects_notification_policy(self):
        """Test environment variable override affects notification_policy."""
        from baldur.settings.intelligence_task import (
            reset_intelligence_task_settings,
        )
        from baldur.tasks.intelligence_tasks import CheckSLADriftTask

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_INTELLIGENCE_TASK_DEFAULT_COOLDOWN_SECONDS": "7200",
            },
        ):
            reset_intelligence_task_settings()
            task = CheckSLADriftTask()
            policy = task.notification_policy
            assert policy.cooldown_seconds == 7200


class TestTrafficAwareReplaySettingsIntegration:
    """Test tasks/traffic_aware_replay.py Settings integration."""

    def setup_method(self):
        """Reset settings before each test."""
        from baldur.settings.intelligence_task import (
            reset_intelligence_task_settings,
        )

        reset_intelligence_task_settings()

    def teardown_method(self):
        """Reset settings after each test."""
        from baldur.settings.intelligence_task import (
            reset_intelligence_task_settings,
        )

        reset_intelligence_task_settings()

    def test_traffic_aware_replay_notification_policy(self):
        """Test TrafficAwareReplayTask uses Settings for notification_policy."""
        from baldur.tasks.traffic_aware_replay import TrafficAwareReplayTask

        task = TrafficAwareReplayTask()
        policy = task.notification_policy

        # cooldown_seconds가 기본값인지 확인
        assert policy.cooldown_seconds == 300  # 5분

    def test_get_cooldown_seconds_default(self):
        """Test _get_cooldown_seconds returns default value."""
        from baldur.tasks.traffic_aware_replay import TrafficAwareReplayTask

        cooldown = TrafficAwareReplayTask._get_cooldown_seconds()
        assert cooldown == 300  # 기본값 5분


# =============================================================================
# NOTE: API View Settings 통합 테스트는 전역 tests 폴더로 이동됨
# 위치: tests/baldur/api/test_api_view_settings_integration.py
# 이유: Django REST Framework 컨텍스트 필요
# =============================================================================
