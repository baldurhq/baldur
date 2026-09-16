"""
DistributedRateLimitChannel unit tests.

Covered:
- publishing a 429 broadcast to Kafka
- subscribe handler registration
- broadcast failure handling
- _dispatch_to_handlers delivery to handlers
- start/stop running state
- the handler_count property
- the quiet no-op posture of an install without the Kafka adapter
"""

from __future__ import annotations

import sys
import time
from unittest.mock import MagicMock, patch

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
    """A DistributedRateLimitChannel over a mock bus."""
    from baldur.services.rate_limit.distributed_channel import (
        DistributedRateLimitChannel,
    )

    return DistributedRateLimitChannel(kafka_bus=mock_kafka_bus)


# =============================================================================
# Kafka broadcast
# =============================================================================


class TestDistributedRateLimitChannel:
    """Basic DistributedRateLimitChannel behaviour."""

    def test_broadcast_rate_limit_429_publishes_to_kafka(self, channel, mock_kafka_bus):
        """broadcast_rate_limit_429() publishes the event to Kafka."""
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
        """subscribe_rate_limit_429() registers the handler."""
        handler_called: list = []

        def test_handler(event_data):
            handler_called.append(event_data)

        channel.subscribe_rate_limit_429(test_handler)

        assert len(channel._handlers) == 1
        mock_kafka_bus.subscribe.assert_called_once()


# =============================================================================
# Broadcast failure handling
# =============================================================================


class TestDistributedRateLimitChannelBroadcastFailure:
    """Broadcast failures on an installed bus are reported and swallowed."""

    def test_broadcast_returns_false_on_publish_failure(self, channel, mock_kafka_bus):
        """A Kafka publish that reports failure returns False."""
        mock_kafka_bus.publish.return_value = False

        result = channel.broadcast_rate_limit_429(
            key="test",
            consecutive_429s=1,
            cooldown_until=time.time() + 10,
            calculated_delay=5.0,
        )

        assert result is False

    def test_broadcast_returns_false_on_exception(self, channel, mock_kafka_bus):
        """A Kafka publish that raises returns False."""
        mock_kafka_bus.publish.side_effect = RuntimeError("kafka down")

        result = channel.broadcast_rate_limit_429(
            key="test",
            consecutive_429s=1,
            cooldown_until=time.time() + 10,
            calculated_delay=5.0,
        )

        assert result is False


# =============================================================================
# _dispatch_to_handlers delivery
# =============================================================================


class TestDistributedRateLimitChannelDispatch:
    """_dispatch_to_handlers delivers to every registered handler."""

    def test_dispatch_calls_all_handlers(self, channel):
        """Every registered handler receives the event."""
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
        """A raising handler does not stop the others."""
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
# start/stop running state
# =============================================================================


class TestDistributedRateLimitChannelStartStop:
    """start/stop running state."""

    def test_start_sets_running(self, channel, mock_kafka_bus):
        """start() marks the channel running."""
        channel.start()
        assert channel.is_running is True
        mock_kafka_bus.start.assert_called_once()

    def test_stop_clears_running(self, channel):
        """stop() clears the running state."""
        channel.start()
        channel.stop()
        assert channel.is_running is False

    def test_handler_count_property(self, channel):
        """handler_count reflects the registered handlers."""
        assert channel.handler_count == 0

        channel._handlers.append(lambda d: None)
        assert channel.handler_count == 1


# =============================================================================
# 317: the _on_broadcast_delivery callback
# =============================================================================


class TestOnBroadcastDeliveryBehavior:
    """317: the Kafka delivery-report callback never raises."""

    def test_successful_delivery_does_not_raise(self):
        """A successful delivery report is handled without raising."""
        from baldur.services.rate_limit.distributed_channel import (
            DistributedRateLimitChannel,
        )

        report = MagicMock()
        report.error = None
        report.topic = "baldur.rate_limit.events"

        DistributedRateLimitChannel._on_broadcast_delivery(report)

    def test_failed_delivery_does_not_raise(self):
        """A failed delivery report is handled without raising (fire-and-forget)."""
        from baldur.services.rate_limit.distributed_channel import (
            DistributedRateLimitChannel,
        )

        report = MagicMock()
        report.error = "BrokerNotAvailable"
        report.topic = "baldur.rate_limit.events"

        DistributedRateLimitChannel._on_broadcast_delivery(report)


# =============================================================================
# 317: broadcast passes the on_delivery callback
# =============================================================================


class TestBroadcastPassesDeliveryCallbackBehavior:
    """317: broadcast_rate_limit_429 hands on_delivery to Kafka."""

    def test_broadcast_passes_on_delivery_callback(self, channel, mock_kafka_bus):
        """The publish call carries on_delivery=_on_broadcast_delivery."""
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


# =============================================================================
# An install without the Kafka adapter
#
# The adapter is not part of the open-source core and is not offered as an
# extra, so on a stock install every 429 the coordinator handles reaches this
# channel with nothing to publish to. Observed on a dogfood cron job: nine 429s
# in two minutes produced eighteen ERROR lines with tracebacks telling an OSS
# user to install a package that is not on offer. The absence is the normal
# state, not an error, and not something to log: a silent no-op.
# =============================================================================


@pytest.fixture
def no_kafka_adapter():
    """Make the Kafka adapter import fail, whatever this environment installs."""
    with patch.dict(sys.modules, {"baldur_dormant.adapters.kafka.event_bus": None}):
        yield


class TestChannelWithoutKafkaAdapterBehavior:
    """The channel on an install that never opted into cluster propagation."""

    def _channel(self):
        from baldur.services.rate_limit.distributed_channel import (
            DistributedRateLimitChannel,
        )

        return DistributedRateLimitChannel()

    def _broadcast(self, channel) -> bool:
        return channel.broadcast_rate_limit_429(
            key="reddit",
            consecutive_429s=1,
            cooldown_until=1000.0,
            calculated_delay=5.0,
        )

    def test_broadcast_returns_false_and_logs_nothing(self, no_kafka_adapter):
        from baldur.services.rate_limit import distributed_channel as module

        channel = self._channel()

        with patch.object(module, "logger") as logger:
            assert self._broadcast(channel) is False
            assert self._broadcast(channel) is False
            assert self._broadcast(channel) is False

        logger.exception.assert_not_called()
        logger.error.assert_not_called()
        logger.warning.assert_not_called()
        logger.info.assert_not_called()
        logger.debug.assert_not_called()

    def test_the_import_is_not_retried_once_it_has_failed(self, no_kafka_adapter):
        channel = self._channel()
        assert self._broadcast(channel) is False
        assert channel._kafka_unavailable is True

        # Were the import retried, this environment could now resolve it; the
        # latch keeps the channel a no-op for the life of the process instead.
        sys.modules.pop("baldur_dormant.adapters.kafka.event_bus", None)
        assert self._broadcast(channel) is False
        assert channel._kafka_bus is None

    def test_subscribe_and_start_stay_quiet_without_the_adapter(self, no_kafka_adapter):
        from baldur.services.rate_limit import distributed_channel as module

        channel = self._channel()

        with patch.object(module, "logger") as logger:
            channel.subscribe_rate_limit_429(lambda event: None)
            channel.start()

        assert channel.handler_count == 1
        assert channel._running is False
        logger.exception.assert_not_called()
        logger.warning.assert_not_called()
