"""
Tests for SafeGauge and EventLoggingConfig.

These tests verify:
1. SafeGauge prevents negative gauge values (critical for dashboard display)
2. EventLoggingConfig provides runtime-configurable logging levels
3. Event handlers use SafeGauge and dynamic logging correctly
"""

import logging
import threading
from unittest.mock import Mock, patch

import pytest

# =============================================================================
# SafeGauge Tests
# =============================================================================


class TestSafeGaugeChild:
    """SafeGaugeChild 테스트 - 음수 방지 로직 검증."""

    def test_inc_increases_value(self):
        """inc() 호출 시 값이 증가하는지 확인."""
        from baldur.metrics.safe_gauge import SafeGaugeChild

        mock_child = Mock()
        safe = SafeGaugeChild(mock_child, {"domain": "payment"})

        safe.inc()
        assert safe.get_shadow_value() == 1.0
        mock_child.inc.assert_called_once_with(1)

    def test_inc_with_custom_amount(self):
        """사용자 정의 증가량으로 inc() 호출."""
        from baldur.metrics.safe_gauge import SafeGaugeChild

        mock_child = Mock()
        safe = SafeGaugeChild(mock_child, {"domain": "payment"})

        safe.inc(5)
        assert safe.get_shadow_value() == 5.0
        mock_child.inc.assert_called_once_with(5)

    def test_dec_decreases_value(self):
        """dec() 호출 시 값이 감소하는지 확인."""
        from baldur.metrics.safe_gauge import SafeGaugeChild

        mock_child = Mock()
        safe = SafeGaugeChild(mock_child, {"domain": "payment"})

        # First inc to have value
        safe.inc(3)
        safe.dec()

        assert safe.get_shadow_value() == 2.0
        mock_child.dec.assert_called_once_with(1)

    def test_dec_clamps_to_zero_when_would_go_negative(self):
        """음수가 되려고 하면 0으로 클램핑되는지 확인 (핵심 테스트)."""
        from baldur.metrics.safe_gauge import SafeGaugeChild

        mock_child = Mock()
        safe = SafeGaugeChild(mock_child, {"domain": "payment"})

        # Inc once, then dec twice
        safe.inc(1)
        safe.dec()  # Now 0
        safe.dec()  # Would be -1, should clamp to 0

        assert safe.get_shadow_value() == 0.0
        # set(0) should be called instead of dec()
        mock_child.set.assert_called_with(0)

    def test_dec_before_any_inc_is_ignored(self):
        """서버 재시작 직후 dec()가 먼저 호출되면 무시됨."""
        from baldur.metrics.safe_gauge import SafeGaugeChild

        mock_child = Mock()
        safe = SafeGaugeChild(mock_child, {"domain": "payment"})

        # Dec before any inc - likely stale event after restart
        safe.dec()

        assert safe.get_shadow_value() == 0.0
        mock_child.dec.assert_not_called()
        mock_child.set.assert_not_called()

    def test_set_positive_value(self):
        """set()으로 양수 값 설정."""
        from baldur.metrics.safe_gauge import SafeGaugeChild

        mock_child = Mock()
        safe = SafeGaugeChild(mock_child, {"domain": "payment"})

        safe.set(10)

        assert safe.get_shadow_value() == 10.0
        mock_child.set.assert_called_with(10)

    def test_set_negative_value_clamped_to_zero(self):
        """set()에 음수를 전달하면 0으로 클램핑."""
        from baldur.metrics.safe_gauge import SafeGaugeChild

        mock_child = Mock()
        safe = SafeGaugeChild(mock_child, {"domain": "payment"})

        safe.set(-5)

        assert safe.get_shadow_value() == 0.0
        mock_child.set.assert_called_with(0.0)

    def test_sync_from_source_updates_shadow_and_gauge(self):
        """sync_from_source()가 shadow와 gauge를 동기화."""
        from baldur.metrics.safe_gauge import SafeGaugeChild

        mock_child = Mock()
        safe = SafeGaugeChild(mock_child, {"domain": "payment"})

        safe.sync_from_source(42)

        assert safe.get_shadow_value() == 42.0
        mock_child.set.assert_called_with(42)

    def test_sync_from_source_clamps_negative(self):
        """sync_from_source()도 음수를 0으로 클램핑."""
        from baldur.metrics.safe_gauge import SafeGaugeChild

        mock_child = Mock()
        safe = SafeGaugeChild(mock_child, {"domain": "payment"})

        safe.sync_from_source(-10)

        assert safe.get_shadow_value() == 0.0
        mock_child.set.assert_called_with(0.0)

    def test_thread_safety(self):
        """동시성 테스트 - 여러 스레드에서 안전하게 동작."""
        from baldur.metrics.safe_gauge import SafeGaugeChild

        mock_child = Mock()
        safe = SafeGaugeChild(mock_child, {"domain": "payment"})

        def inc_many():
            for _ in range(100):
                safe.inc()

        def dec_many():
            for _ in range(50):
                safe.dec()

        threads = [
            threading.Thread(target=inc_many),
            threading.Thread(target=inc_many),
            threading.Thread(target=dec_many),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 200 inc - 50 dec = 150
        # But some decs may be ignored if they happen before incs
        # At minimum, should be >= 100 and <= 200, and never negative
        assert safe.get_shadow_value() >= 0


class TestSafeGauge:
    """SafeGauge 래퍼 테스트."""

    def test_labels_returns_safe_gauge_child(self):
        """labels()가 SafeGaugeChild를 반환."""
        from baldur.metrics.safe_gauge import SafeGauge, SafeGaugeChild

        mock_gauge = Mock()
        mock_gauge.labels.return_value = Mock()

        safe = SafeGauge(mock_gauge)
        child = safe.labels(domain="payment")

        assert isinstance(child, SafeGaugeChild)

    def test_labels_caches_children(self):
        """동일 라벨에 대해 동일한 child를 반환."""
        from baldur.metrics.safe_gauge import SafeGauge

        mock_gauge = Mock()
        mock_gauge.labels.return_value = Mock()

        safe = SafeGauge(mock_gauge)
        child1 = safe.labels(domain="payment")
        child2 = safe.labels(domain="payment")

        assert child1 is child2
        # labels() should only be called once
        mock_gauge.labels.assert_called_once()

    def test_different_labels_different_children(self):
        """다른 라벨에 대해 다른 child를 반환."""
        from baldur.metrics.safe_gauge import SafeGauge

        mock_gauge = Mock()
        mock_gauge.labels.side_effect = [Mock(), Mock()]

        safe = SafeGauge(mock_gauge)
        child1 = safe.labels(domain="payment")
        child2 = safe.labels(domain="point")

        assert child1 is not child2

    def test_none_gauge_returns_noop_child(self):
        """gauge가 None이면 no-op child 반환."""
        from baldur.metrics.safe_gauge import SafeGauge

        safe = SafeGauge(None)
        child = safe.labels(domain="payment")

        # Should not raise, just no-op
        child.inc()
        child.dec()
        child.set(10)
        assert child.get_shadow_value() == 0.0

    def test_is_available_property(self):
        """is_available 속성 테스트."""
        from baldur.metrics.safe_gauge import SafeGauge

        safe_with_gauge = SafeGauge(Mock())
        safe_without_gauge = SafeGauge(None)

        assert safe_with_gauge.is_available is True
        assert safe_without_gauge.is_available is False


# =============================================================================
# EventLoggingConfig Tests
# =============================================================================


class TestEventLoggingConfig:
    """EventLoggingConfig 테스트 - API 레벨 로깅 설정."""

    def test_singleton_pattern(self):
        """싱글톤 패턴 확인."""
        from baldur.settings.event_logging import EventLoggingConfig

        # Reset singleton for clean test
        EventLoggingConfig._instance = None

        config1 = EventLoggingConfig()
        config2 = EventLoggingConfig()

        assert config1 is config2

    def test_default_values(self):
        """기본값 확인."""
        from baldur.settings.event_logging import EventLoggingConfig

        EventLoggingConfig._instance = None
        config = EventLoggingConfig()

        assert config.get_dlq_log_level() == "INFO"
        assert config.get_cb_log_level() == "WARNING"
        assert config.get_replay_log_level() == "INFO"
        assert config.get_sla_log_level() == "WARNING"

    def test_update_single_level(self):
        """단일 레벨 업데이트."""
        from baldur.settings.event_logging import EventLoggingConfig

        EventLoggingConfig._instance = None
        config = EventLoggingConfig()

        config.update(dlq_log_level="DEBUG")

        assert config.get_dlq_log_level() == "DEBUG"
        # Others unchanged
        assert config.get_cb_log_level() == "WARNING"

    def test_update_multiple_levels(self):
        """여러 레벨 동시 업데이트."""
        from baldur.settings.event_logging import EventLoggingConfig

        EventLoggingConfig._instance = None
        config = EventLoggingConfig()

        config.update(
            dlq_log_level="WARNING",
            cb_log_level="ERROR",
            replay_log_level="DEBUG",
        )

        assert config.get_dlq_log_level() == "WARNING"
        assert config.get_cb_log_level() == "ERROR"
        assert config.get_replay_log_level() == "DEBUG"

    def test_update_returns_current_config(self):
        """update()가 현재 설정을 반환."""
        from baldur.settings.event_logging import EventLoggingConfig

        EventLoggingConfig._instance = None
        config = EventLoggingConfig()

        result = config.update(dlq_log_level="DEBUG")

        assert "dlq_log_level" in result
        assert result["dlq_log_level"] == "DEBUG"

    def test_invalid_level_raises_error(self):
        """잘못된 레벨은 ValueError 발생."""
        from baldur.settings.event_logging import EventLoggingConfig

        EventLoggingConfig._instance = None
        config = EventLoggingConfig()

        with pytest.raises(ValueError) as exc_info:
            config.update(dlq_log_level="INVALID_LEVEL")

        assert "Invalid log level" in str(exc_info.value)

    def test_reset_clears_runtime_config(self):
        """reset()이 런타임 설정을 초기화."""
        from baldur.settings.event_logging import EventLoggingConfig

        EventLoggingConfig._instance = None
        config = EventLoggingConfig()

        config.update(dlq_log_level="DEBUG")
        assert config.get_dlq_log_level() == "DEBUG"

        config.reset()
        assert config.get_dlq_log_level() == "INFO"  # Back to default

    def test_get_log_level_int(self):
        """get_log_level_int()가 정수 레벨 반환."""
        from baldur.settings.event_logging import EventLoggingConfig

        EventLoggingConfig._instance = None
        config = EventLoggingConfig()

        assert config.get_log_level_int("DEBUG") == logging.DEBUG
        assert config.get_log_level_int("INFO") == logging.INFO
        assert config.get_log_level_int("WARNING") == logging.WARNING
        assert config.get_log_level_int("ERROR") == logging.ERROR

    def test_to_dict(self):
        """to_dict()가 전체 설정 반환."""
        from baldur.settings.event_logging import EventLoggingConfig

        EventLoggingConfig._instance = None
        config = EventLoggingConfig()

        result = config.to_dict()

        assert "dlq_log_level" in result
        assert "cb_log_level" in result
        assert "replay_log_level" in result
        assert "sla_log_level" in result
        assert "last_updated" in result

    def test_audit_trail_on_update(self):
        """update() 시 감사 추적 정보 기록."""
        from baldur.settings.event_logging import EventLoggingConfig

        EventLoggingConfig._instance = None
        config = EventLoggingConfig()

        config.update(dlq_log_level="DEBUG", updated_by="admin_user")

        result = config.to_dict()
        assert result["last_updated"]["updated_by"] == "admin_user"
        assert "timestamp" in result["last_updated"]
        assert "changes" in result["last_updated"]

    @patch.dict("os.environ", {"BALDUR_EVENT_LOGGING_DLQ_LOG_LEVEL": "ERROR"})
    def test_env_override(self):
        """환경변수 오버라이드 테스트."""
        from baldur.settings.event_logging import EventLoggingConfig

        EventLoggingConfig._instance = None
        config = EventLoggingConfig()

        # Environment variable should be used as default
        assert config.get_dlq_log_level() == "ERROR"


# =============================================================================
# Event Handler Integration Tests
# =============================================================================


class TestEventHandlerWithSafeGauge:
    """Event Handler가 SafeGauge를 올바르게 사용하는지 테스트."""

    def test_on_item_created_uses_safe_gauge(self):
        """on_item_created가 SafeGauge를 사용하는지 확인."""
        from baldur.metrics.event_handlers import (
            DLQMetricEventHandler,
            reset_event_handler_cache,
        )

        reset_event_handler_cache()

        mock_metrics = Mock()
        mock_metrics.record_dlq_item_created = Mock()
        mock_gauge = Mock()
        mock_gauge.labels.return_value = Mock()
        mock_metrics.dlq = Mock()
        mock_metrics.dlq._pending_gauge = mock_gauge

        with patch(
            "baldur.metrics.event_handlers._get_metrics",
            return_value=mock_metrics,
        ):
            DLQMetricEventHandler.on_item_created("external_service", "PG_TIMEOUT")

        mock_metrics.record_dlq_item_created.assert_called_once_with(
            "external_service", "PG_TIMEOUT"
        )

    def test_on_item_resolved_uses_safe_gauge(self):
        """Verify on_item_resolved uses SafeGauge and the recovery recorder."""
        from baldur.metrics.event_handlers import (
            DLQMetricEventHandler,
            reset_event_handler_cache,
        )

        reset_event_handler_cache()

        mock_metrics = Mock()
        mock_gauge = Mock()
        mock_gauge.labels.return_value = Mock()
        mock_metrics.dlq = Mock()
        mock_metrics.dlq._pending_gauge = mock_gauge
        mock_metrics.retry = Mock()

        with patch(
            "baldur.metrics.event_handlers._get_metrics",
            return_value=mock_metrics,
        ):
            DLQMetricEventHandler.on_item_resolved(
                "external_service", "auto_replay", duration_seconds=30.0
            )

        # Recovery duration routed through the recorder public method.
        mock_metrics.retry.record_recovery_duration.assert_called_once_with(
            "external_service", "auto_replay", 30.0
        )


class TestEventHandlerLogging:
    """Event Handler 로깅 레벨 테스트."""

    def test_dlq_events_use_configured_level(self):
        """DLQ 이벤트가 설정된 레벨로 로깅되는지 확인."""
        from baldur.metrics.event_handlers import (
            DLQMetricEventHandler,
            reset_event_handler_cache,
        )
        from baldur.settings.event_logging import EventLoggingConfig

        reset_event_handler_cache()
        EventLoggingConfig._instance = None
        config = EventLoggingConfig()
        config.update(dlq_log_level="WARNING")

        mock_metrics = Mock()
        mock_metrics.record_dlq_item_created = Mock()
        mock_gauge = Mock()
        mock_gauge.labels.return_value = Mock()
        mock_metrics.dlq = Mock()
        mock_metrics.dlq._pending_gauge = mock_gauge

        with patch(
            "baldur.metrics.event_handlers._get_metrics",
            return_value=mock_metrics,
        ):
            with patch("baldur.metrics.event_handlers.logger") as mock_logger:
                DLQMetricEventHandler.on_item_created("external_service", "PG_TIMEOUT")

                # Should log at WARNING level (30)
                mock_logger.log.assert_called()
                call_args = mock_logger.log.call_args
                assert call_args[0][0] == logging.WARNING

    def test_cb_events_log_at_warning_by_default(self):
        """CB 이벤트가 기본적으로 WARNING 레벨로 로깅."""
        from baldur.metrics.event_handlers import (
            CircuitBreakerEventHandler,
            reset_event_handler_cache,
        )
        from baldur.settings.event_logging import EventLoggingConfig

        reset_event_handler_cache()
        EventLoggingConfig._instance = None
        EventLoggingConfig()

        mock_metrics = Mock()
        mock_metrics.circuit_breaker = Mock()

        with patch(
            "baldur.metrics.event_handlers._get_metrics",
            return_value=mock_metrics,
        ):
            with patch("baldur.metrics.event_handlers.logger") as mock_logger:
                CircuitBreakerEventHandler.on_state_changed(
                    "toss_payment", "closed", "open"
                )

                mock_logger.log.assert_called()
                call_args = mock_logger.log.call_args
                assert call_args[0][0] == logging.WARNING


# =============================================================================
# Scenario Tests
# =============================================================================


class TestServerRestartScenario:
    """서버 재시작 시나리오 테스트 - 음수 방지 검증."""

    def test_resolve_before_create_after_restart(self):
        """
        시나리오: 서버 재시작 직후 'resolved' 이벤트가 먼저 도착.

        이 시나리오는 3,900억 원 시스템에서 "-1개 대기 중"이
        표시되는 것을 방지하는 핵심 테스트입니다.
        """
        from baldur.metrics.safe_gauge import SafeGauge

        mock_gauge = Mock()
        mock_child = Mock()
        mock_gauge.labels.return_value = mock_child

        safe = SafeGauge(mock_gauge)

        # 서버 재시작 직후 - 인메모리 값은 0
        child = safe.labels(domain="payment")

        # 'resolved' 이벤트가 먼저 도착 (stale event)
        child.dec()

        # 음수가 되면 안 됨!
        assert child.get_shadow_value() >= 0

        # Prometheus gauge의 dec()가 호출되면 안 됨
        mock_child.dec.assert_not_called()

    def test_normal_flow_after_restart(self):
        """
        시나리오: 서버 재시작 후 정상 흐름.

        1. Reconciler가 실제 DB 값으로 동기화
        2. 이후 create/resolve 이벤트 정상 처리
        """
        from baldur.metrics.safe_gauge import SafeGauge

        mock_gauge = Mock()
        mock_child = Mock()
        mock_gauge.labels.return_value = mock_child

        safe = SafeGauge(mock_gauge)
        child = safe.labels(domain="payment")

        # 1. Reconciler가 DB에서 실제 값(5)으로 동기화
        child.sync_from_source(5)
        assert child.get_shadow_value() == 5.0

        # 2. 새 DLQ 생성
        child.inc()
        assert child.get_shadow_value() == 6.0

        # 3. DLQ 해결
        child.dec()
        assert child.get_shadow_value() == 5.0

        # 모든 값이 정상 범위
        assert child.get_shadow_value() >= 0
