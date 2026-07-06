"""
Metrics & Observability Integration Tests (399)

Verifies multi-component integration across the 399 metrics/observability
implementation:

A. Facade Registration Contract:
    - BaldurMetrics exposes all 8 new recorders with correct types
B. Recorders __init__.py Export Completeness:
    - recorders/__init__.py __all__ includes all 8 new recorder classes
C. EventBus Handler Registration + Emit Flow:
    - Learning + Daily Report handlers registered via register_default_handlers
    - Events flow from emit → handler → structured logging
D. Hedging Delegation End-to-End:
    - Module-level functions route through facade → recorder

Note: All tests use in-memory EventBus and real Prometheus recorders.
No external infra dependency.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from baldur.metrics.prometheus import (
    BaldurMetrics,
    get_metrics,
    reset_metrics,
)
from baldur.metrics.recorders import (
    CanaryMetricRecorder,
    CorruptionShieldMetricRecorder,
    DailyReportMetricRecorder,
    ForecasterMetricRecorder,
    HedgingMetricRecorder,
    LearningMetricRecorder,
    PoolMetricRecorder,
    RuntimeConfigMetricRecorder,
)
from baldur.services.event_bus.bus.convenience import (
    get_event_bus,
    reset_event_bus,
)
from baldur.services.event_bus.bus.default_handlers import (
    register_default_handlers,
)
from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.event_bus.bus.models import BaldurEvent

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset EventBus and Metrics singletons before/after each test."""
    reset_event_bus()
    reset_metrics()
    yield
    reset_event_bus()
    reset_metrics()


@pytest.fixture
def bus_with_handlers() -> BaldurEventBus:
    """Return EventBus with default handlers registered."""
    register_default_handlers()
    return get_event_bus()


@pytest.fixture
def metrics_facade() -> BaldurMetrics:
    """Return a fresh BaldurMetrics facade instance."""
    return get_metrics()


# =============================================================================
# A. Facade Registration Contract
# =============================================================================


class TestFacadeRegistrationContract:
    """BaldurMetrics facade exposes all 8 new recorders.

    Validates:
    - Each recorder attribute exists on the facade
    - Each recorder is an instance of the correct class
    - Facade is initialized (_initialized=True)
    """

    def test_all_new_recorders_registered_in_facade(self, metrics_facade):
        """BaldurMetrics exposes all 8 new recorders with correct types."""
        assert metrics_facade._initialized is True

        assert isinstance(metrics_facade.hedging, HedgingMetricRecorder)
        assert isinstance(metrics_facade.pool_monitor, PoolMetricRecorder)
        assert isinstance(metrics_facade.canary, CanaryMetricRecorder)
        assert isinstance(metrics_facade.runtime_config, RuntimeConfigMetricRecorder)
        assert isinstance(
            metrics_facade.corruption_shield, CorruptionShieldMetricRecorder
        )
        assert isinstance(metrics_facade.learning, LearningMetricRecorder)
        assert isinstance(metrics_facade.forecaster, ForecasterMetricRecorder)
        assert isinstance(metrics_facade.daily_report, DailyReportMetricRecorder)

    def test_new_recorders_coexist_with_existing_recorders(self, metrics_facade):
        """New recorders do not break existing recorder attributes."""
        # Existing recorders (pre-399) still present
        assert hasattr(metrics_facade, "dlq")
        assert hasattr(metrics_facade, "retry")
        assert hasattr(metrics_facade, "circuit_breaker")
        assert hasattr(metrics_facade, "replay")
        assert hasattr(metrics_facade, "infra")
        assert hasattr(metrics_facade, "throttle")

    def test_facade_singleton_returns_same_instance(self):
        """get_metrics() returns the same facade across multiple calls."""
        m1 = get_metrics()
        m2 = get_metrics()
        assert m1 is m2

    def test_facade_reset_creates_new_instance(self):
        """reset_metrics() causes get_metrics() to return a new instance."""
        m1 = get_metrics()
        reset_metrics()
        m2 = get_metrics()
        assert m1 is not m2


