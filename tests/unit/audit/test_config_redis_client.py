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
from baldur.audit.config import (
    HASH_CHAIN_REDIS_URL_ENV,
    AuditConfig,
    create_hash_chain_redis_client,
    hash_chain_redis_url_is_named,
    resolve_hash_chain_redis_url,
)
from baldur.settings.redis import (
    DEFAULT_REDIS_URL,
    get_redis_settings,
    reset_redis_settings,
)


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


class TestHashChainRedisUrlGate:
    """``resolve_hash_chain_redis_url`` and its companion "was one named" gate.

    The two live in one module because a gate that answers a different
    question than the resolver is how a promotion lands on the localhost
    default: the entitlement hook infers a distributed chain only when
    somebody named the URL that chain will actually dial, so the gate has to
    read exactly the channels the resolver reads and no others.

    Verification techniques (per UNIT_TEST_GUIDELINES §8):
    - §8.1 Boundary (whitespace-only override, defaulted canonical URL).
    - §8.5 Dependency interaction (the gate does not delegate to the wider
      Redis-intent predicate).
    """

    def test_argument_wins_over_every_environment_channel(self, monkeypatch):
        monkeypatch.setenv("AUDIT_HASH_CHAIN_REDIS_URL", "redis://feature:6379/0")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://canonical:6379/0")
        reset_redis_settings()

        resolved = resolve_hash_chain_redis_url("redis://explicit:6379/0")

        assert resolved == "redis://explicit:6379/0"

    def test_feature_override_wins_over_the_canonical_url(self, monkeypatch):
        monkeypatch.setenv("AUDIT_HASH_CHAIN_REDIS_URL", "redis://feature:6379/0")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://canonical:6379/0")
        reset_redis_settings()

        assert resolve_hash_chain_redis_url() == "redis://feature:6379/0"

    def test_canonical_url_is_the_last_named_source(self, monkeypatch):
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://canonical:6379/0")
        reset_redis_settings()

        assert resolve_hash_chain_redis_url() == "redis://canonical:6379/0"

    def test_unconfigured_resolution_falls_back_to_the_settings_default(self):
        """The resolver hardcodes no URL of its own — what a zero-config
        process dials is the canonical setting's own default, which is why
        promoting onto it would be the framework talking to itself."""
        assert resolve_hash_chain_redis_url() == get_redis_settings().url

    def test_a_named_feature_override_counts_as_named(self, monkeypatch):
        monkeypatch.setenv("AUDIT_HASH_CHAIN_REDIS_URL", "redis://feature:6379/0")

        assert hash_chain_redis_url_is_named() is True

    def test_a_stated_canonical_url_counts_as_named(self, monkeypatch):
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://canonical:6379/0")
        reset_redis_settings()

        assert hash_chain_redis_url_is_named() is True

    def test_a_defaulted_canonical_url_is_not_named(self):
        """The field's default is an un-named localhost address. Counting it
        would promote a distributed chain on every entitled single-host
        install, pointed at a Redis nobody deployed."""
        assert get_redis_settings().url == DEFAULT_REDIS_URL
        assert hash_chain_redis_url_is_named() is False

    @pytest.mark.parametrize(
        "value",
        ["", "   ", "	"],
        ids=["empty", "spaces", "tab"],
    )
    def test_a_blank_feature_override_is_not_named(self, monkeypatch, value):
        """An exported-but-empty variable is a deployment template that did
        not get filled in, not an operator naming a server."""
        monkeypatch.setenv("AUDIT_HASH_CHAIN_REDIS_URL", value)
        reset_redis_settings()

        assert hash_chain_redis_url_is_named() is False

    def test_the_bare_redis_url_variable_does_not_count_as_named(self, monkeypatch):
        """``REDIS_URL`` is real Redis intent and the wider predicate counts
        it — but it is not a channel this resolver reads, so gating on it
        would promote the chain onto the localhost default."""
        from baldur.settings.redis import redis_explicitly_configured

        monkeypatch.setenv("REDIS_URL", "redis://bare:6379/0")
        reset_redis_settings()

        assert redis_explicitly_configured() is True
        assert resolve_hash_chain_redis_url() == DEFAULT_REDIS_URL
        assert hash_chain_redis_url_is_named() is False

    def test_the_gate_does_not_delegate_to_the_wider_intent_predicate(self):
        """A Django ``CACHES``-only deployment is the other widening case the
        gate has to stay narrower than: real Redis intent, on a channel the
        chain client cannot dial."""
        with patch(
            "baldur.settings.redis.redis_explicitly_configured",
            return_value=True,
        ):
            assert hash_chain_redis_url_is_named() is False

    def test_unreadable_settings_report_not_named_rather_than_raising(self):
        """This gate runs inside the entitlement hook. Failing closed here
        costs a promotion; raising would cost audit activation."""
        with patch(
            "baldur.settings.redis.get_redis_settings",
            side_effect=RuntimeError("settings blew up"),
        ):
            assert hash_chain_redis_url_is_named() is False

    def test_env_var_name_is_the_published_one(self):
        """Hardcoded so a rename shows up here rather than as a silently
        ignored operator override."""
        assert HASH_CHAIN_REDIS_URL_ENV == "AUDIT_HASH_CHAIN_REDIS_URL"
