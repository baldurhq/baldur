"""
Unit tests for GracefulDegradation.

테스트 항목:
- FeaturePriority enum
- Feature 데이터클래스
- GracefulDegradation 기능 등록/해제
- 레벨별 기능 활성화/비활성화
- 콜백 호출
"""

from unittest.mock import Mock

import pytest

from baldur.scaling.config import (
    BackpressureLevel,
    BackpressureSettings,
    reset_backpressure_settings,
)
from baldur.scaling.graceful_degradation import (
    Feature,
    FeaturePriority,
    GracefulDegradation,
    get_graceful_degradation,
)


class TestFeaturePriority:
    """FeaturePriority enum 테스트."""

    def test_priority_values(self):
        """우선순위 값 확인."""
        assert FeaturePriority.CRITICAL.value == 0
        assert FeaturePriority.HIGH.value == 1
        assert FeaturePriority.MEDIUM.value == 2
        assert FeaturePriority.LOW.value == 3
        assert FeaturePriority.OPTIONAL.value == 4

    def test_priorities_are_ordered(self):
        """우선순위가 순서대로인지 확인."""
        assert FeaturePriority.CRITICAL.value < FeaturePriority.HIGH.value
        assert FeaturePriority.HIGH.value < FeaturePriority.MEDIUM.value
        assert FeaturePriority.MEDIUM.value < FeaturePriority.LOW.value
        assert FeaturePriority.LOW.value < FeaturePriority.OPTIONAL.value


class TestFeature:
    """Feature 데이터클래스 테스트."""

    def test_feature_creation(self):
        """Feature 생성."""
        feature = Feature(
            name="test_feature",
            priority=FeaturePriority.MEDIUM,
        )

        assert feature.name == "test_feature"
        assert feature.priority == FeaturePriority.MEDIUM
        assert feature.enabled is True
        assert feature.on_disable is None
        assert feature.on_enable is None

    def test_feature_with_callbacks(self):
        """콜백이 있는 Feature 생성."""
        on_disable = Mock()
        on_enable = Mock()

        feature = Feature(
            name="callback_feature",
            priority=FeaturePriority.LOW,
            enabled=False,
            on_disable=on_disable,
            on_enable=on_enable,
        )

        assert feature.enabled is False
        assert feature.on_disable is on_disable
        assert feature.on_enable is on_enable


class TestGracefulDegradation:
    """GracefulDegradation 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """각 테스트 전후로 싱글톤 리셋."""
        reset_backpressure_settings()
        import baldur.scaling.graceful_degradation as gd_module

        gd_module._degradation = None
        yield
        reset_backpressure_settings()
        gd_module._degradation = None

    def test_register_feature(self):
        """기능 등록."""
        settings = BackpressureSettings()
        degradation = GracefulDegradation(settings=settings)

        feature = Feature(name="test", priority=FeaturePriority.MEDIUM)
        degradation.register_feature(feature)

        assert degradation.is_enabled("test") is True

    def test_unregister_feature(self):
        """기능 등록 해제."""
        settings = BackpressureSettings()
        degradation = GracefulDegradation(settings=settings)

        feature = Feature(name="test", priority=FeaturePriority.MEDIUM)
        degradation.register_feature(feature)
        degradation.unregister_feature("test")

        # 등록 해제된 기능은 True 반환 (기본값)
        assert degradation.is_enabled("test") is True

    def test_is_enabled_unregistered_feature(self):
        """등록되지 않은 기능은 항상 True."""
        settings = BackpressureSettings()
        degradation = GracefulDegradation(settings=settings)

        assert degradation.is_enabled("unknown_feature") is True

    def test_is_enabled_when_disabled(self):
        """Graceful Degradation 비활성화 시 항상 True."""
        settings = BackpressureSettings(graceful_degradation_enabled=False)
        degradation = GracefulDegradation(settings=settings)

        feature = Feature(name="test", priority=FeaturePriority.OPTIONAL)
        degradation.register_feature(feature)
        feature.enabled = False

        # 비활성화 시 항상 True
        assert degradation.is_enabled("test") is True


class TestGracefulDegradationLevelUpdates:
    """GracefulDegradation 레벨 업데이트 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """각 테스트 전후로 싱글톤 리셋."""
        reset_backpressure_settings()
        import baldur.scaling.graceful_degradation as gd_module

        gd_module._degradation = None
        yield
        reset_backpressure_settings()
        gd_module._degradation = None

    def test_update_level_disables_low_priority(self):
        """레벨 업데이트 시 낮은 우선순위 기능 비활성화."""
        settings = BackpressureSettings(graceful_degradation_enabled=True)
        degradation = GracefulDegradation(settings=settings)

        # 다양한 우선순위 기능 등록
        degradation.register_feature(
            Feature(
                name="critical_feature",
                priority=FeaturePriority.CRITICAL,
            )
        )
        degradation.register_feature(
            Feature(
                name="optional_feature",
                priority=FeaturePriority.OPTIONAL,
            )
        )

        # HIGH 레벨로 업데이트
        degradation.update_level(BackpressureLevel.HIGH)

        # CRITICAL은 활성화, OPTIONAL은 비활성화
        assert degradation.is_enabled("critical_feature") is True
        assert degradation.is_enabled("optional_feature") is False

    def test_update_level_none_enables_all(self):
        """NONE 레벨에서 모든 기능 활성화."""
        settings = BackpressureSettings(graceful_degradation_enabled=True)
        degradation = GracefulDegradation(settings=settings)

        degradation.register_feature(
            Feature(
                name="optional_feature",
                priority=FeaturePriority.OPTIONAL,
            )
        )

        # CRITICAL -> NONE
        degradation.update_level(BackpressureLevel.CRITICAL)
        assert degradation.is_enabled("optional_feature") is False

        degradation.update_level(BackpressureLevel.NONE)
        assert degradation.is_enabled("optional_feature") is True

    def test_update_level_critical_only_keeps_critical(self):
        """CRITICAL 레벨에서 CRITICAL 기능만 유지."""
        settings = BackpressureSettings(graceful_degradation_enabled=True)
        degradation = GracefulDegradation(settings=settings)

        degradation.register_feature(
            Feature(
                name="critical",
                priority=FeaturePriority.CRITICAL,
            )
        )
        degradation.register_feature(
            Feature(
                name="high",
                priority=FeaturePriority.HIGH,
            )
        )
        degradation.register_feature(
            Feature(
                name="medium",
                priority=FeaturePriority.MEDIUM,
            )
        )

        degradation.update_level(BackpressureLevel.CRITICAL)

        assert degradation.is_enabled("critical") is True
        assert degradation.is_enabled("high") is False
        assert degradation.is_enabled("medium") is False

    def test_callbacks_called_on_state_change(self):
        """상태 변경 시 콜백 호출."""
        settings = BackpressureSettings(graceful_degradation_enabled=True)
        degradation = GracefulDegradation(settings=settings)

        on_disable = Mock()
        on_enable = Mock()

        degradation.register_feature(
            Feature(
                name="callback_feature",
                priority=FeaturePriority.OPTIONAL,
                on_disable=on_disable,
                on_enable=on_enable,
            )
        )

        # 비활성화
        degradation.update_level(BackpressureLevel.HIGH)
        on_disable.assert_called_once()

        # 활성화
        degradation.update_level(BackpressureLevel.NONE)
        on_enable.assert_called_once()

    def test_callback_error_handled(self):
        """콜백 오류 시 graceful 처리."""
        settings = BackpressureSettings(graceful_degradation_enabled=True)
        degradation = GracefulDegradation(settings=settings)

        on_disable = Mock(side_effect=Exception("Callback error"))

        degradation.register_feature(
            Feature(
                name="error_feature",
                priority=FeaturePriority.OPTIONAL,
                on_disable=on_disable,
            )
        )

        # 예외가 발생해도 계속 진행
        degradation.update_level(BackpressureLevel.HIGH)

        assert degradation.is_enabled("error_feature") is False