# =============================================================================
# B. Recorders __init__.py Export Completeness
# =============================================================================


class TestRecordersExportContract:
    """recorders/__init__.py exports all 8 new recorder classes."""

    _EXPECTED_NEW_RECORDERS = [
        "HedgingMetricRecorder",
        "PoolMetricRecorder",
        "CanaryMetricRecorder",
        "RuntimeConfigMetricRecorder",
        "CorruptionShieldMetricRecorder",
        "LearningMetricRecorder",
        "ForecasterMetricRecorder",
        "DailyReportMetricRecorder",
    ]

    def test_all_new_recorders_in_recorders_init_all(self):
        """recorders/__init__.py __all__ includes all 8 new recorders."""
        import baldur.metrics.recorders as recorders_pkg

        for name in self._EXPECTED_NEW_RECORDERS:
            assert name in recorders_pkg.__all__, (
                f"{name} missing from recorders/__init__.py __all__"
            )

    def test_all_new_recorders_importable_from_package(self):
        """All 8 new recorder classes are importable from the recorders package."""
        import baldur.metrics.recorders as recorders_pkg

        for name in self._EXPECTED_NEW_RECORDERS:
            cls = getattr(recorders_pkg, name, None)
            assert cls is not None, f"{name} not importable from recorders package"
            assert callable(cls), f"{name} is not callable (not a class)"

    def test_base_recorder_also_exported(self):
        """BaseMetricRecorder is exported for subclassing."""
        import baldur.metrics.recorders as recorders_pkg

        assert "BaseMetricRecorder" in recorders_pkg.__all__


# =============================================================================
# C. EventBus Handler Registration + Emit Flow
# =============================================================================


class TestLearningEventHandlerIntegration:
    """Learning event handlers: registration + emit → handler → logging.

    Validates:
    - register_default_handlers subscribes learning handlers
    - Emit flows through to handler-level structured logging
    """

    def test_learning_parameter_blacklisted_event_reaches_handler(
        self, bus_with_handlers
    ):
        """Emit LEARNING_PARAMETER_BLACKLISTED -> handler called, structured log emitted."""
        # Given: handlers registered (via fixture)
        bus = bus_with_handlers

        # When: emit event
        with patch(
            "baldur.services.event_bus.bus._learning_handlers.logger"
        ) as mock_logger:
            handlers_called = bus.emit(
                event_type=EventType.LEARNING_PARAMETER_BLACKLISTED,
                data={
                    "pattern_key": "retry_delay",
                    "blocked_values": [0.001],
                    "reason": "too_aggressive",
                },
                source="learning_service",
            )

        # Then: handler was invoked
        assert handlers_called >= 1
        mock_logger.info.assert_called_once_with(
            "learning.parameter_blacklisted",
            pattern_key="retry_delay",
            blocked_values=[0.001],
            reason="too_aggressive",
        )

    def test_learning_pattern_detected_event_reaches_handler(self, bus_with_handlers):
        """Emit LEARNING_PATTERN_DETECTED -> handler called, structured log emitted."""
        bus = bus_with_handlers

        with patch(
            "baldur.services.event_bus.bus._learning_handlers.logger"
        ) as mock_logger:
            handlers_called = bus.emit(
                event_type=EventType.LEARNING_PATTERN_DETECTED,
                data={
                    "rule_name": "circuit_breaker_cascade",
                    "pattern_type": "correlation",
                },
                source="learning_service",
            )

        assert handlers_called >= 1
        mock_logger.info.assert_called_once_with(
            "learning.pattern_detected",
            rule_name="circuit_breaker_cascade",
            pattern_type="correlation",
        )

    def test_learning_manual_only_activated_event_reaches_handler(
        self, bus_with_handlers
    ):
        """Emit LEARNING_MANUAL_ONLY_ACTIVATED -> handler called, structured log emitted."""
        bus = bus_with_handlers

        with patch(
            "baldur.services.event_bus.bus._learning_handlers.logger"
        ) as mock_logger:
            handlers_called = bus.emit(
                event_type=EventType.LEARNING_MANUAL_ONLY_ACTIVATED,
                data={"module": "auto_tuning"},
                source="learning_service",
            )

        assert handlers_called >= 1
        mock_logger.info.assert_called_once_with(
            "learning.manual_only_activated",
            module="auto_tuning",
        )

    def test_learning_manual_only_deactivated_event_reaches_handler(
        self, bus_with_handlers
    ):
        """Emit LEARNING_MANUAL_ONLY_DEACTIVATED -> handler called, structured log emitted."""
        bus = bus_with_handlers

        with patch(
            "baldur.services.event_bus.bus._learning_handlers.logger"
        ) as mock_logger:
            handlers_called = bus.emit(
                event_type=EventType.LEARNING_MANUAL_ONLY_DEACTIVATED,
                data={"module": "auto_tuning"},
                source="learning_service",
            )

        assert handlers_called >= 1
        mock_logger.info.assert_called_once_with(
            "learning.manual_only_deactivated",
            module="auto_tuning",
        )

    def test_learning_handlers_registered_by_default(self, bus_with_handlers):
        """register_default_handlers registers all 4 learning event handlers."""
        bus = bus_with_handlers

        learning_event_types = [
            EventType.LEARNING_PARAMETER_BLACKLISTED,
            EventType.LEARNING_PATTERN_DETECTED,
            EventType.LEARNING_MANUAL_ONLY_ACTIVATED,
            EventType.LEARNING_MANUAL_ONLY_DEACTIVATED,
        ]

        for event_type in learning_event_types:
            subs = bus.get_subscriptions(event_type)
            assert len(subs) >= 1, f"No handler registered for {event_type.value}"


