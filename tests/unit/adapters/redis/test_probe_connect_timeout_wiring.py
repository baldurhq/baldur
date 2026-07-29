"""Redis admission probes must fail fast, and must do it from settings.

An *admission probe* is a ping whose result decides whether a subsystem uses
Redis at all — failure is an expected outcome that degrades to another backend.
``RedisSettings.probe_connect_timeout`` exists for exactly that shape, and its
own description states the contract: deliberately shorter than the data-path
``socket_connect_timeout`` so an unreachable Redis degrades quickly instead of
blocking.

These cases pin the wiring for the probes whose failure path re-attempts on its
own cadence, so a fail-fast timeout costs at most one cycle:

- the Redis event bus (its listener reconnects on a fixed interval)
- the Meta-Watchdog Redis probe (the watchdog re-probes every pass)

Each asserts a NON-default value reaches the constructor, which is what
distinguishes "reads the setting" from "hardcodes 0.5".

The metric source adapter is here for the adjacent defect: it built its client
with a bare ``redis.from_url`` and its own environment read, so it bypassed the
configured socket timeouts entirely and its verification ping blocked on the OS
TCP timeout against an unreachable host.

Reference:
    src/baldur/settings/redis.py — ``probe_connect_timeout``
    src/baldur/adapters/redis/connection_factory.py
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.redis.connection_factory import RedisConnectionFactory
from baldur.settings.event_bus import EventBusSettings
from baldur.settings.redis import RedisSettings

# Deliberately not the shipped 0.5 default: a hardcoded literal would still
# satisfy an assertion written against the default.
_PROBE_TIMEOUT = 0.75


@pytest.fixture
def redis_settings():
    """A real settings instance — no spec question, and the fields are real."""
    return RedisSettings(
        url="redis://probe-host:6379/0",
        probe_connect_timeout=_PROBE_TIMEOUT,
        socket_connect_timeout=9.0,
    )


class TestEventBusProbeConnectTimeoutWiring:
    """The event bus ping gates whether the bus runs at all."""

    def test_connect_forwards_the_settings_probe_timeout(self, redis_settings):
        """The connect timeout comes from the setting, not a literal."""
        from baldur.services.event_bus.redis_bus import RedisEventBus

        mock_factory = MagicMock(spec=RedisConnectionFactory)
        # No dedicated bus URL — the shared RedisSettings.url is used.
        event_bus_settings = EventBusSettings(redis_url=None)

        with (
            patch(
                "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
                autospec=True,
                return_value=mock_factory,
            ),
            patch(
                "baldur.settings.redis.get_redis_settings",
                autospec=True,
                return_value=redis_settings,
            ),
            patch(
                "baldur.settings.event_bus.get_event_bus_settings",
                autospec=True,
                return_value=event_bus_settings,
            ),
        ):
            bus = RedisEventBus.__new__(RedisEventBus)
            bus._redis_client = None
            assert bus._connect_redis() is True

        assert (
            mock_factory.create.call_args.kwargs["socket_connect_timeout"]
            == _PROBE_TIMEOUT
        )

    def test_connect_keeps_decoding_responses(self, redis_settings):
        """Negative assertion: the new kwarg must not displace an existing one.

        The bus reads its channel payloads as ``str``; dropping
        ``decode_responses`` would hand it bytes at the first published event,
        not at connect time.
        """
        from baldur.services.event_bus.redis_bus import RedisEventBus

        mock_factory = MagicMock(spec=RedisConnectionFactory)
        # No dedicated bus URL — the shared RedisSettings.url is used.
        event_bus_settings = EventBusSettings(redis_url=None)

        with (
            patch(
                "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
                autospec=True,
                return_value=mock_factory,
            ),
            patch(
                "baldur.settings.redis.get_redis_settings",
                autospec=True,
                return_value=redis_settings,
            ),
            patch(
                "baldur.settings.event_bus.get_event_bus_settings",
                autospec=True,
                return_value=event_bus_settings,
            ),
        ):
            bus = RedisEventBus.__new__(RedisEventBus)
            bus._redis_client = None
            bus._connect_redis()

        assert mock_factory.create.call_args.kwargs["decode_responses"] is True

    def test_unreachable_redis_still_degrades_rather_than_raising(self, redis_settings):
        """The fail direction is unchanged — only how long it takes to get there."""
        from baldur.services.event_bus.redis_bus import RedisEventBus

        mock_factory = MagicMock(spec=RedisConnectionFactory)
        mock_factory.create.return_value.ping.side_effect = OSError("unreachable")
        event_bus_settings = EventBusSettings(redis_url=None)

        with (
            patch(
                "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
                autospec=True,
                return_value=mock_factory,
            ),
            patch(
                "baldur.settings.redis.get_redis_settings",
                autospec=True,
                return_value=redis_settings,
            ),
            patch(
                "baldur.settings.event_bus.get_event_bus_settings",
                autospec=True,
                return_value=event_bus_settings,
            ),
        ):
            bus = RedisEventBus.__new__(RedisEventBus)
            bus._redis_client = None
            assert bus._connect_redis() is False


class TestMetaWatchdogRedisProbeConnectTimeoutWiring:
    """The watchdog's Redis probe re-asks on every pass."""

    def test_probe_forwards_the_settings_probe_timeout(self, redis_settings):
        """The probe client is built for reachability, not for data I/O."""
        from baldur.meta.health_probe import RedisProbe

        with (
            patch(
                "baldur.settings.redis.get_redis_settings",
                autospec=True,
                return_value=redis_settings,
            ),
            patch(
                "baldur.adapters.cache.redis_adapter.RedisCacheAdapter",
                autospec=True,
            ) as mock_adapter,
        ):
            RedisProbe().probe()

        assert mock_adapter.call_args.kwargs["socket_connect_timeout"] == _PROBE_TIMEOUT


