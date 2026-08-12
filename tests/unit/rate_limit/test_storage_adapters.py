"""Rate-limit storage: monotonic cooldown writes and strict reads (#754 D2).

A shared outbound cooldown is written by every worker that observes a 429, and
those writes are not ordered. Under the previous last-writer-wins ``set_cooldown``
a worker whose provider sent no ``Retry-After`` could replace a peer's honored
long cooldown with its own ~10-60 s ladder delay, and the whole fleet resumed
before the provider's stated earliest time. ``extend_cooldown`` merges by ``max``,
which makes the write commutative and idempotent so the order stops mattering.

Coverage split: the four implementations whose merge runs in Python are pinned
here. The Redis adapter's *scripted* path performs its merge and derives its key
TTL inside a Lua script, which no client double can execute — its call shape and
reply handling are pinned here, and the server-side semantics in
``tests/integration/redis/test_rate_limit_extend_cooldown_redis.py``.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.rate_limit.database_adapter import DatabaseRateLimitStorage
from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage
from baldur.adapters.rate_limit.redis_adapter import (
    _COOLDOWN_TTL_MARGIN_SECONDS,
    _EXTEND_COOLDOWN_SCRIPT,
    RedisRateLimitStorage,
)
from baldur.interfaces.rate_limit_storage import (
    RateLimitState,
    RateLimitStorageInterface,
    RateLimitStorageType,
    RateLimitStorageUnavailableError,
)

# The adapter's own default when settings are unreachable, and the shipped
# default of the settings field it reads — the two agree, so either resolution
# order yields this.
CONFIGURED_REDIS_TTL = 3600

# Long enough that a covering TTL must exceed CONFIGURED_REDIS_TTL, which is the
# regression these tests exist for: a key evicted before the cooldown it carries.
HONORED_HEADER_COOLDOWN_SECONDS = 7200
LADDER_COOLDOWN_SECONDS = 10


# =============================================================================
# Doubles
# =============================================================================


class _MinimalStorage(RateLimitStorageInterface):
    """A bring-your-own implementation: only the abstract methods, nothing else.

    Its ``extend_cooldown`` and ``get_state_strict`` are therefore the ABC's
    inherited defaults, which is exactly the surface a third-party adapter gets
    for free and the reason both methods are non-abstract.
    """

    def __init__(self) -> None:
        self._states: dict[str, RateLimitState] = {}
        self.set_cooldown_calls: list[tuple[str, float, int | None]] = []

    @property
    def storage_type(self) -> RateLimitStorageType:
        return RateLimitStorageType.MEMORY

    def get_state(self, key: str) -> RateLimitState:
        return self._states.get(key, RateLimitState(key=key))

    def set_cooldown(
        self, key: str, cooldown_until: float, ttl: int | None = None
    ) -> None:
        self.set_cooldown_calls.append((key, cooldown_until, ttl))
        state = self._states.setdefault(key, RateLimitState(key=key))
        state.cooldown_until = cooldown_until

    def increment_consecutive_429s(self, key: str) -> int:
        state = self._states.setdefault(key, RateLimitState(key=key))
        state.consecutive_429s += 1
        return state.consecutive_429s

    def reset_consecutive_429s(self, key: str) -> None:
        if key in self._states:
            self._states[key].consecutive_429s = 0

    def clear(self, key: str) -> None:
        self._states.pop(key, None)


class _FakeRepository:
    """Dict-backed repository for DatabaseRateLimitStorage."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.readable = True

    def get(self, key: str) -> dict | None:
        if not self.readable:
            raise ConnectionError("database connection lost")
        return self.rows.get(key)

    def upsert(self, rate_limit_key: str, data: dict) -> None:
        self.rows.setdefault(rate_limit_key, {}).update(data)

    def increment(self, key: str, field: str) -> int:
        row = self.rows.setdefault(key, {})
        row[field] = row.get(field, 0) + 1
        return row[field]

    def update(self, key: str, data: dict) -> None:
        self.rows.setdefault(key, {}).update(data)

    def delete(self, key: str) -> None:
        self.rows.pop(key, None)