class TestDailyReportEventHandlerIntegration:
    """Daily Report event handler: registration + emit -> handler -> logging."""

    def test_daily_report_send_failed_event_reaches_handler(self, bus_with_handlers):
        """Emit DAILY_REPORT_SEND_FAILED -> handler called, WARNING log emitted."""
        bus = bus_with_handlers

        with patch(
            "baldur.services.event_bus.bus._daily_report_handlers.logger"
        ) as mock_logger:
            handlers_called = bus.emit(
                event_type=EventType.DAILY_REPORT_SEND_FAILED,
                data={
                    "channel": "slack",
                    "error": "webhook_timeout",
                    "date": "2026-03-28",
                },
                source="daily_report_service",
            )

        assert handlers_called >= 1
        mock_logger.warning.assert_called_once_with(
            "daily_report.send_failed",
            channel="slack",
            error="webhook_timeout",
            date="2026-03-28",
        )

    def test_daily_report_handler_registered_by_default(self, bus_with_handlers):
        """register_default_handlers registers DAILY_REPORT_SEND_FAILED handler."""
        bus = bus_with_handlers
        subs = bus.get_subscriptions(EventType.DAILY_REPORT_SEND_FAILED)
        assert len(subs) >= 1

        handler_names = [s["handler_name"] for s in subs]
        assert "_on_daily_report_send_failed" in handler_names

    def test_daily_report_handler_does_not_record_delivery_metric(
        self, bus_with_handlers, metrics_facade
    ):
        """The EventBus handler only logs — it must NOT record the delivery
        metric. DailyReportService records record_delivery(channel, False)
        before emitting the event, so recording again here would double-count.
        """
        bus = bus_with_handlers
        recorder = metrics_facade.daily_report

        with patch.object(
            recorder, "record_delivery", wraps=recorder.record_delivery
        ) as spy:
            handlers_called = bus.emit(
                event_type=EventType.DAILY_REPORT_SEND_FAILED,
                data={
                    "channel": "email",
                    "error": "smtp_refused",
                    "date": "2026-03-28",
                },
                source="daily_report_service",
            )

        assert handlers_called >= 1
        spy.assert_not_called()


