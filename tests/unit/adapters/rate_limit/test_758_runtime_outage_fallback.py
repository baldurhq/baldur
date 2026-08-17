"""758 — the outbound 429 cooldown survives a Redis that dies mid-run.

``RedisRateLimitStorage`` resolves its backend once. A Redis that goes away
afterwards used to remove the cooldown entirely: the write methods raised, every
caller's fail-open wrap swallowed the raise, and the next read folded to "no
cooldown" — so the whole fleet kept calling a 429-ing upstream at full rate for
the length of the outage, with the one gauge that names that state reading 0.

Four families are pinned here, each for a defect that shipped or was caught late:

- **Entry and service.** A transport failure latches one transition and the
  private per-worker store serves every delegating method. Every case asserts
  the *positive* half — the cooldown is present, the gauge reads 1 — never only
  "no exception raised", which a no-op implementation would also satisfy.
- **Data faults are not outages.** An unparseable stored value, or an ``INCR``
  against one, belongs to the key that carries it. Degrading the whole adapter
  over one bad byte string would flap the mode, the gauge and the transition log
  every probe interval for as long as the value existed. The write arm arrives as
  a bare reply error, so the same file asserts that a ``MISCONF`` / ``READONLY``
  reply — which is *not* a data fault — still enters fallback.
- **Recovery is verified, never assumed.** A backend that answers ``PING`` while
  refusing writes is the read-only replica a Sentinel failover leaves behind. The
  probe therefore replays the adapter's own write vocabulary, and the client
  double implements real ``INCR`` type rules so a float-shaped probe value could
  not pass here either.
- **Cadence and bounding.** The probe gate runs on a monotonic clock, so a
  backwards wall-clock step cannot strand a process in the degraded mode; the
  interval is jittered per reservation; and the local store is discarded at every
  verified exit, so nothing accumulates across outages.
"""

from __future__ import annotations

import itertools
import time
from unittest.mock import patch

import pytest
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
from pydantic import ValidationError
from redis._parsers.base import BaseParser
from redis.client import Pipeline
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
)
from redis.exceptions import (
    OutOfMemoryError,
    ReadOnlyError,
    ResponseError,
)
from structlog.testing import capture_logs

from baldur.adapters.rate_limit.redis_adapter import (
    _RECOVERY_PROBE_GATE_KEY,
    _RECOVERY_PROBE_JITTER_RATIO,
    _RECOVERY_PROBE_KEY_NAME,
    _RECOVERY_PROBE_VALUE,
    RedisRateLimitStorage,
    _get_recovery_probe_interval,
    _is_data_error,
)
from baldur.interfaces.rate_limit_storage import RateLimitStorageUnavailableError
from baldur.metrics import drift_metrics
from baldur.settings.rate_limit import RateLimitSettings, reset_rate_limit_settings

_KEY = "payment_api"
_OTHER_KEY = "search_api"

# The adapter's own default when settings are unreachable, and the shipped
# default of the field it reads — the two agree, so either resolution yields it.
_CONFIGURED_TTL = 3600

# Long enough that a cooldown installed during a test is unambiguously live when
# it is read back, whatever the wall clock does in between.
_COOLDOWN_SECONDS = 7200

_UNAVAILABLE_EVENT = "redis_rate_limit_storage.redis_unavailable"
_RECOVERED_EVENT = "redis_rate_limit_storage.fallback_recovered"

# What a server says when a command is refused rather than the value being
# wrong-shaped. Both are reply errors, which is exactly why the split cannot be
# made on the exception class alone.
_MISCONF_REPLY = (
    "MISCONF Redis is configured to save RDB snapshots, but is currently "
    "not able to persist on disk."
)
_READONLY_REPLY = "READONLY You can't write against a read only replica."
_OOM_REPLY = "OOM command not allowed when used memory > 'maxmemory'."
_NON_INTEGER_REPLY = "ERR value is not an integer or out of range"
_WRONGTYPE_REPLY = "WRONGTYPE Operation against a key holding the wrong kind of value"


def _client_reply_error(wire: str, *, pipelined: bool) -> BaseException:
    """The exception redis-py hands the adapter for a given server reply.

    Built with redis-py's own parser and pipeline wrapper rather than by
    constructing ``ResponseError(wire)``, because the two are not the same
    string: the parser strips a recognised error code, and a pipelined
    command's error is re-wrapped with the command that caused it. A double
    that raises the wire text verbatim asserts against a shape no client ever
    produces — and every read and every ``INCR`` this adapter issues is
    pipelined, so the wrapped form is the normal case here.
    """
    error = BaseParser.parse_error(wire)
    if pipelined:
        Pipeline.annotate_exception(
            None, error, 1, ("INCR", _redis_key(_KEY, "consecutive_429s"))
        )
    return error


# =============================================================================
# Doubles
# =============================================================================


class _FakeRedisPipeline:
    """Queue-and-replay pipeline, like redis-py's transactional one.

    ``execute`` raises on the first command that faults, after the earlier ones
    in the batch have applied — the shape a partially-refused ``MULTI/EXEC``
    reports to the client.
    """

    def __init__(self, client: _FakeRedisClient) -> None:
        self._client = client
        self._queued: list[tuple[str, tuple, dict]] = []

    def get(self, name: str) -> _FakeRedisPipeline:
        self._queued.append(("get", (name,), {}))
        return self

    def set(self, name: str, value: str, ex: int | None = None) -> _FakeRedisPipeline:
        self._queued.append(("set", (name, value), {"ex": ex}))
        return self

    def incr(self, name: str) -> _FakeRedisPipeline:
        self._queued.append(("incr", (name,), {}))
        return self

    def expire(self, name: str, ttl: int) -> _FakeRedisPipeline:
        self._queued.append(("expire", (name, ttl), {}))
        return self

    def delete(self, *names: str) -> _FakeRedisPipeline:
        self._queued.append(("delete", names, {}))
        return self

    def execute(self) -> list:
        queued, self._queued = self._queued, []
        results = []
        for number, (op, args, kw) in enumerate(queued, start=1):
            try:
                results.append(getattr(self._client, op)(*args, **kw))
            except ResponseError as error:
                # redis-py re-wraps a pipelined command's reply error with the
                # command that caused it, which moves the reply text off the
                # start of the message. Reproduced because the adapter reads
                # that message to tell a wrong-shaped value from a dead server.
                Pipeline.annotate_exception(None, error, number, (op.upper(), *args))
                raise
        return results


