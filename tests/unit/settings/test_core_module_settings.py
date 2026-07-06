"""
Tests for Core Module Settings (Step 1, 2).

Settings modules for core module hardcoded config values:
- RuntimeFeedbackSettings
- AutoRollbackSettings
- SafetyBoundsSettings
- StateCacheSettings
- ApplyStrategySettings
- DecisionEngineSettings
- JitterSettings (extensions)
"""

import pytest
from pydantic import ValidationError


class TestRuntimeFeedbackSettings:
    """RuntimeFeedbackSettings 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.settings.runtime_feedback import (
            reset_runtime_feedback_settings,
        )

        reset_runtime_feedback_settings()
        yield
        reset_runtime_feedback_settings()

    def test_default_values(self):
        """기본값이 core/runtime_feedback.py 상수와 일치하는지 검증."""
        from baldur.settings.runtime_feedback import RuntimeFeedbackSettings

        settings = RuntimeFeedbackSettings()

        # core/runtime_feedback.py lines 86-90
        assert settings.max_consecutive_failures == 3
        assert settings.rollback_cooldown == 120
        assert settings.adjustment_wait == 30

    def test_env_override(self, monkeypatch):
        """환경변수로 값 오버라이드 검증."""
        from baldur.settings.runtime_feedback import RuntimeFeedbackSettings

        monkeypatch.setenv("BALDUR_RUNTIME_FEEDBACK_MAX_CONSECUTIVE_FAILURES", "5")
        monkeypatch.setenv("BALDUR_RUNTIME_FEEDBACK_ROLLBACK_COOLDOWN", "300")
        monkeypatch.setenv("BALDUR_RUNTIME_FEEDBACK_ADJUSTMENT_WAIT", "60")

        settings = RuntimeFeedbackSettings()

        assert settings.max_consecutive_failures == 5
        assert settings.rollback_cooldown == 300
        assert settings.adjustment_wait == 60

    def test_validation_min_max(self):
        """min/max 범위 검증."""
        from baldur.settings.runtime_feedback import RuntimeFeedbackSettings

        # max_consecutive_failures: ge=1, le=20
        with pytest.raises(ValidationError):
            RuntimeFeedbackSettings(max_consecutive_failures=0)
        with pytest.raises(ValidationError):
            RuntimeFeedbackSettings(max_consecutive_failures=21)

        # rollback_cooldown: ge=10, le=3600
        with pytest.raises(ValidationError):
            RuntimeFeedbackSettings(rollback_cooldown=5)
        with pytest.raises(ValidationError):
            RuntimeFeedbackSettings(rollback_cooldown=3601)

    def test_degradation_threshold_defaults(self):
        """338 extension: degradation detection threshold defaults."""
        from baldur.settings.runtime_feedback import RuntimeFeedbackSettings

        settings = RuntimeFeedbackSettings()

        assert settings.error_increase_threshold == 0.2
        assert settings.latency_increase_threshold == 0.5
        assert settings.zero_to_error_threshold == 0.05

    def test_degradation_threshold_env_override(self, monkeypatch):
        """338 extension: degradation thresholds overridden via env vars."""
        from baldur.settings.runtime_feedback import RuntimeFeedbackSettings

        monkeypatch.setenv("BALDUR_RUNTIME_FEEDBACK_ERROR_INCREASE_THRESHOLD", "0.3")
        monkeypatch.setenv("BALDUR_RUNTIME_FEEDBACK_LATENCY_INCREASE_THRESHOLD", "0.8")
        monkeypatch.setenv("BALDUR_RUNTIME_FEEDBACK_ZERO_TO_ERROR_THRESHOLD", "0.1")

        settings = RuntimeFeedbackSettings()

        assert settings.error_increase_threshold == 0.3
        assert settings.latency_increase_threshold == 0.8
        assert settings.zero_to_error_threshold == 0.1

    def test_degradation_threshold_boundary_below_minimum_raises(self):
        """338 extension: error_increase_threshold below ge=0.01 raises."""
        from baldur.settings.runtime_feedback import RuntimeFeedbackSettings

        with pytest.raises(ValidationError):
            RuntimeFeedbackSettings(error_increase_threshold=0.009)

    def test_singleton_pattern(self):
        """싱글톤 패턴 동작 검증."""
        from baldur.settings.runtime_feedback import (
            get_runtime_feedback_settings,
            reset_runtime_feedback_settings,
        )

        settings1 = get_runtime_feedback_settings()
        settings2 = get_runtime_feedback_settings()

        assert settings1 is settings2

        reset_runtime_feedback_settings()
        settings3 = get_runtime_feedback_settings()

        assert settings3 is not settings1


class TestAutoRollbackSettings:
    """AutoRollbackSettings 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.settings.auto_rollback import reset_auto_rollback_settings

        reset_auto_rollback_settings()
        yield
        reset_auto_rollback_settings()

    def test_default_values(self):
        """기본값이 core/auto_rollback_guard.py 상수와 일치하는지 검증."""
        from baldur.settings.auto_rollback import AutoRollbackSettings

        settings = AutoRollbackSettings()

        # core/auto_rollback_guard.py lines 135-142
        assert settings.error_rate_major == 0.1
        assert settings.error_rate_critical == 0.3
        assert settings.latency_major_ms == 5000
        assert settings.latency_critical_ms == 10000
        assert settings.failures_alert == 3
        assert settings.failures_emergency == 5

    def test_env_override(self, monkeypatch):
        """환경변수로 값 오버라이드 검증."""
        from baldur.settings.auto_rollback import AutoRollbackSettings

        monkeypatch.setenv("BALDUR_AUTO_ROLLBACK_ERROR_RATE_MAJOR", "0.05")
        monkeypatch.setenv("BALDUR_AUTO_ROLLBACK_ERROR_RATE_CRITICAL", "0.2")
        monkeypatch.setenv("BALDUR_AUTO_ROLLBACK_LATENCY_MAJOR_MS", "3000")
        monkeypatch.setenv("BALDUR_AUTO_ROLLBACK_LATENCY_CRITICAL_MS", "8000")

        settings = AutoRollbackSettings()

        assert settings.error_rate_major == 0.05
        assert settings.error_rate_critical == 0.2
        assert settings.latency_major_ms == 3000
        assert settings.latency_critical_ms == 8000

    def test_validation_threshold_order(self):
        """임계값 순서 검증 (major < critical)."""
        from baldur.settings.auto_rollback import AutoRollbackSettings

        # error_rate: major >= critical 이면 에러
        with pytest.raises(ValidationError) as exc_info:
            AutoRollbackSettings(error_rate_major=0.3, error_rate_critical=0.3)
        assert "error_rate_major" in str(exc_info.value)

        # latency: major >= critical 이면 에러
        with pytest.raises(ValidationError) as exc_info:
            AutoRollbackSettings(latency_major_ms=10000, latency_critical_ms=10000)
        assert "latency_major_ms" in str(exc_info.value)

        # failures: alert >= emergency 이면 에러
        with pytest.raises(ValidationError) as exc_info:
            AutoRollbackSettings(failures_alert=5, failures_emergency=5)
        assert "failures_alert" in str(exc_info.value)

    def test_minor_threshold_defaults(self):
        """338 extension: minor degradation threshold and cooldown defaults."""
        from baldur.settings.auto_rollback import AutoRollbackSettings

        settings = AutoRollbackSettings()

        assert settings.error_rate_minor == 0.05
        assert settings.latency_minor_ms == 3000
        assert settings.cooldown_minutes == 5

    def test_minor_threshold_env_override(self, monkeypatch):
        """338 extension: minor thresholds overridden via env vars."""
        from baldur.settings.auto_rollback import AutoRollbackSettings

        monkeypatch.setenv("BALDUR_AUTO_ROLLBACK_ERROR_RATE_MINOR", "0.08")
        monkeypatch.setenv("BALDUR_AUTO_ROLLBACK_LATENCY_MINOR_MS", "2000")
        monkeypatch.setenv("BALDUR_AUTO_ROLLBACK_COOLDOWN_MINUTES", "10")

        settings = AutoRollbackSettings()

        assert settings.error_rate_minor == 0.08
        assert settings.latency_minor_ms == 2000
        assert settings.cooldown_minutes == 10

    def test_cooldown_below_minimum_raises_validation_error(self):
        """338 extension: cooldown_minutes below ge=1 raises."""
        from baldur.settings.auto_rollback import AutoRollbackSettings

        with pytest.raises(ValidationError):
            AutoRollbackSettings(cooldown_minutes=0)