class TestEventHandlerIsolationBehavior:
    """Handler failures do not break other handlers on the same event type."""

    def test_handler_exception_does_not_block_other_handlers(self, bus_with_handlers):
        """If one handler raises, other handlers for the same event still execute."""
        bus = bus_with_handlers

        # Given: add a failing handler BEFORE the normal handler
        call_log = []

        def failing_handler(event: BaldurEvent) -> None:
            raise RuntimeError("Intentional test failure")

        def tracking_handler(event: BaldurEvent) -> None:
            call_log.append(event.event_type)

        bus.subscribe(
            EventType.LEARNING_PATTERN_DETECTED,
            failing_handler,
        )
        bus.subscribe(
            EventType.LEARNING_PATTERN_DETECTED,
            tracking_handler,
        )

        # When: emit event
        handlers_called = bus.emit(
            event_type=EventType.LEARNING_PATTERN_DETECTED,
            data={"rule_name": "test", "pattern_type": "test"},
            source="test",
        )

        # Then: tracking handler still called despite failing handler
        assert EventType.LEARNING_PATTERN_DETECTED in call_log
        # At least the default handler + tracking_handler should be called
        # (failing_handler counts but throws)
        assert handlers_called >= 2


# =============================================================================
# D. Hedging Delegation End-to-End
# =============================================================================


# =============================================================================
# E. Cross-Component Wiring: Event -> Handler -> Recorder -> Metric
# =============================================================================


class TestLearningEventToMetricBehavior:
    """Full path: event emit -> handler -> recorder method invocation."""

    def test_learning_blacklisted_handler_full_path(
        self, bus_with_handlers, metrics_facade
    ):
        """Full path: LEARNING_PARAMETER_BLACKLISTED emit -> handler -> recorder.record_blacklisted."""
        bus = bus_with_handlers
        recorder = metrics_facade.learning

        assert hasattr(recorder, "record_blacklisted")

        with patch.object(
            recorder, "record_blacklisted", wraps=recorder.record_blacklisted
        ) as spy:
            handlers_called = bus.emit(
                event_type=EventType.LEARNING_PARAMETER_BLACKLISTED,
                data={
                    "module": "retry",
                    "blocked_values": [0.001],
                    "reason": "too_aggressive",
                },
                source="learning_service",
            )

        assert handlers_called >= 1
        spy.assert_called_once_with(module="retry", reason="too_aggressive")

    def test_daily_report_service_records_failed_delivery_metric(self, metrics_facade):
        """Full path: a failed channel send in DailyReportService records
        record_delivery(channel, False). The DAILY_REPORT_SEND_FAILED event it
        then emits is for logging only — the metric is recorded here, in the
        service, before the emit.
        """
        from baldur.services.daily_report import DailyReportService

        recorder = metrics_facade.daily_report

        # MagicMock report: empty entries but truthy snapshot summaries keep it
        # past the "nothing to report" skip gate without touching the collector
        # singleton (which would leak into other tests).
        report = MagicMock()
        report.entries = []

        svc = DailyReportService()
        with (
            patch(
                "baldur.services.daily_report.service.aggregate_daily_results",
                return_value=report,
            ),
            patch.object(svc, "_collect_snapshots"),
            patch.object(svc, "_send_to_channel", side_effect=Exception("timeout")),
            patch.object(svc, "_persist_report"),
            patch.object(
                recorder, "record_delivery", wraps=recorder.record_delivery
            ) as spy,
        ):
            svc.generate_and_send_report(date=datetime.now(UTC), channels=["slack"])

        # Service calls record_delivery positionally: (channel, False)
        spy.assert_called_once_with("slack", False)