class _FakePipeline:
    """Queue-and-replay pipeline over ``_FakeRedis``, like redis-py's."""

    def __init__(self, client: _FakeRedis) -> None:
        self._client = client
        self._queued: list[tuple[str, tuple, dict]] = []

    def get(self, name: str) -> _FakePipeline:
        self._queued.append(("get", (name,), {}))
        return self

    def set(self, name: str, value: str, ex: int | None = None) -> _FakePipeline:
        self._queued.append(("set", (name, value), {"ex": ex}))
        return self

    def execute(self) -> list:
        queued, self._queued = self._queued, []
        return [getattr(self._client, op)(*args, **kw) for op, args, kw in queued]


class _FakeRedis:
    """Redis client double: real string storage, optional scripting, optional faults."""

    def __init__(
        self,
        *,
        scripting: bool = True,
        readable: bool = True,
        writable: bool = True,
    ) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self.scripting = scripting
        self.readable = readable
        self.writable = writable
        self.script_attempts = 0

    def ping(self) -> bool:
        return True

    def get(self, name: str) -> str | None:
        if not self.readable:
            raise ConnectionError("redis read failed")
        return self.values.get(name)

    def set(self, name: str, value: str, ex: int | None = None) -> bool:
        if not self.writable:
            raise ConnectionError("redis write failed")
        self.values[name] = value
        if ex is not None:
            self.expirations[name] = ex
        return True

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    def script_load(self, body: str) -> str:
        self.script_attempts += 1
        if not self.scripting:
            raise RuntimeError("ERR unknown command 'SCRIPT'")
        return "deadbeef"

    def evalsha(self, sha: str, numkeys: int, *args: object) -> str:
        self.script_attempts += 1
        if not self.scripting:
            raise RuntimeError("ERR unknown command 'EVALSHA'")
        raise AssertionError("scripted execution is asserted through the registry")

    def eval(self, body: str, numkeys: int, *args: object) -> str:
        self.script_attempts += 1
        raise RuntimeError("ERR unknown command 'EVAL'")


def _make_redis_storage(**client_kwargs) -> tuple[RedisRateLimitStorage, _FakeRedis]:
    """A Redis adapter over the client double, with the shipped TTL."""
    client = _FakeRedis(**client_kwargs)
    return RedisRateLimitStorage(client, ttl=CONFIGURED_REDIS_TTL), client


def _cooldown_key(key: str) -> str:
    return f"{RedisRateLimitStorage.KEY_PREFIX}:{key}:cooldown_until"


# =============================================================================
# extend_cooldown — the monotonic contract, across every Python-side merge
# =============================================================================


@pytest.fixture(
    params=["abc-default", "memory", "database", "redis-unscripted"],
)
def storage(request) -> RateLimitStorageInterface:
    """One storage per implementation whose max-merge runs in Python.

    ``redis-unscripted`` is the degraded path a Redis without scripting takes
    (no ``lupa`` in fakeredis, an ACL denying ``@scripting``, an EVAL-rejecting
    proxy). It is a shipped path, not a test artifact, and it must honor the
    same contract as the others.
    """
    if request.param == "abc-default":
        return _MinimalStorage()
    if request.param == "memory":
        # A cleanup interval far above any single test's operation count, so no
        # sweep runs mid-test and the assertions read only the merge.
        return InMemoryRateLimitStorage(cleanup_interval=10_000)
    if request.param == "database":
        repository = _FakeRepository()
        return DatabaseRateLimitStorage(repository_factory=lambda: repository)
    adapter, _client = _make_redis_storage(scripting=False)
    return adapter


