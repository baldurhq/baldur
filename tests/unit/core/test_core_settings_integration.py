"""
Core 모듈과 Settings 연동 테스트.

각 core 모듈이 settings에서 설정값을 올바르게 가져오는지 검증.
환경변수 오버라이드가 정상 동작하는지 테스트.
"""

import os
from unittest import mock


class TestRuntimeFeedbackSettingsIntegration:
    """RuntimeFeedbackLoop과 RuntimeFeedbackSettings 연동 테스트."""

    def test_default_values_match_original_hardcoded(self):
        """기본값이 원래 하드코딩된 값과 일치해야 함."""
        from baldur.settings.runtime_feedback import (
            get_runtime_feedback_settings,
            reset_runtime_feedback_settings,
        )

        reset_runtime_feedback_settings()
        settings = get_runtime_feedback_settings()

        assert settings.max_consecutive_failures == 3
        assert settings.rollback_cooldown == 120
        assert settings.adjustment_wait == 30

    def test_loop_uses_settings_values(self):
        """RuntimeFeedbackLoop이 settings 값을 사용해야 함."""
        from baldur.core.runtime_feedback import RuntimeFeedbackLoop
        from baldur.settings.runtime_feedback import (
            reset_runtime_feedback_settings,
        )

        reset_runtime_feedback_settings()

        # Mock dependencies
        mock_metrics = mock.MagicMock()
        mock_decision = mock.MagicMock()
        mock_safety = mock.MagicMock()
        mock_audit = mock.MagicMock()
        mock_alert = mock.MagicMock()
        mock_applier = mock.MagicMock()

        loop = RuntimeFeedbackLoop(
            metrics_adapter=mock_metrics,
            decision_engine=mock_decision,
            safety_bounds=mock_safety,
            audit_adapter=mock_audit,
            alert_manager=mock_alert,
            config_applier=mock_applier,
        )

        assert loop.MAX_CONSECUTIVE_FAILURES == 3
        assert loop.POST_ROLLBACK_COOLDOWN == 120
        assert loop.POST_ADJUSTMENT_WAIT == 30

    def test_environment_variable_override(self):
        """환경변수로 설정값 오버라이드 가능해야 함."""
        from baldur.settings.runtime_feedback import (
            get_runtime_feedback_settings,
            reset_runtime_feedback_settings,
        )

        reset_runtime_feedback_settings()

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_RUNTIME_FEEDBACK_MAX_CONSECUTIVE_FAILURES": "5",
                "BALDUR_RUNTIME_FEEDBACK_ROLLBACK_COOLDOWN": "180",
                "BALDUR_RUNTIME_FEEDBACK_ADJUSTMENT_WAIT": "45",
            },
        ):
            reset_runtime_feedback_settings()
            settings = get_runtime_feedback_settings()

            assert settings.max_consecutive_failures == 5
            assert settings.rollback_cooldown == 180
            assert settings.adjustment_wait == 45

        reset_runtime_feedback_settings()


class TestAutoRollbackSettingsIntegration:
    """AutoRollbackGuard와 AutoRollbackSettings 연동 테스트."""

    def test_default_values_match_original_hardcoded(self):
        """기본값이 원래 하드코딩된 값과 일치해야 함."""
        from baldur.settings.auto_rollback import (
            get_auto_rollback_settings,
            reset_auto_rollback_settings,
        )

        reset_auto_rollback_settings()
        settings = get_auto_rollback_settings()

        assert settings.error_rate_major == 0.1
        assert settings.error_rate_critical == 0.3
        assert settings.latency_major_ms == 5000
        assert settings.latency_critical_ms == 10000
        assert settings.failures_alert == 3
        assert settings.failures_emergency == 5

    def test_guard_uses_settings_values(self):
        """AutoRollbackGuard가 settings 값을 사용해야 함."""
        from baldur.core.auto_rollback_guard import AutoRollbackGuard
        from baldur.settings.auto_rollback import (
            reset_auto_rollback_settings,
        )

        reset_auto_rollback_settings()

        # Mock dependencies — return values prevent TypeError if
        # monitoring thread runs (MagicMock >= float is unsupported).
        mock_metrics = mock.MagicMock()
        mock_metrics.get_error_rate.return_value = 0.01
        mock_metrics.get_latency_p99.return_value = 100.0
        mock_metrics.get_throughput.return_value = 1000.0
        mock_applier = mock.MagicMock()

        guard = AutoRollbackGuard(
            metrics_provider=mock_metrics,
            config_applier=mock_applier,
        )

        assert guard.ERROR_RATE_MAJOR == 0.1
        assert guard.ERROR_RATE_CRITICAL == 0.3
        assert guard.LATENCY_MAJOR_MS == 5000
        assert guard.LATENCY_CRITICAL_MS == 10000
        assert guard.CONSECUTIVE_FAILURES_ALERT == 3
        assert guard.CONSECUTIVE_FAILURES_EMERGENCY == 5

    def test_environment_variable_override(self):
        """환경변수로 설정값 오버라이드 가능해야 함."""
        from baldur.settings.auto_rollback import (
            get_auto_rollback_settings,
            reset_auto_rollback_settings,
        )

        reset_auto_rollback_settings()

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_AUTO_ROLLBACK_ERROR_RATE_MAJOR": "0.15",
                "BALDUR_AUTO_ROLLBACK_ERROR_RATE_CRITICAL": "0.4",
                "BALDUR_AUTO_ROLLBACK_LATENCY_MAJOR_MS": "6000",
            },
        ):
            reset_auto_rollback_settings()
            settings = get_auto_rollback_settings()

            assert settings.error_rate_major == 0.15
            assert settings.error_rate_critical == 0.4
            assert settings.latency_major_ms == 6000

        reset_auto_rollback_settings()


