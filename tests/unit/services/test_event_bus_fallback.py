"""
RedisEventBus event-propagation fallback chain tests.

Targets:
- _is_critical_event(): critical event classification
- publish(): Redis -> Kafka -> WAL fallback chain
- _publish_to_kafka_fallback(): Kafka fallback publish
- _write_to_wal(): the WAL as the final safety net
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.services.event_bus.bus import (
    BaldurEvent,
    EventType,
)
from baldur.services.event_bus.redis_bus import (
    CRITICAL_EVENT_TYPES,
    RedisEventBus,
)


def _make_bus_no_redis() -> RedisEventBus:
    """Create test bus without Redis connection."""
    with patch.object(RedisEventBus, "_connect_redis", return_value=False):
        bus = RedisEventBus()
    bus._redis_client = None
    return bus


def _make_bus_with_redis(mock_redis: MagicMock) -> RedisEventBus:
    """Create test bus with mocked Redis client."""
    with patch.object(RedisEventBus, "_connect_redis", return_value=True):
        bus = RedisEventBus()
    bus._redis_client = mock_redis
    return bus


def _make_critical_event() -> BaldurEvent:
    return BaldurEvent(
        event_type=EventType.REGION_PRIMARY_CHANGED,
        data={"key": "region_primary", "value": "us-west-2"},
        source="failover",
    )


def _make_normal_event() -> BaldurEvent:
    return BaldurEvent(
        event_type=EventType.CONFIG_UPDATED,
        data={"key": "test"},
        source="test",
    )


# =============================================================================
# CRITICAL_EVENT_TYPES contract
# =============================================================================


class TestCriticalEventTypesContract:
    """Contract values of the critical event type set."""

    def test_contains_region_primary_changed(self) -> None:
        """REGION_PRIMARY_CHANGED is a critical event."""
        assert EventType.REGION_PRIMARY_CHANGED in CRITICAL_EVENT_TYPES

    def test_contains_emergency_activated(self) -> None:
        """EMERGENCY_ACTIVATED is a critical event."""
        assert EventType.EMERGENCY_ACTIVATED in CRITICAL_EVENT_TYPES

    def test_contains_kill_switch_activated(self) -> None:
        """KILL_SWITCH_ACTIVATED is a critical event."""
        assert EventType.KILL_SWITCH_ACTIVATED in CRITICAL_EVENT_TYPES

    def test_count(self) -> None:
        """There are exactly three critical event types."""
        assert len(CRITICAL_EVENT_TYPES) == 3

    def test_is_frozenset(self) -> None:
        """CRITICAL_EVENT_TYPES is a frozenset."""
        assert isinstance(CRITICAL_EVENT_TYPES, frozenset)


# =============================================================================
# _is_critical_event() behavior
# =============================================================================


class TestIsCriticalEventBehavior:
    """_is_critical_event() behavior tests."""

    def test_critical_event_returns_true(self) -> None:
        """Returns True for every critical event type."""
        bus = _make_bus_no_redis()
        for event_type in CRITICAL_EVENT_TYPES:
            event = BaldurEvent(
                event_type=event_type,
                data={},
                source="test",
            )
            assert bus._is_critical_event(event) is True

    def test_non_critical_event_returns_false(self) -> None:
        """Returns False for a non-critical event type."""
        bus = _make_bus_no_redis()
        event = BaldurEvent(
            event_type=EventType.CONFIG_UPDATED,
            data={},
            source="test",
        )
        assert bus._is_critical_event(event) is False


# =============================================================================
# publish() fallback chain behavior
# =============================================================================


class TestPublishFallbackChainBehavior:
    """publish() Redis -> Kafka -> WAL fallback chain behavior."""

    def test_redis_success_does_not_trigger_fallback(self) -> None:
        """A successful Redis publish calls neither the Kafka nor the WAL fallback."""
        mock_redis = MagicMock()
        bus = _make_bus_with_redis(mock_redis)

        with (
            patch.object(bus, "_publish_to_kafka_fallback") as mock_kafka,
            patch.object(bus, "_write_to_wal") as mock_wal,
        ):
            bus.publish(_make_critical_event())

            mock_kafka.assert_not_called()
            mock_wal.assert_not_called()

    def test_redis_failure_triggers_kafka_for_critical(self) -> None:
        """Redis failure on a critical event falls back to Kafka."""
        mock_redis = MagicMock()
        mock_redis.publish.side_effect = Exception("Redis down")
        bus = _make_bus_with_redis(mock_redis)

        with (
            patch.object(bus, "_publish_to_kafka_fallback") as mock_kafka,
            patch.object(bus, "_write_to_wal") as mock_wal,
        ):
            bus.publish(_make_critical_event())

            mock_kafka.assert_called_once()
            mock_wal.assert_not_called()

    def test_redis_failure_no_kafka_for_non_critical(self) -> None:
        """Redis failure on a non-critical event does not fall back to Kafka."""
        mock_redis = MagicMock()
        mock_redis.publish.side_effect = Exception("Redis down")
        bus = _make_bus_with_redis(mock_redis)

        with (
            patch.object(bus, "_publish_to_kafka_fallback") as mock_kafka,
            patch.object(bus, "_write_to_wal") as mock_wal,
        ):
            bus.publish(_make_normal_event())

            mock_kafka.assert_not_called()
            mock_wal.assert_not_called()

    def test_redis_and_kafka_failure_triggers_wal(self) -> None:
        """With Redis and Kafka both failing, a critical event lands in the WAL."""
        mock_redis = MagicMock()
        mock_redis.publish.side_effect = Exception("Redis down")
        bus = _make_bus_with_redis(mock_redis)

        with (
            patch.object(
                bus, "_publish_to_kafka_fallback", side_effect=Exception("Kafka down")
            ),
            patch.object(bus, "_write_to_wal") as mock_wal,
        ):
            bus.publish(_make_critical_event())

            mock_wal.assert_called_once()

    def test_no_redis_client_triggers_kafka_fallback(self) -> None:
        """With no Redis client, a critical event falls back to Kafka."""
        bus = _make_bus_no_redis()

        with patch.object(bus, "_publish_to_kafka_fallback") as mock_kafka:
            bus.publish(_make_critical_event())
            mock_kafka.assert_called_once()

    def test_local_bus_always_receives_event(self) -> None:
        """Local handlers always receive the event."""
        mock_redis = MagicMock()
        mock_redis.publish.side_effect = Exception("Redis down")
        bus = _make_bus_with_redis(mock_redis)

        received = []
        bus._local_bus.subscribe(
            EventType.REGION_PRIMARY_CHANGED,
            lambda e: received.append(e),
        )

        with patch.object(bus, "_publish_to_kafka_fallback"):
            bus.publish(_make_critical_event())

        assert len(received) == 1

    def test_publish_returns_handler_count(self) -> None:
        """publish() returns the number of local handlers called."""
        bus = _make_bus_no_redis()

        received = []
        bus._local_bus.subscribe(
            EventType.REGION_PRIMARY_CHANGED,
            lambda e: received.append(e),
        )

        with patch.object(bus, "_publish_to_kafka_fallback"):
            count = bus.publish(_make_critical_event())

        assert count == 1


# =============================================================================
# _publish_to_kafka_fallback() behavior
# =============================================================================


class TestPublishToKafkaFallbackBehavior:
    """_publish_to_kafka_fallback() behavior tests."""

    def test_falls_through_to_wal_quietly_when_kafka_not_installed(self) -> None:
        """No Kafka adapter installed -> quiet log + WAL write, no exception.

        An install that never opted into Kafka must not have every critical
        event report a Kafka misconfiguration; the WAL is the real safety net.
        """
        bus = _make_bus_no_redis()
        event = BaldurEvent(
            event_type=EventType.REGION_PRIMARY_CHANGED,
            data={},
            source="test",
        )
        # 528 D10-v2: kafka producer relocated to baldur_dormant. Simulate
        # absence by patching sys.modules to force ImportError.
        with patch.dict(
            "sys.modules", {"baldur_dormant.adapters.kafka.producer": None}
        ):
            with patch.object(bus, "_write_to_wal") as mock_wal:
                bus._publish_to_kafka_fallback(event)

        mock_wal.assert_called_once_with(event)

    def test_kafka_not_installed_logs_quietly_instead_of_at_exception_level(
        self,
    ) -> None:
        """The absence of a never-installed optional adapter is DEBUG, not an
        exception.

        publish() logs ``redis_event_bus.kafka_fallback_failed`` at exception
        level whenever this method raises. While it raised AdapterError on the
        baldur_dormant ImportError, an OSS-only install with Redis down
        reported a Kafka misconfiguration on every critical event - including
        every kill-switch flip.
        """
        bus = _make_bus_no_redis()
        event = BaldurEvent(
            event_type=EventType.KILL_SWITCH_ACTIVATED,
            data={},
            source="system_control",
        )

        # The private tree runs with baldur_dormant present, so the
        # not-installed branch has to be forced rather than relied on.
        with patch("baldur.services.event_bus.redis_bus.logger") as mock_logger:
            with patch.dict(
                "sys.modules", {"baldur_dormant.adapters.kafka.producer": None}
            ):
                with patch.object(bus, "_write_to_wal"):
                    bus._publish_to_kafka_fallback(event)

        mock_logger.debug.assert_called_once_with(
            "redis_event_bus.kafka_fallback_not_installed"
        )
        mock_logger.exception.assert_not_called()
        mock_logger.warning.assert_not_called()

    def test_calls_kafka_producer_singleton(self) -> None:
        """Uses get_kafka_producer() singleton for fire-and-forget publish."""
        pytest.importorskip("baldur_dormant")

        bus = _make_bus_no_redis()
        event = BaldurEvent(
            event_type=EventType.REGION_PRIMARY_CHANGED,
            data={},
            source="test",
        )
        mock_producer = MagicMock()
        mock_producer.publish.return_value = True
        with patch(
            "baldur_dormant.adapters.kafka.producer.get_kafka_producer",
            return_value=mock_producer,
        ):
            bus._publish_to_kafka_fallback(event)
            mock_producer.publish.assert_called_once()


# =============================================================================
# _write_to_wal() behavior
# =============================================================================


class TestWriteToWalBehavior:
    """_write_to_wal() behavior tests."""

    @patch("baldur.audit.wal.WriteAheadLog")
    @patch("baldur.audit.wal._models.WALConfig")
    def test_writes_event_to_wal(
        self, mock_config_cls: MagicMock, mock_wal_cls: MagicMock
    ) -> None:
        """Writes a critical event to the WAL."""
        mock_wal = MagicMock()
        mock_wal_cls.return_value = mock_wal

        bus = _make_bus_no_redis()
        event = BaldurEvent(
            event_type=EventType.REGION_PRIMARY_CHANGED,
            data={"key": "test"},
            source="failover",
        )
        bus._write_to_wal(event)

        mock_wal.write.assert_called_once()

    def test_wal_import_failure_does_not_raise(self) -> None:
        """A WAL import failure does not propagate."""
        bus = _make_bus_no_redis()
        event = BaldurEvent(
            event_type=EventType.REGION_PRIMARY_CHANGED,
            data={},
            source="test",
        )
        with patch(
            "baldur.audit.wal.WriteAheadLog",
            side_effect=ImportError("no WAL module"),
        ):
            # Completes without raising
            bus._write_to_wal(event)