class TestExtendCooldownContract:
    """``max(stored, candidate)`` — the same answer from every implementation."""

    def test_first_write_stores_and_returns_the_candidate(self, storage):
        """Boundary: with nothing stored, the candidate is the whole merge."""
        candidate = time.time() + LADDER_COOLDOWN_SECONDS

        effective = storage.extend_cooldown("payment_api", candidate)

        assert effective == candidate
        assert storage.get_state("payment_api").cooldown_until == candidate

    def test_a_shorter_candidate_never_shortens_a_live_cooldown(self, storage):
        """The headline regression: an honored header outlives a headerless 429.

        A peer honors ``Retry-After: 7200`` and a second worker's headerless 429
        lands moments later with a ~10 s ladder delay. Last-writer-wins stored
        the 10 s and released the fleet nearly two hours early.
        """
        now = time.time()
        honored = now + HONORED_HEADER_COOLDOWN_SECONDS
        storage.extend_cooldown("payment_api", honored)

        effective = storage.extend_cooldown(
            "payment_api", now + LADDER_COOLDOWN_SECONDS
        )

        assert effective == honored
        assert storage.get_state("payment_api").cooldown_until == honored

    def test_a_longer_candidate_moves_the_cooldown_later(self, storage):
        """The permitted direction: later is always accepted."""
        now = time.time()
        storage.extend_cooldown("payment_api", now + LADDER_COOLDOWN_SECONDS)

        honored = now + HONORED_HEADER_COOLDOWN_SECONDS
        effective = storage.extend_cooldown("payment_api", honored)

        assert effective == honored
        assert storage.get_state("payment_api").cooldown_until == honored

    def test_repeating_the_same_candidate_changes_nothing(self, storage):
        """Idempotency: a retried or duplicated write is indistinguishable from one."""
        candidate = time.time() + LADDER_COOLDOWN_SECONDS

        results = [storage.extend_cooldown("payment_api", candidate) for _ in range(5)]

        assert results == [candidate] * 5
        assert storage.get_state("payment_api").cooldown_until == candidate

    def test_write_order_does_not_change_the_outcome(self, storage):
        """Commutativity: this is what makes unordered fleet writes safe.

        Two workers write a long and a short cooldown with no coordination
        between them. Whichever lands second, the key ends up on the long one.
        """
        now = time.time()
        short = now + LADDER_COOLDOWN_SECONDS
        long_ = now + HONORED_HEADER_COOLDOWN_SECONDS

        storage.extend_cooldown("short_first", short)
        storage.extend_cooldown("short_first", long_)
        storage.extend_cooldown("long_first", long_)
        storage.extend_cooldown("long_first", short)

        assert storage.get_state("short_first").cooldown_until == long_
        assert storage.get_state("long_first").cooldown_until == long_

    def test_an_expired_stored_cooldown_does_not_block_a_new_one(self, storage):
        """Boundary: ``max(past, now + delay)`` is the new cooldown, never the past one.

        Monotonicity is a rule about a *live* cooldown. If an expiry already in
        the past could win the merge, a key would be permanently unable to enter
        a new cooldown after its first one lapsed.
        """
        now = time.time()
        storage.extend_cooldown("payment_api", now - 100)

        fresh = now + LADDER_COOLDOWN_SECONDS
        effective = storage.extend_cooldown("payment_api", fresh)

        assert effective == fresh

    def test_the_returned_expiry_is_the_one_a_reader_will_see(self, storage):
        """The return value is authoritative — callers arm timers and report on it.

        A return that drifted from the stored value would put the coordinator's
        all-clear and its operator-facing numbers on a cooldown nobody is in.
        """
        now = time.time()
        storage.extend_cooldown("payment_api", now + HONORED_HEADER_COOLDOWN_SECONDS)

        effective = storage.extend_cooldown(
            "payment_api", now + LADDER_COOLDOWN_SECONDS
        )

        assert storage.get_state("payment_api").cooldown_until == effective

    def test_keys_are_merged_independently(self, storage):
        """A long cooldown on one provider does not hold another one down."""
        now = time.time()
        storage.extend_cooldown("slow_api", now + HONORED_HEADER_COOLDOWN_SECONDS)

        effective = storage.extend_cooldown("fast_api", now + LADDER_COOLDOWN_SECONDS)

        assert effective == now + LADDER_COOLDOWN_SECONDS