class TestSafetyBoundsSettings:
    """SafetyBoundsSettings 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.settings.safety_bounds import reset_safety_bounds_settings

        reset_safety_bounds_settings()
        yield
        reset_safety_bounds_settings()

    def test_default_values(self):
        """기본값이 core/safety_bounds.py DEFAULT_BOUNDS와 일치하는지 검증."""
        from baldur.settings.safety_bounds import SafetyBoundsSettings

        settings = SafetyBoundsSettings()

        # core/safety_bounds.py lines 53-90
        # timeout_ms
        assert settings.timeout_ms_min == 100
        assert settings.timeout_ms_max == 30000
        assert settings.timeout_ms_max_change == 0.3

        # retry_count
        assert settings.retry_count_min == 0
        assert settings.retry_count_max == 10
        assert settings.retry_count_max_change == 0.5

        # circuit_breaker_threshold
        assert settings.circuit_breaker_threshold_min == 0.1
        assert settings.circuit_breaker_threshold_max == 0.9
        assert settings.circuit_breaker_threshold_max_change == 0.2

    def test_env_override(self, monkeypatch):
        """환경변수로 값 오버라이드 검증."""
        from baldur.settings.safety_bounds import SafetyBoundsSettings

        monkeypatch.setenv("BALDUR_SAFETY_BOUNDS_TIMEOUT_MS_MIN", "200")
        monkeypatch.setenv("BALDUR_SAFETY_BOUNDS_TIMEOUT_MS_MAX", "60000")

        settings = SafetyBoundsSettings()

        assert settings.timeout_ms_min == 200
        assert settings.timeout_ms_max == 60000

    def test_get_bounds_method(self):
        """get_bounds() 메서드로 ParameterBoundConfig 조회."""
        from baldur.settings.safety_bounds import SafetyBoundsSettings

        settings = SafetyBoundsSettings()

        bounds = settings.get_bounds("timeout_ms")
        assert bounds is not None
        assert bounds.min_value == 100
        assert bounds.max_value == 30000
        assert bounds.max_change_per_cycle == 0.3

        # 알 수 없는 파라미터
        unknown = settings.get_bounds("unknown_param")
        assert unknown is None

    def test_parameter_bound_config_validation(self):
        """ParameterBoundConfig min > max 검증."""
        from baldur.settings.safety_bounds import ParameterBoundConfig

        with pytest.raises(ValidationError) as exc_info:
            ParameterBoundConfig(
                min_value=100,
                max_value=50,  # min > max
                max_change_per_cycle=0.3,
            )
        assert "min_value" in str(exc_info.value) or "max_value" in str(exc_info.value)


class TestStateCacheSettings:
    """StateCacheSettings 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.settings.state_cache import reset_state_cache_settings

        reset_state_cache_settings()
        yield
        reset_state_cache_settings()

    def test_default_values(self):
        """기본값이 core/state_cache.py 상수와 일치하는지 검증."""
        from baldur.settings.state_cache import StateCacheSettings

        settings = StateCacheSettings()

        # core/state_cache.py lines 38-39
        assert settings.base_ttl == 5.0
        assert settings.jitter_range == 0.5

    def test_env_override(self, monkeypatch):
        """환경변수로 값 오버라이드 검증."""
        from baldur.settings.state_cache import StateCacheSettings

        monkeypatch.setenv("BALDUR_STATE_CACHE_BASE_TTL", "10.0")
        monkeypatch.setenv("BALDUR_STATE_CACHE_JITTER_RANGE", "2.0")

        settings = StateCacheSettings()

        assert settings.base_ttl == 10.0
        assert settings.jitter_range == 2.0

    def test_validation_jitter_exceeds_ttl(self):
        """jitter_range가 base_ttl을 초과하면 에러."""
        from baldur.settings.state_cache import StateCacheSettings

        with pytest.raises(ValidationError) as exc_info:
            StateCacheSettings(base_ttl=1.0, jitter_range=2.0)
        assert "jitter_range" in str(exc_info.value)


