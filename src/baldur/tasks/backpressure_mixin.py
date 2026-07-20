"""
Backpressure Task Mixin for Celery.

Mixin class that adds backpressure behavior to a Celery Task.
Integrates RateController and GracefulDegradation.

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

    Adds backpressure behavior to a Celery Task class.

    Features:
    - Automatic retry under overload (configurable countdown)
    - Graceful Degradation feature registration / lookup
    - Processing decision after a rate check

    Attributes:
        backpressure_enabled: Whether backpressure is enabled
        backpressure_retry_countdown: Retry wait time (seconds)
        backpressure_max_retries: Maximum retry count
        backpressure_features: Features to register

    Usage:
        from celery import Task

        class MyTask(BackpressureTaskMixin, Task):
            backpressure_enabled = True
            backpressure_retry_countdown = 5
            backpressure_max_retries = 3

            def run(self, *args, **kwargs):
                # Retried automatically under overload
                return self.process_data(*args, **kwargs)

    Integration with BaseNotifyingTask:
        from baldur.tasks.base import BaseNotifyingTask

        class MyTask(BackpressureTaskMixin, BaseNotifyingTask):
            notification_policy = NotificationPolicy(...)

            def run(self, *args, **kwargs):
                return {...}
    """

    # Backpressure settings (overridable in subclasses)
    backpressure_enabled: ClassVar[bool] = True
    backpressure_retry_countdown: ClassVar[int] = 5
    backpressure_max_retries: ClassVar[int] = 3
    backpressure_features: ClassVar[list[Feature]] = []

    # Internal state
    _backpressure_initialized: ClassVar[bool] = False
    _backpressure_retry_count: int = 0

    def __init__(self) -> None:
        """Initialize."""
        super().__init__()
        self._init_backpressure()

    def _init_backpressure(self) -> None:
        """Initialize backpressure (runs only once)."""
        if not _SCALING_AVAILABLE:
            return

        if self.__class__._backpressure_initialized:
            return

        self.__class__._backpressure_initialized = True

        # Register features
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
        Decide whether to process after a backpressure check.

        Returns:
            True: proceed with processing
            False: hold off processing because of overload
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
        Schedule a retry caused by backpressure.

        Calls Celery's retry() so the task runs again later.
        Raises once the maximum retry count is exceeded.

        Raises:
            MaxRetriesExceededError: when the maximum retry count is exceeded
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

        # Call the Celery Task retry method (when subclassing a Celery Task)
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
        Check whether a Graceful Degradation feature is enabled.

        Args:
            feature_name: Feature name

        Returns:
            True: the feature is enabled
            False: the feature is disabled
        """
        if not _SCALING_AVAILABLE:
            return True
        degradation = get_graceful_degradation()
        return degradation.is_enabled(feature_name)


class BackpressureMaxRetriesExceeded(Exception):
    """Raised when the backpressure maximum retry count is exceeded."""

    pass


class BackpressureRetryRequired(Exception):
    """Raised when a backpressure retry is required (non-Celery environment)."""

    pass


# =============================================================================
# Default feature definitions (for Celery Tasks)
# =============================================================================

# Predefined features available to tasks
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
