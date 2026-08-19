"""Unit tests for the distributed-hash-chain Redis client resolution.

Two layers:

1. ``AuditConfig.get_redis_client`` — the canonical Redis-URL fallback: when
   the distributed hash chain is enabled and no per-feature override
   (AUDIT_HASH_CHAIN_REDIS_URL) is set, the URL resolves from the canonical
   BALDUR_REDIS_URL (RedisSettings.url) instead of a bare localhost default.
   The bare REDIS_URL read was dropped.
2. ``create_hash_chain_redis_client`` — the resolution extracted out of that
   method so the adapter factory can call it too. The factory previously
   probed a ``ProviderRegistry.get_cache_adapter`` attribute that exists
   nowhere, so the client was always ``None`` and the distributed chain
   silently ran local no matter what the operator set.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import redis

from baldur.adapters.redis.connection_factory import RedisConnectionFactory
from baldur.audit.config import AuditConfig, create_hash_chain_redis_client
from baldur.settings.redis import reset_redis_settings


@pytest.fixture(autouse=True)
def _isolate_redis_env(monkeypatch):
    """Start each test with all Redis-URL env sources cleared."""
    monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
    monkeypatch.delenv("AUDIT_HASH_CHAIN_REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    reset_redis_settings()
    yield
    reset_redis_settings()


class TestGetRedisClientCanonicalFallback:
    """D2: distributed-hash-chain Redis client resolves via BALDUR_REDIS_URL."""

    def test_get_redis_client_returns_none_when_distributed_disabled(self):
        # Given: distributed hash chain disabled
        config = AuditConfig(hash_seed="test-seed", hash_chain_distributed=False)

        # When/Then: no factory is consulted, returns None
        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory"
        ) as mock_get_factory:
            assert config.get_redis_client() is None
        mock_get_factory.assert_not_called()

    def test_hash_chain_distributed_resolves_baldur_redis_url_fallback(
        self, monkeypatch
    ):
        # Given: distributed on, no per-feature override, only BALDUR_REDIS_URL set
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://canonical-host:6379/3")
        reset_redis_settings()
        config = AuditConfig(hash_seed="test-seed", hash_chain_distributed=True)

        mock_factory = MagicMock(spec=RedisConnectionFactory)
        mock_client = MagicMock(spec=redis.Redis)
        mock_factory.create.return_value = mock_client

        # When
        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            return_value=mock_factory,
        ):
            client = config.get_redis_client()

        # Then: client created against the BALDUR_REDIS_URL value
        assert client is mock_client
        mock_factory.create.assert_called_once_with("redis://canonical-host:6379/3")

    def test_hash_chain_per_feature_override_wins_over_fallback(self, monkeypatch):
        # Given: both an explicit AUDIT_HASH_CHAIN_REDIS_URL and BALDUR_REDIS_URL
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://canonical-host:6379/3")
        reset_redis_settings()
        config = AuditConfig(
            hash_seed="test-seed",
            hash_chain_distributed=True,
            hash_chain_redis_url="redis://override-host:6379/9",
        )

        mock_factory = MagicMock(spec=RedisConnectionFactory)

        # When
        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            return_value=mock_factory,
        ):
            config.get_redis_client()

        # Then: the per-feature override wins
        mock_factory.create.assert_called_once_with("redis://override-host:6379/9")


class TestAuditConfigHashChainRedisUrlContract:
    """D2 contract: hash_chain_redis_url default no longer reads bare REDIS_URL."""

    def test_hash_chain_redis_url_default_is_none(self, monkeypatch):
        """Default is None (opt-in) — even when a bare REDIS_URL is present."""
        monkeypatch.delenv("AUDIT_HASH_CHAIN_REDIS_URL", raising=False)
        monkeypatch.setenv("REDIS_URL", "redis://bare-host:6379/0")
        config = AuditConfig(hash_seed="test-seed")
        assert config.hash_chain_redis_url is None

    def test_hash_chain_redis_url_reads_per_feature_env(self, monkeypatch):
        """The per-feature AUDIT_HASH_CHAIN_REDIS_URL override is still honored."""
        monkeypatch.setenv("AUDIT_HASH_CHAIN_REDIS_URL", "redis://feature-host:6379/1")
        config = AuditConfig(hash_seed="test-seed")
        assert config.hash_chain_redis_url == "redis://feature-host:6379/1"


class TestCreateHashChainRedisClientBehavior:
    """The extracted helper answers *which* client, never *whether*.

    ``AuditConfig.get_redis_client`` keeps its own gate and delegates here;
    the adapter factory gates on ``AuditSettings.distributed_hash_chain``
    and calls the helper directly. So the helper itself must build a client
    unconditionally and resolve the URL in a fixed precedence order.
    """

    def test_argument_wins_over_both_env_sources(self, monkeypatch):
        # Given: every source populated with a distinguishable URL
        monkeypatch.setenv("AUDIT_HASH_CHAIN_REDIS_URL", "redis://feature-host:6379/1")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://canonical-host:6379/3")
        reset_redis_settings()
        mock_factory = MagicMock(spec=RedisConnectionFactory)

        # When
        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            return_value=mock_factory,
        ):
            create_hash_chain_redis_client("redis://explicit-host:6379/7")

        # Then
        mock_factory.create.assert_called_once_with("redis://explicit-host:6379/7")

    def test_per_feature_env_wins_when_argument_omitted(self, monkeypatch):
        # Given: no argument, both env sources set
        monkeypatch.setenv("AUDIT_HASH_CHAIN_REDIS_URL", "redis://feature-host:6379/1")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://canonical-host:6379/3")
        reset_redis_settings()
        mock_factory = MagicMock(spec=RedisConnectionFactory)

        # When — the factory path that passes no argument
        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            return_value=mock_factory,
        ):
            create_hash_chain_redis_client()

        # Then
        mock_factory.create.assert_called_once_with("redis://feature-host:6379/1")

    def test_canonical_url_used_when_no_argument_and_no_per_feature_env(
        self, monkeypatch
    ):
        # Given: only BALDUR_REDIS_URL
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://canonical-host:6379/3")
        reset_redis_settings()
        mock_factory = MagicMock(spec=RedisConnectionFactory)
        mock_client = MagicMock(spec=redis.Redis)
        mock_factory.create.return_value = mock_client

        # When
        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            return_value=mock_factory,
        ):
            client = create_hash_chain_redis_client()

        # Then
        assert client is mock_client
        mock_factory.create.assert_called_once_with("redis://canonical-host:6379/3")

    def test_builds_a_client_without_consulting_any_distributed_switch(
        self, monkeypatch
    ):
        """The helper has no gate of its own — callers own that decision.

        Asserted with the settings switch OFF: a helper that re-checked it
        would return ``None`` here and every factory call would silently
        fall back to the local chain, which is the exact defect the dead
        ``get_cache_adapter`` duck-probe produced.
        """
        # Given
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://canonical-host:6379/3")
        monkeypatch.setenv("BALDUR_AUDIT_DISTRIBUTED_HASH_CHAIN", "false")
        reset_redis_settings()
        mock_factory = MagicMock(spec=RedisConnectionFactory)
        mock_client = MagicMock(spec=redis.Redis)
        mock_factory.create.return_value = mock_client

        # When
        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            return_value=mock_factory,
        ):
            client = create_hash_chain_redis_client()

        # Then
        assert client is mock_client

    def test_import_error_returns_none_sentinel(self, monkeypatch):
        """Sentinel contract: callers read ``None`` as "use the local chain"."""
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://canonical-host:6379/3")
        reset_redis_settings()

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            side_effect=ImportError("redis not installed"),
        ):
            assert create_hash_chain_redis_client() is None

    def test_factory_failure_returns_none_sentinel(self, monkeypatch):
        """Any other failure is also absorbed — never raised at the caller."""
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://canonical-host:6379/3")
        reset_redis_settings()
        mock_factory = MagicMock(spec=RedisConnectionFactory)
        mock_factory.create.side_effect = RuntimeError("connection refused")

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            return_value=mock_factory,
        ):
            assert create_hash_chain_redis_client() is None


class TestAuditConfigDelegatesToHelperBehavior:
    """``AuditConfig.get_redis_client`` keeps the gate, delegates the rest."""

    def test_disabled_config_does_not_call_the_helper(self):
        config = AuditConfig(hash_seed="test-seed", hash_chain_distributed=False)

        with patch("baldur.audit.config.create_hash_chain_redis_client") as mock_helper:
            assert config.get_redis_client() is None

        mock_helper.assert_not_called()

    def test_enabled_config_forwards_its_per_feature_url(self):
        config = AuditConfig(
            hash_seed="test-seed",
            hash_chain_distributed=True,
            hash_chain_redis_url="redis://override-host:6379/9",
        )
        # An identity token, not a collaborator — nothing calls it.
        sentinel = object()

        with patch(
            "baldur.audit.config.create_hash_chain_redis_client",
            return_value=sentinel,
        ) as mock_helper:
            client = config.get_redis_client()

        assert client is sentinel
        mock_helper.assert_called_once_with("redis://override-host:6379/9")