class TestApplyStrategySettings:
    """ApplyStrategySettings 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.settings.apply_strategy import reset_apply_strategy_settings

        reset_apply_strategy_settings()
        yield
        reset_apply_strategy_settings()

    def test_default_values(self):
        """기본값이 core/apply_strategy.py DEFAULT_APPLY_STRATEGIES와 일치하는지 검증."""
        from baldur.settings.apply_strategy import ApplyStrategySettings

        settings = ApplyStrategySettings()

        # 즉시 적용
        assert settings.sla_delay == 0
        assert settings.metrics_delay == 0
        assert settings.notification_delay == 0
        assert settings.forensic_delay == 0
        assert settings.rate_limit_delay == 0

        # 지연 적용
        assert settings.retry_delay == 10
        assert settings.dlq_delay == 10

        # 핵심 보호
        assert settings.circuit_breaker_delay == 30
        assert settings.idempotency_delay == 30
        assert settings.security_delay == 60
        assert settings.error_budget_delay == 30

        assert settings.default_grace_timeout == 60

    def test_env_override(self, monkeypatch):
        """환경변수로 값 오버라이드 검증."""
        from baldur.settings.apply_strategy import ApplyStrategySettings

        monkeypatch.setenv("BALDUR_APPLY_STRATEGY_CIRCUIT_BREAKER_DELAY", "60")
        monkeypatch.setenv("BALDUR_APPLY_STRATEGY_SECURITY_DELAY", "120")

        settings = ApplyStrategySettings()

        assert settings.circuit_breaker_delay == 60
        assert settings.security_delay == 120


class TestDecisionEngineSettings:
    """DecisionEngineSettings 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.settings.decision_engine import reset_decision_engine_settings

        reset_decision_engine_settings()
        yield
        reset_decision_engine_settings()

    def test_default_values(self):
        """기본값이 core/decision_engine.py 상수와 일치하는지 검증."""
        from baldur.settings.decision_engine import DecisionEngineSettings

        settings = DecisionEngineSettings()

        # core/decision_engine.py line 119
        assert settings.min_change_ratio == 0.05

        # 샘플 수 임계값 (lines 244-254)
        assert settings.confidence_samples_very_low == 5
        assert settings.confidence_samples_low == 20
        assert settings.confidence_samples_medium == 50
        assert settings.confidence_samples_high == 100

        # 신뢰도 값
        assert settings.confidence_value_very_low == 0.3
        assert settings.confidence_value_low == 0.5
        assert settings.confidence_value_medium == 0.65
        assert settings.confidence_value_high == 0.75
        assert settings.confidence_value_very_high == 0.9

        # 안정성 계수 (lines 256-267)
        assert settings.stability_cv_high == 0.5
        assert settings.stability_cv_medium == 0.2
        assert settings.stability_factor_unstable == 0.7
        assert settings.stability_factor_moderate == 0.85
        assert settings.stability_factor_stable == 1.0

    def test_env_override(self, monkeypatch):
        """환경변수로 값 오버라이드 검증."""
        from baldur.settings.decision_engine import DecisionEngineSettings

        monkeypatch.setenv("BALDUR_DECISION_ENGINE_MIN_CHANGE_RATIO", "0.1")

        settings = DecisionEngineSettings()

        assert settings.min_change_ratio == 0.1

    def test_get_sample_confidence(self):
        """get_sample_confidence() 메서드 검증."""
        from baldur.settings.decision_engine import DecisionEngineSettings

        settings = DecisionEngineSettings()

        # 샘플 수별 신뢰도
        assert settings.get_sample_confidence(3) == 0.3  # < 5
        assert settings.get_sample_confidence(10) == 0.5  # 5 <= x < 20
        assert settings.get_sample_confidence(30) == 0.65  # 20 <= x < 50
        assert settings.get_sample_confidence(75) == 0.75  # 50 <= x < 100
        assert settings.get_sample_confidence(150) == 0.9  # >= 100

    def test_get_stability_factor(self):
        """get_stability_factor() 메서드 검증."""
        from baldur.settings.decision_engine import DecisionEngineSettings

        settings = DecisionEngineSettings()

        # CV(변동계수)별 안정성 계수
        assert settings.get_stability_factor(0.6) == 0.7  # > 0.5 (unstable)
        assert settings.get_stability_factor(0.3) == 0.85  # > 0.2 (moderate)
        assert settings.get_stability_factor(0.1) == 1.0  # <= 0.2 (stable)

    def test_validation_threshold_order(self):
        """임계값 순서 검증."""
        from baldur.settings.decision_engine import DecisionEngineSettings

        # 샘플 수 순서 에러
        with pytest.raises(ValidationError) as exc_info:
            DecisionEngineSettings(
                confidence_samples_very_low=50,  # 순서 위반
                confidence_samples_low=20,
            )
        assert "ascending order" in str(exc_info.value)

        # CV 순서 에러
        with pytest.raises(ValidationError) as exc_info:
            DecisionEngineSettings(
                stability_cv_medium=0.6,  # medium >= high
                stability_cv_high=0.5,
            )
        assert "stability_cv_medium" in str(exc_info.value)