class TestAbcDefaultExtendCooldownBehavior:
    """What a bring-your-own implementer inherits without writing any code."""

    def test_the_default_persists_through_the_implementer_s_own_set_cooldown(self):
        """The default is built from the two abstract methods and nothing else.

        That is why it is non-abstract: adding it to the interface cannot break
        an existing implementation, because the merge composes the primitives
        that implementation already had to provide.
        """
        storage = _MinimalStorage()
        now = time.time()
        honored = now + HONORED_HEADER_COOLDOWN_SECONDS

        storage.extend_cooldown("payment_api", honored)
        storage.extend_cooldown("payment_api", now + LADDER_COOLDOWN_SECONDS)

        # Both writes reach set_cooldown, and both carry the merged value.
        assert [call[1] for call in storage.set_cooldown_calls] == [honored, honored]

    def test_the_default_forwards_the_ttl_it_was_given(self):
        """``ttl`` is passed through, so a TTL-bearing backend keeps its cleanup."""
        storage = _MinimalStorage()

        storage.extend_cooldown("payment_api", time.time() + 60, ttl=120)

        assert storage.set_cooldown_calls[-1][2] == 120


# =============================================================================
# Redis TTL coverage — the key outlives the cooldown it carries
# =============================================================================


class TestRedisExtendCooldownTtlBehavior:
    """A cooldown longer than ``redis_ttl`` must not be evicted while it is live.

    ``redis_ttl`` is bounded at 60 s from below and an honored ``Retry-After``
    at 86400 s from above, so the configured TTL can sit far under the expiry
    the key carries. Deleting that key is an earlier movement of the shared
    expiry, which is precisely what the monotonic contract forbids.
    """

    def test_a_cooldown_longer_than_the_configured_ttl_gets_a_covering_ttl(self):
        """The stored TTL outlives the expiry, with the margin as headroom."""
        storage, client = _make_redis_storage(scripting=False)
        now = time.time()

        storage.extend_cooldown("payment_api", now + HONORED_HEADER_COOLDOWN_SECONDS)

        expected = HONORED_HEADER_COOLDOWN_SECONDS + _COOLDOWN_TTL_MARGIN_SECONDS
        assert client.expirations[_cooldown_key("payment_api")] >= expected

    def test_a_cooldown_shorter_than_the_configured_ttl_keeps_the_configured_one(self):
        """Boundary the other way: the covering TTL never shortens the configured one.

        Short cooldowns are the common case, and their keys stay for the
        configured retention so the consecutive-429 ladder and the operator's
        state reads keep working after the cooldown lapses.
        """
        storage, client = _make_redis_storage(scripting=False)

        storage.extend_cooldown("payment_api", time.time() + LADDER_COOLDOWN_SECONDS)

        assert client.expirations[_cooldown_key("payment_api")] == CONFIGURED_REDIS_TTL

    def test_a_short_second_write_does_not_shrink_the_ttl_of_a_long_cooldown(self):
        """The TTL follows the *effective* expiry, never the caller's candidate.

        Deriving it from the candidate is the subtle version of the same bug the
        merge fixes: the value survives the write but the key does not, so Redis
        drops a live cooldown early and every worker resumes.
        """
        storage, client = _make_redis_storage(scripting=False)
        now = time.time()
        storage.extend_cooldown("payment_api", now + HONORED_HEADER_COOLDOWN_SECONDS)

        storage.extend_cooldown("payment_api", now + LADDER_COOLDOWN_SECONDS)

        expected = HONORED_HEADER_COOLDOWN_SECONDS + _COOLDOWN_TTL_MARGIN_SECONDS
        assert client.expirations[_cooldown_key("payment_api")] >= expected

    def test_set_cooldown_keeps_its_plain_configured_ttl(self):
        """The coverage rule belongs to the monotonic write only.

        ``set_cooldown`` stays the raw last-writer-wins primitive, TTL included:
        the settings-propagation contract elsewhere pins that it applies the
        configured value verbatim.
        """
        storage, client = _make_redis_storage(scripting=False)

        storage.set_cooldown(
            "payment_api", time.time() + HONORED_HEADER_COOLDOWN_SECONDS
        )

        assert client.expirations[_cooldown_key("payment_api")] == CONFIGURED_REDIS_TTL


