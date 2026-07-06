"""
Unit tests for #422 Factory Bypass Cleanup — caller-side routing verification.

Complements `test_connection_factory.py` (URL-scheme dispatch tests) by
verifying the **9 refactored callsites** route Redis client creation
through ``get_redis_connection_factory().create()`` rather than
``redis.from_url()`` directly.

Sites covered here (callsite verification — Sentinel URL passes through Factory):
  1. ``audit/config.py::AuditConfig.get_redis_client``
  2. ``core/state_backend.py::RedisStateBackend._initialize_client``
  3. ``adapters/django/startup/env_auditor.py::
        EnvironmentAuditor._get_redis_client_for_hash_chain`` (Strategy 3)
  4. ``adapters/audit/redis_buffer.py::create_redis_audit_buffer``
  5. ``adapters/airgap/factory.py::_create_redis_adapter``
  6. ``adapters/redis/__init__.py::_try_acquire_redis_client``
        (Strategies 3 + 4)

Sites covered elsewhere (kept as cross-reference):
  - ``core/tiered_redis.py`` — ``tests/unit/core/test_tiered_redis.py``
    (TestTieredRedisProviderGetRedis)
  - ``adapters/redis/connection_factory.py`` — ``test_connection_factory.py``
    (URL-scheme dispatch is the Factory's own contract, not a callsite)
  - ``audit/checkpoint/redis_storage.py`` — Optional DI via ``get_redis_client()``
    (D1 indirection — Factory invocation lives in ``adapters/redis/__init__``,
    asserted in site #6 below)
  - ``api/django/rate_limit/redis_health_checker.py`` — D2 documented exception,
    Factory bypass is intentional and out of scope.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

SENTINEL_URL = "redis+sentinel://mymaster@s1:26379,s2:26379/0"
STANDALONE_URL = "redis://host:6379/0"


@pytest.fixture
def factory_spy():
    """Yield a mock factory + create() spy patched into connection_factory module.

    Callsites import ``get_redis_connection_factory`` from
    ``baldur.adapters.redis.connection_factory`` inside the function body, so
    patching the source module's attribute intercepts every callsite.
    """
    with patch(
        "baldur.adapters.redis.connection_factory.get_redis_connection_factory"
    ) as mock_get_factory:
        mock_factory = MagicMock()
        mock_client = MagicMock()
        # ping() is invoked by sites #4, #5, #2 — must succeed
        mock_client.ping.return_value = True
        mock_factory.create.return_value = mock_client
        mock_get_factory.return_value = mock_factory
        yield mock_factory


# ===========================================================================
# Contract: each callsite routes Sentinel URL through the Factory
# ===========================================================================


class TestCallsiteFactoryRoutingContract:
    """#422: each refactored callsite must route Redis URLs through the Factory.

    Hardcoded assertion: factory.create is invoked with the exact URL passed
    in by the caller. The Factory's own dispatch (Sentinel/Cluster/Standalone)
    is contract-verified in test_connection_factory.py — this class only
    proves the URL reaches the Factory at all.
    """

    def test_audit_config_get_redis_client_routes_sentinel_url(self, factory_spy):
        """audit/config.py: AuditConfig.get_redis_client → Factory.create(url)."""
        from baldur.audit.config import AuditConfig

        config = AuditConfig(
            hash_chain_distributed=True,
            hash_chain_redis_url=SENTINEL_URL,
        )

        config.get_redis_client()

        factory_spy.create.assert_called_once_with(SENTINEL_URL)

    def test_audit_config_skips_factory_when_distributed_disabled(self, factory_spy):
        """audit/config.py: distributed=False short-circuits before Factory call."""
        from baldur.audit.config import AuditConfig

        config = AuditConfig(
            hash_chain_distributed=False,
            hash_chain_redis_url=SENTINEL_URL,
        )

        result = config.get_redis_client()

        assert result is None
        factory_spy.create.assert_not_called()

    def test_state_backend_initialize_routes_sentinel_url(self, factory_spy):
        """core/state_backend.py: RedisStateBackend.__init__ → Factory.create(url, decode_responses=True)."""
        from baldur.core.state_backend import RedisStateBackend

        RedisStateBackend(redis_url=SENTINEL_URL)

        factory_spy.create.assert_called_once_with(SENTINEL_URL, decode_responses=True)

    def test_redis_audit_buffer_routes_sentinel_url(self, factory_spy):
        """adapters/audit/redis_buffer.py: create_redis_audit_buffer → Factory.create(url)."""
        from baldur.adapters.audit.redis_buffer import create_redis_audit_buffer

        buffer = create_redis_audit_buffer(redis_url=SENTINEL_URL)

        factory_spy.create.assert_called_once_with(SENTINEL_URL)
        assert buffer is not None

    def test_airgap_factory_routes_sentinel_url(self, factory_spy, monkeypatch):
        """adapters/airgap/factory.py: _create_redis_adapter → Factory.create(url)."""
        monkeypatch.setenv("BALDUR_AIRGAP_REDIS_URL", SENTINEL_URL)
        from baldur.adapters.airgap.factory import _create_redis_adapter

        adapter = _create_redis_adapter()

        factory_spy.create.assert_called_once_with(SENTINEL_URL)
        assert adapter is not None

    def test_env_auditor_strategy_3_routes_sentinel_url(self, factory_spy):
        """env_auditor.py Strategy 3: Django BALDUR_REDIS_URL → Factory.create(url).

        Strategies 1 (ResilientStorageBackend) and 2 (django_redis) must fail
        for Strategy 3 to fire. We bypass Strategy 1 by raising and Strategy
        2 by raising on import.
        """
        from baldur.adapters.django.startup.env_auditor import EnvironmentAuditor

        django_settings_stub = MagicMock()
        django_settings_stub.BALDUR_REDIS_URL = SENTINEL_URL

        with (
            patch(
                "baldur.adapters.resilient.backend.ResilientStorageBackend",
                side_effect=RuntimeError("force strategy 1 to fail"),
            ),
            patch.dict("sys.modules", {"django_redis": None}),
            patch(
                "baldur.adapters.django.startup.env_auditor.settings",
                django_settings_stub,
            ),
        ):
            EnvironmentAuditor._get_redis_client_for_hash_chain()

        factory_spy.create.assert_called_once_with(SENTINEL_URL)

    def test_acquire_strategy_3_routes_sentinel_url(self, factory_spy):
        """adapters/redis/__init__.py Strategy 3: Django settings → Factory.create(url).

        Forces Strategy 1 (ResilientStorageBackend) and Strategy 2 (django_redis)
        to fail so Strategy 3 (Django settings.BALDUR_REDIS_URL) fires.
        """
        from baldur.adapters.redis import _try_acquire_redis_client

        django_settings_stub = MagicMock()
        django_settings_stub.BALDUR_REDIS_URL = SENTINEL_URL
        django_conf_stub = MagicMock()
        django_conf_stub.settings = django_settings_stub

        with (
            patch(
                "baldur.adapters.resilient.backend.ResilientStorageBackend",
                side_effect=RuntimeError("force strategy 1 to fail"),
            ),
            patch.dict(
                "sys.modules",
                {"django_redis": None, "django.conf": django_conf_stub},
            ),
        ):
            _try_acquire_redis_client()

        factory_spy.create.assert_called_once_with(SENTINEL_URL)

    def test_acquire_strategy_4_routes_sentinel_url_via_env(
        self, factory_spy, monkeypatch
    ):
        """adapters/redis/__init__.py Strategy 4: REDIS_URL env var → Factory.create(url).

        Forces Strategies 1-3 to miss so Strategy 4 (env var fallback) fires.
        """
        from baldur.adapters.redis import _try_acquire_redis_client

        monkeypatch.setenv("REDIS_URL", SENTINEL_URL)
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)

        django_settings_stub = MagicMock(spec=[])  # no BALDUR_REDIS_URL attribute
        django_conf_stub = MagicMock()
        django_conf_stub.settings = django_settings_stub

        with (
            patch(
                "baldur.adapters.resilient.backend.ResilientStorageBackend",
                side_effect=RuntimeError("force strategy 1 to fail"),
            ),
            patch.dict(
                "sys.modules",
                {"django_redis": None, "django.conf": django_conf_stub},
            ),
        ):
            _try_acquire_redis_client()

        factory_spy.create.assert_called_once_with(SENTINEL_URL)


# ===========================================================================
# Behavior: callsites pass URL opaquely (Standalone parity)
# ===========================================================================


class TestCallsiteUrlOpacityBehavior:
    """Callsites must not parse or rewrite the URL — Factory is the sole arbiter.

    Verifies parity between Sentinel and Standalone URLs at every site:
    if the URL flows through unchanged for one scheme, the same call shape
    holds for the other. This guards against regressions where a caller
    short-circuits (e.g. parses the URL itself, or strips the scheme).
    """

    @pytest.mark.parametrize("url", [SENTINEL_URL, STANDALONE_URL])
    def test_audit_config_url_opaque(self, factory_spy, url):
        from baldur.audit.config import AuditConfig

        AuditConfig(
            hash_chain_distributed=True,
            hash_chain_redis_url=url,
        ).get_redis_client()

        assert factory_spy.create.call_args.args == (url,)

    @pytest.mark.parametrize("url", [SENTINEL_URL, STANDALONE_URL])
    def test_state_backend_url_opaque(self, factory_spy, url):
        from baldur.core.state_backend import RedisStateBackend

        RedisStateBackend(redis_url=url)

        assert factory_spy.create.call_args.args == (url,)

    @pytest.mark.parametrize("url", [SENTINEL_URL, STANDALONE_URL])
    def test_redis_audit_buffer_url_opaque(self, factory_spy, url):
        from baldur.adapters.audit.redis_buffer import create_redis_audit_buffer

        create_redis_audit_buffer(redis_url=url)

        assert factory_spy.create.call_args.args == (url,)

    @pytest.mark.parametrize("url", [SENTINEL_URL, STANDALONE_URL])
    def test_airgap_url_opaque(self, factory_spy, monkeypatch, url):
        monkeypatch.setenv("BALDUR_AIRGAP_REDIS_URL", url)
        from baldur.adapters.airgap.factory import _create_redis_adapter

        _create_redis_adapter()

        assert factory_spy.create.call_args.args == (url,)


# ===========================================================================
# Behavior: callsites use Factory, not redis.from_url directly
# ===========================================================================


class TestNoDirectFromUrlBehavior:
    """#422 D3: callsites must NOT call ``redis.from_url`` directly.

    Patches ``redis.from_url`` and asserts callsites never reach it. The
    Factory is allowed to call it internally for standalone URLs — this
    test patches the factory itself, so the Factory's internal use is
    masked. Any direct callsite use would still register on the patched
    ``redis.from_url``.
    """

    def test_audit_config_does_not_bypass_factory(self, factory_spy):
        from baldur.audit.config import AuditConfig

        with patch("redis.from_url", autospec=True) as mock_from_url:
            AuditConfig(
                hash_chain_distributed=True,
                hash_chain_redis_url=SENTINEL_URL,
            ).get_redis_client()

        mock_from_url.assert_not_called()

    def test_state_backend_does_not_bypass_factory(self, factory_spy):
        from baldur.core.state_backend import RedisStateBackend

        with patch("redis.from_url", autospec=True) as mock_from_url:
            RedisStateBackend(redis_url=SENTINEL_URL)

        mock_from_url.assert_not_called()

    def test_redis_audit_buffer_does_not_bypass_factory(self, factory_spy):
        from baldur.adapters.audit.redis_buffer import create_redis_audit_buffer

        with patch("redis.from_url", autospec=True) as mock_from_url:
            create_redis_audit_buffer(redis_url=SENTINEL_URL)

        mock_from_url.assert_not_called()

    def test_airgap_does_not_bypass_factory(self, factory_spy, monkeypatch):
        monkeypatch.setenv("BALDUR_AIRGAP_REDIS_URL", SENTINEL_URL)
        from baldur.adapters.airgap.factory import _create_redis_adapter

        with patch("redis.from_url", autospec=True) as mock_from_url:
            _create_redis_adapter()

        mock_from_url.assert_not_called()
