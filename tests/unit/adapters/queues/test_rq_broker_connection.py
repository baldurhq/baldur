"""The RQ broker connection takes routing from the factory, nothing else.

The broker URL comes from a constructor argument, Django ``settings.REDIS_URL``
or ``os.environ["REDIS_URL"]`` — channels that commonly name a *different*
instance from the framework's own Redis. Building the client through the shared
connection factory buys bounded socket budgets and Sentinel/Cluster routing;
inheriting ``RedisSettings`` along with them would be a defect in three
separate ways, and each is asserted on its own here:

- Credentials: AUTH sent to an unauthenticated broker fails every command with
  ``ResponseError``, which the enqueue path does not treat as retryable.
- Timeouts: an operator who shortens the framework's socket budget to make its
  own Redis fail fast must not thereby impose that budget on the broker.
- ``retry_on_timeout``: rq assigns the job id client-side before the write, so
  replaying a timed-out LPUSH would push the same id twice.

The connection had no socket timeout at all before, so the budgets asserted
here are also the fix for an enqueue that could block on the OS TCP timeout.
"""

from __future__ import annotations

import pytest

from baldur.adapters.queues.rq_adapter import (
    _BROKER_CONNECT_TIMEOUT_SECONDS,
    _BROKER_SOCKET_TIMEOUT_SECONDS,
    RQTaskAdapter,
)
from baldur.adapters.redis.connection_factory import reset_redis_connection_factory
from baldur.settings.redis import get_redis_settings
from baldur.settings.root import reset_config

_BROKER_URL = "redis://broker-host:6379/0"

# Distinct from every shipped default AND from the broker constants, so a
# value that leaked across the channel boundary is visible as itself.
_FRAMEWORK_PASSWORD = "framework-only-secret"
_FRAMEWORK_USERNAME = "framework-acl-user"
_FRAMEWORK_SOCKET_TIMEOUT = 1.5
_FRAMEWORK_CONNECT_TIMEOUT = 2.5


@pytest.fixture
def framework_redis_fully_configured(monkeypatch):
    """The framework's own Redis, configured with credentials and budgets.

    Set through the environment rather than by constructing a settings object:
    the boundary under test is what an operator's ``BALDUR_REDIS_*`` variables
    reach, and the connection factory singleton reads the settings at its own
    construction, so both caches are dropped around the case.
    """
    monkeypatch.setenv("BALDUR_REDIS_PASSWORD", _FRAMEWORK_PASSWORD)
    monkeypatch.setenv("BALDUR_REDIS_USERNAME", _FRAMEWORK_USERNAME)
    monkeypatch.setenv("BALDUR_REDIS_SOCKET_TIMEOUT", str(_FRAMEWORK_SOCKET_TIMEOUT))
    monkeypatch.setenv(
        "BALDUR_REDIS_SOCKET_CONNECT_TIMEOUT", str(_FRAMEWORK_CONNECT_TIMEOUT)
    )
    monkeypatch.setenv("BALDUR_REDIS_RETRY_ON_TIMEOUT", "true")
    reset_config()
    reset_redis_connection_factory()

    settings = get_redis_settings()
    # Precondition: without this the assertions below would pass against a
    # settings object that never carried the values they claim to exclude.
    assert settings.password == _FRAMEWORK_PASSWORD
    assert settings.socket_timeout == _FRAMEWORK_SOCKET_TIMEOUT
    assert settings.retry_on_timeout is True

    yield settings

    reset_redis_connection_factory()
    reset_config()


@pytest.fixture
def broker_connection_kwargs(framework_redis_fully_configured):
    """The connection kwargs of the client the adapter actually keeps.

    Read off the real connection pool rather than off a mocked factory call:
    redis-py builds a standalone client without opening a socket, so this is
    the same object an enqueue would use.
    """
    adapter = RQTaskAdapter(redis_url=_BROKER_URL)
    return adapter.connection.connection_pool.connection_kwargs


class TestRQBrokerConnectionContract:
    """What the broker client is, and is not, built with."""

    def test_broker_connection_carries_no_framework_password(
        self, broker_connection_kwargs
    ):
        """The framework's AUTH never travels to a foreign broker."""
        assert "password" not in broker_connection_kwargs

    def test_broker_connection_carries_no_framework_username(
        self, broker_connection_kwargs
    ):
        """Same boundary for the ACL username."""
        assert "username" not in broker_connection_kwargs

    def test_broker_connection_uses_the_adapter_socket_timeout(
        self, broker_connection_kwargs
    ):
        """The read budget is the adapter's own constant, not the setting."""
        assert broker_connection_kwargs["socket_timeout"] == (
            _BROKER_SOCKET_TIMEOUT_SECONDS
        )
        assert broker_connection_kwargs["socket_timeout"] != _FRAMEWORK_SOCKET_TIMEOUT

    def test_broker_connection_uses_the_adapter_connect_timeout(
        self, broker_connection_kwargs
    ):
        """Data-path class, not the admission-probe class — this is an enqueue
        client, and it never serves a blocking dequeue."""
        assert broker_connection_kwargs["socket_connect_timeout"] == (
            _BROKER_CONNECT_TIMEOUT_SECONDS
        )
        assert (
            broker_connection_kwargs["socket_connect_timeout"]
            != _FRAMEWORK_CONNECT_TIMEOUT
        )

    def test_broker_connection_never_retries_a_timed_out_write(
        self, broker_connection_kwargs
    ):
        """False even though the framework setting says True.

        rq assigns the job id before the write, so a replayed LPUSH pushes the
        same id twice.
        """
        assert broker_connection_kwargs["retry_on_timeout"] is False

    def test_broker_connection_routes_to_the_url_it_was_given(
        self, broker_connection_kwargs
    ):
        """Negative guard: the framework's own URL is a different channel."""
        assert broker_connection_kwargs["host"] == "broker-host"


class TestRQBrokerConnectionBehavior:
    """How the connection is acquired."""

    def test_connection_is_built_once_and_memoized(
        self, framework_redis_fully_configured
    ):
        """Each enqueue must not re-resolve the URL or rebuild the pool."""
        adapter = RQTaskAdapter(redis_url=_BROKER_URL)

        assert adapter.connection is adapter.connection