class TestRedisScriptedExtendCooldownBehavior:
    """The scripted path's call shape and reply handling.

    The merge and the TTL derivation happen inside the Lua script, so what is
    verifiable without a server is that the adapter hands the script everything
    it needs to compute them and reads its reply without losing precision.
    """

    def test_the_script_is_called_with_one_key_and_the_ttl_inputs_in_argv(self):
        """Single KEY on purpose, and the TTL inputs travel to the server.

        The shared script registry validates hash slots for any multi-key call
        and rejects keys that span slots, and Redis Cluster refuses a second key
        named through ARGV outright — so a two-key shape would raise on every
        call, on standalone Redis too. Passing the configured TTL and the margin
        as ARGV is what lets the *script* derive the TTL from the effective
        expiry, which is the only place that value is known.
        """
        storage, _client = _make_redis_storage()
        candidate = time.time() + HONORED_HEADER_COOLDOWN_SECONDS

        with patch.object(
            storage._lua, "execute", autospec=True, return_value="1.0"
        ) as mock_execute:
            storage.extend_cooldown("payment_api", candidate)

        kwargs = mock_execute.call_args[1]
        assert mock_execute.call_args[0][0] == _EXTEND_COOLDOWN_SCRIPT
        assert kwargs["keys"] == [_cooldown_key("payment_api")]
        assert float(kwargs["args"][0]) == candidate
        assert float(kwargs["args"][1]) == pytest.approx(time.time(), abs=5)
        assert kwargs["args"][2] == CONFIGURED_REDIS_TTL
        assert kwargs["args"][3] == _COOLDOWN_TTL_MARGIN_SECONDS

    def test_a_sub_second_reply_survives_the_round_trip(self):
        """The reply is a string, and it is parsed as one.

        A Lua number reply is converted to a Redis integer, which truncates the
        fractional part — an all-clear armed up to a second early, and an event
        payload that disagrees with the stored expiry.
        """
        storage, _client = _make_redis_storage()
        effective = 1785000000.123456

        with patch.object(
            storage._lua, "execute", autospec=True, return_value=f"{effective:.6f}"
        ):
            assert storage.extend_cooldown("payment_api", 1.0) == effective


