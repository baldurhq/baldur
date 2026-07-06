"""
BackpressureTaskMixin 단위 테스트.

테스트 항목:
- should_process_with_backpressure 동작
- retry_with_backpressure 동작
- is_feature_enabled 동작
- 예외 클래스들
- DEFAULT_TASK_FEATURES 정의
"""

from unittest.mock import MagicMock, patch

import pytest

from baldur.scaling.config import BackpressureSettings
from baldur.scaling.graceful_degradation import Feature, FeaturePriority
from baldur.tasks.backpressure_mixin import (
    DEFAULT_TASK_FEATURES,
    TASK_AUDIT_LOGGING,
    TASK_DETAILED_LOGGING,
    TASK_METRICS_COLLECTION,
    TASK_NOTIFICATION,
    BackpressureMaxRetriesExceeded,
    BackpressureRetryRequired,
    BackpressureTaskMixin,
)


class TestBackpressureTaskMixin:
    """BackpressureTaskMixin 테스트."""

    @pytest.fixture
    def mixin_class(self):
        """테스트용 Mixin 클래스."""
        # 이전 테스트 상태 초기화
        BackpressureTaskMixin._backpressure_initialized = False

        class TestTask(BackpressureTaskMixin):
            backpressure_enabled = True
            backpressure_retry_countdown = 3
            backpressure_max_retries = 2

        return TestTask

    def test_should_process_when_enabled(self, mixin_class):
        """Backpressure 활성화 시 처리 여부 결정 확인."""
        task = mixin_class()

        mock_controller = MagicMock()
        mock_controller.should_process.return_value = True

        with (
            patch(
                "baldur.tasks.backpressure_mixin.get_rate_controller",
                return_value=mock_controller,
            ),
            patch(
                "baldur.tasks.backpressure_mixin.get_backpressure_settings",
                return_value=BackpressureSettings(backpressure_enabled=True),
            ),
        ):
            result = task.should_process_with_backpressure()

        assert result is True
        mock_controller.should_process.assert_called_once()

    def test_should_process_when_disabled_in_class(self, mixin_class):
        """클래스에서 Backpressure 비활성화 시 항상 True 반환."""
        mixin_class.backpressure_enabled = False
        task = mixin_class()

        result = task.should_process_with_backpressure()

        assert result is True

    def test_should_process_when_disabled_in_settings(self, mixin_class):
        """설정에서 Backpressure 비활성화 시 항상 True 반환."""
        task = mixin_class()

        with patch(
            "baldur.tasks.backpressure_mixin.get_backpressure_settings",
            return_value=BackpressureSettings(backpressure_enabled=False),
        ):
            result = task.should_process_with_backpressure()

        assert result is True

    def test_retry_with_backpressure_increments_counter(self, mixin_class):
        """재시도 시 카운터 증가 확인."""
        task = mixin_class()
        task._backpressure_retry_count = 0

        with pytest.raises(BackpressureRetryRequired):
            task.retry_with_backpressure()

        assert task._backpressure_retry_count == 1

    def test_retry_with_backpressure_exceeds_max(self, mixin_class):
        """최대 재시도 초과 시 예외 발생 확인."""
        task = mixin_class()
        task._backpressure_retry_count = 2  # max_retries = 2

        with pytest.raises(BackpressureMaxRetriesExceeded):
            task.retry_with_backpressure()

    def test_retry_with_celery_task(self, mixin_class):
        """Celery Task의 retry 메서드 호출 확인."""
        task = mixin_class()
        task.retry = MagicMock()
        task._backpressure_retry_count = 0

        task.retry_with_backpressure()

        task.retry.assert_called_once_with(countdown=3, max_retries=2)

    def test_is_feature_enabled(self, mixin_class):
        """기능 활성화 여부 확인."""
        task = mixin_class()

        mock_degradation = MagicMock()
        mock_degradation.is_enabled.return_value = True

        with patch(
            "baldur.tasks.backpressure_mixin.get_graceful_degradation",
            return_value=mock_degradation,
        ):
            result = task.is_feature_enabled("test_feature")

        assert result is True
        mock_degradation.is_enabled.assert_called_once_with("test_feature")

    def test_feature_registration_on_init(self):
        """초기화 시 기능 등록 확인."""
        BackpressureTaskMixin._backpressure_initialized = False

        test_feature = Feature(
            name="test_feature",
            priority=FeaturePriority.LOW,
        )

        class TestTaskWithFeatures(BackpressureTaskMixin):
            backpressure_features = [test_feature]

        mock_degradation = MagicMock()

        with patch(
            "baldur.tasks.backpressure_mixin.get_graceful_degradation",
            return_value=mock_degradation,
        ):
            TestTaskWithFeatures()

        mock_degradation.register_feature.assert_called_once_with(test_feature)


