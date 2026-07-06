"""Unit tests for AuditConfig.get_redis_client canonical Redis-URL fallback (D2).

When the distributed hash chain is enabled and no per-feature override
(AUDIT_HASH_CHAIN_REDIS_URL) is set, the Redis client URL resolves from the
canonical BALDUR_REDIS_URL (RedisSettings.url) instead of a bare localhost
default. The bare REDIS_URL read was dropped.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.audit.config import AuditConfig
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

        mock_factory = MagicMock()
        mock_client = MagicMock()
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

        mock_factory = MagicMock()

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