class _FakeRedisClient:
    """Redis client double covering the adapter's whole command vocabulary.

    The existing double in ``tests/unit/rate_limit/test_storage_adapters.py``
    implements ``get`` / ``set`` / ``ping`` / scripting only, so it cannot reach
    the recovery probe at all.

    Two properties are load-bearing rather than incidental:

    - ``incr`` enforces real integer semantics, so a probe value that Redis
      could not consume would fail here too. The probe runs ``INCR`` against
      what its own ``SET`` wrote, and a float-shaped constant would pin a
      process in fallback against a *fully healthy* server.
    - faults are injected per command, because the failure the verified exit
      exists for is a backend that answers some commands and refuses others.
    """

    def __init__(self, *, down: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.down = down
        # Wire-form reply messages, keyed by command. Stored as the server
        # sends them and turned into exceptions by redis-py's own parser, so
        # the double cannot disagree with the client about their shape.
        self.faults: dict[str, str] = {}
        self.commands: list[str] = []
        self.script_shas: dict[str, str] = {}

    # -- fault plumbing ----------------------------------------------------

    def _run(self, command: str) -> None:
        self.commands.append(command)
        if self.down:
            raise RedisConnectionError(
                "Error 111 connecting to localhost:6379. Connection refused."
            )
        fault = self.faults.get(command)
        if fault is not None:
            raise BaseParser.parse_error(fault)

    def refuse(self, command: str, message: str = _READONLY_REPLY) -> None:
        """Make one command answer a reply error while the rest keep working."""
        self.faults[command] = message

    # -- commands ----------------------------------------------------------

    def ping(self) -> bool:
        self._run("ping")
        return True

    def get(self, name: str) -> str | None:
        self._run("get")
        return self.store.get(name)

    def set(self, name: str, value: str, ex: int | None = None) -> bool:
        self._run("set")
        self.store[name] = str(value)
        if ex is not None:
            self.ttls[name] = ex
        return True

    def incr(self, name: str) -> int:
        self._run("incr")
        raw = self.store.get(name, "0")
        try:
            value = int(raw)
        except ValueError:
            raise BaseParser.parse_error(_NON_INTEGER_REPLY) from None
        value += 1
        self.store[name] = str(value)
        return value

    def expire(self, name: str, ttl: int) -> bool:
        self._run("expire")
        if name not in self.store:
            return False
        self.ttls[name] = ttl
        return True

    def delete(self, *names: str) -> int:
        self._run("delete")
        removed = 0
        for name in names:
            if self.store.pop(name, None) is not None:
                removed += 1
            self.ttls.pop(name, None)
        return removed

    def pipeline(self) -> _FakeRedisPipeline:
        # Creating a pipeline does not talk to the server, so it never faults.
        return _FakeRedisPipeline(self)

    # -- scripting ---------------------------------------------------------

    def script_load(self, body: str) -> str:
        self._run("script_load")
        sha = f"sha-{len(self.script_shas)}"
        self.script_shas[sha] = body
        return sha

    def evalsha(self, sha: str, numkeys: int, *args: object) -> str:
        """The shipped max-merge, modelled in Python.

        No client double executes Lua, so the server-side semantics are pinned
        against a real Redis elsewhere. What this reproduction is for is that
        the healthy ``extend_cooldown`` path works end-to-end here, so the
        degraded path is measured against a working control rather than against
        a permanently-unscripted adapter.
        """
        self._run("evalsha")
        key = str(args[0])
        candidate, now = float(str(args[1])), float(str(args[2]))
        configured_ttl, margin = int(args[3]), int(args[4])  # type: ignore[arg-type]
        stored = float(self.store.get(key) or 0.0)
        effective = max(stored, candidate)
        self.store[key] = f"{effective:.6f}"
        self.ttls[key] = max(int(effective - now) + margin, configured_ttl, 1)
        return f"{effective:.6f}"


class _FakeMonotonic:
    """A monotonic clock the test drives, injected at adapter construction.

    ``CooldownGate`` captures the callable it is built with, so the clock has to
    be in place before the adapter exists — patching ``time.monotonic``
    afterwards would leave the gate holding the real one.
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


# =============================================================================
# Helpers
# =============================================================================


def _metric_value(metric, sample_name: str) -> float:
    for family in metric.collect():
        for sample in family.samples:
            if sample.name == sample_name:
                return sample.value
    raise AssertionError(f"no sample named {sample_name}")


def _fallback_gauge() -> float:
    return _metric_value(
        drift_metrics.ratelimit_fallback_active, "baldur_ratelimit_fallback_active"
    )


def _unavailable_counter() -> float:
    return _metric_value(
        drift_metrics.ratelimit_redis_unavailable_total,
        "baldur_ratelimit_redis_unavailable_total",
    )


def _redis_key(key: str, suffix: str) -> str:
    return f"{RedisRateLimitStorage.KEY_PREFIX}:{key}:{suffix}"


def _probe_key() -> str:
    return f"{RedisRateLimitStorage.KEY_PREFIX}:{_RECOVERY_PROBE_KEY_NAME}"


def _max_probe_window() -> float:
    """The longest window the jitter can hand out for one reservation."""
    return _get_recovery_probe_interval() * (1 + _RECOVERY_PROBE_JITTER_RATIO)


def _make_storage(
    **client_kwargs,
) -> tuple[RedisRateLimitStorage, _FakeRedisClient, _FakeMonotonic]:
    """An adapter over the double, with a clock the test owns."""
    clock = _FakeMonotonic()
    client = _FakeRedisClient(**client_kwargs)
    with patch("time.monotonic", clock):
        storage = RedisRateLimitStorage(client, ttl=_CONFIGURED_TTL)
    return storage, client, clock


def _degraded_storage() -> tuple[
    RedisRateLimitStorage, _FakeRedisClient, _FakeMonotonic
]:
    """An adapter that resolved against a healthy Redis and then lost it.

    The outage is discovered by a read, so the method under test in each case is
    never also the one that performed the transition.
    """
    storage, client, clock = _make_storage()
    client.down = True
    storage.get_state(_KEY)
    assert storage._fallback_mode is True, "setup failed: the outage was not latched"
    client.commands.clear()
    return storage, client, clock


def _events(logs: list[dict], name: str) -> list[dict]:
    return [entry for entry in logs if entry.get("event") == name]


@pytest.fixture(autouse=True)
def _pinned_fallback_gauge():
    """Pin the process-global gauge at both ends of every case.

    ``baldur_ratelimit_fallback_active`` is a module attribute shared by the
    whole worker: read blind it proves nothing, and a case that enters fallback
    and does not recover would leak a 1 into any later reader.
    """
    drift_metrics.set_ratelimit_fallback_mode(False)
    yield
    drift_metrics.set_ratelimit_fallback_mode(False)


# =============================================================================
# Entry into fallback, and service from the local store
# =============================================================================


# The six methods that route through ``_dispatch``, each as the call a caller
# actually makes. Keyed by method name so the parametrize ids name the method.
_DELEGATING_CALLS = {
    "get_state": lambda s: s.get_state(_KEY),
    "set_cooldown": lambda s: s.set_cooldown(_KEY, time.time() + _COOLDOWN_SECONDS),
    "extend_cooldown": lambda s: s.extend_cooldown(
        _KEY, time.time() + _COOLDOWN_SECONDS
    ),
    "increment_consecutive_429s": lambda s: s.increment_consecutive_429s(_KEY),
    "reset_consecutive_429s": lambda s: s.reset_consecutive_429s(_KEY),
    "clear": lambda s: s.clear(_KEY),
}


class TestRuntimeOutageFallbackBehavior:
    """Every delegating method degrades in place instead of disappearing."""

    @pytest.mark.parametrize(
        "invoke", _DELEGATING_CALLS.values(), ids=list(_DELEGATING_CALLS)
    )
    def test_a_method_meeting_a_dead_backend_latches_the_degraded_mode(self, invoke):
        """All six share one dispatch, and all six have to arm the same latch.

        Asserted on the mode and the gauge rather than on the absence of an
        exception: an implementation that swallowed the failure and did nothing
        would satisfy "did not raise" while leaving the fleet unprotected.
        """
        storage, client, _clock = _make_storage()
        client.down = True

        invoke(storage)

        assert storage._fallback_mode is True
        assert _fallback_gauge() == 1

    def test_a_cooldown_set_during_an_outage_is_read_back_as_live(self):
        """The central claim: the cooldown survives, per worker."""
        # Given: the backend is gone
        storage, client, _clock = _make_storage()
        client.down = True
        until = time.time() + _COOLDOWN_SECONDS

        # When: a 429 installs a cooldown and a later caller checks it
        storage.set_cooldown(_KEY, until)
        state = storage.get_state(_KEY)

        # Then: the caller defers, exactly as it would with Redis up
        assert state.cooldown_until == until
        assert state.is_in_cooldown is True

    def test_extend_cooldown_during_an_outage_returns_the_locally_effective_expiry(
        self,
    ):
        """The return value is what the caller waits on, so it must be real."""
        storage, _client, _clock = _degraded_storage()
        honored = time.time() + _COOLDOWN_SECONDS

        effective = storage.extend_cooldown(_KEY, honored)

        assert effective == honored
        assert storage.get_state(_KEY).is_in_cooldown is True

    def test_extend_cooldown_during_an_outage_keeps_the_longer_of_two_writes(self):
        """The monotonic contract holds locally too — a headerless ladder delay
        arriving after an honored ``Retry-After`` must not shorten it."""
        storage, _client, _clock = _degraded_storage()
        honored = time.time() + _COOLDOWN_SECONDS

        storage.extend_cooldown(_KEY, honored)
        effective = storage.extend_cooldown(_KEY, time.time() + 10)

        assert effective == honored

    def test_the_429_counter_climbs_in_the_local_store_during_an_outage(self):
        """The ladder still advances, so the backoff still escalates."""
        storage, _client, _clock = _degraded_storage()

        first = storage.increment_consecutive_429s(_KEY)
        second = storage.increment_consecutive_429s(_KEY)

        assert (first, second) == (1, 2)
        assert storage.get_state(_KEY).consecutive_429s == 2

    def test_a_success_signal_during_an_outage_zeroes_the_local_counter(self):
        storage, _client, _clock = _degraded_storage()
        storage.increment_consecutive_429s(_KEY)
        storage.increment_consecutive_429s(_KEY)

        storage.reset_consecutive_429s(_KEY)

        assert storage.get_state(_KEY).consecutive_429s == 0

    def test_an_operator_clear_during_an_outage_drops_the_local_cooldown(self):
        """The clearing worker honors its own clear for the outage window.

        The Redis-side value is untouched and resumes afterwards, which is why
        the assertion is scoped to this worker's view.
        """
        storage, _client, _clock = _degraded_storage()
        storage.set_cooldown(_KEY, time.time() + _COOLDOWN_SECONDS)

        storage.clear(_KEY)

        assert storage.get_state(_KEY).is_in_cooldown is False

    def test_the_steady_outage_path_stops_dialling_the_dead_backend(self):
        """One probe per interval, not one connect attempt per protected call.

        Without this the degraded path would pay a socket timeout on every 429 —
        the cost the old raise-and-swallow behaviour also paid.
        """
        storage, client, _clock = _degraded_storage()

        storage.get_state(_KEY)
        storage.increment_consecutive_429s(_KEY)
        storage.set_cooldown(_KEY, time.time() + _COOLDOWN_SECONDS)

        assert client.commands == []


# =============================================================================
# A data fault belongs to one key, not to the adapter
# =============================================================================


class TestFailureClassificationContract:
    """``_is_data_error`` sorts a wrong-shaped value from an unusable server.

    The messages are the specification — redis-py raises a bare
    ``ResponseError`` for both cases — so they are hardcoded here rather than
    imported from the module that decides on them.
    """

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (ValueError("could not convert string to float: 'soon'"), True),
            (TypeError("float() argument must be a string or a real number"), True),
            (RedisConnectionError("Connection refused."), False),
            (TimeoutError("Timeout reading from socket"), False),
        ],
        ids=[
            "parse_value_error",
            "parse_type_error",
            "connection_refused",
            "socket_timeout",
        ],
    )
    def test_a_client_side_failure_is_decided_by_its_class(self, error, expected):
        assert _is_data_error(error) is expected

    @pytest.mark.parametrize("pipelined", [False, True], ids=["direct", "pipelined"])
    @pytest.mark.parametrize(
        ("wire", "expected"),
        [
            (_WRONGTYPE_REPLY, True),
            (_NON_INTEGER_REPLY, True),
            (_MISCONF_REPLY, False),
            (_READONLY_REPLY, False),
            (_OOM_REPLY, False),
        ],
        ids=[
            "wrongtype_reply",
            "non_integer_reply",
            "misconf_reply",
            "readonly_reply",
            "out_of_memory",
        ],
    )
    def test_a_server_reply_is_decided_by_the_body_of_the_reply(
        self, wire, expected, pipelined
    ):
        """Both arrival shapes, because the adapter only ever sees the wrapped
        one: every read and every ``INCR`` it issues goes through a pipeline."""
        error = _client_reply_error(wire, pipelined=pipelined)

        assert _is_data_error(error) is expected

    def test_the_client_moves_the_reply_text_off_the_start_of_the_message(self):
        """Why the rule reads the reply body rather than ``str(exc)``.

        Two client-side rewrites sit between the wire and the exception text:
        redis-py strips a recognised error code, and a pipelined command's
        error is re-wrapped with the command that caused it. A rule matching
        the raw string is inert against every real server while still passing
        against a double that raises the wire text verbatim — so the shipped
        ``INCR``-on-a-non-integer guard would degrade the whole adapter and
        flap it every probe interval, which is the outcome it exists to stop.
        """
        stripped = _client_reply_error(_NON_INTEGER_REPLY, pipelined=False)
        wrapped = _client_reply_error(_WRONGTYPE_REPLY, pipelined=True)

        assert not str(stripped).startswith(_NON_INTEGER_REPLY)
        assert not str(wrapped).startswith(_WRONGTYPE_REPLY)
        assert _is_data_error(stripped) is True
        assert _is_data_error(wrapped) is True

    def test_a_data_fault_wrapped_in_the_domain_error_is_still_a_data_fault(self):
        """Wrappers are raised ``from`` the client error, so the walk has to
        survive the hop the strict read adds."""
        cause = ValueError("could not convert string to float: 'soon'")
        wrapped = RateLimitStorageUnavailableError(str(cause))
        wrapped.__cause__ = cause

        assert _is_data_error(wrapped) is True

    def test_a_cause_chain_longer_than_the_bound_falls_to_the_transport_class(self):
        """Boundary: the walk is bounded, and it fails toward protecting the
        caller — a pathological chain degrades the adapter rather than being
        followed forever."""
        error: BaseException = ValueError("the data fault, buried")
        for _ in range(6):
            wrapper = RuntimeError("transport")
            wrapper.__cause__ = error
            error = wrapper

        assert _is_data_error(error) is False


class TestDataFaultIsolationBehavior:
    """One bad stored value degrades its own key and nothing else."""

    def test_an_unparseable_stored_value_folds_only_its_own_key(self):
        """A ``cooldown_until`` of ``"soon"`` is a poisoned key, not an outage.

        Degrading the adapter here would drop every key onto the local store and
        then flap — the probe passes against the healthy server, the next read
        re-poisons — for as long as the value existed.
        """
        # Given: one poisoned key beside one healthy key
        storage, client, _clock = _make_storage()
        client.store[_redis_key(_KEY, "cooldown_until")] = "soon"
        healthy_until = time.time() + _COOLDOWN_SECONDS
        client.store[_redis_key(_OTHER_KEY, "cooldown_until")] = str(healthy_until)

        # When
        poisoned = storage.get_state(_KEY)
        healthy = storage.get_state(_OTHER_KEY)

        # Then: only the poisoned key folds, and the adapter is still on Redis
        assert poisoned.cooldown_until == 0.0
        assert healthy.cooldown_until == healthy_until
        assert storage._fallback_mode is False
        assert _fallback_gauge() == 0

    def test_an_incr_against_a_non_integer_value_keeps_its_per_key_raise(self):
        """The write-arm twin: the same fault, arriving as a bare reply error.

        A data rule written as plain ``ResponseError`` would pass the read-arm
        case above and fail here in the other direction, which is why both arms
        and their opposite live in one file.
        """
        storage, client, _clock = _make_storage()
        client.store[_redis_key(_KEY, "consecutive_429s")] = "not-a-number"

        with pytest.raises(RateLimitStorageUnavailableError):
            storage.increment_consecutive_429s(_KEY)

        assert storage._fallback_mode is False
        assert _fallback_gauge() == 0

    def test_a_data_fault_leaves_the_probe_cadence_unarmed(self):
        """Nothing was wrong with the server, so nothing may consume a slot."""
        storage, client, _clock = _make_storage()
        client.store[_redis_key(_KEY, "cooldown_until")] = "soon"

        storage.get_state(_KEY)

        assert storage._probe_gate.keys() == []

    @pytest.mark.parametrize(
        ("reply", "client_class"),
        [
            (_MISCONF_REPLY, ResponseError),
            (_READONLY_REPLY, ReadOnlyError),
            (_OOM_REPLY, OutOfMemoryError),
        ],
        ids=["misconf", "readonly_replica", "out_of_memory"],
    )
    def test_a_server_unusable_reply_error_still_enters_fallback(
        self, reply, client_class
    ):
        """The opposite of the two cases above: the adapter must degrade rather
        than raise per key.

        The class the client produces is pinned alongside, because it is what
        makes the split impossible to make on the class: two of these three are
        dedicated subclasses and one is a bare ``ResponseError``, exactly like
        the wrong-shaped-value replies above.
        """
        storage, client, _clock = _make_storage()
        client.faults["incr"] = reply
        assert type(_client_reply_error(reply, pipelined=False)) is client_class

        counted = storage.increment_consecutive_429s(_KEY)

        assert counted == 1
        assert storage._fallback_mode is True
        assert _fallback_gauge() == 1


# =============================================================================
# The latched transition
# =============================================================================


class TestFallbackTransitionBehavior:
    """Flag, gauge, counter and one log line move together, once."""

    def test_the_transition_writes_every_effect_as_one_unit(self):
        storage, client, _clock = _make_storage()
        client.down = True
        before = _unavailable_counter()

        with (
            patch(
                "baldur.settings.redis.redis_absence_is_expected", return_value=False
            ),
            capture_logs() as logs,
        ):
            storage.get_state(_KEY)

        assert storage._fallback_mode is True
        assert _fallback_gauge() == 1
        assert _unavailable_counter() == before + 1
        assert len(_events(logs, _UNAVAILABLE_EVENT)) == 1

    def test_a_second_failure_inside_the_same_outage_writes_nothing_more(self):
        """Edge-only: the latch reports a transition, not a failure rate."""
        storage, client, _clock = _make_storage()
        client.down = True
        storage.get_state(_KEY)
        after_entry = _unavailable_counter()

        with capture_logs() as logs:
            storage.get_state(_KEY)
            storage.increment_consecutive_429s(_KEY)

        assert _unavailable_counter() == after_entry
        assert _events(logs, _UNAVAILABLE_EVENT) == []

    @pytest.mark.parametrize(
        ("absence_expected", "expected_level"),
        [(True, "debug"), (False, "warning")],
        ids=["unconfigured_quiet", "configured_loud"],
    )
    def test_the_announcement_level_splits_on_the_posture(
        self, absence_expected, expected_level
    ):
        """Nobody named a Redis outside production, so the framework finding its
        own default unreachable is posture, not an incident. The split is the
        same one the admission probe applies — asserted here on the operation
        path, which is a different entry point into the same helper.
        """
        storage, client, _clock = _make_storage()
        client.down = True

        with (
            patch(
                "baldur.settings.redis.redis_absence_is_expected",
                return_value=absence_expected,
            ),
            capture_logs() as logs,
        ):
            storage.get_state(_KEY)

        records = _events(logs, _UNAVAILABLE_EVENT)
        assert [entry["log_level"] for entry in records] == [expected_level]


# =============================================================================
# Verified recovery
# =============================================================================


class TestVerifiedRecoveryBehavior:
    """A ping is not recovery: the write vocabulary has to answer too."""

    def test_a_write_verified_probe_clears_the_mode_the_gauge_and_the_log(self):
        # Given: a degraded adapter and a server that came back
        storage, client, clock = _degraded_storage()
        client.down = False
        clock.advance(_max_probe_window() + 1)

        # When: the next caller wins the probe slot
        with capture_logs() as logs:
            storage.set_cooldown(_KEY, time.time() + _COOLDOWN_SECONDS)

        # Then: the adapter is back on Redis and says so once
        assert storage._fallback_mode is False
        assert _fallback_gauge() == 0
        assert len(_events(logs, _RECOVERED_EVENT)) == 1
        assert client.store[_redis_key(_KEY, "cooldown_until")]

    def test_the_probe_replays_the_whole_write_vocabulary(self):
        storage, client, clock = _degraded_storage()
        client.down = False
        clock.advance(_max_probe_window() + 1)

        storage.get_state(_KEY)

        probe_commands = client.commands[: client.commands.index("get")]
        assert probe_commands == ["ping", "set", "incr", "expire", "delete"]

    def test_the_probe_leaves_no_key_behind(self):
        """Its own trailing ``DEL`` lands, so a passing probe is invisible in
        the keyspace afterwards."""
        storage, client, clock = _degraded_storage()
        client.down = False
        clock.advance(_max_probe_window() + 1)

        storage.get_state(_KEY)

        assert _probe_key() not in client.store

    def test_the_probe_key_cannot_collide_with_a_real_coordination_key(self):
        """Two segments against the three every real key carries."""
        assert _probe_key() == "ratelimit:__recovery_probe__"
        assert _probe_key() != _redis_key(_RECOVERY_PROBE_KEY_NAME, "cooldown_until")

    def test_the_probe_value_survives_the_incr_the_probe_itself_issues(self):
        """Guard of the guard.

        The probe runs ``INCR`` against what its own ``SET`` wrote, so a
        float-shaped constant would make a fully healthy, fully permissive
        server answer "value is not an integer" and pin the process in fallback
        for its whole life. The shipped constant is driven through a real
        ``INCR`` rule here rather than eyeballed.
        """
        client = _FakeRedisClient()
        client.set(_probe_key(), _RECOVERY_PROBE_VALUE)

        assert client.incr(_probe_key()) == 1

    @pytest.mark.parametrize(
        "refused",
        ["set", "incr", "expire", "delete"],
        ids=["writes_refused", "incr_refused", "expire_refused", "delete_refused"],
    )
    def test_a_backend_that_pings_but_refuses_one_write_never_all_clears(self, refused):
        """The read-only replica a Sentinel failover leaves behind answers
        ``PING`` and reads. A single-command probe would be fooled by three of
        these four; the adapter is held in fallback across two grants, so a
        one-tick flap could not pass for a stable exit either.
        """
        # Given: a locally-enforced cooldown and a half-working backend
        storage, client, clock = _degraded_storage()
        storage.set_cooldown(_KEY, time.time() + _COOLDOWN_SECONDS)
        client.down = False
        client.refuse(refused)

        # When: two separate probe windows open
        for _ in range(2):
            clock.advance(_max_probe_window() + 1)
            storage.get_state(_KEY)

        # Then: still degraded, and still protecting the caller
        assert storage._fallback_mode is True
        assert _fallback_gauge() == 1
        assert storage.get_state(_KEY).is_in_cooldown is True

    def test_a_failed_probe_consumes_its_slot_so_the_next_caller_waits(self):
        """Boundary: a granted-and-failed attempt costs a full interval, so a
        storm cannot turn the canary into one connect per 429."""
        storage, client, clock = _degraded_storage()
        client.down = False
        client.refuse("set")
        clock.advance(_max_probe_window() + 1)
        storage.get_state(_KEY)
        client.commands.clear()

        storage.get_state(_KEY)

        assert client.commands == []

    def test_a_second_exit_after_recovery_is_a_no_op(self):
        """Idempotency: the mode re-check returns before the pipeline.

        The admission path calls into this on every ordinary resolution-time
        probe, where the mode was never set — paying the probe there would be
        three round trips on the path that exists to stay cheap, plus a clear of
        a store the caller has no reason to touch.
        """
        storage, client, clock = _degraded_storage()
        client.down = False
        clock.advance(_max_probe_window() + 1)
        storage.get_state(_KEY)
        assert storage._fallback_mode is False
        client.commands.clear()

        assert storage._try_exit_fallback() is False
        assert client.commands == []

    def test_an_internal_error_during_the_exit_reports_still_fallback(self):
        """Fails closed on anything, the flip included.

        An error escaping here would reach the caller's fail-open wrap *after*
        the mode had flipped, which drops the caller's cooldown entirely — the
        exact outcome this whole path exists to prevent.
        """
        storage, client, clock = _degraded_storage()
        client.down = False
        clock.advance(_max_probe_window() + 1)

        with patch.object(
            RedisRateLimitStorage,
            "_run_recovery_write_probe",
            autospec=True,
            side_effect=RuntimeError("probe bug"),
        ) as probe:
            recovered = storage._try_exit_fallback()

        probe.assert_called_once()
        assert recovered is False
        assert storage._fallback_mode is True
        assert _fallback_gauge() == 1


# =============================================================================
# The strict read
# =============================================================================


class TestGetStateStrictDegradedBehavior:
    """``get_state_strict`` has no in-tree production caller, so this is the
    only place its degraded-window contract is exercised."""

    def test_the_strict_read_still_attempts_redis_while_degraded(self):
        """Serving the local store here would be exactly the fold this variant
        exists to forbid."""
        storage, client, _clock = _degraded_storage()

        with pytest.raises(RateLimitStorageUnavailableError):
            storage.get_state_strict(_KEY)

        assert client.commands == ["get"]

    def test_a_transport_failure_on_the_strict_read_latches_the_degraded_mode(self):
        storage, client, _clock = _make_storage()
        client.down = True

        with pytest.raises(RateLimitStorageUnavailableError):
            storage.get_state_strict(_KEY)

        assert storage._fallback_mode is True
        assert _fallback_gauge() == 1

    def test_a_data_fault_on_the_strict_read_does_not_degrade_the_adapter(self):
        storage, client, _clock = _make_storage()
        client.store[_redis_key(_KEY, "cooldown_until")] = "soon"

        with pytest.raises(RateLimitStorageUnavailableError):
            storage.get_state_strict(_KEY)

        assert storage._fallback_mode is False
        assert _fallback_gauge() == 0

    def test_a_locally_enforced_cooldown_is_never_reported_as_ended(self):
        """The window where Redis is readable again but the mode is still set.

        Handing back the remote value alone would answer a definite "ended" for
        a cooldown this worker is actively enforcing.
        """
        # Given: a cooldown that lives only in this worker, and a readable server
        storage, client, _clock = _degraded_storage()
        until = time.time() + _COOLDOWN_SECONDS
        storage.set_cooldown(_KEY, until)
        client.down = False

        # When: a strict read runs before the probe window opens
        state = storage.get_state_strict(_KEY)

        # Then: the merge preserves it
        assert storage._fallback_mode is True
        assert state.cooldown_until == until
        assert state.is_in_cooldown is True

    def test_the_merge_stops_once_the_mode_is_clear(self):
        """Boundary: the merge is scoped to the degraded window.

        The delegate is seeded directly after recovery — the verified exit
        empties it, so there would otherwise be nothing left to distinguish "no
        merge" from "nothing to merge".
        """
        storage, client, clock = _degraded_storage()
        client.down = False
        clock.advance(_max_probe_window() + 1)
        storage.get_state(_KEY)
        assert storage._fallback_mode is False
        storage._delegate.set_cooldown(_KEY, time.time() + _COOLDOWN_SECONDS)

        state = storage.get_state_strict(_KEY)

        assert state.cooldown_until == 0.0
        assert state.is_in_cooldown is False


# =============================================================================
# The admission probe
# =============================================================================


class TestAvailabilityProbeFallbackBehavior:
    """``is_available`` is a second entry point into the same two paths."""

    def test_two_failed_probes_count_one_transition(self):
        """Idempotency: the probe is not a second, unsynchronised writer.

        Left as its own check-then-act it could double-count the transition, or
        leave the gauge reading 1 with the mode clear and nothing to clear it.
        """
        storage, client, _clock = _make_storage(down=True)
        before = _unavailable_counter()

        assert storage.is_available() is False
        assert storage.is_available() is False

        assert _unavailable_counter() == before + 1
        assert _fallback_gauge() == 1

    def test_a_healthy_probe_with_the_mode_clear_issues_no_write_probe(self):
        """The ordinary resolution-time call: one ping, nothing else."""
        storage, client, _clock = _make_storage()

        assert storage.is_available() is True

        assert client.commands == ["ping"]

    def test_a_healthy_probe_while_degraded_runs_the_verified_exit(self):
        storage, client, clock = _degraded_storage()
        client.down = False
        clock.advance(_max_probe_window() + 1)

        assert storage.is_available() is True

        assert storage._fallback_mode is False
        assert _fallback_gauge() == 0

    def test_a_healthy_ping_over_a_refusing_backend_is_not_an_all_clear(self):
        """The bool it returns is ping-based as before, but the *mode* is not."""
        storage, client, clock = _degraded_storage()
        client.down = False
        client.refuse("set")
        clock.advance(_max_probe_window() + 1)

        assert storage.is_available() is True

        assert storage._fallback_mode is True
        assert _fallback_gauge() == 1


# =============================================================================
# Probe cadence
# =============================================================================


class TestRecoveryProbeCadenceBehavior:
    """One attempt per interval per process, on a clock that cannot run backwards."""

    def test_a_backwards_wall_clock_step_does_not_shut_the_probe_gate(self):
        """An NTP correction, a resumed snapshot, a late container sync.

        Measured on the wall clock, a backwards step of more than one interval
        shuts the gate for the size of the step and strands the process in the
        degraded mode.
        """
        storage, client, clock = _degraded_storage()
        client.down = False
        clock.advance(_max_probe_window() + 1)

        with patch("time.time", return_value=time.time() - 10 * _max_probe_window()):
            storage.get_state(_KEY)

        assert storage._fallback_mode is False

    def test_the_gate_stays_shut_inside_its_own_window(self):
        """The other half of the same boundary — without it the case above
        would pass against a gate that never suppressed anything."""
        storage, client, _clock = _degraded_storage()
        client.down = False

        storage.get_state(_KEY)

        assert storage._fallback_mode is True
        assert _fallback_gauge() == 1

    def test_each_outage_draws_its_own_jittered_window(self):
        """Redis dies once, so unjittered gates arm within one timeout of each
        other and the whole fleet exits on the same tick — each worker then
        spending its own 429 on every still-cooling key. A constant here, or a
        value resolved once for the whole process, would stagger nothing.
        """
        storage, _client, _clock = _make_storage()
        interval = _get_recovery_probe_interval()

        windows = [storage._draw_probe_window() for _ in range(20)]

        assert len(set(windows)) > 1
        assert all(
            interval * (1 - _RECOVERY_PROBE_JITTER_RATIO)
            <= window
            <= interval * (1 + _RECOVERY_PROBE_JITTER_RATIO)
            for window in windows
        )

    def test_a_held_window_is_not_eroded_by_the_calls_it_denies(self):
        """The drawn window governs the outage, not the floor of the band.

        ``CooldownGate`` evicts against the window stored with the reservation
        but suppresses against the CALL's, so it opens at the *minimum* of the
        two. A window redrawn on every denied call would therefore let a
        process taking many calls slide down to the jitter floor — and a fleet
        that entered together would re-align there, under exactly the 429 storm
        the stagger exists for.
        """
        # Given: an outage whose window was drawn at the top of the band
        ceiling = 1.0 + _RECOVERY_PROBE_JITTER_RATIO
        with patch("random.uniform", return_value=ceiling):
            storage, client, clock = _degraded_storage()
        window = _get_recovery_probe_interval() * ceiling
        assert storage._probe_window_seconds == pytest.approx(window)
        client.down = False

        # When: callers poll densely across the whole band below it
        step = window / 100
        for _ in range(99):
            clock.advance(step)
            storage.get_state(_KEY)

        # Then: none of them opened the gate early, and crossing it does
        assert storage._fallback_mode is True

        clock.advance(step * 2)
        storage.get_state(_KEY)

        assert storage._fallback_mode is False

    def test_entering_fallback_consumes_a_slot_up_front(self):
        """The call that just paid a timeout must not immediately pay another."""
        storage, client, _clock = _make_storage()
        client.down = True

        storage.get_state(_KEY)

        assert storage._probe_gate.keys() == [_RECOVERY_PROBE_GATE_KEY]


# =============================================================================
# Bounding the local store
# =============================================================================


class TestLocalStateBoundingBehavior:
    """The local store's lifetime is exactly one outage window."""

    def test_the_verified_exit_discards_the_local_store(self):
        """Nothing is carried back: each still-cooling key re-arms in Redis
        through its own next 429."""
        storage, client, clock = _degraded_storage()
        storage.set_cooldown(_KEY, time.time() + _COOLDOWN_SECONDS)
        storage.increment_consecutive_429s(_KEY)
        assert storage._delegate._data != {}

        client.down = False
        clock.advance(_max_probe_window() + 1)
        storage.get_state(_KEY)

        assert storage._fallback_mode is False
        assert storage._delegate._data == {}

    def test_two_outage_cycles_do_not_accumulate_local_state(self):
        """The resident set is bounded by one window's distinct keys, not by the
        process's lifetime — the failure mode the withdrawn warm mirror had."""
        storage, client, clock = _make_storage()

        for key in (_KEY, _OTHER_KEY):
            client.down = True
            storage.set_cooldown(key, time.time() + _COOLDOWN_SECONDS)
            assert storage._fallback_mode is True
            client.down = False
            clock.advance(_max_probe_window() + 1)
            storage.get_state(key)

        assert storage._fallback_mode is False
        assert storage._delegate._data == {}


# =============================================================================
# The operator dial
# =============================================================================


class TestRateLimitSettingsContract:
    """``redis_recovery_probe_interval_seconds`` — the shipped design values."""

    def test_the_probe_interval_defaults_to_30_seconds(self):
        assert (
            RateLimitSettings.model_fields[
                "redis_recovery_probe_interval_seconds"
            ].default
            == 30
        )

    @pytest.mark.parametrize(
        "value", [1, 30, 3600], ids=["floor", "default", "ceiling"]
    )
    def test_an_in_range_probe_interval_is_accepted(self, value):
        settings = RateLimitSettings(redis_recovery_probe_interval_seconds=value)

        assert settings.redis_recovery_probe_interval_seconds == value

    @pytest.mark.parametrize(
        "value", [0, -1, 3601], ids=["gate_disabling", "negative", "above_ceiling"]
    )
    def test_an_out_of_range_probe_interval_is_rejected(self, value):
        """The lower bound is load-bearing rather than cosmetic: ``CooldownGate``
        treats ``<= 0`` as "no gate", so a zero here would let every caller probe
        on every 429 — one connect attempt per protected call for the length of
        an outage, which is the cost the fallback exists to remove.
        """
        with pytest.raises(ValidationError):
            RateLimitSettings(redis_recovery_probe_interval_seconds=value)


class TestAdapterConstructorSettingsToleranceBehavior:
    """One bad ``BALDUR_RATE_LIMIT_*`` value must not disable coordination.

    The adapter constructor is settings-tolerant on purpose. A raise here
    escapes backend auto-detection, the coordinator's resolver catches it and
    returns ``None``, and outbound 429 coordination is then silently off for the
    whole process — a configuration typo reintroducing, permanently and
    fleet-wide, exactly the failure this file is about.
    """

    @pytest.fixture
    def out_of_range_rate_limit_env(self, monkeypatch):
        """Env vars are set before the settings group is built, and the cached
        group is dropped at both ends so no other case inherits it."""

        def _apply(**env: str) -> None:
            for name, value in env.items():
                monkeypatch.setenv(name, value)
            reset_rate_limit_settings()

        reset_rate_limit_settings()
        yield _apply
        reset_rate_limit_settings()

    def test_an_out_of_range_ttl_still_constructs_a_working_adapter(
        self, out_of_range_rate_limit_env
    ):
        # Given: a TTL below the field's floor of 60
        out_of_range_rate_limit_env(BALDUR_RATE_LIMIT_REDIS_TTL="30")

        # When
        storage = RedisRateLimitStorage(_FakeRedisClient())

        # Then: the adapter runs on its own default rather than failing to exist
        assert storage._ttl == _CONFIGURED_TTL
        assert storage.get_state(_KEY).cooldown_until == 0.0

    def test_an_out_of_range_cleanup_cadence_still_constructs_the_delegate(
        self, out_of_range_rate_limit_env
    ):
        """The delegate reads the same settings class unguarded when it is built
        bare, which is why the adapter resolves the cadence itself."""
        # Given: a cadence above the field's ceiling of 1000
        out_of_range_rate_limit_env(
            BALDUR_RATE_LIMIT_MEMORY_CLEANUP_INTERVAL_OPS="5000"
        )

        storage = RedisRateLimitStorage(_FakeRedisClient())

        assert storage._delegate._cleanup_interval == 100


# =============================================================================
# The coordinator on top of the degraded adapter
# =============================================================================


def _sample(name: str, **labels: str) -> float | None:
    """Read one prometheus sample, or None where that series was never recorded."""
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels)