class TestGracefulDegradationFeatureLists:
    """기능 목록 조회 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """각 테스트 전후로 싱글톤 리셋."""
        reset_backpressure_settings()
        import baldur.scaling.graceful_degradation as gd_module

        gd_module._degradation = None
        yield
        reset_backpressure_settings()
        gd_module._degradation = None

    def test_get_enabled_features(self):
        """활성화된 기능 목록 조회."""
        settings = BackpressureSettings(graceful_degradation_enabled=True)
        degradation = GracefulDegradation(settings=settings)

        degradation.register_feature(
            Feature(
                name="critical",
                priority=FeaturePriority.CRITICAL,
            )
        )
        degradation.register_feature(
            Feature(
                name="optional",
                priority=FeaturePriority.OPTIONAL,
            )
        )

        degradation.update_level(BackpressureLevel.HIGH)

        enabled = degradation.get_enabled_features()
        assert "critical" in enabled
        assert "optional" not in enabled

    def test_get_disabled_features(self):
        """비활성화된 기능 목록 조회."""
        settings = BackpressureSettings(graceful_degradation_enabled=True)
        degradation = GracefulDegradation(settings=settings)

        degradation.register_feature(
            Feature(
                name="critical",
                priority=FeaturePriority.CRITICAL,
            )
        )
        degradation.register_feature(
            Feature(
                name="optional",
                priority=FeaturePriority.OPTIONAL,
            )
        )

        degradation.update_level(BackpressureLevel.HIGH)

        disabled = degradation.get_disabled_features()
        assert "optional" in disabled
        assert "critical" not in disabled

    def test_get_current_level(self):
        """현재 레벨 조회."""
        settings = BackpressureSettings(graceful_degradation_enabled=True)
        degradation = GracefulDegradation(settings=settings)

        assert degradation.get_current_level() == BackpressureLevel.NONE

        degradation.update_level(BackpressureLevel.MEDIUM)
        assert degradation.get_current_level() == BackpressureLevel.MEDIUM


class TestGracefulDegradationSingleton:
    """GracefulDegradation 싱글톤 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """각 테스트 전후로 싱글톤 리셋."""
        reset_backpressure_settings()
        import baldur.scaling.graceful_degradation as gd_module

        gd_module._degradation = None
        yield
        reset_backpressure_settings()
        gd_module._degradation = None

    def test_singleton_returns_same_instance(self):
        """싱글톤이 동일한 인스턴스를 반환하는지 확인."""
        degradation1 = get_graceful_degradation()
        degradation2 = get_graceful_degradation()
        assert degradation1 is degradation2
