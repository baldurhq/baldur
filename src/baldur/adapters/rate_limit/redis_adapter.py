"""
Redis Rate Limit Storage Adapter

High-performance distributed rate limit storage using Redis.
Provides atomic operations for multi-server environments.

Requirements:
    - redis>=4.0.0

Features:
    - Atomic increment/set operations
    - Automatic TTL-based cleanup
    - Fastest option for distributed rate limiting
    - Per-worker cooldown fallback while Redis is unreachable, exited only on a
      write-verified recovery
"""

from __future__ import annotations

import math
import random
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

import structlog

from baldur.interfaces.rate_limit_storage import (
    RateLimitState,
    RateLimitStorageInterface,
    RateLimitStorageType,
    RateLimitStorageUnavailableError,
)

if TYPE_CHECKING:
    from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage

# Fallback-mode metrics
try:
    from baldur.metrics.drift_metrics import (
        record_ratelimit_redis_unavailable,
        set_ratelimit_fallback_mode,
    )

    HAS_DRIFT_METRICS = True
except ImportError:
    HAS_DRIFT_METRICS = False

    def record_ratelimit_redis_unavailable() -> None:
        return None

    def set_ratelimit_fallback_mode(active: bool) -> None:
        return None


# A reply error is the only fault this adapter cannot sort by exception class:
# redis-py raises a bare ResponseError both for a wrong-shaped stored value and
# for a server that is unusable (MISCONF, OOM, READONLY). The class is imported
# defensively because redis is an optional dependency.
try:
    from redis.exceptions import ResponseError

    _REPLY_ERROR_TYPES: tuple[type[BaseException], ...] = (ResponseError,)
except ImportError:  # pragma: no cover - redis absent
    _REPLY_ERROR_TYPES = ()


logger = structlog.get_logger()

_T = TypeVar("_T")

# Seconds of headroom added on top of a cooldown's remaining time when deriving
# the Redis key TTL, so the key cannot be evicted in the instant before the
# cooldown it carries expires. Not an operator tuning axis — the quantity is a
# rounding cushion, not a policy.
_COOLDOWN_TTL_MARGIN_SECONDS = 60

_EXTEND_COOLDOWN_SCRIPT = "rate_limit_extend_cooldown"

# Defaults applied when the settings tree cannot be read at all. The adapter
# constructor is deliberately settings-tolerant: a raise there escapes backend
# auto-detection and leaves the process with no outbound 429 coordination for
# its whole life, which is strictly worse than running on a default.
_DEFAULT_REDIS_TTL_SECONDS = 3600
_DEFAULT_RECOVERY_PROBE_INTERVAL_SECONDS = 30
_DEFAULT_DELEGATE_CLEANUP_INTERVAL_OPS = 100

# Reserved key the recovery write-probe operates on. Two segments, so it cannot
# collide with the three-segment real keys.
_RECOVERY_PROBE_KEY_NAME = "__recovery_probe__"

# The probe writes an integer-shaped value because the probe runs INCR against
# what its own SET wrote. A float-shaped value would make a fully healthy,
# fully permissive Redis answer "value is not an integer", and the process could
# then never leave fallback. The data itself is meaningless.
_RECOVERY_PROBE_VALUE = "0"

# Insurance TTL on the probe key, in case the trailing DEL never lands.
_RECOVERY_PROBE_TTL_SECONDS = 10

# Single-slot key for the probe interval: one recovery attempt per interval per
# process, not per rate-limit key.
_RECOVERY_PROBE_GATE_KEY = "redis_recovery_probe"

# Fraction of the probe interval the per-reservation jitter spans. Redis dies
# once, so unjittered gates arm within one timeout of each other and the whole
# fleet exits fallback on the same tick — each worker then spending its own 429
# on every still-cooling key. Staggering collapses that to roughly one 429 per
# key, because the first worker out installs the cooldown the later ones read.
_RECOVERY_PROBE_JITTER_RATIO = 0.2

