"""Unit tests for ``RedisCacheAdapter`` no-arg URL source + ``close()``.

Source: ``src/baldur/adapters/cache/redis_adapter.py``

Covers:

- 463 D4 / G5 — no-arg constructor reads ``BALDUR_REDIS_URL`` via
  :func:`get_redis_settings` (NOT ``django.conf.settings.REDIS_URL``).
  Non-Django runtimes (FastAPI / Flask / plain Python) can use the
  no-arg constructor without a Django shim.
- 463 D16 — ``close()`` drains the underlying ``connection_pool``.
  Idempotent (safe to call twice). Exceptions are swallowed and
  logged as ``redis_cache.close_failed``. Required by
  ``reset_init_state(cleanup=True)`` so xdist re-init cycles do not
  leak file descriptors.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.5 Dependency interaction (mock ``connection_pool.disconnect``).
- §8.3 Idempotency (``close()`` called twice).
- §8.2 Exception/edge cases (disconnect raises → no propagation).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.adapters.cache.redis_adapter import RedisCacheAdapter

# ---------------------------------------------------------------------------
# No-arg URL source — D4 / G5
# ---------------------------------------------------------------------------


class TestRedisCacheAdapterNoArgUrlBehavior:
    """Constructor with no ``url`` reads ``RedisSettings.url`` (BALDUR_REDIS_URL).

    The pre-D4 path read ``django.conf.settings.REDIS_URL`` and tied
    framework-symmetric runtimes to a Django settings shim. After D4 the
    sole URL source is the framework's canonical ``BALDUR_REDIS_URL`` env
    var via :func:`get_redis_settings`.
    """

    def test_no_arg_constructor_reads_redis_settings_url(self):
        """The factory is called with the URL returned by ``get_redis_settings()``."""
        # Stub get_redis_settings → carries an arbitrary URL we can fingerprint.
        sentinel_url = "redis://stub-host:9999/7"
        settings_stub = MagicMock()
        settings_stub.url = sentinel_url

        factory = MagicMock()
        factory.create.return_value = MagicMock()

        with (
            patch(
                "baldur.settings.redis.get_redis_settings",
                return_value=settings_stub,
            ),
            patch(
                "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
                return_value=factory,
            ),
        ):
            RedisCacheAdapter()  # no-arg construction

        # The factory MUST receive the URL from RedisSettings, not a Django shim.
        called_url = factory.create.call_args[0][0]
        assert called_url == sentinel_url

    def test_explicit_url_overrides_settings_url(self):
        """When ``url=`` is passed explicitly, ``get_redis_settings`` is NOT called."""
        explicit_url = "redis://explicit:1234/0"

        factory = MagicMock()
        factory.create.return_value = MagicMock()

        with (
            patch(
                "baldur.settings.redis.get_redis_settings",
            ) as m_settings,
            patch(
                "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
                return_value=factory,
            ),
        ):
            RedisCacheAdapter(url=explicit_url)

        m_settings.assert_not_called()
        called_url = factory.create.call_args[0][0]
        assert called_url == explicit_url

    def test_explicit_client_skips_factory_and_settings(self):
        """When ``client=`` is passed, neither URL source is consulted."""
        injected_client = MagicMock()

        with (
            patch(
                "baldur.settings.redis.get_redis_settings",
            ) as m_settings,
            patch(
                "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            ) as m_factory,
        ):
            adapter = RedisCacheAdapter(client=injected_client)

        m_settings.assert_not_called()
        m_factory.assert_not_called()
        assert adapter._redis is injected_client

    def test_no_arg_constructor_does_not_import_django_conf_settings(self):
        """No path reads ``django.conf.settings`` — framework symmetry probe.

        Probes the constructor's import behavior under the no-arg path. If
        a future regression re-introduces a ``from django.conf import settings``
        line on the no-arg path, this test fails because the patched
        ``get_redis_settings`` would never be called.
        """
        sentinel_url = "redis://probe:6379/0"
        settings_stub = MagicMock()
        settings_stub.url = sentinel_url

        factory = MagicMock()
        factory.create.return_value = MagicMock()

        with (
            patch(
                "baldur.settings.redis.get_redis_settings",
                return_value=settings_stub,
            ) as m_get_settings,
            patch(
                "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
                return_value=factory,
            ),
        ):
            RedisCacheAdapter()

        m_get_settings.assert_called_once()


# ---------------------------------------------------------------------------
# Socket-timeout / retry delegation — settings parity with the async adapter
# ---------------------------------------------------------------------------


class TestRedisCacheAdapterTimeoutDelegationBehavior:
    """The ctor delegates socket-timeout/retry resolution to the connection
    factory (which backfills from ``RedisSettings``) rather than hardcoding.

    Regression guard: the sync ctor previously defaulted
    ``socket_timeout``/``socket_connect_timeout`` to a literal ``5.0`` and
    ``retry_on_timeout`` to ``True``, so ``BALDUR_REDIS_*`` socket tuning never
    reached the sync path (only the async twin honored the settings). Passing
    ``None`` through lets the factory resolve every value from ``RedisSettings``.
    """

    def test_default_construction_delegates_timeouts_as_none(self):
        """Unset timeout kwargs reach the factory as ``None`` (settings-resolved)."""
        factory = MagicMock()
        factory.create.return_value = MagicMock()

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            return_value=factory,
        ):
            RedisCacheAdapter(url="redis://host:6379/0")

        create_kwargs = factory.create.call_args[1]
        assert create_kwargs["socket_timeout"] is None
        assert create_kwargs["socket_connect_timeout"] is None
        assert create_kwargs["retry_on_timeout"] is None

    def test_explicit_timeouts_forwarded_to_factory(self):
        """Explicit ctor timeouts still take precedence over settings resolution."""
        factory = MagicMock()
        factory.create.return_value = MagicMock()

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            return_value=factory,
        ):
            RedisCacheAdapter(
                url="redis://host:6379/0",
                socket_timeout=2.0,
                socket_connect_timeout=1.0,
                retry_on_timeout=False,
            )

        create_kwargs = factory.create.call_args[1]
        assert create_kwargs["socket_timeout"] == 2.0
        assert create_kwargs["socket_connect_timeout"] == 1.0
        assert create_kwargs["retry_on_timeout"] is False


# ---------------------------------------------------------------------------
# close() — D16 connection pool drain
# ---------------------------------------------------------------------------


class TestRedisCacheAdapterCloseBehavior:
    """``close()`` drains the underlying connection pool. Idempotent + swallow."""

    def test_close_invokes_connection_pool_disconnect(self):
        """``close()`` calls ``self._redis.connection_pool.disconnect()`` once."""
        client = MagicMock()
        client.connection_pool = MagicMock()
        adapter = RedisCacheAdapter(client=client)

        adapter.close()

        client.connection_pool.disconnect.assert_called_once_with()

    def test_close_is_idempotent_when_pool_already_disconnected(self):
        """Calling ``close()`` twice does not raise."""
        client = MagicMock()
        # disconnect is a no-op even when pool is empty — adapter must allow N calls.
        client.connection_pool.disconnect.return_value = None
        adapter = RedisCacheAdapter(client=client)

        adapter.close()
        adapter.close()

        # Both calls reach the underlying disconnect — adapter does not gate.
        assert client.connection_pool.disconnect.call_count == 2

    def test_close_swallows_disconnect_exception(self):
        """A ``disconnect`` failure is logged at WARNING but does NOT propagate.

        The test-fixture reset chain (``reset_init_state(cleanup=True)``)
        relies on this — a failed Redis cleanup must not block subsequent
        steps (cache default re-assert, ``reset_runtime``).
        """
        client = MagicMock()
        client.connection_pool.disconnect.side_effect = Exception("pool gone")
        adapter = RedisCacheAdapter(client=client)

        # Must not raise.
        adapter.close()

        client.connection_pool.disconnect.assert_called_once_with()