# The rate-limit series are module-level collectors on the process-global
# registry, so their values survive every test in the same worker. Recording
# under a key nothing else has touched turns each read into an absolute value.
_COORDINATOR_KEY_SEQUENCE = itertools.count()


class TestCoordinatorOverADegradedAdapterBehavior:
    """``on_rate_limited`` runs to completion, so both series keep recording.

    This is what the shipped alert's re-scope rests on. "The 429 counter climbs
    while the cooldown histogram stays flat" is the degradation signal for a
    store that *raises* — and this adapter no longer does, so the divergence no
    longer opens for it. Driven through the real adapter rather than a raising
    mock, because the claim is about this adapter specifically; the mock-driven
    twin of this case (the divergence that still opens for a raising store)
    lives beside the coordinator and stays valid unchanged.
    """

    def test_a_429_during_an_outage_still_records_the_cooldown_it_installed(self):
        from baldur.services.rate_limit_coordinator.coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        # Given: a coordinator over an adapter whose backend died mid-run
        storage, _client, _clock = _degraded_storage()
        key = f"outage_cooldown_{next(_COORDINATOR_KEY_SEQUENCE)}"
        coordinator = RateLimitCoordinator(
            storage=storage,
            config=RateLimitCoordinatorConfig(
                jitter_percent=0.0,
                debounce_window_seconds=0.0,
            ),
        )

        # When: the upstream returns a 429 (the all-clear timer and the event
        # emit are stubbed out — neither is what this case pins, and the timer
        # would outlive the test)
        with (
            patch.object(coordinator, "_schedule_cooldown_end_event"),
            patch(
                "baldur.services.rate_limit_coordinator"
                ".coordinator._emit_rate_limit_event"
            ),
        ):
            in_force = coordinator.on_rate_limited(key)

        # Then: no exception, both series advanced, and the cooldown is real
        assert in_force > 0
        assert _sample("baldur_rate_limit_429_total", key=key, status_code="429") == 1.0
        assert _sample("baldur_rate_limit_cooldown_seconds_count", key=key) == 1.0
        assert storage.get_state(key).is_in_cooldown is True