# Reply-message prefixes that mean "the stored value is the wrong shape" rather
# than "the server is unusable". Narrow and protocol-stable; anything else falls
# to the transport class, i.e. toward protecting the caller.
#
# Matched against the reply BODY, not against str(exc): two client-side rewrites
# sit between the wire and the exception text, and matching the raw string means
# the rule never fires against a real server.
#   - redis-py strips a recognised error code before constructing the exception,
#     so `ERR value is not an integer or out of range` arrives without its `ERR`.
#     Both forms are listed, because a proxy or a non-redis-py client need not
#     strip it.
#   - a *pipelined* command's error is re-wrapped as
#     `Command # N (CMD key) of pipeline caused error: <reply>` — and this
#     adapter issues every read and every INCR through a pipeline, so this is
#     the normal case here rather than the exotic one.
_PIPELINE_ERROR_MARKER = "of pipeline caused error: "
_DATA_ERROR_REPLY_PREFIXES = (
    "WRONGTYPE",
    "value is not an integer",
    "ERR value is not an integer",
)

# How far to follow __cause__ when classifying a failure. Domain wrappers are
# raised ``from`` the client error, so the classification has to survive one or
# two hops; the bound keeps a pathological chain from being walked forever.
_ERROR_CAUSE_DEPTH = 5

# Monotonic cooldown write, atomic across processes and hosts.
#
# Single KEY on purpose: the shared LuaScriptRegistry rejects multi-key calls
# whose keys land in different hash slots, and on Redis Cluster a second key
# named through ARGV is a non-local key the server refuses outright. The
# last_updated bookkeeping therefore stays outside the script.
#
# The TTL is derived from the *effective* expiry, never from the caller's
# candidate: a short headerless 429 arriving against a long honored cooldown
# would otherwise shrink the key's TTL below the expiry it carries and let Redis
# drop a live cooldown early. Only the script knows the effective value.
LUA_EXTEND_COOLDOWN = """
local stored = tonumber(redis.call('GET', KEYS[1]) or '0') or 0
local candidate = tonumber(ARGV[1])
local now = tonumber(ARGV[2])
local configured_ttl = tonumber(ARGV[3])
local margin = tonumber(ARGV[4])

local effective = stored
if candidate > effective then
    effective = candidate
end

local ttl = math.ceil(effective - now) + margin
if ttl < configured_ttl then
    ttl = configured_ttl
end
if ttl < 1 then
    ttl = 1
end

redis.call('SET', KEYS[1], string.format('%.6f', effective), 'EX', string.format('%d', ttl))
return string.format('%.6f', effective)
"""


def _rate_limit_setting(field: str, default: int) -> int:
    """Read one RateLimitSettings integer field, defaulting when unreachable."""
    try:
        from baldur.settings.rate_limit import get_rate_limit_settings

        return int(getattr(get_rate_limit_settings(), field))
    except Exception:
        return default


def _get_redis_ttl() -> int:
    """Read the Redis TTL from RateLimitSettings."""
    return _rate_limit_setting("redis_ttl", _DEFAULT_REDIS_TTL_SECONDS)


def _get_recovery_probe_interval() -> int:
    """Read the recovery-probe interval from RateLimitSettings."""
    return _rate_limit_setting(
        "redis_recovery_probe_interval_seconds",
        _DEFAULT_RECOVERY_PROBE_INTERVAL_SECONDS,
    )


def _get_delegate_cleanup_interval() -> int:
    """Read the sweep cadence the fallback delegate is constructed with.

    Resolved here rather than left to the in-memory store's own ``None``
    default, which reads the same settings class unguarded: that read inside
    this adapter's constructor would turn one out-of-range BALDUR_RATE_LIMIT_*
    value into a process with no outbound 429 coordination at all.
    """
    return _rate_limit_setting(
        "memory_cleanup_interval_ops",
        _DEFAULT_DELEGATE_CLEANUP_INTERVAL_OPS,
    )


def _error_chain(error: BaseException) -> list[BaseException]:
    """The exception plus the causes it was raised ``from``, outermost first."""
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and len(chain) < _ERROR_CAUSE_DEPTH:
        chain.append(current)
        current = current.__cause__
    return chain


