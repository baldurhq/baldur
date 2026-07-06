"""
DistributedRateLimitChannel 단위 테스트.

테스트 대상:
- Kafka broadcast 429 메시지 발행
- subscribe 핸들러 등록
- broadcast 실패 처리
- _dispatch_to_handlers 핸들러 전달
- start/stop 상태 관리
- handler_count 속성
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_kafka_bus():
    """MagicMock KafkaEventBus."""
    bus = MagicMock()
    bus.publish.return_value = True
    return bus


@pytest.fixture
def channel(mock_kafka_bus):
    """DistributedRateLimitChannel 인스턴스."""
    from baldur.services.rate_limit.distributed_channel import (
        DistributedRateLimitChannel,
    )

    return DistributedRateLimitChannel(kafka_bus=mock_kafka_bus)


# =============================================================================
# Kafka broadcast 테스트
# =============================================================================


class TestDistributedRateLimitChannel:
    """DistributedRateLimitChannel 기본 단위 테스트."""

    def test_broadcast_rate_limit_429_publishes_to_kafka(self, channel, mock_kafka_bus):
        """broadcast_rate_limit_429()이 Kafka에 메시지 발행."""
        from baldur.services.rate_limit.distributed_channel import RATE_LIMIT_TOPIC

        success = channel.broadcast_rate_limit_429(
            key="payment_api",
            consecutive_429s=3,
            cooldown_until=time.time() + 10,
            calculated_delay=5.0,
        )

        assert success is True
        mock_kafka_bus.publish.assert_called_once()

        call_kwargs = mock_kafka_bus.publish.call_args[1]
        assert call_kwargs["topic"] == RATE_LIMIT_TOPIC
        assert call_kwargs["key"] == "payment_api"
        assert call_kwargs["event"]["event_type"] == "RATE_LIMIT_429"
        assert call_kwargs["event"]["consecutive_429s"] == 3

    def test_subscribe_registers_handler(self, channel, mock_kafka_bus):
        """subscribe_rate_limit_429()이 핸들러 등록."""
        handler_called: list = []

        def test_handler(event_data):
            handler_called.append(event_data)

        channel.subscribe_rate_limit_429(test_handler)

        assert len(channel._handlers) == 1
        mock_kafka_bus.subscribe.assert_called_once()


# =============================================================================
# broadcast 실패 처리 테스트
# =============================================================================


class TestDistributedRateLimitChannelBroadcastFailure:
    """DistributedRateLimitChannel broadcast 실패 처리 테스트."""

    def test_broadcast_returns_false_on_publish_failure(self, channel, mock_kafka_bus):
        """Kafka publish 실패 시 False 반환."""
        mock_kafka_bus.publish.return_value = False

        result = channel.broadcast_rate_limit_429(
            key="test",
            consecutive_429s=1,
            cooldown_until=time.time() + 10,
            calculated_delay=5.0,
        )

        assert result is False

    def test_broadcast_returns_false_on_exception(self, channel, mock_kafka_bus):
        """Kafka publish 예외 시 False 반환."""
        mock_kafka_bus.publish.side_effect = RuntimeError("kafka down")

        result = channel.broadcast_rate_limit_429(
            key="test",
            consecutive_429s=1,
            cooldown_until=time.time() + 10,
            calculated_delay=5.0,
        )

        assert result is False


# =============================================================================
# _dispatch_to_handlers 핸들러 전달 테스트
# =============================================================================


class TestDistributedRateLimitChannelDispatch:
    """_dispatch_to_handlers 핸들러 전달 테스트."""

    def test_dispatch_calls_all_handlers(self, channel):
        """모든 등록된 핸들러에 이벤트 전달."""
        results: list[tuple[str, dict]] = []

        channel._handlers = [
            lambda data, tag="a": results.append((tag, data)),
            lambda data, tag="b": results.append((tag, data)),
        ]

        event = MagicMock()
        event.value = {"key": "test_api", "consecutive_429s": 1}

        success = channel._dispatch_to_handlers(event)

        assert success is True
        assert len(results) == 2
        assert results[0][0] == "a"
        assert results[1][0] == "b"

    def test_dispatch_survives_handler_exception(self, channel):
        """핸들러 예외 시에도 다른 핸들러 계속 실행."""
        results: list = []

        def failing_handler(data):
            raise ValueError("handler crash")

        def working_handler(data):
            results.append(data)

        channel._handlers = [failing_handler, working_handler]

        event = MagicMock()
        event.value = {"key": "test"}

        channel._dispatch_to_handlers(event)
        assert len(results) == 1


# =============================================================================
# start/stop 상태 관리 테스트
# =============================================================================


class TestDistributedRateLimitChannelStartStop:
    """start/stop 상태 관리 테스트."""

    def test_start_sets_running(self, channel, mock_kafka_bus):
        """start() 호출 시 running 상태."""
        channel.start()
        assert channel.is_running is True
        mock_kafka_bus.start.assert_called_once()

    def test_stop_clears_running(self, channel):
        """stop() 호출 시 running 해제."""
        channel.start()
        channel.stop()
        assert channel.is_running is False

    def test_handler_count_property(self, channel):
        """handler_count 속성 확인."""
        assert channel.handler_count == 0

        channel._handlers.append(lambda d: None)
        assert channel.handler_count == 1


# =============================================================================
# 317: _on_broadcast_delivery 콜백 테스트
# =============================================================================


class TestOnBroadcastDeliveryBehavior:
    """317: _on_broadcast_delivery Kafka 전송 결과 콜백 동작 검증."""

    def test_successful_delivery_does_not_raise(self):
        """전송 성공 리포트 시 예외 없이 처리."""
        from baldur.services.rate_limit.distributed_channel import (
            DistributedRateLimitChannel,
        )

        report = MagicMock()
        report.error = None
        report.topic = "baldur.rate_limit.events"

        DistributedRateLimitChannel._on_broadcast_delivery(report)

    def test_failed_delivery_does_not_raise(self):
        """전송 실패 리포트 시 예외 없이 처리 (Fire-and-Forget)."""
        from baldur.services.rate_limit.distributed_channel import (
            DistributedRateLimitChannel,
        )

        report = MagicMock()
        report.error = "BrokerNotAvailable"
        report.topic = "baldur.rate_limit.events"

        DistributedRateLimitChannel._on_broadcast_delivery(report)


# =============================================================================
# 317: broadcast with on_delivery 콜백 전달 테스트
# =============================================================================


class TestBroadcastPassesDeliveryCallbackBehavior:
    """317: broadcast_rate_limit_429이 on_delivery 콜백을 Kafka에 전달하는지 검증."""

    def test_broadcast_passes_on_delivery_callback(self, channel, mock_kafka_bus):
        """broadcast 호출 시 on_delivery=_on_broadcast_delivery가 전달된다."""
        from baldur.services.rate_limit.distributed_channel import (
            DistributedRateLimitChannel,
        )

        channel.broadcast_rate_limit_429(
            key="test_api",
            consecutive_429s=1,
            cooldown_until=1000.0,
            calculated_delay=5.0,
        )

        call_kwargs = mock_kafka_bus.publish.call_args[1]
        assert (
            call_kwargs["on_delivery"]
            is DistributedRateLimitChannel._on_broadcast_delivery
        )