# =============================================================================
# The mode/gauge state machine under a flapping backend
# =============================================================================


class _FallbackModeMachine(RuleBasedStateMachine):
    """Random flip / flip-back / per-command refusal against one adapter.

    Two invariants hold after every rule: the shipped gauge agrees with the
    mode it names, and the local store is empty whenever the mode is clear.
    Deliberately not framed as a deadlock proof — a fuzz run cannot show the
    absence of one, and it would present as a timeout kill with no attribution.
    """

    def __init__(self) -> None:
        super().__init__()
        drift_metrics.set_ratelimit_fallback_mode(False)
        self.clock = _FakeMonotonic()
        self.client = _FakeRedisClient()
        with patch("time.monotonic", self.clock):
            self.storage = RedisRateLimitStorage(self.client, ttl=_CONFIGURED_TTL)

    @rule()
    def the_backend_dies(self):
        self.client.down = True

    @rule()
    def the_backend_comes_back(self):
        self.client.down = False
        self.client.faults.clear()

    @rule(command=st.sampled_from(["get", "set", "incr", "expire", "delete"]))
    def the_backend_refuses_one_command(self, command):
        self.client.refuse(command)

    def _maybe_open_the_probe_window(self, window_open: bool) -> None:
        """Advancing the clock is folded into the caller rules on purpose.

        As a rule of its own it is a third independent draw, and the recovery
        half of this machine then needs three specific rules in order after the
        outage — which no generated schedule reached (measured: 13 entries into
        fallback, 0 exits, so the second invariant below was never evaluated in
        the state it exists for). Folded in, every caller is a potential probe.
        """
        if window_open:
            self.clock.advance(_max_probe_window() + 1)

    @rule(key=st.sampled_from([_KEY, _OTHER_KEY]), window_open=st.booleans())
    def a_caller_observes_a_429(self, key, window_open):
        self._maybe_open_the_probe_window(window_open)
        try:
            self.storage.increment_consecutive_429s(key)
        except RateLimitStorageUnavailableError:
            pass
        self.storage.extend_cooldown(key, time.time() + _COOLDOWN_SECONDS)

    @rule(key=st.sampled_from([_KEY, _OTHER_KEY]), window_open=st.booleans())
    def a_caller_checks_the_cooldown(self, key, window_open):
        self._maybe_open_the_probe_window(window_open)
        self.storage.get_state(key)

    @invariant()
    def the_gauge_agrees_with_the_mode(self):
        expected = 1 if self.storage._fallback_mode else 0
        assert _fallback_gauge() == expected

    @invariant()
    def the_local_store_is_empty_while_healthy(self):
        if not self.storage._fallback_mode:
            assert self.storage._delegate._data == {}


_FallbackModeMachine.TestCase.settings = hyp_settings(
    max_examples=100, deadline=None, stateful_step_count=20
)
TestFallbackModeStateMachine = _FallbackModeMachine.TestCase
