"""
Tests for BaldurEventBus.
Verifies event bus subscribe/publish/unsubscribe, history, stats, and control features.
"""

import contextvars
import threading
from unittest.mock import MagicMock, patch

import pytest

from baldur.services.event_bus import (
    BaldurEvent,
    BaldurEventBus,
    EventPriority,
    EventSubscription,
    EventType,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_event_bus():
    """각 테스트 전후로 이벤트 버스 싱글톤을 초기화."""
    bus = BaldurEventBus()
    bus.reset()
    yield
    bus.reset()


@pytest.fixture
def bus():
    """초기화된 이벤트 버스 인스턴스 반환."""
    return BaldurEventBus()


def _make_event(
    event_type=EventType.CONFIG_UPDATED,
    data=None,
    source="test",
    priority=EventPriority.NORMAL,
):
    """테스트용 이벤트 생성 헬퍼."""
    return BaldurEvent(
        event_type=event_type,
        data=data or {},
        source=source,
        priority=priority,
    )


# =============================================================================
# BaldurEvent / EventSubscription Tests
# =============================================================================


class TestBaldurEvent:
    """BaldurEvent 데이터클래스 테스트."""

    def test_to_dict(self):
        """To dict
        to_dict()가 올바른 딕셔너리를 반환하는지 확인.
        """
        event = _make_event(data={"key": "value"}, source="test_src")
        d = event.to_dict()
        assert d["event_type"] == "config_updated"
        assert d["data"] == {"key": "value"}
        assert d["source"] == "test_src"
        assert "timestamp" in d

    def test_default_priority(self):
        """Default priority
        기본 우선순위가 NORMAL인지 확인.
        """
        event = _make_event()
        assert event.priority == EventPriority.NORMAL

    def test_correlation_id(self):
        """Correlation ID
        correlation_id가 올바르게 설정되는지 확인.
        """
        event = BaldurEvent(
            event_type=EventType.CONFIG_UPDATED,
            data={},
            source="test",
            correlation_id="abc-123",
        )
        assert event.correlation_id == "abc-123"
        assert event.to_dict()["correlation_id"] == "abc-123"


class TestEventSubscription:
    """EventSubscription 데이터클래스 테스트."""

    def test_hash_uniqueness(self):
        """Hash uniqueness
        같은 핸들러명의 구독은 동일한 해시를 갖는지 확인.
        """

        def handler(e):
            return None

        handler.__name__ = "my_handler"
        sub1 = EventSubscription(
            event_type=EventType.CONFIG_UPDATED,
            handler=handler,
            handler_name="my_handler",
        )
        sub2 = EventSubscription(
            event_type=EventType.CONFIG_UPDATED,
            handler=handler,
            handler_name="my_handler",
        )
        assert hash(sub1) == hash(sub2)


# =============================================================================
# Subscribe / Unsubscribe Tests
# =============================================================================


class TestSubscription:
    """구독 관리 테스트."""

    def test_subscribe(self, bus):
        """Subscribe
        핸들러를 구독하면 구독 목록에 추가되는지 확인.
        """
        handler = MagicMock()
        sub = bus.subscribe(EventType.CONFIG_UPDATED, handler)
        assert isinstance(sub, EventSubscription)
        assert sub.event_type == EventType.CONFIG_UPDATED

    def test_duplicate_subscribe_returns_existing(self, bus):
        """Duplicate subscribe returns existing
        같은 핸들러를 중복 구독하면 기존 구독을 반환하는지 확인.
        """
        handler = MagicMock(__name__="my_handler")
        sub1 = bus.subscribe(EventType.CONFIG_UPDATED, handler)
        sub2 = bus.subscribe(EventType.CONFIG_UPDATED, handler)
        assert sub1 is sub2

    def test_unsubscribe(self, bus):
        """Unsubscribe
        구독 해제가 올바르게 동작하는지 확인.
        """
        handler = MagicMock(__name__="my_handler")
        bus.subscribe(EventType.CONFIG_UPDATED, handler)
        removed = bus.unsubscribe(EventType.CONFIG_UPDATED, handler)
        assert removed is True

    def test_unsubscribe_not_subscribed(self, bus):
        """Unsubscribe not subscribed
        구독하지 않은 핸들러를 해제하면 False를 반환하는지 확인.
        """
        handler = MagicMock(__name__="my_handler")
        removed = bus.unsubscribe(EventType.CONFIG_UPDATED, handler)
        assert removed is False

    def test_unsubscribe_all(self, bus):
        """Unsubscribe all
        모든 구독을 해제하면 구독 목록이 비어있는지 확인.
        """
        handler1 = MagicMock(__name__="h1")
        handler2 = MagicMock(__name__="h2")
        bus.subscribe(EventType.CONFIG_UPDATED, handler1)
        bus.subscribe(EventType.ERROR_BUDGET_CRITICAL, handler2)
        bus.unsubscribe_all()
        stats = bus.get_stats()
        assert stats["subscriptions_count"] == 0

    def test_unsubscribe_all_specific_type(self, bus):
        """Unsubscribe all specific type
        특정 이벤트 타입의 모든 구독만 해제되는지 확인.
        """
        handler1 = MagicMock(__name__="h1")
        handler2 = MagicMock(__name__="h2")
        bus.subscribe(EventType.CONFIG_UPDATED, handler1)
        bus.subscribe(EventType.ERROR_BUDGET_CRITICAL, handler2)
        bus.unsubscribe_all(EventType.CONFIG_UPDATED)

        subs = bus.get_subscriptions()
        assert len(subs) == 1
        assert subs[0]["event_type"] == "error_budget_critical"


# =============================================================================
# Publish Tests
# =============================================================================


class TestPublish:
    """이벤트 발행 테스트."""

    def test_publish_calls_handler(self, bus):
        """Publish calls handler
        이벤트 발행 시 등록된 핸들러가 호출되는지 확인.
        """
        handler = MagicMock(__name__="h")
        bus.subscribe(EventType.CONFIG_UPDATED, handler)
        event = _make_event()
        count = bus.publish(event)
        assert count == 1
        handler.assert_called_once_with(event)

    def test_publish_no_subscribers(self, bus):
        """Publish no subscribers
        구독자가 없으면 0을 반환하는지 확인.
        """
        event = _make_event()
        count = bus.publish(event)
        assert count == 0

    def test_publish_disabled_bus(self, bus):
        """Publish disabled bus
        비활성화된 버스에서 이벤트 발행 시 0을 반환하는지 확인.
        """
        handler = MagicMock(__name__="h")
        bus.subscribe(EventType.CONFIG_UPDATED, handler)
        bus.disable()
        event = _make_event()
        count = bus.publish(event)
        assert count == 0
        handler.assert_not_called()

    def test_handler_exception_does_not_break_others(self, bus):
        """Handler exception does not break others
        한 핸들러가 예외를 발생시켜도 나머지 핸들러는 실행되는지 확인.
        """
        bad_handler = MagicMock(__name__="bad", side_effect=RuntimeError("oops"))
        good_handler = MagicMock(__name__="good")
        bus.subscribe(
            EventType.CONFIG_UPDATED, bad_handler, priority=EventPriority.HIGH
        )
        bus.subscribe(
            EventType.CONFIG_UPDATED, good_handler, priority=EventPriority.LOW
        )
        event = _make_event()
        count = bus.publish(event)
        # bad_handler는 실행됐지만 예외 발생, good_handler도 실행됨
        assert count == 1  # good_handler만 성공

    def test_publish_priority_order(self, bus):
        """Publish priority order
        높은 우선순위의 핸들러가 먼저 호출되는지 확인.
        """
        call_order = []
        low_handler = MagicMock(
            __name__="low", side_effect=lambda e: call_order.append("low")
        )
        high_handler = MagicMock(
            __name__="high", side_effect=lambda e: call_order.append("high")
        )
        bus.subscribe(EventType.CONFIG_UPDATED, low_handler, priority=EventPriority.LOW)
        bus.subscribe(
            EventType.CONFIG_UPDATED, high_handler, priority=EventPriority.HIGH
        )
        bus.publish(_make_event())
        assert call_order == ["high", "low"]

    def test_emit_convenience(self, bus):
        """Emit convenience
        emit() 간편 메서드가 올바르게 동작하는지 확인.
        """
        handler = MagicMock(__name__="h")
        bus.subscribe(EventType.CONFIG_UPDATED, handler)
        count = bus.emit(
            EventType.CONFIG_UPDATED,
            data={"key": "val"},
            source="test",
        )
        assert count == 1
        handler.assert_called_once()

    def test_disabled_subscription_skipped(self, bus):
        """Disabled subscription skipped
        비활성화된 구독은 건너뛰는지 확인.
        """
        handler = MagicMock(__name__="h")
        sub = bus.subscribe(EventType.CONFIG_UPDATED, handler)
        sub.enabled = False
        count = bus.publish(_make_event())
        assert count == 0
        handler.assert_not_called()


# =============================================================================
# History Tests
# =============================================================================


class TestEventHistory:
    """이벤트 히스토리 테스트."""

    def test_history_recorded(self, bus):
        """History recorded
        발행된 이벤트가 히스토리에 기록되는지 확인.
        """
        bus.publish(_make_event())
        history = bus.get_history()
        assert len(history) == 1
        assert history[0]["event_type"] == "config_updated"

    def test_history_limit(self, bus):
        """History limit
        히스토리 조회 시 limit 파라미터가 동작하는지 확인.
        """
        for _ in range(10):
            bus.publish(_make_event())
        history = bus.get_history(limit=5)
        assert len(history) == 5

    def test_history_filter_by_type(self, bus):
        """History filter by type
        이벤트 타입으로 히스토리를 필터링할 수 있는지 확인.
        """
        bus.publish(_make_event(event_type=EventType.CONFIG_UPDATED))
        bus.publish(_make_event(event_type=EventType.ERROR_BUDGET_CRITICAL))

        history = bus.get_history(event_type=EventType.CONFIG_UPDATED)
        assert len(history) == 1
        assert history[0]["event_type"] == "config_updated"

    def test_clear_history(self, bus):
        """Clear history
        히스토리 초기화가 올바르게 동작하는지 확인.
        """
        bus.publish(_make_event())
        bus.clear_history()
        assert len(bus.get_history()) == 0

    def test_max_history_cap(self, bus):
        """Max history cap
        히스토리가 max_history를 초과하지 않는지 확인.
        """
        from collections import deque

        bus._max_history = 5
        bus._event_history = deque(bus._event_history, maxlen=5)
        for _ in range(10):
            bus.publish(_make_event())
        history = bus.get_history(limit=100)
        assert len(history) <= 5


# =============================================================================
# Control Tests
# =============================================================================


class TestEventBusControl:
    """이벤트 버스 제어 테스트."""

    def test_enable_disable(self, bus):
        """Enable disable
        enable/disable이 is_enabled에 반영되는지 확인.
        """
        assert bus.is_enabled() is True
        bus.disable()
        assert bus.is_enabled() is False
        bus.enable()
        assert bus.is_enabled() is True


# =============================================================================
# Statistics Tests
# =============================================================================


class TestEventBusStats:
    """이벤트 버스 통계 테스트."""

    def test_stats_structure(self, bus):
        """Stats structure
        get_stats()가 올바른 키를 포함하는지 확인.
        """
        stats = bus.get_stats()
        assert "enabled" in stats
        assert "subscriptions_count" in stats
        assert "history_count" in stats
        assert "max_history" in stats

    def test_stats_after_subscribe(self, bus):
        """Stats after subscribe
        구독 후 subscriptions_count가 증가하는지 확인.
        """
        handler = MagicMock(__name__="h")
        bus.subscribe(EventType.CONFIG_UPDATED, handler)
        stats = bus.get_stats()
        assert stats["subscriptions_count"] == 1
        assert stats["event_types_with_subscribers"] == 1

    def test_get_subscriptions_list(self, bus):
        """Get subscriptions list
        get_subscriptions()가 올바른 형식의 리스트를 반환하는지 확인.
        """
        handler = MagicMock(__name__="my_handler")
        bus.subscribe(EventType.CONFIG_UPDATED, handler, priority=EventPriority.HIGH)
        subs = bus.get_subscriptions(event_type=EventType.CONFIG_UPDATED)
        assert len(subs) == 1
        assert subs[0]["handler_name"] == "my_handler"
        assert subs[0]["priority"] == "HIGH"


# =============================================================================
# Singleton Tests
# =============================================================================


class TestEventBusSingleton:
    """BaldurEventBus 싱글톤 동작 테스트."""

    def test_singleton_identity(self):
        """Singleton identity
        get_event_bus()가 동일한 객체를 반환하는지 확인.
        """
        from baldur.services.event_bus import get_event_bus

        bus1 = get_event_bus()
        bus2 = get_event_bus()
        assert bus1 is bus2

    def test_reset_clears_state(self, bus):
        """Reset clears state
        reset()이 모든 상태를 초기화하는지 확인.
        """
        handler = MagicMock(__name__="h")
        bus.subscribe(EventType.CONFIG_UPDATED, handler)
        bus.publish(_make_event())
        bus.disable()

        bus.reset()

        assert bus.is_enabled() is True
        assert bus.get_stats()["subscriptions_count"] == 0
        assert bus.get_stats()["history_count"] == 0


# =============================================================================
# EventType / EventPriority Enum Tests
# =============================================================================


class TestEnums:
    """EventType, EventPriority enum 테스트."""

    def test_event_type_values(self):
        """Event type values
        주요 EventType 값들이 존재하는지 확인.
        """
        assert EventType.EMERGENCY_LEVEL_CHANGED.value == "emergency_level_changed"
        assert EventType.CIRCUIT_BREAKER_OPENED.value == "circuit_breaker_opened"
        assert EventType.CONFIG_UPDATED.value == "config_updated"
        assert EventType.ERROR_BUDGET_CRITICAL.value == "error_budget_critical"

    def test_event_priority_ordering(self):
        """Event priority ordering
        EventPriority 값이 순서대로 정렬되는지 확인.
        """
        assert EventPriority.LOW.value < EventPriority.NORMAL.value
        assert EventPriority.NORMAL.value < EventPriority.HIGH.value
        assert EventPriority.HIGH.value < EventPriority.CRITICAL.value


# =============================================================================
# Handler Timeout Guard — Contract (doc 438)
# =============================================================================


class TestHandlerTimeoutContract:
    """Handler timeout guard design contract values (doc 438)."""

    def test_fallback_handler_timeout_constant(self):
        """_FALLBACK_HANDLER_TIMEOUT is 5.0 (doc 438)."""
        from baldur.services.event_bus.bus.event_bus import _FALLBACK_HANDLER_TIMEOUT

        assert _FALLBACK_HANDLER_TIMEOUT == 5.0

    def test_init_sets_handler_timeout(self):
        """BaldurEventBus.__init__ initializes _handler_timeout attribute."""
        bus = BaldurEventBus()
        assert hasattr(bus, "_handler_timeout")
        assert isinstance(bus._handler_timeout, float)


# =============================================================================
# Handler Timeout Guard — Behavior (doc 438)
# =============================================================================


class TestLoadHandlerTimeoutBehavior:
    """_load_handler_timeout() settings load and fallback behavior."""

    def test_load_handler_timeout_returns_settings_value(self):
        """Returns handler_timeout_seconds from EventBusSettings."""
        with patch(
            "baldur.settings.event_bus.get_event_bus_settings",
        ) as mock_get:
            mock_settings = MagicMock()
            mock_settings.handler_timeout_seconds = 10.0
            mock_get.return_value = mock_settings
            result = BaldurEventBus._load_handler_timeout()
        assert result == 10.0

    def test_load_handler_timeout_returns_fallback_on_import_error(self):
        """Returns _FALLBACK_HANDLER_TIMEOUT when settings import fails."""
        from baldur.services.event_bus.bus.event_bus import _FALLBACK_HANDLER_TIMEOUT

        with patch(
            "baldur.settings.event_bus.get_event_bus_settings",
            side_effect=Exception("settings unavailable"),
        ):
            result = BaldurEventBus._load_handler_timeout()
        assert result == _FALLBACK_HANDLER_TIMEOUT


class TestExecuteHandlerWithTimeoutBehavior:
    """_execute_handler_with_timeout() behavior verification."""

    def test_handler_completes_within_timeout_returns_true(self, bus):
        """Handler that finishes in time returns True."""
        handler = MagicMock(__name__="fast_handler")
        event = _make_event()
        result = bus._execute_handler_with_timeout(handler, event, timeout=5.0)
        assert result is True
        handler.assert_called_once_with(event)

    def test_timeout_zero_bypasses_guard_and_calls_directly(self, bus):
        """timeout=0 disables guard, calls handler directly (no ThreadPoolExecutor)."""
        handler = MagicMock(__name__="direct_handler")
        event = _make_event()
        result = bus._execute_handler_with_timeout(handler, event, timeout=0)
        assert result is True
        handler.assert_called_once_with(event)

    def test_timeout_negative_bypasses_guard(self, bus):
        """Negative timeout also bypasses guard (timeout <= 0 branch)."""
        handler = MagicMock(__name__="neg_handler")
        event = _make_event()
        result = bus._execute_handler_with_timeout(handler, event, timeout=-1.0)
        assert result is True
        handler.assert_called_once_with(event)

    def test_slow_handler_returns_false_on_timeout(self, bus):
        """Handler exceeding timeout returns False."""
        gate = threading.Event()

        def slow_handler(event):
            gate.wait(timeout=10.0)

        event = _make_event()
        result = bus._execute_handler_with_timeout(slow_handler, event, timeout=0.1)
        gate.set()
        assert result is False

    def test_handler_exception_propagates_through(self, bus):
        """Handler exception is not caught by _execute_handler_with_timeout."""

        def raising_handler(event):
            raise ValueError("handler error")

        event = _make_event()
        with pytest.raises(ValueError, match="handler error"):
            bus._execute_handler_with_timeout(raising_handler, event, timeout=5.0)

    def test_contextvars_propagated_to_worker_thread(self, bus):
        """contextvars from parent thread are available in handler thread (D8)."""
        test_var = contextvars.ContextVar("test_var", default=None)
        captured = []

        def capturing_handler(event):
            captured.append(test_var.get())

        # Given
        token = test_var.set("propagated_value")
        event = _make_event()

        # When
        try:
            bus._execute_handler_with_timeout(capturing_handler, event, timeout=5.0)
        finally:
            test_var.reset(token)

        # Then
        assert captured == ["propagated_value"]

    def test_timeout_logs_warning_with_handler_name(self, bus, caplog):
        """Timed-out handler emits WARNING log with handler name."""
        import logging

        gate = threading.Event()

        def blocking_handler(event):
            gate.wait(timeout=10.0)

        event = _make_event()
        with caplog.at_level(logging.WARNING):
            bus._execute_handler_with_timeout(blocking_handler, event, timeout=0.1)
        gate.set()
        assert any("handler_timeout" in r.message for r in caplog.records) or any(
            "handler_timeout" in getattr(r, "msg", "") for r in caplog.records
        )


class TestPublishTimeoutIntegrationBehavior:
    """publish() integration with handler timeout guard."""

    def test_slow_handler_does_not_block_subsequent_handlers(self, bus):
        """Slow handler times out but fast handler still executes (doc 438)."""
        gate = threading.Event()
        results = []

        def slow_handler(event):
            gate.wait(timeout=10.0)

        def fast_handler(event):
            results.append("ok")

        # Given — set a short timeout
        bus._handler_timeout = 0.2
        bus.subscribe(EventType.CONFIG_UPDATED, slow_handler)
        bus.subscribe(EventType.CONFIG_UPDATED, fast_handler)

        # When
        count = bus.publish(_make_event())
        gate.set()

        # Then
        assert results == ["ok"]
        assert count == 1

    def test_timed_out_handler_excluded_from_count(self, bus):
        """Timed-out handler is not counted in handlers_called."""
        gate = threading.Event()

        def blocking_handler(event):
            gate.wait(timeout=10.0)

        bus._handler_timeout = 0.1
        bus.subscribe(EventType.CONFIG_UPDATED, blocking_handler)

        count = bus.publish(_make_event())
        gate.set()
        assert count == 0

    def test_timeout_zero_disables_guard_in_publish(self, bus):
        """handler_timeout=0 restores original behavior (no timeout)."""
        handler = MagicMock(__name__="normal_handler")
        bus._handler_timeout = 0
        bus.subscribe(EventType.CONFIG_UPDATED, handler)
        count = bus.publish(_make_event())
        assert count == 1
        handler.assert_called_once()
