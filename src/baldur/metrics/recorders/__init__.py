"""
Domain-specific metric recorders for the baldur system.

Each recorder owns its metric definitions (via get_or_create_*) and
recording methods for a single domain. This replaces the monolithic
BaldurMetrics class with focused, testable units.
"""

from baldur.metrics.recorders.auto_tuning import AutoTuningMetricRecorder
from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.recorders.canary import CanaryMetricRecorder
from baldur.metrics.recorders.circuit_breaker import CBMetricRecorder
from baldur.metrics.recorders.correlation_engine import (
    CorrelationEngineMetricRecorder,
)
from baldur.metrics.recorders.corruption_shield import (
    CorruptionShieldMetricRecorder,
)
from baldur.metrics.recorders.daily_report import DailyReportMetricRecorder
from baldur.metrics.recorders.dlq import DLQMetricRecorder
from baldur.metrics.recorders.emergency_mode import EmergencyModeMetricRecorder
from baldur.metrics.recorders.event_bus import EventBusMetricRecorder
from baldur.metrics.recorders.forecaster import ForecasterMetricRecorder
from baldur.metrics.recorders.governance import GovernanceMetricRecorder
from baldur.metrics.recorders.health_check import HealthCheckMetricRecorder
from baldur.metrics.recorders.hedging import HedgingMetricRecorder
from baldur.metrics.recorders.idempotency import IdempotencyMetricRecorder
from baldur.metrics.recorders.infrastructure import InfraMetricRecorder
from baldur.metrics.recorders.learning import LearningMetricRecorder
from baldur.metrics.recorders.notification import NotificationMetricRecorder
from baldur.metrics.recorders.pool_monitor import PoolMetricRecorder
from baldur.metrics.recorders.postmortem import PostmortemMetricRecorder
from baldur.metrics.recorders.recommendation import RecommendationMetricRecorder
from baldur.metrics.recorders.replay import ReplayMetricRecorder
from baldur.metrics.recorders.retry import RetryMetricRecorder
from baldur.metrics.recorders.runtime_config import RuntimeConfigMetricRecorder
from baldur.metrics.recorders.shutdown import ShutdownMetricRecorder
from baldur.metrics.recorders.system_control import SystemControlMetricRecorder
from baldur.metrics.recorders.throttle import ThrottleMetricRecorder
from baldur.metrics.recorders.watchdog import WatchdogMetricRecorder

__all__ = [
    "AutoTuningMetricRecorder",
    "BaseMetricRecorder",
    "CanaryMetricRecorder",
    "CBMetricRecorder",
    "CorrelationEngineMetricRecorder",
    "CorruptionShieldMetricRecorder",
    "DailyReportMetricRecorder",
    "DLQMetricRecorder",
    "EmergencyModeMetricRecorder",
    "EventBusMetricRecorder",
    "ForecasterMetricRecorder",
    "GovernanceMetricRecorder",
    "HealthCheckMetricRecorder",
    "HedgingMetricRecorder",
    "IdempotencyMetricRecorder",
    "InfraMetricRecorder",
    "LearningMetricRecorder",
    "NotificationMetricRecorder",
    "PoolMetricRecorder",
    "PostmortemMetricRecorder",
    "RecommendationMetricRecorder",
    "ReplayMetricRecorder",
    "RetryMetricRecorder",
    "RuntimeConfigMetricRecorder",
    "ShutdownMetricRecorder",
    "SystemControlMetricRecorder",
    "ThrottleMetricRecorder",
    "WatchdogMetricRecorder",
]