class TestExceptionClasses:
    """예외 클래스 테스트."""

    def test_backpressure_max_retries_exceeded(self):
        """BackpressureMaxRetriesExceeded 예외."""
        with pytest.raises(BackpressureMaxRetriesExceeded) as exc_info:
            raise BackpressureMaxRetriesExceeded("Max retries exceeded")

        assert "Max retries exceeded" in str(exc_info.value)

    def test_backpressure_retry_required(self):
        """BackpressureRetryRequired 예외."""
        with pytest.raises(BackpressureRetryRequired) as exc_info:
            raise BackpressureRetryRequired("Retry required")

        assert "Retry required" in str(exc_info.value)


class TestDefaultTaskFeatures:
    """기본 태스크 기능 테스트."""

    def test_default_features_defined(self):
        """기본 기능들이 정의되어 있는지 확인."""
        assert len(DEFAULT_TASK_FEATURES) == 4

    def test_task_detailed_logging(self):
        """TASK_DETAILED_LOGGING 확인."""
        assert TASK_DETAILED_LOGGING.name == "task_detailed_logging"
        assert TASK_DETAILED_LOGGING.priority == FeaturePriority.OPTIONAL

    def test_task_metrics_collection(self):
        """TASK_METRICS_COLLECTION 확인."""
        assert TASK_METRICS_COLLECTION.name == "task_metrics_collection"
        assert TASK_METRICS_COLLECTION.priority == FeaturePriority.LOW

    def test_task_notification(self):
        """TASK_NOTIFICATION 확인."""
        assert TASK_NOTIFICATION.name == "task_notification"
        assert TASK_NOTIFICATION.priority == FeaturePriority.MEDIUM

    def test_task_audit_logging(self):
        """TASK_AUDIT_LOGGING 확인."""
        assert TASK_AUDIT_LOGGING.name == "task_audit_logging"
        assert TASK_AUDIT_LOGGING.priority == FeaturePriority.HIGH


class TestBackpressureTaskMixinScalingUnavailableBehavior:
    """Scaling module unavailable (OSS-only deployment) fail-open behavior."""

    MODULE = "baldur.tasks.backpressure_mixin"

    @pytest.fixture
    def mixin_class(self):
        """Test mixin class with clean state."""
        BackpressureTaskMixin._backpressure_initialized = False

        class TestTask(BackpressureTaskMixin):
            backpressure_enabled = True
            backpressure_features = [MagicMock(name="test_feat")]

        return TestTask

    def test_init_backpressure_scaling_unavailable_skips_registration(
        self, mixin_class
    ):
        """_SCALING_AVAILABLE=False → _init_backpressure returns early, no feature registration."""
        with patch(f"{self.MODULE}._SCALING_AVAILABLE", False):
            mixin_class()

        # _backpressure_initialized remains False (early return before setting it)
        assert mixin_class._backpressure_initialized is False

    def test_should_process_scaling_unavailable_returns_true(self, mixin_class):
        """_SCALING_AVAILABLE=False → should_process_with_backpressure returns True (fail-open)."""
        with patch(f"{self.MODULE}._SCALING_AVAILABLE", False):
            task = mixin_class()
            result = task.should_process_with_backpressure()

        assert result is True

    def test_is_feature_enabled_scaling_unavailable_returns_true(self, mixin_class):
        """_SCALING_AVAILABLE=False → is_feature_enabled returns True for any feature."""
        with patch(f"{self.MODULE}._SCALING_AVAILABLE", False):
            task = mixin_class()
            result = task.is_feature_enabled("any_feature")

        assert result is True

    def test_is_feature_enabled_scaling_unavailable_does_not_call_degradation(
        self, mixin_class
    ):
        """_SCALING_AVAILABLE=False → get_graceful_degradation is never called."""
        with (
            patch(f"{self.MODULE}._SCALING_AVAILABLE", False),
            patch(f"{self.MODULE}.get_graceful_degradation") as mock_degradation_fn,
        ):
            task = mixin_class()
            task.is_feature_enabled("any_feature")

        mock_degradation_fn.assert_not_called()
