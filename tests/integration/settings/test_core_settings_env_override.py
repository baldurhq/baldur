"""
Core 모듈 Settings 환경변수 오버라이드 통합 테스트.

Core 모듈이 Settings에서 값을 가져올 때, 환경변수로 오버라이드된 값이
실제 비즈니스 로직에 올바르게 반영되는지 검증합니다.

대상 모듈 (103_HARDCODED_CONFIG_CORE_REFACTORING.md Step 3 완료):
1. RuntimeFeedbackLoop ↔ RuntimeFeedbackSettings
2. AutoRollbackGuard ↔ AutoRollbackSettings
3. AdaptiveJitter ↔ JitterSettings
4. SafetyBounds ↔ SafetyBoundsSettings
5. CBStateCache ↔ StateCacheSettings
6. CgroupResourceMonitor ↔ ResourceMonitorSettings
7. apply_strategy ↔ ApplyStrategySettings
8. DecisionEngine ↔ DecisionEngineSettings
"""

import os
from unittest import mock

import pytest


class TestRuntimeFeedbackEnvOverride:
    """RuntimeFeedbackLoop 환경변수 오버라이드가 실제 동작에 반영되는지 검증."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.settings.runtime_feedback import (
            reset_runtime_feedback_settings,
        )

        reset_runtime_feedback_settings()
        yield
        reset_runtime_feedback_settings()

    def test_max_consecutive_failures_affects_loop_behavior(self):
        """MAX_CONSECUTIVE_FAILURES 변경이 RuntimeFeedbackLoop에 반영."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_RUNTIME_FEEDBACK_MAX_CONSECUTIVE_FAILURES": "10",
            },
        ):
            from baldur.core.runtime_feedback import RuntimeFeedbackLoop
            from baldur.settings.runtime_feedback import (
                reset_runtime_feedback_settings,
            )

            reset_runtime_feedback_settings()

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

            # 환경변수로 변경된 값이 적용됨
            assert loop.MAX_CONSECUTIVE_FAILURES == 10

    def test_rollback_cooldown_affects_loop_behavior(self):
        """POST_ROLLBACK_COOLDOWN 변경이 RuntimeFeedbackLoop에 반영."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_RUNTIME_FEEDBACK_ROLLBACK_COOLDOWN": "300",
            },
        ):
            from baldur.core.runtime_feedback import RuntimeFeedbackLoop
            from baldur.settings.runtime_feedback import (
                reset_runtime_feedback_settings,
            )

            reset_runtime_feedback_settings()

            mock_deps = [mock.MagicMock() for _ in range(6)]

            loop = RuntimeFeedbackLoop(
                metrics_adapter=mock_deps[0],
                decision_engine=mock_deps[1],
                safety_bounds=mock_deps[2],
                audit_adapter=mock_deps[3],
                alert_manager=mock_deps[4],
                config_applier=mock_deps[5],
            )

            assert loop.POST_ROLLBACK_COOLDOWN == 300


class TestAutoRollbackGuardEnvOverride:
    """AutoRollbackGuard 환경변수 오버라이드가 실제 동작에 반영되는지 검증."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.settings.auto_rollback import reset_auto_rollback_settings

        reset_auto_rollback_settings()
        yield
        reset_auto_rollback_settings()

    @staticmethod
    def _make_guard_metrics_mock():
        """Create a MagicMock with proper return values for MetricsProvider.

        Bare MagicMock causes TypeError in AutoRollbackGuard daemon thread
        because MagicMock >= float is unsupported.
        """
        m = mock.MagicMock()
        m.get_error_rate.return_value = 0.01
        m.get_latency_p99.return_value = 100.0
        m.get_throughput.return_value = 1000.0
        return m

    def test_error_rate_thresholds_affect_guard_behavior(self):
        """에러율 임계값 변경이 AutoRollbackGuard에 반영."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_AUTO_ROLLBACK_ERROR_RATE_MAJOR": "0.05",
                "BALDUR_AUTO_ROLLBACK_ERROR_RATE_CRITICAL": "0.15",
            },
        ):
            from baldur.core.auto_rollback_guard import AutoRollbackGuard
            from baldur.settings.auto_rollback import reset_auto_rollback_settings

            reset_auto_rollback_settings()

            guard = AutoRollbackGuard(
                metrics_provider=self._make_guard_metrics_mock(),
                config_applier=mock.MagicMock(),
            )

            # 더 민감한 임계값으로 변경됨
            assert guard.ERROR_RATE_MAJOR == 0.05
            assert guard.ERROR_RATE_CRITICAL == 0.15

    def test_latency_thresholds_affect_guard_behavior(self):
        """레이턴시 임계값 변경이 AutoRollbackGuard에 반영."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_AUTO_ROLLBACK_LATENCY_MAJOR_MS": "3000",
                "BALDUR_AUTO_ROLLBACK_LATENCY_CRITICAL_MS": "7000",
            },
        ):
            from baldur.core.auto_rollback_guard import AutoRollbackGuard
            from baldur.settings.auto_rollback import reset_auto_rollback_settings

            reset_auto_rollback_settings()

            guard = AutoRollbackGuard(
                metrics_provider=self._make_guard_metrics_mock(),
                config_applier=mock.MagicMock(),
            )

            assert guard.LATENCY_MAJOR_MS == 3000
            assert guard.LATENCY_CRITICAL_MS == 7000

    def test_failure_counts_affect_guard_behavior(self):
        """연속 실패 횟수 임계값 변경이 AutoRollbackGuard에 반영."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_AUTO_ROLLBACK_FAILURES_ALERT": "5",
                "BALDUR_AUTO_ROLLBACK_FAILURES_EMERGENCY": "10",
            },
        ):
            from baldur.core.auto_rollback_guard import AutoRollbackGuard
            from baldur.settings.auto_rollback import reset_auto_rollback_settings

            reset_auto_rollback_settings()

            guard = AutoRollbackGuard(
                metrics_provider=self._make_guard_metrics_mock(),
                config_applier=mock.MagicMock(),
            )

            assert guard.CONSECUTIVE_FAILURES_ALERT == 5
            assert guard.CONSECUTIVE_FAILURES_EMERGENCY == 10


class TestAdaptiveJitterEnvOverride:
    """AdaptiveJitter 환경변수 오버라이드가 실제 jitter 계산에 반영되는지 검증."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.settings.jitter import reset_jitter_settings

        reset_jitter_settings()
        yield
        reset_jitter_settings()

    def test_error_budget_threshold_changes_jitter_behavior(self):
        """에러 버짓 임계값 변경이 AdaptiveJitter의 jitter 범위 선택에 영향."""
        # 기본값: danger=0.2, safe=0.5
        # 변경: danger=0.3, safe=0.6
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_JITTER_ERROR_BUDGET_DANGER_THRESHOLD": "0.3",
                "BALDUR_JITTER_ERROR_BUDGET_SAFE_THRESHOLD": "0.6",
            },
        ):
            from baldur.core.adaptive_jitter import AdaptiveJitter
            from baldur.settings.jitter import reset_jitter_settings

            reset_jitter_settings()

            # 25% 남음: 기본값에서는 danger(0.2) 초과로 normal, 변경 후에는 danger(0.3) 미만으로 stressed
            jitter_range = AdaptiveJitter.get_jitter_range(
                error_budget_remaining=0.25,
                current_load=0.5,
            )
            # 변경된 임계값에서 0.25 < 0.3 이므로 STRESSED
            assert jitter_range == AdaptiveJitter.JITTER_MIN_STRESSED

    def test_load_threshold_changes_jitter_behavior(self):
        """부하 임계값 변경이 AdaptiveJitter의 jitter 범위 선택에 영향."""
        # 기본값: high=0.8, low=0.3
        # 변경: high=0.7, low=0.2
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_JITTER_LOAD_HIGH_THRESHOLD": "0.7",
                "BALDUR_JITTER_LOAD_LOW_THRESHOLD": "0.2",
            },
        ):
            from baldur.core.adaptive_jitter import AdaptiveJitter
            from baldur.settings.jitter import reset_jitter_settings

            reset_jitter_settings()

            # 75% 부하: 기본값에서는 high(0.8) 미만으로 normal, 변경 후에는 high(0.7) 초과로 stressed
            jitter_range = AdaptiveJitter.get_jitter_range(
                error_budget_remaining=0.6,  # 충분한 에러 버짓
                current_load=0.75,
            )
            # 변경된 임계값에서 0.75 > 0.7 이므로 STRESSED
            assert jitter_range == AdaptiveJitter.JITTER_MIN_STRESSED