class TestRedisScriptFallbackBehavior:
    """A Redis that cannot run Lua degrades; it never installs no cooldown at all.

    Raising here would reach every caller's fail-open wrap and drop the cooldown
    entirely — the worst available outcome, on exactly the storm the cooldown
    exists to damp. Best-effort monotonic is strictly better than the
    unconditional last-writer-wins it replaces.
    """

    def test_a_client_without_scripting_still_merges_monotonically(self):
        """The degraded path is still the contract, just without cross-process atomicity."""
        storage, _client = _make_redis_storage(scripting=False)
        now = time.time()
        honored = now + HONORED_HEADER_COOLDOWN_SECONDS

        storage.extend_cooldown("payment_api", honored)
        effective = storage.extend_cooldown(
            "payment_api", now + LADDER_COOLDOWN_SECONDS
        )

        assert effective == honored

    def test_the_fallback_is_announced_once_per_instance(self):
        """Sticky, so a 429 storm cannot turn one broken capability into log spam."""
        storage, _client = _make_redis_storage(scripting=False)

        with capture_logs() as logs:
            for _ in range(5):
                storage.extend_cooldown("payment_api", time.time() + 60)

        fallback_logs = [
            log
            for log in logs
            if log["event"] == "redis_rate_limit_storage.script_fallback"
        ]
        assert len(fallback_logs) == 1
        assert fallback_logs[0]["log_level"] == "warning"

    def test_scripting_is_not_retried_after_the_first_refusal(self):
        """The flag also removes the per-429 cost of a call that cannot succeed."""
        storage, client = _make_redis_storage(scripting=False)

        storage.extend_cooldown("payment_api", time.time() + 60)
        attempts_after_first = client.script_attempts
        storage.extend_cooldown("payment_api", time.time() + 60)

        assert client.script_attempts == attempts_after_first

    def test_a_backend_that_is_actually_down_still_raises(self):
        """The fallback covers a missing capability, not a missing server.

        If the plain-command path fails too, the backend is down — and the
        caller has to learn that rather than believe a cooldown was installed.
        """
        storage, _client = _make_redis_storage(scripting=False, readable=False)

        with pytest.raises(RateLimitStorageUnavailableError):
            storage.extend_cooldown("payment_api", time.time() + 60)

    def test_a_write_failure_after_a_readable_backend_raises(self):
        """The same for a backend that reads but cannot write."""
        storage, _client = _make_redis_storage(scripting=False, writable=False)

        with pytest.raises(RateLimitStorageUnavailableError):
            storage.extend_cooldown("payment_api", time.time() + 60)


# =============================================================================
# get_state_strict — "no cooldown" told apart from "cannot tell"
# =============================================================================


class TestGetStateStrictContract:
    """``get_state`` folds a backend failure into a clean state; this one raises.

    Folding is the right bias for a caller deciding whether to *wait* — an
    unreachable store should not block traffic. It is the wrong bias for a
    caller deciding whether a cooldown has *ended*, where the same fold reads as
    "ended" and releases the fleet on an outage.
    """

    def test_redis_get_state_folds_a_read_failure_into_a_clean_state(self):
        """The premise: this is what a verifying reader must not be handed."""
        storage, _client = _make_redis_storage(readable=False)

        state = storage.get_state("payment_api")

        assert state.cooldown_until == 0.0
        assert state.is_in_cooldown is False

    def test_redis_get_state_strict_raises_on_the_same_read_failure(self):
        """Same fault, same adapter, distinguishable answer."""
        storage, _client = _make_redis_storage(readable=False)

        with pytest.raises(RateLimitStorageUnavailableError):
            storage.get_state_strict("payment_api")

    def test_database_get_state_strict_raises_on_the_same_read_failure(self):
        """The database adapter folds and raises in the same pair."""
        repository = _FakeRepository()
        storage = DatabaseRateLimitStorage(repository_factory=lambda: repository)
        repository.readable = False

        assert storage.get_state("payment_api").cooldown_until == 0.0
        with pytest.raises(ConnectionError):
            storage.get_state_strict("payment_api")

    @pytest.mark.parametrize(
        "make_storage",
        [
            lambda: InMemoryRateLimitStorage(cleanup_interval=10_000),
            _MinimalStorage,
        ],
        ids=["memory", "abc-default"],
    )
    def test_a_backend_that_cannot_fail_reads_the_same_either_way(self, make_storage):
        """An in-process dict has no failure to report, so the two agree.

        The ABC default delegates for the same reason a bring-your-own adapter
        does: it keeps today's folding until its author overrides it, so adding
        the method breaks nothing.
        """
        storage = make_storage()
        cooldown_until = time.time() + HONORED_HEADER_COOLDOWN_SECONDS
        storage.extend_cooldown("payment_api", cooldown_until)

        assert (
            storage.get_state_strict("payment_api").cooldown_until
            == storage.get_state("payment_api").cooldown_until
            == cooldown_until
        )
