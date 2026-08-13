"""The RQ broker channel against a real, unauthenticated Redis.

The offline contract proves the connection carries no ``password`` key. It
cannot prove the consequence, which is what an operator actually experiences:
a Redis with no ``requirepass`` rejects the AUTH during redis-py's connection
handshake, so ``AuthenticationError`` comes out of *every* command rather than
a single failed one. Only a real server produces it — a mock can be told to
raise anything, including the wrong class, which is how the wrong class ended
up named in the source comments this file corrected.

The pairing is what makes the guard falsifiable: the same server, the same
settings, and a client built *with* settings auth injection must fail. Without
that half, "the enqueue worked" would also pass on a run where the password
was never set in the first place.

The failure's *class* is load-bearing, not incidental. redis-py's
``AuthenticationError`` subclasses redis-py's own ``ConnectionError``, not the
builtin one, so it does not match the enqueue path's retryable tuple and the
leak surfaces immediately instead of three backoff rounds later.

The broker is a foreign channel by construction here: it is named by
constructor argument, and the framework's own ``BALDUR_REDIS_*`` variables
point nowhere near it.
"""

from __future__ import annotations

import pytest
import redis

from baldur.adapters.queues.rq_adapter import RQTaskAdapter
from baldur.adapters.redis.connection_factory import (
    RedisConnectionFactory,
    reset_redis_connection_factory,
)
from baldur.settings.redis import get_redis_settings
from baldur.settings.root import reset_config

pytestmark = pytest.mark.requires_redis

# A credential that belongs to the framework's own Redis and must not travel.
_FRAMEWORK_PASSWORD = "framework-only-secret"

_QUEUE_NAME = "baldur-broker-isolation"

# The builtin classes ``RQTaskAdapter.enqueue`` retries its Redis call on.
# Restated here on purpose: the claim under test is that the auth failure is
# outside this set, and a test that read the set from production could not
# fail when production widened it.
_ENQUEUE_RETRYABLE_CLASSES = (ConnectionError, OSError, TimeoutError)


def marker_task(value: int) -> int:
    """Module-level so rq can serialize the reference it enqueues."""
    return value * 2


@pytest.fixture
def framework_password_set(monkeypatch, redis_url):
    """The framework's Redis has a password; the broker is a different server.

    Yields the broker URL. The settings root and the factory singleton are
    both dropped so the password is really in play — the factory reads the
    settings at its own construction.
    """
    monkeypatch.setenv("BALDUR_REDIS_PASSWORD", _FRAMEWORK_PASSWORD)
    reset_config()
    reset_redis_connection_factory()

    # Precondition: without this the "enqueue succeeded" assertion below would
    # also pass on a run where no credential existed to leak.
    assert get_redis_settings().password == _FRAMEWORK_PASSWORD

    yield redis_url

    reset_redis_connection_factory()
    reset_config()


@pytest.fixture
def unauthenticated_broker(redis_test_client, framework_password_set):
    """Confirm the server really has no password before relying on it.

    A server that *did* require one would accept the leaked AUTH and the
    guard's negative half would pass for the wrong reason.
    """
    configured = redis_test_client.config_get("requirepass")
    if configured.get("requirepass"):
        pytest.skip("this Redis requires a password; the guard needs one that does not")
    return framework_password_set


class TestRQBrokerCredentialScopeBehavior:
    """A framework credential must not reach a broker that never asked for one."""

    def test_enqueue_succeeds_against_an_unauthenticated_broker(
        self, unauthenticated_broker
    ):
        """The whole point: the write lands, with a password set in settings."""
        adapter = RQTaskAdapter(
            redis_url=unauthenticated_broker, default_queue=_QUEUE_NAME
        )
        adapter.task(name="marker_task")(marker_task)

        job_id = adapter.enqueue("marker_task", args=(21,))

        assert job_id

    def test_enqueued_job_is_readable_back_off_the_broker(
        self, unauthenticated_broker, redis_test_client
    ):
        """Not merely "no exception" — the job is on the queue afterwards.

        Reads through a client this test owns, so the assertion does not
        depend on the same connection that wrote it.
        """
        adapter = RQTaskAdapter(
            redis_url=unauthenticated_broker, default_queue=_QUEUE_NAME
        )
        adapter.task(name="marker_task")(marker_task)

        job_id = adapter.enqueue("marker_task", args=(21,))

        assert redis_test_client.exists(f"rq:job:{job_id}") == 1

    def test_a_client_carrying_the_settings_password_is_rejected(
        self, unauthenticated_broker
    ):
        """The negative half — proof the credential would have broken the lane.

        Same server, same settings, auth injection left at its default. The
        server rejects the AUTH, which is exactly the failure the broker
        channel opts out of.
        """
        factory = RedisConnectionFactory(settings=get_redis_settings())

        leaky_client = factory.create(unauthenticated_broker)

        with pytest.raises(redis.exceptions.AuthenticationError):
            leaky_client.ping()

    def test_leaked_credential_fails_the_enqueue_outright(self, unauthenticated_broker):
        """The pre-change shape, driven through the adapter's own enqueue.

        Asserted at the enqueue rather than at a bare ping because that is
        where an operator meets it, and because the enqueue wraps its call in
        a retry ladder — reaching the caller at all is the claim.
        """
        factory = RedisConnectionFactory(settings=get_redis_settings())
        adapter = RQTaskAdapter(
            redis_url=unauthenticated_broker, default_queue=_QUEUE_NAME
        )
        adapter._connection = factory.create(unauthenticated_broker)
        adapter.task(name="marker_task")(marker_task)

        with pytest.raises(redis.exceptions.AuthenticationError):
            adapter.enqueue("marker_task", args=(21,))

    def test_leaked_credential_is_not_swallowed_by_the_enqueue_retry_ladder(
        self, unauthenticated_broker
    ):
        """The class matters: a retryable one would cost the ladder first.

        redis-py's ``AuthenticationError`` subclasses redis-py's own
        ``ConnectionError``, so it does not match the builtin classes the
        enqueue path retries on. Asserted structurally rather than by clock —
        the property is which base classes the error has, and a timing bound
        would only observe that property indirectly.
        """
        factory = RedisConnectionFactory(settings=get_redis_settings())
        adapter = RQTaskAdapter(
            redis_url=unauthenticated_broker, default_queue=_QUEUE_NAME
        )
        adapter._connection = factory.create(unauthenticated_broker)
        adapter.task(name="marker_task")(marker_task)

        with pytest.raises(redis.exceptions.AuthenticationError) as raised:
            adapter.enqueue("marker_task", args=(21,))

        assert not isinstance(raised.value, _ENQUEUE_RETRYABLE_CLASSES)