class TestMetricSourceAdapterUsesTheConnectionFactory:
    """The metric adapter's verification ping had no connect timeout at all.

    Building the client directly bypassed the connection factory, which is what
    applies the configured socket timeouts, injects credentials from settings
    rather than the URL, and resolves Sentinel/Cluster URLs.
    """

    def test_client_is_built_through_the_connection_factory(self, redis_settings):
        """Negative assertion: no direct ``redis.from_url`` construction."""
        from baldur.adapters.metrics.factory import _create_redis_adapter

        mock_factory = MagicMock(spec=RedisConnectionFactory)

        with (
            patch(
                "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
                autospec=True,
                return_value=mock_factory,
            ),
            patch(
                "baldur.settings.redis.get_redis_settings",
                autospec=True,
                return_value=redis_settings,
            ),
            patch("redis.from_url", autospec=True) as mock_from_url,
        ):
            _create_redis_adapter()

        mock_factory.create.assert_called_once()
        mock_from_url.assert_not_called()

    def test_url_comes_from_settings_not_a_raw_environment_read(
        self, redis_settings, monkeypatch
    ):
        """The URL resolves through the settings layer like every other consumer.

        ``RedisSettings`` binds the same ``BALDUR_REDIS_URL`` variable with the
        same default, so this is the identical value — read through the layer
        that also carries auth and topology.
        """
        from baldur.adapters.metrics.factory import _create_redis_adapter

        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://ignored-raw-env:6379/0")
        mock_factory = MagicMock(spec=RedisConnectionFactory)

        with (
            patch(
                "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
                autospec=True,
                return_value=mock_factory,
            ),
            patch(
                "baldur.settings.redis.get_redis_settings",
                autospec=True,
                return_value=redis_settings,
            ),
        ):
            _create_redis_adapter()

        assert mock_factory.create.call_args.args[0] == redis_settings.url

    def test_unreachable_redis_still_falls_back_to_the_null_adapter(
        self, redis_settings
    ):
        """The fail direction is unchanged — the ping still degrades, not raises."""
        from baldur.adapters.metrics.base import NullMetricSourceAdapter
        from baldur.adapters.metrics.factory import _create_redis_adapter

        mock_factory = MagicMock(spec=RedisConnectionFactory)
        mock_factory.create.return_value.ping.side_effect = OSError("unreachable")

        with (
            patch(
                "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
                autospec=True,
                return_value=mock_factory,
            ),
            patch(
                "baldur.settings.redis.get_redis_settings",
                autospec=True,
                return_value=redis_settings,
            ),
        ):
            adapter = _create_redis_adapter()

        assert isinstance(adapter, NullMetricSourceAdapter)