class TestJitterSettingsExtensions:
    """JitterSettings AdaptiveJitter 임계값 확장 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.settings.jitter import reset_jitter_settings

        reset_jitter_settings()
        yield
        reset_jitter_settings()

    def test_adaptive_jitter_default_values(self):
        """AdaptiveJitter 임계값 기본값 검증."""
        from baldur.settings.jitter import JitterSettings

        settings = JitterSettings()

        # core/adaptive_jitter.py lines 43-46
        assert settings.error_budget_danger_threshold == 0.2
        assert settings.error_budget_safe_threshold == 0.5
        assert settings.load_high_threshold == 0.8
        assert settings.load_low_threshold == 0.3

    def test_env_override(self, monkeypatch):
        """환경변수로 값 오버라이드 검증."""
        from baldur.settings.jitter import JitterSettings

        monkeypatch.setenv("BALDUR_JITTER_ERROR_BUDGET_DANGER_THRESHOLD", "0.1")
        monkeypatch.setenv("BALDUR_JITTER_ERROR_BUDGET_SAFE_THRESHOLD", "0.6")
        monkeypatch.setenv("BALDUR_JITTER_LOAD_HIGH_THRESHOLD", "0.9")
        monkeypatch.setenv("BALDUR_JITTER_LOAD_LOW_THRESHOLD", "0.2")

        settings = JitterSettings()

        assert settings.error_budget_danger_threshold == 0.1
        assert settings.error_budget_safe_threshold == 0.6
        assert settings.load_high_threshold == 0.9
        assert settings.load_low_threshold == 0.2

    def test_validation_threshold_order(self):
        """임계값 순서 검증 (danger < safe, low < high)."""
        from baldur.settings.jitter import JitterSettings

        # error_budget: danger >= safe 이면 에러
        with pytest.raises(ValidationError) as exc_info:
            JitterSettings(
                error_budget_danger_threshold=0.5,
                error_budget_safe_threshold=0.5,
            )
        assert "error_budget_danger_threshold" in str(exc_info.value)

        # load: low >= high 이면 에러
        with pytest.raises(ValidationError) as exc_info:
            JitterSettings(
                load_low_threshold=0.8,
                load_high_threshold=0.8,
            )
        assert "load_low_threshold" in str(exc_info.value)


class TestSettingsImportFromInit:
    """settings/__init__.py에서 신규 모듈 import 가능 검증."""

    def test_import_all_new_settings(self):
        """신규 settings 클래스들 import 가능."""
        from baldur.settings import (
            ApplyStrategySettings,
            AutoRollbackSettings,
            DecisionEngineSettings,
            RuntimeFeedbackSettings,
            SafetyBoundsSettings,
            StateCacheSettings,
        )

        # 모든 클래스 인스턴스화 가능
        assert RuntimeFeedbackSettings() is not None
        assert AutoRollbackSettings() is not None
        assert SafetyBoundsSettings() is not None
        assert StateCacheSettings() is not None
        assert ApplyStrategySettings() is not None
        assert DecisionEngineSettings() is not None