def _reply_message_body(error: BaseException) -> str:
    """The server's reply text, with the client's pipeline wrapper removed."""
    return str(error).rsplit(_PIPELINE_ERROR_MARKER, 1)[-1]


def _is_data_error(error: BaseException) -> bool:
    """Is this a wrong-shaped stored value rather than an unusable backend?

    A data fault belongs to exactly the key that carries it; a transport fault
    belongs to the whole adapter. Degrading the adapter over one bad byte string
    would drop every key onto the per-worker store, and — because the recovery
    probe then passes against the healthy server while the next read re-poisons
    — flap the mode, the gauge and the transition log for as long as the value
    exists.

    The read arm raises ``ValueError``/``TypeError`` from parsing. The write arm
    raises a bare reply error, so it is sorted by the prefix of the reply body;
    an unrecognised reply error falls to transport, never to a false all-clear.
    """
    for cause in _error_chain(error):
        if isinstance(cause, ValueError | TypeError):
            return True
        if isinstance(cause, _REPLY_ERROR_TYPES) and _reply_message_body(
            cause
        ).startswith(_DATA_ERROR_REPLY_PREFIXES):
            return True
    return False


class RedisRateLimitStorage(RateLimitStorageInterface):
    """
    Redis-based rate limit storage.

    Uses Redis for atomic distributed rate limit state management.
    Recommended for production multi-server environments.

    Runtime degradation:
    - While Redis is unreachable every operation is served from a private
      per-worker in-memory store, so the outbound cooldown keeps being enforced
      rather than disappearing for the length of the outage. Coordination is per
      worker for that window, not fleet-wide.
    - The degraded window reads 1 on ``baldur_ratelimit_fallback_active``, and
      each entry into it increments
      ``baldur_ratelimit_redis_unavailable_total``.
    - Recovery is verified before it is believed. One caller per probe interval
      pings and then replays this adapter's own write vocabulary against a
      reserved probe key, so a backend that answers PING while refusing writes
      cannot produce a false all-clear.
    - Nothing is carried back at recovery: the local store is discarded, and
      each still-cooling key re-arms in Redis through its own next 429.
    - The local store starts empty at every outage, so a cooldown that lived
      only in Redis reads as absent on the first degraded call for that key and
      is re-installed by that call's own 429.

    Key schema:
        ratelimit:{key}:cooldown_until - float timestamp
        ratelimit:{key}:consecutive_429s - int counter
        ratelimit:{key}:last_updated - float timestamp

    Example:
        redis_client = redis.Redis(host='localhost', port=6379, db=0)
        storage = RedisRateLimitStorage(redis_client)

        # Set cooldown after 429
        storage.set_cooldown("payment_api", time.time() + 60)
    """

    KEY_PREFIX = "ratelimit"
    DEFAULT_TTL = 3600  # Legacy constant kept for backward compatibility

    def __init__(self, redis_client: Any, ttl: int | None = None) -> None:
        """
        Initialize Redis rate limit storage.

        Args:
            redis_client: Redis client instance (redis.Redis or compatible)
            ttl: Redis key TTL (seconds). Taken from settings when None.
        """
        from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage
        from baldur.audit.performance.lua_registry import LuaScriptRegistry
        from baldur.core.rate_limiting import CooldownGate

        self._redis = redis_client
        self._ttl = ttl if ttl is not None else _get_redis_ttl()
        self._available: bool | None = None
        # Runtime-outage fallback. The delegate holds only what this worker
        # observed during the current outage: it is cleared at every verified
        # exit, so its resident set is bounded by the distinct coordination keys
        # touched inside one outage window.
        self._fallback_mode = False
        self._delegate: InMemoryRateLimitStorage = InMemoryRateLimitStorage(
            cleanup_interval=_get_delegate_cleanup_interval()
        )
        # Serializes the mode transitions, the delegate clear, and the fallback
        # writes' mode re-check. Never held across a network call.
        self._sync_lock = threading.Lock()
        # Collapses concurrent recovery attempts to one, including the ungated
        # one arriving through is_available() on the registry-cached instance.
        self._exit_attempt_lock = threading.Lock()
        # Monotonic clock on purpose: the default wall clock can step backwards
        # (an NTP correction, a resumed snapshot, a late container sync), which
        # would shut the gate for the size of the step and strand the process in
        # fallback. The cooldown values themselves stay wall-clock, because they
        # are absolute deadlines compared across workers.
        self._probe_gate = CooldownGate(clock=time.monotonic)
        # The jittered interval the current outage is gated on. Held rather than
        # redrawn per call, and never zero — zero disables the gate outright,
        # which would put a connect attempt back on every protected call.
        self._probe_window_seconds = self._draw_probe_window()
        self._lua = LuaScriptRegistry(redis_client)
        self._lua.register(_EXTEND_COOLDOWN_SCRIPT, LUA_EXTEND_COOLDOWN)
        # Sticky once a reachable backend has proven it cannot run Lua (a client
        # without scripting support, an ACL denying @scripting, an EVAL-rejecting
        # proxy). Deliberately unlocked and never reset: the worst race is one
        # extra WARNING per thread already inside the window, once per process.
        self._script_fallback = False

    @property
    def storage_type(self) -> RateLimitStorageType:
        return RateLimitStorageType.REDIS

    def _make_key(self, key: str, suffix: str) -> str:
        """Generate Redis key with prefix."""
        return f"{self.KEY_PREFIX}:{key}:{suffix}"

    def _recovery_probe_key(self) -> str:
        """The reserved key the recovery write-probe operates on."""
        return f"{self.KEY_PREFIX}:{_RECOVERY_PROBE_KEY_NAME}"

    # =========================================================================
    # Runtime-outage fallback: entry, service, verified exit
    # =========================================================================

    def _draw_probe_window(self) -> float:
        """Draw this outage's probe interval, jittered.

        Drawn once per entry into fallback and held until the next entry, never
        redrawn per call. ``CooldownGate`` evicts against the window stored with
        the reservation but decides suppression against the CALL's value, so it
        opens at the *minimum* of the two: a fresh draw on every denied call
        would let a process taking many calls converge on the jitter floor, and
        the whole fleet would re-align there — under exactly the 429 storm the
        stagger exists for. One draw per outage keeps each worker on its own
        phase, so the first worker out installs the cooldown in Redis and the
        later ones read it instead of each spending their own 429.
        """
        interval = _get_recovery_probe_interval()
        return interval * random.uniform(
            1.0 - _RECOVERY_PROBE_JITTER_RATIO,
            1.0 + _RECOVERY_PROBE_JITTER_RATIO,
        )

    def _enter_fallback(self, error: BaseException) -> None:
        """Latch the transition into per-worker service."""
        with self._sync_lock:
            self._enter_fallback_locked(error)

    def _enter_fallback_locked(self, error: BaseException) -> None:
        """Write every transition effect as one latched unit under the lock.

        Only the False->True edge writes: the flag, the gauge, the unavailable
        counter and one log line move together, so no interleaving can leave the
        gauge disagreeing with the mode or double-count the transition.

        The edge also consumes a probe slot, so the call that just failed does
        not immediately pay a second Redis attempt through the recovery canary.
        """
        if self._fallback_mode:
            return

        from baldur.settings.redis import redis_absence_is_expected

        self._fallback_mode = True
        set_ratelimit_fallback_mode(True)
        record_ratelimit_redis_unavailable()
        if redis_absence_is_expected():
            logger.debug(
                "redis_rate_limit_storage.redis_unavailable",
                error=error,
            )
        else:
            logger.warning(
                "redis_rate_limit_storage.redis_unavailable",
                error=error,
            )
        self._probe_window_seconds = self._draw_probe_window()
        self._probe_gate.try_reserve(
            _RECOVERY_PROBE_GATE_KEY, self._probe_window_seconds
        )

    def _recovery_verified_this_call(self) -> bool:
        """Did this caller win the probe slot and prove Redis usable again?

        A denied reservation is the steady outage path: the delegate serves and
        Redis is not touched. A granted one that fails its probe leaves the slot
        consumed, so the next canary waits a full interval.
        """
        granted, _token = self._probe_gate.try_reserve(
            _RECOVERY_PROBE_GATE_KEY, self._probe_window_seconds
        )
        if not granted:
            return False
        return self._try_exit_fallback()

    def _try_exit_fallback(self) -> bool:
        """Verify Redis is usable and, if so, leave fallback. Never raises.

        A successful ping alone is not recovery, so the ping is followed by a
        write probe over this adapter's own command vocabulary. Any failure —
        including an unexpected internal one — reports "still fallback": an
        error escaping here would reach the caller's fail-open wrap after the
        mode had already flipped.
        """
        with self._sync_lock:
            if not self._fallback_mode:
                # is_available()'s recovery half routes here on every ordinary
                # resolution-time call, where the mode was never set. Without
                # this the healthy admission path would pay the whole probe
                # pipeline and clear a delegate it has no reason to touch.
                return False

        if not self._exit_attempt_lock.acquire(blocking=False):
            return False
        try:
            self._redis.ping()
            self._run_recovery_write_probe()
            self._clear_fallback_after_verified_probe()
            return True
        except Exception as e:
            # Fails closed on anything, the flip included: reporting "still
            # fallback" costs one probe interval, while letting an internal
            # error escape hands it to a fail-open wrap that would drop the
            # caller's cooldown entirely.
            logger.debug(
                "redis_rate_limit_storage.recovery_probe_failed",
                error=e,
            )
            return False
        finally:
            self._exit_attempt_lock.release()

    def _run_recovery_write_probe(self) -> None:
        """Prove the backend accepts this adapter's whole write vocabulary.

        A backend that answers PING and reads while refusing writes — a
        read-only failover replica, a MISCONF full disk — or one that accepts
        SET while an ACL denies INCR/EXPIRE, must not produce a false all-clear.
        EVAL is deliberately not probed: a reachable Lua-less Redis is genuinely
        serviceable through the sticky unscripted path.
        """
        probe_key = self._recovery_probe_key()
        pipeline = self._redis.pipeline()
        pipeline.set(probe_key, _RECOVERY_PROBE_VALUE, ex=_RECOVERY_PROBE_TTL_SECONDS)
        pipeline.incr(probe_key)
        pipeline.expire(probe_key, _RECOVERY_PROBE_TTL_SECONDS)
        pipeline.delete(probe_key)
        pipeline.execute()

    def _clear_fallback_after_verified_probe(self) -> None:
        """Clear the mode, the gauge and the local state as one step.

        The delegate is discarded rather than replayed: nothing it holds is
        written back, and each still-cooling key re-arms in Redis through its own
        next 429. A fallback write racing this clear either lands before it — and
        is discarded with the rest — or finds the mode already clear under the
        same lock and re-routes to Redis.
        """
        with self._sync_lock:
            if not self._fallback_mode:
                return
            self._fallback_mode = False
            set_ratelimit_fallback_mode(False)
            self._delegate.clear_all()
        logger.info("redis_rate_limit_storage.fallback_recovered")

    def _dispatch(
        self,
        redis_call: Callable[[], _T],
        delegate_call: Callable[[RateLimitStorageInterface], _T],
        on_data_error: Callable[[Exception], _T],
    ) -> _T:
        """The wrapped-method flow every delegating method shares.

        While the mode is set the delegate serves without touching Redis, except
        for the one caller per probe interval whose reservation succeeds — it
        runs the verified exit and, on success, proceeds on Redis. A transport
        failure latches the outage and serves the delegate; a data failure keeps
        this method's per-key behaviour and moves neither the mode, the gauge nor
        the probe gate.
        """
        if self._fallback_mode and not self._recovery_verified_this_call():
            with self._sync_lock:
                if self._fallback_mode:
                    return delegate_call(self._delegate)

        try:
            return redis_call()
        except Exception as e:
            if _is_data_error(e):
                return on_data_error(e)
            with self._sync_lock:
                self._enter_fallback_locked(e)
                return delegate_call(self._delegate)

    # =========================================================================
    # Availability
    # =========================================================================

    def is_available(self) -> bool:
        """Check if Redis is available.

        A resolution-time probe: the bool it returns is ping-based, as before.
        Its recovery half routes through the same single-flight verified exit the
        data paths use — a ping alone is never treated as recovery — and its
        failure half through the same latched transition, so this method is not a
        second, unsynchronised writer of the fallback mode or the gauge.

        The unavailable edge is announced at WARNING, except when nobody
        configured Redis outside production: this adapter is only
        auto-constructed against the shipped default URL in that posture, so
        the failure is the framework finding its own default unreachable.
        """
        try:
            self._redis.ping()
        except Exception as e:
            self._enter_fallback(e)
            self._available = False
            return False

        self._try_exit_fallback()
        self._available = True
        return True

    # =========================================================================
    # Reads
    # =========================================================================

    def _read_state(self, key: str) -> RateLimitState:
        """Read the three state keys in one round trip. Lets Redis errors escape."""
        pipeline = self._redis.pipeline()
        pipeline.get(self._make_key(key, "cooldown_until"))
        pipeline.get(self._make_key(key, "consecutive_429s"))
        pipeline.get(self._make_key(key, "last_updated"))

        results = pipeline.execute()

        return RateLimitState(
            key=key,
            cooldown_until=float(results[0]) if results[0] else 0.0,
            consecutive_429s=int(results[1]) if results[1] else 0,
            last_updated=float(results[2]) if results[2] else 0.0,
        )

    def _read_state_or_raise(self, key: str) -> RateLimitState:
        """Strict read straight from Redis, never merged with the local state.

        The unscripted write path needs the raise — a folded 0.0 would let a
        short candidate overwrite a live long cooldown fleet-wide — without the
        local merge the public strict read applies, because a locally-derived
        value must not be written back into Redis.
        """
        try:
            return self._read_state(key)

        except Exception as e:
            logger.exception(
                "redis_rate_limit_storage.get_state_strict_failed",
                error=e,
            )
            raise RateLimitStorageUnavailableError(str(e)) from e

    def _merged_with_local(self, remote: RateLimitState) -> RateLimitState:
        """Field-wise max of a fresh Redis read and the local fallback state.

        Applies only while the fallback is active. In that window this process
        is enforcing a cooldown Redis has not seen, and a caller handed the
        remote value alone would read a definite "ended" for it — exactly the
        answer the strict read exists to prevent.
        """
        with self._sync_lock:
            if not self._fallback_mode:
                return remote
            local = self._delegate.get_state(remote.key)

        return RateLimitState(
            key=remote.key,
            cooldown_until=max(remote.cooldown_until, local.cooldown_until),
            consecutive_429s=max(remote.consecutive_429s, local.consecutive_429s),
            last_updated=max(remote.last_updated, local.last_updated),
        )

    def get_state(self, key: str) -> RateLimitState:
        """Get rate limit state from Redis, or from the local store while degraded."""

        def on_data_error(error: Exception) -> RateLimitState:
            logger.exception(
                "redis_rate_limit_storage.get_state_failed",
                error=error,
            )
            return RateLimitState(key=key)

        return self._dispatch(
            lambda: self._read_state(key),
            lambda delegate: delegate.get_state(key),
            on_data_error,
        )

    def get_state_strict(self, key: str) -> RateLimitState:
        """Get rate limit state from Redis, raising instead of folding on failure.

        Always attempts Redis, even while the fallback is active: serving the
        per-worker store here would be exactly the fold this variant exists to
        forbid. A successful read during that window is merged with the local
        state, so a locally-enforced cooldown is never reported as ended.
        """
        try:
            state = self._read_state_or_raise(key)
        except RateLimitStorageUnavailableError as e:
            if not _is_data_error(e):
                self._enter_fallback(e)
            raise
        return self._merged_with_local(state)

    # =========================================================================
    # Writes
    # =========================================================================

    def set_cooldown(
        self,
        key: str,
        cooldown_until: float,
        ttl: int | None = None,
    ) -> None:
        """Set cooldown in Redis with TTL, or in the local store while degraded."""

        def on_redis() -> None:
            ttl_seconds = ttl or self._ttl
            now = time.time()

            pipeline = self._redis.pipeline()
            pipeline.set(
                self._make_key(key, "cooldown_until"),
                str(cooldown_until),
                ex=ttl_seconds,
            )
            pipeline.set(
                self._make_key(key, "last_updated"),
                str(now),
                ex=ttl_seconds,
            )
            pipeline.execute()

            logger.debug(
                "redis_rate_limit_storage.set_cooldown",
                redis_key=key,
                cooldown_until=cooldown_until,
                ttl=ttl_seconds,
            )

        def on_data_error(error: Exception) -> None:
            logger.exception(
                "redis_rate_limit_storage.set_cooldown_failed",
                error=error,
            )
            raise RateLimitStorageUnavailableError(str(error)) from error

        self._dispatch(
            on_redis,
            lambda delegate: delegate.set_cooldown(key, cooldown_until, ttl),
            on_data_error,
        )

    def extend_cooldown(
        self,
        key: str,
        cooldown_until: float,
        ttl: int | None = None,
    ) -> float:
        """Move the cooldown end time later in one atomic server-side max-merge.

        Falls back to a read-modify-write when this Redis cannot run Lua. That
        fallback is unsynchronised — it is monotonic with respect to what it
        read, so a longer write landing during its round trip is lost, whether
        that writer is another thread here or another host. It is still the only
        acceptable degradation: raising instead would reach every caller's
        fail-open wrap and install no cooldown at all — the worst outcome
        available, on exactly the storm the cooldown exists to damp.

        When Redis itself is unreachable the merge runs against the local store
        and the local effective time is returned, for the same reason.
        """

        def on_data_error(error: Exception) -> float:
            logger.exception(
                "redis_rate_limit_storage.extend_cooldown_failed",
                error=error,
            )
            raise RateLimitStorageUnavailableError(str(error)) from error

        return self._dispatch(
            lambda: self._extend_cooldown_on_redis(key, cooldown_until, ttl),
            lambda delegate: delegate.extend_cooldown(key, cooldown_until, ttl),
            on_data_error,
        )

    def _extend_cooldown_on_redis(
        self,
        key: str,
        cooldown_until: float,
        ttl: int | None,
    ) -> float:
        """Scripted max-merge, with the plain-command path as the capability fallback."""
        if self._script_fallback:
            return self._extend_cooldown_unscripted(key, cooldown_until, ttl)

        try:
            return self._extend_cooldown_scripted(key, cooldown_until, ttl)
        except Exception as script_error:
            # The plain-command path decides what this failure was: if it also
            # fails the backend is down and the error propagates, if it succeeds
            # the backend is reachable and scripting specifically is unusable.
            effective = self._extend_cooldown_unscripted(key, cooldown_until, ttl)
            self._script_fallback = True
            logger.warning(
                "redis_rate_limit_storage.script_fallback",
                redis_key=key,
                error=script_error,
            )
            return effective

    def _extend_cooldown_scripted(
        self,
        key: str,
        cooldown_until: float,
        ttl: int | None,
    ) -> float:
        """Atomic max-merge through the shared Lua registry."""
        now = time.time()
        effective = float(
            self._lua.execute(
                _EXTEND_COOLDOWN_SCRIPT,
                keys=[self._make_key(key, "cooldown_until")],
                args=[
                    repr(cooldown_until),
                    repr(now),
                    ttl or self._ttl,
                    _COOLDOWN_TTL_MARGIN_SECONDS,
                ],
            )
        )
        self._touch_last_updated(key, now, self._covering_ttl(effective, now, ttl))
        return effective

    def _extend_cooldown_unscripted(
        self,
        key: str,
        cooldown_until: float,
        ttl: int | None,
    ) -> float:
        """Read-modify-write max-merge, for a Redis that cannot run Lua.

        Reads strictly, and straight from Redis: a folded read would report no
        stored cooldown and let a short candidate replace a live long one, which
        is the very regression the monotonic contract exists to close, and a read
        merged with the local store would send a locally-derived value into Redis.
        """
        now = time.time()
        stored = self._read_state_or_raise(key).cooldown_until
        effective = max(stored, cooldown_until)
        covering_ttl = self._covering_ttl(effective, now, ttl)

        pipeline = self._redis.pipeline()
        pipeline.set(
            self._make_key(key, "cooldown_until"),
            str(effective),
            ex=covering_ttl,
        )
        pipeline.set(
            self._make_key(key, "last_updated"),
            str(now),
            ex=covering_ttl,
        )
        pipeline.execute()

        return effective

    def _touch_last_updated(self, key: str, now: float, ttl: int) -> None:
        """Write the last_updated marker beside the cooldown (best-effort).

        Kept out of the Lua script because a second key would make the call
        multi-key, which the shared registry rejects on standalone Redis and
        Redis Cluster refuses outright. Nothing branches on this value, so an
        unordered write beside the atomic one costs nothing.
        """
        try:
            self._redis.set(self._make_key(key, "last_updated"), str(now), ex=ttl)
        except Exception as e:
            logger.debug(
                "redis_rate_limit_storage.last_updated_write_skipped",
                redis_key=key,
                error=e,
            )

    def _covering_ttl(self, effective: float, now: float, ttl: int | None) -> int:
        """TTL that outlives the effective expiry it is applied to."""
        configured = ttl or self._ttl
        return max(
            configured,
            math.ceil(effective - now) + _COOLDOWN_TTL_MARGIN_SECONDS,
        )

    def increment_consecutive_429s(self, key: str) -> int:
        """Increment the 429 counter — atomically in Redis, or in the local store."""

        def on_redis() -> int:
            redis_key = self._make_key(key, "consecutive_429s")

            # Atomic increment with TTL
            pipeline = self._redis.pipeline()
            pipeline.incr(redis_key)
            pipeline.expire(redis_key, self._ttl)
            results = pipeline.execute()

            new_value: int = results[0]
            logger.debug(
                "redis_rate_limit_storage.incremented_counter",
                redis_key=key,
                new_value=new_value,
            )
            return new_value

        def on_data_error(error: Exception) -> int:
            logger.exception(
                "redis_rate_limit_storage.increment_failed",
                error=error,
            )
            raise RateLimitStorageUnavailableError(str(error)) from error

        return self._dispatch(
            on_redis,
            lambda delegate: delegate.increment_consecutive_429s(key),
            on_data_error,
        )

    def reset_consecutive_429s(self, key: str) -> None:
        """Reset the 429 counter. Best-effort toward Redis, as before.

        Issued while Redis is unreachable it applies locally; the Redis-side
        value survives the outage and self-heals on the next successful
        coordinated call for that key.
        """

        def on_redis() -> None:
            self._redis.delete(self._make_key(key, "consecutive_429s"))
            logger.debug(
                "redis_rate_limit_storage.reset_counter",
                redis_key=key,
            )

        def on_data_error(error: Exception) -> None:
            logger.exception(
                "redis_rate_limit_storage.reset_failed",
                error=error,
            )

        self._dispatch(
            on_redis,
            lambda delegate: delegate.reset_consecutive_429s(key),
            on_data_error,
        )

    def clear(self, key: str) -> None:
        """Clear all rate limit state for a key. Never raises, as before.

        Best-effort toward Redis: issued while Redis is unreachable it clears the
        local state this process is enforcing, and the Redis-side value survives
        until recovery, so the fleet — this worker included — resumes honoring it
        afterwards. The remedy is to re-issue the clear once Redis is back.
        """

        def on_redis() -> None:
            pipeline = self._redis.pipeline()
            pipeline.delete(self._make_key(key, "cooldown_until"))
            pipeline.delete(self._make_key(key, "consecutive_429s"))
            pipeline.delete(self._make_key(key, "last_updated"))
            pipeline.execute()

            logger.debug(
                "redis_rate_limit_storage.cleared_state",
                redis_key=key,
            )

        def on_data_error(error: Exception) -> None:
            logger.exception(
                "redis_rate_limit_storage.clear_failed",
                error=error,
            )

        self._dispatch(
            on_redis,
            lambda delegate: delegate.clear(key),
            on_data_error,
        )
