"""
Backpressure Task Mixin for Celery.

Celery Task에 Backpressure 기능을 추가하는 Mixin 클래스입니다.
RateController와 GracefulDegradation을 통합합니다.

Usage:
    from celery import Task
    from baldur.tasks.backpressure_mixin import BackpressureTaskMixin

    class MyTask(BackpressureTaskMixin, Task):
        def run(self, *args, **kwargs):
            # Task implementation
            pass
"""

from __future__ import annotations

from typing import Any, ClassVar

import structlog

from baldur.settings.backpressure import get_backpressure_settings

try:
    from baldur.scaling.graceful_degradation import (
        Feature,
        FeaturePriority,
        get_graceful_degradation,
    )
    from baldur.scaling.rate_controller import get_rate_controller

    _SCALING_AVAILABLE = True
except ImportError:
    _SCALING_AVAILABLE = False

logger = structlog.get_logger()


class BackpressureTaskMixin:
    """
    Backpressure Task Mixin for Celery.

    Celery Task 클래스에 Backpressure 기능을 추가합니다.

    기능:
    - 과부하 시 자동 재시도 (configurable countdown)
    - Graceful Degradation 기능 등록/확인
    - Rate 체크 후 처리 결정

    Attributes:
        backpressure_enabled: Backpressure 활성화 여부
        backpressure_retry_countdown: 재시도 대기 시간 (초)
        backpressure_max_retries: 최대 재시도 횟수
        backpressure_features: 등록할 기능 목록

    Usage:
        from celery import Task

        class MyTask(BackpressureTaskMixin, Task):
            backpressure_enabled = True
            backpressure_retry_countdown = 5
            backpressure_max_retries = 3

            def run(self, *args, **kwargs):
                # 과부하 시 자동으로 재시도됨
                return self.process_data(*args, **kwargs)

    Integration with BaseNotifyingTask:
        from baldur.tasks.base import BaseNotifyingTask

        class MyTask(BackpressureTaskMixin, BaseNotifyingTask):
            notification_policy = NotificationPolicy(...)

            def run(self, *args, **kwargs):
                return {...}
    """

    # Backpressure 설정 (서브클래스에서 오버라이드 가능)
    backpressure_enabled: ClassVar[bool] = True
    backpressure_retry_countdown: ClassVar[int] = 5
    backpressure_max_retries: ClassVar[int] = 3
    backpressure_features: ClassVar[list[Feature]] = []

    # 내부 상태
    _backpressure_initialized: ClassVar[bool] = False
    _backpressure_retry_count: int = 0

    def __init__(self) -> None:
        """초기화."""
        super().__init__()
        self._init_backpressure()

    def _init_backpressure(self) -> None:
        """Backpressure 초기화 (한 번만 실행)."""
        if not _SCALING_AVAILABLE:
            return

        if self.__class__._backpressure_initialized:
            return

        self.__class__._backpressure_initialized = True

        # 기능 등록
        if self.backpressure_features:
            degradation = get_graceful_degradation()
            for feature in self.backpressure_features:
                degradation.register_feature(feature)
                logger.debug(
                    "cell_registry.bulkheads_registered",
                    feature=feature.name,
                )

    def should_process_with_backpressure(self) -> bool:
        """
        Backpressure 체크 후 처리 여부 결정.

        Returns:
            True: 처리 진행
            False: 과부하로 인해 처리 보류
        """
        if not _SCALING_AVAILABLE or not self.backpressure_enabled:
            return True

        settings = get_backpressure_settings()
        if not settings.backpressure_enabled:
            return True

        controller = get_rate_controller()
        return controller.should_process()

    def retry_with_backpressure(self, *args: Any, **kwargs: Any) -> Any:
        """
        Backpressure로 인한 재시도 스케줄.

        Celery의 retry()를 호출하여 나중에 다시 실행합니다.
        최대 재시도 횟수를 초과하면 예외를 발생시킵니다.

        Raises:
            MaxRetriesExceededError: 최대 재시도 횟수 초과 시
        """
        self._backpressure_retry_count += 1

        if self._backpressure_retry_count > self.backpressure_max_retries:
            logger.error(
                "backpressure_task_mixin.max_retries_exceeded_task",
                backpressure_retry_count=self._backpressure_retry_count,
            )
            raise BackpressureMaxRetriesExceeded(
                f"Max backpressure retries ({self.backpressure_max_retries}) exceeded"
            )

        logger.info(
            "backpressure_task_mixin.scheduling_retry",
            backpressure_retry_countdown=self.backpressure_retry_countdown,
            retry_count=self._backpressure_retry_count,
            max_retries=self.backpressure_max_retries,
        )

        # Celery Task의 retry 메서드 호출 (Celery Task를 상속한 경우)
        if hasattr(self, "retry"):
            return self.retry(
                countdown=self.backpressure_retry_countdown,
                max_retries=self.backpressure_max_retries,
            )
        raise BackpressureRetryRequired(
            f"Backpressure retry required (countdown={self.backpressure_retry_countdown})"
        )

    def is_feature_enabled(self, feature_name: str) -> bool:
        """
        Graceful Degradation 기능 활성화 여부 확인.

        Args:
            feature_name: 기능 이름

        Returns:
            True: 기능 활성화됨
            False: 기능 비활성화됨
        """
        if not _SCALING_AVAILABLE:
            return True
        degradation = get_graceful_degradation()
        return degradation.is_enabled(feature_name)


class BackpressureMaxRetriesExceeded(Exception):
    """Backpressure 최대 재시도 횟수 초과 예외."""

    pass


class BackpressureRetryRequired(Exception):
    """Backpressure 재시도 필요 예외 (non-Celery 환경용)."""

    pass


# =============================================================================
# 기본 기능 정의 (Celery Task용)
# =============================================================================

# 태스크에서 사용할 수 있는 미리 정의된 기능들
if _SCALING_AVAILABLE:
    TASK_DETAILED_LOGGING = Feature(
        name="task_detailed_logging",
        priority=FeaturePriority.OPTIONAL,
    )

    TASK_METRICS_COLLECTION = Feature(
        name="task_metrics_collection",
        priority=FeaturePriority.LOW,
    )

    TASK_NOTIFICATION = Feature(
        name="task_notification",
        priority=FeaturePriority.MEDIUM,
    )

    TASK_AUDIT_LOGGING = Feature(
        name="task_audit_logging",
        priority=FeaturePriority.HIGH,
    )

    DEFAULT_TASK_FEATURES: list = [
        TASK_DETAILED_LOGGING,
        TASK_METRICS_COLLECTION,
        TASK_NOTIFICATION,
        TASK_AUDIT_LOGGING,
    ]
else:
    DEFAULT_TASK_FEATURES: list = []
