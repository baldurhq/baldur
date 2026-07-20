"""
Graceful Degradation - staged feature reduction.

Disables non-essential features one step at a time under overload.
Features are enabled/disabled automatically according to the backpressure
level.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum

import structlog

from baldur.scaling.config import (
    BackpressureLevel,
    BackpressureSettings,
    get_backpressure_settings,
)

logger = structlog.get_logger()


class FeaturePriority(IntEnum):
    """
    Feature priority.

    Lower value = higher priority (excluded from disabling).
    """

    CRITICAL = 0  # always kept (core functionality)
    HIGH = 1  # high (DLQ processing, etc.)
    MEDIUM = 2  # medium (notifications)
    LOW = 3  # low (logging, statistics)
    OPTIONAL = 4  # optional (debugging, tracing)


@dataclass
class Feature:
    """Feature definition."""

    name: str
    """Feature name (unique identifier)."""

    priority: FeaturePriority
    """Priority."""

    enabled: bool = True
    """Current enabled state."""

    on_disable: Callable[[], None] | None = None
    """Callback invoked on disable."""

    on_enable: Callable[[], None] | None = None
    """Callback invoked on enable."""


class GracefulDegradation:
    """
    Graceful Degradation Manager.

    Enables/disables features according to the backpressure level.

    Behavior per level:
    - NONE: all features enabled
    - LOW: OPTIONAL disabled
    - MEDIUM: LOW and below disabled
    - HIGH: MEDIUM and below disabled
    - CRITICAL: only CRITICAL kept

    Usage:
        degradation = GracefulDegradation()

        # Register a feature
        degradation.register_feature(Feature(
            name="detailed_logging",
            priority=FeaturePriority.OPTIONAL,
        ))

        # Update the level
        degradation.update_level(BackpressureLevel.HIGH)

        # Check whether a feature is usable
        if degradation.is_enabled("detailed_logging"):
            log_details()
    """

    # Per-level enablement priority threshold.
    # At a given level, anything at or below this priority (larger value) is
    # disabled.
    LEVEL_THRESHOLDS: dict[BackpressureLevel, FeaturePriority] = {
        BackpressureLevel.NONE: FeaturePriority.OPTIONAL,  # keep everything
        BackpressureLevel.LOW: FeaturePriority.LOW,  # disable OPTIONAL
        BackpressureLevel.MEDIUM: FeaturePriority.MEDIUM,  # disable LOW and below
        BackpressureLevel.HIGH: FeaturePriority.HIGH,  # disable MEDIUM and below
        BackpressureLevel.CRITICAL: FeaturePriority.CRITICAL,  # keep CRITICAL only
    }

    def __init__(
        self,
        settings: BackpressureSettings | None = None,
    ):
        """
        Args:
            settings: Backpressure settings
        """
        self._settings = settings or get_backpressure_settings()
        self._features: dict[str, Feature] = {}
        self._current_level = BackpressureLevel.NONE

    def register_feature(self, feature: Feature) -> None:
        """
        Register a feature.

        Args:
            feature: Feature to register
        """
        self._features[feature.name] = feature
        logger.debug(
            "graceful_degradation.feature_registered",
            feature=feature.name,
        )

    def unregister_feature(self, name: str) -> None:
        """
        Unregister a feature.

        Args:
            name: Feature name
        """
        if name in self._features:
            del self._features[name]

    def is_enabled(self, name: str) -> bool:
        """
        Check whether a feature is enabled.

        Args:
            name: Feature name

        Returns:
            Whether the feature is enabled (unregistered features return True)
        """
        if not self._settings.graceful_degradation_enabled:
            return True

        feature = self._features.get(name)
        if feature is None:
            return True

        return feature.enabled

    def update_level(self, level: BackpressureLevel) -> None:
        """
        Update the backpressure level.

        Enables/disables features according to the level.

        Args:
            level: New backpressure level
        """
        if not self._settings.graceful_degradation_enabled:
            return

        if level == self._current_level:
            return

        old_level = self._current_level
        self._current_level = level

        threshold = self.LEVEL_THRESHOLDS.get(level, FeaturePriority.OPTIONAL)

        for feature in self._features.values():
            # Enable when the priority value is at or below the threshold
            # (smaller value = higher priority)
            should_enable = feature.priority.value <= threshold.value

            if should_enable and not feature.enabled:
                feature.enabled = True
                if feature.on_enable:
                    try:
                        feature.on_enable()
                    except Exception as e:
                        logger.exception(
                            "graceful_degradation.error",
                            error=e,
                        )
                logger.info(
                    "graceful_degradation.enabled",
                    feature=feature.name,
                )

            elif not should_enable and feature.enabled:
                feature.enabled = False
                if feature.on_disable:
                    try:
                        feature.on_disable()
                    except Exception as e:
                        logger.exception(
                            "graceful_degradation.error",
                            error=e,
                        )
                logger.info(
                    "graceful_degradation.disabled",
                    feature=feature.name,
                )

        logger.info(
            "graceful_degradation.level_changed",
            old_level=old_level.value,
            degradation_level=level.value,
        )

    def get_enabled_features(self) -> list[str]:
        """
        Return the list of enabled features.

        Returns:
            Names of the enabled features
        """
        return [name for name, feature in self._features.items() if feature.enabled]

    def get_disabled_features(self) -> list[str]:
        """
        Return the list of disabled features.

        Returns:
            Names of the disabled features
        """
        return [name for name, feature in self._features.items() if not feature.enabled]

    def get_current_level(self) -> BackpressureLevel:
        """Return the current level."""
        return self._current_level


# =============================================================================
# Singleton
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

get_graceful_degradation, configure_graceful_degradation, reset_graceful_degradation = (
    make_singleton_factory("graceful_degradation", GracefulDegradation)
)