class TestAdaptiveJitterSettingsIntegration:
    """AdaptiveJitter와 JitterSettings 연동 테스트."""

    def test_default_threshold_values(self):
        """기본 임계값이 원래 하드코딩된 값과 일치해야 함."""
        from baldur.settings.jitter import (
            get_jitter_settings,
            reset_jitter_settings,
        )

        reset_jitter_settings()
        settings = get_jitter_settings()

        assert settings.error_budget_danger_threshold == 0.2
        assert settings.error_budget_safe_threshold == 0.5
        assert settings.load_high_threshold == 0.8
        assert settings.load_low_threshold == 0.3

    def test_adaptive_jitter_uses_settings(self):
        """AdaptiveJitter가 settings 값을 사용해야 함."""
        from baldur.core.adaptive_jitter import AdaptiveJitter
        from baldur.settings.jitter import reset_jitter_settings

        reset_jitter_settings()

        # 위험 상황 (에러 버짓 10% 남음)
        jitter_range = AdaptiveJitter.get_jitter_range(
            error_budget_remaining=0.1,
            current_load=0.5,
        )
        assert jitter_range == AdaptiveJitter.JITTER_MIN_STRESSED

        # 여유 상황 (에러 버짓 60% 남음, 부하 20%)
        jitter_range = AdaptiveJitter.get_jitter_range(
            error_budget_remaining=0.6,
            current_load=0.2,
        )
        assert jitter_range == AdaptiveJitter.JITTER_MIN_RELAXED


class TestSafetyBoundsSettingsIntegration:
    """SafetyBounds와 SafetyBoundsSettings 연동 테스트."""

    def test_default_bounds_match_original(self):
        """기본 한계값이 원래 하드코딩된 값과 일치해야 함."""
        from baldur.settings.safety_bounds import (
            get_safety_bounds_settings,
            reset_safety_bounds_settings,
        )

        reset_safety_bounds_settings()
        settings = get_safety_bounds_settings()

        assert settings.timeout_ms_min == 100
        assert settings.timeout_ms_max == 30000
        assert settings.timeout_ms_max_change == 0.3

    def test_safety_bounds_uses_settings(self):
        """SafetyBounds가 settings 값을 사용해야 함."""
        from baldur.core.safety_bounds import SafetyBounds
        from baldur.settings.safety_bounds import reset_safety_bounds_settings

        reset_safety_bounds_settings()

        bounds = SafetyBounds()

        assert bounds.bounds["timeout_ms"].min_value == 100
        assert bounds.bounds["timeout_ms"].max_value == 30000
        assert bounds.bounds["timeout_ms"].max_change_per_cycle == 0.3

    def test_environment_variable_override(self):
        """환경변수로 한계값 오버라이드 가능해야 함."""
        from baldur.settings.safety_bounds import (
            get_safety_bounds_settings,
            reset_safety_bounds_settings,
        )

        reset_safety_bounds_settings()

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_SAFETY_BOUNDS_TIMEOUT_MS_MIN": "200",
                "BALDUR_SAFETY_BOUNDS_TIMEOUT_MS_MAX": "60000",
            },
        ):
            reset_safety_bounds_settings()
            settings = get_safety_bounds_settings()

            assert settings.timeout_ms_min == 200
            assert settings.timeout_ms_max == 60000

        reset_safety_bounds_settings()