class TestSafetyBoundsEnvOverride:
    """SafetyBounds 환경변수 오버라이드가 실제 클램핑 동작에 반영되는지 검증."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.settings.safety_bounds import reset_safety_bounds_settings

        reset_safety_bounds_settings()
        yield
        reset_safety_bounds_settings()

    def test_timeout_bounds_affect_clamping(self):
        """timeout_ms 한계값 변경이 SafetyBounds 클램핑에 반영."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_SAFETY_BOUNDS_TIMEOUT_MS_MIN": "500",
                "BALDUR_SAFETY_BOUNDS_TIMEOUT_MS_MAX": "20000",
            },
        ):
            from baldur.core.safety_bounds import SafetyBounds
            from baldur.settings.safety_bounds import reset_safety_bounds_settings

            reset_safety_bounds_settings()

            bounds = SafetyBounds()

            # 변경된 min/max 범위 확인
            assert bounds.bounds["timeout_ms"].min_value == 500
            assert bounds.bounds["timeout_ms"].max_value == 20000

            # clamp_to_bounds로 클램핑 동작 확인: 100 → 500 (min), 30000 → 20000 (max)
            clamped_min = bounds.clamp_to_bounds("timeout_ms", 100)
            clamped_max = bounds.clamp_to_bounds("timeout_ms", 30000)

            assert clamped_min == 500
            assert clamped_max == 20000

    def test_max_change_per_cycle_affects_adjustment_limiting(self):
        """max_change_per_cycle 변경이 조정폭 제한에 반영."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_SAFETY_BOUNDS_TIMEOUT_MS_MAX_CHANGE": "0.5",  # 50%로 확대
            },
        ):
            from baldur.core.safety_bounds import SafetyBounds
            from baldur.settings.safety_bounds import reset_safety_bounds_settings

            reset_safety_bounds_settings()

            bounds = SafetyBounds()

            # 변경된 max_change_per_cycle 확인
            assert bounds.bounds["timeout_ms"].max_change_per_cycle == 0.5

            # clamp_to_bounds로 제한된 조정폭 확인: current=1000, new=2000 (100% 변경 시도)
            # 기본값 0.3이면 1000 → 1300, 0.5면 1000 → 1500
            limited = bounds.clamp_to_bounds("timeout_ms", 2000, current_value=1000)
            assert limited == 1500  # 50% 변경만 허용


class TestStateCacheEnvOverride:
    """CBStateCache 환경변수 오버라이드가 실제 TTL 계산에 반영되는지 검증."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.settings.state_cache import reset_state_cache_settings

        reset_state_cache_settings()
        yield
        reset_state_cache_settings()

    def test_base_ttl_affects_cache_expiration(self):
        """base_ttl 변경이 CBStateCache TTL 계산에 반영."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_STATE_CACHE_BASE_TTL": "10.0",
                "BALDUR_STATE_CACHE_JITTER_RANGE": "2.0",
            },
        ):
            from baldur.core.state_cache import CBStateCache
            from baldur.settings.state_cache import reset_state_cache_settings

            reset_state_cache_settings()

            # TTL 범위 확인: 10.0 ± 2.0 = 8.0 ~ 12.0
            for _ in range(20):
                ttl = CBStateCache._calculate_ttl()
                assert 8.0 <= ttl <= 12.0


class TestResourceMonitorEnvOverride:
    """CgroupResourceMonitor 환경변수 오버라이드가 안전 마진에 반영되는지 검증."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.settings.resource_monitor import (
            reset_resource_monitor_settings,
        )

        reset_resource_monitor_settings()
        yield
        reset_resource_monitor_settings()

    def test_safety_margin_affects_memory_calculation(self):
        """safety_margin 변경이 CgroupResourceMonitor 메모리 계산에 반영."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_RESOURCE_MONITOR_SAFETY_MARGIN": "0.25",
            },
        ):
            from baldur.core.resource_monitor import CgroupResourceMonitor
            from baldur.settings.resource_monitor import (
                reset_resource_monitor_settings,
            )

            reset_resource_monitor_settings()

            margin = CgroupResourceMonitor._get_default_safety_margin()
            assert margin == 0.25


class TestApplyStrategyEnvOverride:
    """apply_strategy 환경변수 오버라이드가 실제 딜레이 값에 반영되는지 검증."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.settings.apply_strategy import reset_apply_strategy_settings

        reset_apply_strategy_settings()
        yield
        reset_apply_strategy_settings()

    def test_circuit_breaker_delay_affects_apply_config(self):
        """circuit_breaker_delay 변경이 get_default_apply_config에 반영."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_APPLY_STRATEGY_CIRCUIT_BREAKER_DELAY": "60",
            },
        ):
            from baldur.core.apply_strategy import get_default_apply_config
            from baldur.settings.apply_strategy import (
                reset_apply_strategy_settings,
            )

            reset_apply_strategy_settings()

            config = get_default_apply_config("circuit_breaker")
            assert config.delay_seconds == 60

    def test_security_delay_affects_apply_config(self):
        """security_delay 변경이 get_default_apply_config에 반영."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_APPLY_STRATEGY_SECURITY_DELAY": "120",
            },
        ):
            from baldur.core.apply_strategy import get_default_apply_config
            from baldur.settings.apply_strategy import (
                reset_apply_strategy_settings,
            )

            reset_apply_strategy_settings()

            config = get_default_apply_config("security")
            assert config.delay_seconds == 120

    def test_multiple_delays_affect_respective_configs(self):
        """여러 딜레이 값 동시 변경이 각 config에 독립적으로 반영."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_APPLY_STRATEGY_RETRY_DELAY": "20",
                "BALDUR_APPLY_STRATEGY_DLQ_DELAY": "25",
                "BALDUR_APPLY_STRATEGY_ERROR_BUDGET_DELAY": "45",
            },
        ):
            from baldur.core.apply_strategy import get_default_apply_config
            from baldur.settings.apply_strategy import (
                reset_apply_strategy_settings,
            )

            reset_apply_strategy_settings()

            assert get_default_apply_config("retry").delay_seconds == 20
            assert get_default_apply_config("dlq").delay_seconds == 25
            assert get_default_apply_config("error_budget").delay_seconds == 45


class TestDecisionEngineEnvOverride:
    """DecisionEngine 환경변수 오버라이드가 실제 결정 로직에 반영되는지 검증."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.settings.decision_engine import reset_decision_engine_settings

        reset_decision_engine_settings()
        yield
        reset_decision_engine_settings()

    def test_min_change_ratio_affects_engine_behavior(self):
        """min_change_ratio 변경이 DecisionEngine에 반영."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_DECISION_ENGINE_MIN_CHANGE_RATIO": "0.1",
            },
        ):
            from baldur.core.decision_engine import DecisionEngine
            from baldur.settings.decision_engine import (
                reset_decision_engine_settings,
            )

            reset_decision_engine_settings()

            mock_provider = mock.MagicMock()
            engine = DecisionEngine(config_provider=mock_provider)

            # 더 큰 변경 비율 요구 (5% → 10%)
            assert engine.MIN_CHANGE_RATIO == 0.1

    def test_confidence_thresholds_affect_settings_method(self):
        """신뢰도 임계값 변경이 get_sample_confidence에 반영."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_DECISION_ENGINE_CONFIDENCE_SAMPLES_VERY_LOW": "10",
                "BALDUR_DECISION_ENGINE_CONFIDENCE_SAMPLES_LOW": "30",
                "BALDUR_DECISION_ENGINE_CONFIDENCE_VALUE_VERY_LOW": "0.4",
            },
        ):
            from baldur.settings.decision_engine import (
                get_decision_engine_settings,
                reset_decision_engine_settings,
            )

            reset_decision_engine_settings()
            settings = get_decision_engine_settings()

            # 변경된 임계값 확인
            assert settings.confidence_samples_very_low == 10
            assert settings.confidence_samples_low == 30
            assert settings.confidence_value_very_low == 0.4

            # 5 샘플: 변경 전에는 0.5 (5 <= x < 20), 변경 후에는 0.4 (x < 10)
            assert settings.get_sample_confidence(5) == 0.4

    def test_stability_thresholds_affect_settings_method(self):
        """안정성 임계값 변경이 get_stability_factor에 반영."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_DECISION_ENGINE_STABILITY_CV_HIGH": "0.4",
                "BALDUR_DECISION_ENGINE_STABILITY_CV_MEDIUM": "0.15",
                "BALDUR_DECISION_ENGINE_STABILITY_FACTOR_UNSTABLE": "0.6",
            },
        ):
            from baldur.settings.decision_engine import (
                get_decision_engine_settings,
                reset_decision_engine_settings,
            )

            reset_decision_engine_settings()
            settings = get_decision_engine_settings()

            # 변경된 임계값 확인
            assert settings.stability_cv_high == 0.4
            assert settings.stability_cv_medium == 0.15
            assert settings.stability_factor_unstable == 0.6

            # CV 0.45: 변경 전에는 0.85 (0.2 < x <= 0.5), 변경 후에는 0.6 (x > 0.4)
            assert settings.get_stability_factor(0.45) == 0.6


class TestMultipleCoreModulesEnvOverride:
    """여러 Core 모듈의 Settings 환경변수가 독립적으로 동작하는지 검증."""

    @pytest.fixture(autouse=True)
    def reset_all_settings(self):
        """테스트 전후 모든 싱글톤 초기화."""
        from baldur.settings.apply_strategy import reset_apply_strategy_settings
        from baldur.settings.auto_rollback import reset_auto_rollback_settings
        from baldur.settings.decision_engine import reset_decision_engine_settings
        from baldur.settings.jitter import reset_jitter_settings
        from baldur.settings.resource_monitor import (
            reset_resource_monitor_settings,
        )
        from baldur.settings.runtime_feedback import (
            reset_runtime_feedback_settings,
        )
        from baldur.settings.safety_bounds import reset_safety_bounds_settings
        from baldur.settings.state_cache import reset_state_cache_settings

        reset_runtime_feedback_settings()
        reset_auto_rollback_settings()
        reset_jitter_settings()
        reset_safety_bounds_settings()
        reset_state_cache_settings()
        reset_resource_monitor_settings()
        reset_apply_strategy_settings()
        reset_decision_engine_settings()
        yield
        reset_runtime_feedback_settings()
        reset_auto_rollback_settings()
        reset_jitter_settings()
        reset_safety_bounds_settings()
        reset_state_cache_settings()
        reset_resource_monitor_settings()
        reset_apply_strategy_settings()
        reset_decision_engine_settings()

    def test_all_core_settings_independent(self):
        """각 Core 모듈의 환경변수가 독립적으로 동작."""
        with mock.patch.dict(
            os.environ,
            {
                # RuntimeFeedback
                "BALDUR_RUNTIME_FEEDBACK_MAX_CONSECUTIVE_FAILURES": "8",
                # AutoRollback
                "BALDUR_AUTO_ROLLBACK_ERROR_RATE_MAJOR": "0.08",
                # Jitter
                "BALDUR_JITTER_ERROR_BUDGET_DANGER_THRESHOLD": "0.15",
                # SafetyBounds
                "BALDUR_SAFETY_BOUNDS_TIMEOUT_MS_MIN": "250",
                # StateCache
                "BALDUR_STATE_CACHE_BASE_TTL": "8.0",
                # ResourceMonitor
                "BALDUR_RESOURCE_MONITOR_SAFETY_MARGIN": "0.20",
                # ApplyStrategy
                "BALDUR_APPLY_STRATEGY_CIRCUIT_BREAKER_DELAY": "45",
                # DecisionEngine
                "BALDUR_DECISION_ENGINE_MIN_CHANGE_RATIO": "0.08",
            },
        ):
            from baldur.settings.apply_strategy import (
                get_apply_strategy_settings,
                reset_apply_strategy_settings,
            )
            from baldur.settings.auto_rollback import (
                get_auto_rollback_settings,
                reset_auto_rollback_settings,
            )
            from baldur.settings.decision_engine import (
                get_decision_engine_settings,
                reset_decision_engine_settings,
            )
            from baldur.settings.jitter import (
                get_jitter_settings,
                reset_jitter_settings,
            )
            from baldur.settings.resource_monitor import (
                get_resource_monitor_settings,
                reset_resource_monitor_settings,
            )
            from baldur.settings.runtime_feedback import (
                get_runtime_feedback_settings,
                reset_runtime_feedback_settings,
            )
            from baldur.settings.safety_bounds import (
                get_safety_bounds_settings,
                reset_safety_bounds_settings,
            )
            from baldur.settings.state_cache import (
                get_state_cache_settings,
                reset_state_cache_settings,
            )

            # 모든 싱글톤 리셋
            reset_runtime_feedback_settings()
            reset_auto_rollback_settings()
            reset_jitter_settings()
            reset_safety_bounds_settings()
            reset_state_cache_settings()
            reset_resource_monitor_settings()
            reset_apply_strategy_settings()
            reset_decision_engine_settings()

            # 각 모듈별 환경변수 반영 확인
            runtime = get_runtime_feedback_settings()
            assert runtime.max_consecutive_failures == 8
            # 다른 필드는 기본값 유지
            assert runtime.rollback_cooldown == 120

            rollback = get_auto_rollback_settings()
            assert rollback.error_rate_major == 0.08
            assert rollback.error_rate_critical == 0.3  # 기본값

            jitter = get_jitter_settings()
            assert jitter.error_budget_danger_threshold == 0.15
            assert jitter.error_budget_safe_threshold == 0.5  # 기본값

            bounds = get_safety_bounds_settings()
            assert bounds.timeout_ms_min == 250
            assert bounds.timeout_ms_max == 30000  # 기본값

            cache = get_state_cache_settings()
            assert cache.base_ttl == 8.0
            assert cache.jitter_range == 0.5  # 기본값

            resource = get_resource_monitor_settings()
            assert resource.safety_margin == 0.20

            strategy = get_apply_strategy_settings()
            assert strategy.circuit_breaker_delay == 45
            assert strategy.security_delay == 60  # 기본값

            decision = get_decision_engine_settings()
            assert decision.min_change_ratio == 0.08
            assert decision.confidence_samples_very_low == 5  # 기본값

    def test_no_cross_contamination_between_modules(self):
        """한 모듈의 환경변수가 다른 모듈에 영향 없음."""
        # BALDUR_RUNTIME_FEEDBACK_ prefix가 BALDUR_AUTO_ROLLBACK_에 영향 없어야 함
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_RUNTIME_FEEDBACK_MAX_CONSECUTIVE_FAILURES": "15",
            },
        ):
            from baldur.settings.auto_rollback import (
                get_auto_rollback_settings,
                reset_auto_rollback_settings,
            )
            from baldur.settings.runtime_feedback import (
                get_runtime_feedback_settings,
                reset_runtime_feedback_settings,
            )

            reset_runtime_feedback_settings()
            reset_auto_rollback_settings()

            runtime = get_runtime_feedback_settings()
            rollback = get_auto_rollback_settings()

            # RuntimeFeedback만 변경됨
            assert runtime.max_consecutive_failures == 15

            # AutoRollback은 기본값 유지
            assert rollback.error_rate_major == 0.1
            assert rollback.failures_alert == 3