class TestStateCacheSettingsIntegration:
    """CBStateCache와 StateCacheSettings 연동 테스트."""

    def test_default_values_match_original(self):
        """기본값이 원래 하드코딩된 값과 일치해야 함."""
        from baldur.settings.state_cache import (
            get_state_cache_settings,
            reset_state_cache_settings,
        )

        reset_state_cache_settings()
        settings = get_state_cache_settings()

        assert settings.base_ttl == 5.0
        assert settings.jitter_range == 0.5

    def test_state_cache_uses_settings(self):
        """CBStateCache가 settings 값을 사용해야 함."""
        from baldur.core.state_cache import CBStateCache
        from baldur.settings.state_cache import reset_state_cache_settings

        reset_state_cache_settings()

        # TTL 계산 테스트 (5.0 ± 0.5)
        for _ in range(10):
            ttl = CBStateCache._calculate_ttl()
            assert 4.5 <= ttl <= 5.5


class TestResourceMonitorSettingsIntegration:
    """CgroupResourceMonitor와 ResourceMonitorSettings 연동 테스트."""

    def test_default_safety_margin(self):
        """기본 안전 마진이 원래 하드코딩된 값과 일치해야 함."""
        from baldur.settings.resource_monitor import (
            get_resource_monitor_settings,
            reset_resource_monitor_settings,
        )

        reset_resource_monitor_settings()
        settings = get_resource_monitor_settings()

        assert settings.safety_margin == 0.15

    def test_resource_monitor_uses_settings(self):
        """CgroupResourceMonitor가 settings 값을 사용해야 함."""
        from baldur.core.resource_monitor import CgroupResourceMonitor
        from baldur.settings.resource_monitor import (
            reset_resource_monitor_settings,
        )

        reset_resource_monitor_settings()

        margin = CgroupResourceMonitor._get_default_safety_margin()
        assert margin == 0.15

    def test_environment_variable_override(self):
        """환경변수로 안전 마진 오버라이드 가능해야 함."""
        from baldur.settings.resource_monitor import (
            get_resource_monitor_settings,
            reset_resource_monitor_settings,
        )

        reset_resource_monitor_settings()

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_RESOURCE_MONITOR_SAFETY_MARGIN": "0.20",
            },
        ):
            reset_resource_monitor_settings()
            settings = get_resource_monitor_settings()

            assert settings.safety_margin == 0.20

        reset_resource_monitor_settings()


class TestApplyStrategySettingsIntegration:
    """apply_strategy 모듈과 ApplyStrategySettings 연동 테스트."""

    def test_default_delays_match_original(self):
        """기본 delay 값이 원래 하드코딩된 값과 일치해야 함."""
        from baldur.settings.apply_strategy import (
            get_apply_strategy_settings,
            reset_apply_strategy_settings,
        )

        reset_apply_strategy_settings()
        settings = get_apply_strategy_settings()

        assert settings.sla_delay == 0
        assert settings.retry_delay == 10
        assert settings.circuit_breaker_delay == 30
        assert settings.security_delay == 60

    def test_get_default_apply_config_uses_settings(self):
        """get_default_apply_config이 settings 값을 사용해야 함."""
        from baldur.core.apply_strategy import get_default_apply_config
        from baldur.settings.apply_strategy import reset_apply_strategy_settings

        reset_apply_strategy_settings()

        cb_config = get_default_apply_config("circuit_breaker")
        assert cb_config.delay_seconds == 30

        security_config = get_default_apply_config("security")
        assert security_config.delay_seconds == 60


class TestDecisionEngineSettingsIntegration:
    """DecisionEngine과 DecisionEngineSettings 연동 테스트."""

    def test_default_values_match_original(self):
        """기본값이 원래 하드코딩된 값과 일치해야 함."""
        from baldur.settings.decision_engine import (
            get_decision_engine_settings,
            reset_decision_engine_settings,
        )

        reset_decision_engine_settings()
        settings = get_decision_engine_settings()

        assert settings.min_change_ratio == 0.05

        # 샘플 수 기반 신뢰도
        assert settings.get_sample_confidence(3) == 0.3
        assert settings.get_sample_confidence(10) == 0.5
        assert settings.get_sample_confidence(30) == 0.65
        assert settings.get_sample_confidence(80) == 0.75
        assert settings.get_sample_confidence(150) == 0.9

        # 변동계수 기반 안정성 계수
        assert settings.get_stability_factor(0.6) == 0.7
        assert settings.get_stability_factor(0.3) == 0.85
        assert settings.get_stability_factor(0.1) == 1.0

    def test_decision_engine_uses_settings(self):
        """DecisionEngine이 settings 값을 사용해야 함."""
        from baldur.core.decision_engine import DecisionEngine
        from baldur.settings.decision_engine import reset_decision_engine_settings

        reset_decision_engine_settings()

        mock_provider = mock.MagicMock()
        engine = DecisionEngine(config_provider=mock_provider)

        assert engine.MIN_CHANGE_RATIO == 0.05
