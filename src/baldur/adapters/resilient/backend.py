# verified-by: test_wal_survives_memory_clear
"""
Resilient Storage Backend Implementation.

Redis-First + Graceful Degradation + WAL architecture
for zero data loss guarantees.

Key Principles:
- WAL-First: In degraded mode, WAL is written BEFORE memory (server crash safe)
- Zero Data Loss: WAL enables recovery after server restart
- Redis-Only: Normal mode uses Redis exclusively for simplicity
"""

from __future__ import annotations

import base64
import os
import random
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Any

import structlog

from baldur.core.exceptions import ConfigurationError

if TYPE_CHECKING:
    from baldur.settings.resilient_storage import ResilientStorageSettings

logger = structlog.get_logger()

# Environment variable an operator sets to choose this backend's WAL
# directory. Named in fallback warnings, in the WAL configuration error and
# in the production boot gate's break-glass instruction.
WAL_DIR_ENV_VAR = "BALDUR_RESILIENT_STORAGE_WAL_DIR"

# Prevent garbled logs from OS error messages emitted as cp949 under a
# Windows Korean locale. Normalize to errno-based English messages.
_ERRNO_MESSAGES: dict[int, str] = {
    10061: "Connection refused",  # WSAECONNREFUSED
    10060: "Connection timed out",  # WSAETIMEDOUT
    10065: "No route to host",  # WSAEHOSTUNREACH
    111: "Connection refused",  # ECONNREFUSED (Linux)
    110: "Connection timed out",  # ETIMEDOUT (Linux)
}


def _safe_error_message(e: Exception) -> str:
    """Return an ASCII-safe error description (avoids cp949 garbled text on Windows)."""
    if isinstance(e, OSError) and e.errno in _ERRNO_MESSAGES:
        return f"{type(e).__name__}: {_ERRNO_MESSAGES[e.errno]} (errno={e.errno})"
    try:
        msg = str(e)
        msg.encode("ascii")
        return msg
    except (UnicodeEncodeError, UnicodeDecodeError):
        return f"{type(e).__name__}(errno={getattr(e, 'errno', 'N/A')})"


class ResilientStorageMode(str, Enum):
    """Storage operation mode."""

    REDIS = "redis"  # Normal mode - Redis only
    DEGRADED = "degraded"  # Fallback mode - Memory + WAL
    RECOVERING = "recovering"  # Transitioning back to Redis


class ResilientStorageBackend:
    """
    Redis-First + Graceful Degradation + WAL.

    Single-process zero-loss storage backend that:
    - Uses Redis in normal mode
    - Falls back to Memory + WAL on Redis failure
    - Recovers from WAL on server restart
    - Syncs WAL to Redis on recovery

    Zero-loss coverage (degraded mode):
    - WAL-backed + replayable: KV (set), hash (hset/hdel), sorted-set
      (zadd/zrem), counter (incr, replayed as set-to-absolute), and blob
      (set_blob) ops. Scope is **single-process** — concurrent worker
      processes sharing one wal_dir can collide degraded-allocated DLQ IDs
      (SB-012, deferred); the guarantee holds for the tested -w 1 topology.
    - Accept-loss: list-history (lpush/ltrim) is non-idempotent append/trim
      and is intentionally NOT WAL-backed (DLQ uses neither; only
      non-critical history features do). See D7 #470.

    Core Invariant:
        WAL-First Write Protocol in degraded mode:
        1. WAL.write() + fsync() - disk persisted first
        2. Memory[key] = value  - then memory

        This ensures server crash at any point is recoverable.
    """

    def __init__(self, settings: ResilientStorageSettings | None = None):
        """
        Initialize Resilient Storage Backend.

        Args:
            settings: Storage settings. When None, the singleton from
                ``get_resilient_storage_settings()`` is used so
                ``BALDUR_RESILIENT_STORAGE_*`` env vars apply
                automatically. Tests can pass an instance directly with
                overridden fields to bypass the singleton.
        """
        if settings is None:
            from baldur.settings.resilient_storage import (
                get_resilient_storage_settings,
            )

            settings = get_resilient_storage_settings()
        self.config = settings
        self._mode = ResilientStorageMode.DEGRADED
        # Monotonic count of REDIS -> degraded transitions. See ``degrade_count``.
        self._degrade_count = 0
        self._lock = threading.RLock()

        # Redis client
        self._redis: Any | None = None
        self._redis_initialized = False
        self._next_redis_probe: float = 0.0
        self._REDIS_PROBE_INTERVAL: float = 30.0
        self._degraded_critical_logged: bool = False
        # One-time WARNING flag for the first degraded blob eviction
        # (D2 #539). Reset on recovery success alongside
        # ``_degraded_critical_logged`` so each sustained outage that sheds
        # blobs re-warns operators.
        self._degraded_blob_memory_full_logged: bool = False

        # Recovery dispatch (D2): non-reentrant lock — at most one
        # dispatcher in flight. The lock is released by the daemon
        # thread's ``finally`` clause. Read paths are kept off this
        # lock entirely; ``self._lock`` (RLock) covers mode/state.
        self._recovery_lock = threading.Lock()

        # WAL for disk-based recovery queue
        self._wal: Any | None = None
        self._wal_initialized = False
        # True when the WAL is usable but landed on a fallback directory
        # rather than the configured one. Runtime write paths do not consult
        # it - a fallback WAL is a working WAL - only the production boot
        # gate, which promises the WAL is on its configured directory.
        self._wal_on_fallback_dir = False

        # Memory fallback
        self._memory: dict[str, Any] = {}

        # Degraded-mode blob store (D2 #539): a dedicated bounded
        # OrderedDict keyed by short key, separate from ``_memory`` so the
        # blob OOM amplifier is bounded by a byte budget without touching
        # non-blob degraded values. ``_blob_memory_bytes`` is the running
        # accumulator that drives least-recently-written eviction in
        # ``_mem_apply_set_blob``.
        self._blob_memory: OrderedDict[str, bytes] = OrderedDict()
        self._blob_memory_bytes: int = 0

        # Shadow Logger for forensic logging
        self._shadow: Any | None = None

        # Last processed WAL sequence
        self._last_processed_wal_seq: int = 0

        # Initialize components (Redis is deferred to _ensure_redis())
        self._init_components()

    def _init_components(self) -> None:
        """Initialize local components (WAL, ShadowLogger). Redis is deferred."""
        self._init_wal()
        self._init_shadow_logger()

    def _get_full_key(self, key: str) -> str:
        """
        Return the full key with the dynamic prefix applied.

        When TestModeContext is active, an ``xtest:`` prefix is prepended so
        production data and test data stay separated.

        Args:
            key: Original key

        Returns:
            Full key with the prefix applied
            - Production mode: "baldur:dlq:pending"
            - Synthetic mode: "xtest:baldur:dlq:pending"
        """
        if self.config.use_dynamic_prefix:
            from baldur.settings.namespace import get_effective_key_prefix

            prefix = get_effective_key_prefix()
            return f"{prefix}{key}"

        return f"{self.config.key_prefix}{key}"

    def _ensure_redis(self) -> bool:  # noqa: C901
        """Lazy Redis init / degraded-mode recovery dispatch — called at
        start of each operation.

        Three-way branch (D1 #470):

        - REDIS (already initialized & mode==REDIS): fast return True.
        - DEGRADED (``_redis_initialized`` is True but mode reverted to
          DEGRADED via ``_switch_to_degraded()``): cooldown-gated
          background recovery dispatch. Returns False — the caller
          falls through to the degraded-mode read path. The dispatch
          uses ``recovery_probe_interval`` (default 5s) instead of the
          30s first-init cooldown so the diverged-write window is
          capped at PRO scale.
        - Uninitialized (``_redis_initialized`` is False): existing
          first-init lazy probe with 30s cooldown on failure (437).

        Returns True only when Redis is currently available for hot
        path use. On failure paths the operation is expected to fall
        back to memory + WAL.
        """
        # Fast path — Redis is currently the active backend.
        if self._redis_initialized and self._mode == ResilientStorageMode.REDIS:
            return True

        # Degraded re-entry path — Redis was once connected but has
        # since failed. Dispatch background recovery, but always return
        # False so the caller goes through the degraded read path.
        if self._redis_initialized and self._mode == ResilientStorageMode.DEGRADED:
            self._maybe_dispatch_recovery()
            return False

        # First-init path — Redis has never been connected.
        if time.monotonic() < self._next_redis_probe:
            return False

        try:
            from baldur.adapters.redis import _REDIS_RETRY_INTERVAL, _redis_state

            _state = _redis_state()
            if _state.unavailable:
                elapsed = time.monotonic() - _state.fail_time
                if elapsed < _REDIS_RETRY_INTERVAL:
                    self._next_redis_probe = (
                        time.monotonic() + self._REDIS_PROBE_INTERVAL
                    )
                    return False
        except ImportError:
            pass

        with self._lock:
            if self._redis_initialized:
                return self._mode == ResilientStorageMode.REDIS
            try:
                from baldur.adapters.cache import RedisCacheAdapter
                from baldur.settings.redis import get_redis_settings

                self._redis = RedisCacheAdapter(
                    url=self.config.redis_url,
                    key_prefix="",
                    socket_connect_timeout=get_redis_settings().probe_connect_timeout,
                )
                self._redis.raw_client.ping()
                self._redis_initialized = True
                self._mode = ResilientStorageMode.REDIS
                logger.info("resilient_storage.redis_connected")
                self._recover_from_wal_on_startup()
                return True
            except Exception as e:
                err_msg = _safe_error_message(e)
                logger.warning(
                    "resilient_storage.lazy_redis_probe_failed", error=err_msg
                )
                self._next_redis_probe = time.monotonic() + self._REDIS_PROBE_INTERVAL
                if not self._degraded_critical_logged:
                    if self._shadow:
                        try:
                            self._shadow.record_sync_failure(
                                service_name="redis_init",
                                intended_state="connected",
                                error=e,
                                adapter_type="redis",
                            )
                        except Exception:
                            logger.debug("resilient_storage.shadow_record_failed")
                    if not self.config.allow_memory_only:
                        logger.critical(
                            "resilient_storage.degraded_mode_entered",
                            reason="redis_unavailable",
                            fallback="memory_wal",
                        )
                    self._degraded_critical_logged = True
                return False

    @property
    def raw_redis_client(self) -> Any:
        """Return the underlying redis client, or None when Redis is inactive.

        Public seam for composed repositories (the Redis DLQ and
        circuit-breaker state repositories) that need the raw client for
        operations the cache interface does not expose. Returns None when the
        backend has no live Redis adapter (degraded / uninitialized) so
        callers can fall through to their degraded-mode path.
        """
        return self._redis.raw_client if self._redis is not None else None

    def ensure_redis(self) -> bool:
        """Public seam over the internal lazy-init for composed repositories.

        Lazily initializes Redis (or dispatches degraded-mode recovery) and
        returns True only when Redis is currently available for hot-path use.
        Production consumer: the Redis DLQ repository's availability gate.
        """
        return self._ensure_redis()

    def _maybe_dispatch_recovery(self) -> None:
        """Cooldown-gated, non-blocking dispatch of degraded-mode
        recovery (D1+D2+D3 #470).

        - ``auto_recovery=False`` short-circuits — operators can opt
          out of the recovery loop entirely (emergency kill switch).
        - When ``recovery_probe_interval`` has not yet elapsed since
          the last dispatch, returns immediately with a DEBUG log.
        - Otherwise tries to acquire ``_recovery_lock`` non-blocking;
          on success spawns a daemon thread running
          ``check_and_recover()``. Concurrent callers fail the lock
          try and return. The lock is released in the thread's
          ``finally``.
        """
        if not self.config.auto_recovery:
            return

        now = time.monotonic()
        if now < self._next_redis_probe:
            logger.debug(
                "resilient_storage.recovery_cooldown_active",
                seconds_until_next=self._next_redis_probe - now,
            )
            return

        if not self._recovery_lock.acquire(blocking=False):
            logger.debug("resilient_storage.recovery_skipped")
            return

        # Cooldown is set BEFORE dispatch so concurrent callers
        # arriving while the thread is mid-flight see "cooldown
        # active" instead of trying to acquire the lock again.
        self._next_redis_probe = now + self.config.recovery_probe_interval

        thread = threading.Thread(
            target=self._run_recovery_payload,
            name=f"baldur-resilient-recovery-{os.getpid()}",
            daemon=True,
        )
        logger.debug("resilient_storage.recovery_dispatched")
        thread.start()

    def _run_recovery_payload(self) -> None:
        """Daemon-thread entry point for recovery dispatch (D4 #470).

        Wraps ``check_and_recover()`` so:

        - Ping pre-check short-circuits when Redis is still down.
        - Jitter (``recovery_jitter_max``) disperses the thundering
          herd when N workers' cooldowns expire near-simultaneously.
        - On success, ``_degraded_critical_logged`` is reset (D5) so
          the next degraded entry logs CRITICAL again — recurring
          flapping stays visible to operators.
        - The recovery lock is always released, even on exception.
        """
        try:
            recovered = self.check_and_recover()
            if recovered:
                with self._lock:
                    self._degraded_critical_logged = False
                    # Reset the one-time blob-eviction WARNING flag (D2 #539)
                    # so the next sustained outage that sheds blobs re-warns.
                    self._degraded_blob_memory_full_logged = False
                logger.info("resilient_storage.recovery_succeeded")
            else:
                logger.warning("resilient_storage.recovery_failed")
        except Exception as e:
            logger.exception(
                "resilient_storage.recovery_failed",
                _safe_error_message=_safe_error_message(e),
            )
        finally:
            self._recovery_lock.release()

    def _init_wal(self) -> None:
        """Initialize Write-Ahead Log.

        Directory resolution lives inside ``WriteAheadLog``: an
        operator-chosen ``wal_dir`` that is unwritable raises, while the
        shipped default falls back to a writable location.
        """
        try:
            from baldur.audit.wal import WALConfig, WriteAheadLog

            wal_config = WALConfig(
                wal_dir=self.config.wal_dir,
                wal_dir_operator_set=("wal_dir" in self.config.model_fields_set),
                wal_dir_env_var=WAL_DIR_ENV_VAR,
                sync_on_write=True,  # fsync guarantee - server crash safe
                file_prefix="resilient_storage",
            )

            self._wal = WriteAheadLog(config=wal_config)
            resolved = self._wal.resolved_dir
            self._wal_on_fallback_dir = bool(resolved and resolved.fell_back)
            self._wal_initialized = True
            logger.debug("resilient_storage.wal_initialized")

        except ConfigurationError as e:
            # Expected, handled condition - the primitive already warned
            # with the resolved paths, so no traceback here.
            logger.warning(
                "resilient_storage.wal_init_failed",
                wal_dir=self.config.wal_dir,
                override_env=WAL_DIR_ENV_VAR,
                _safe_error_message=_safe_error_message(e),
            )
            self._wal_initialized = False
        except Exception as e:
            logger.exception(
                "resilient_storage.wal_init_failed",
                _safe_error_message=_safe_error_message(e),
            )
            # WAL failure is serious but we continue with memory-only
            self._wal_initialized = False

    @property
    def _wal_honors_configured_dir(self) -> bool:
        """Whether the WAL runs on the directory it was configured with."""
        return self._wal_initialized and not self._wal_on_fallback_dir

    def _init_shadow_logger(self) -> None:
        """Initialize Shadow Logger for forensic logging."""
        try:
            from baldur.adapters.memory.shadow_logger import get_shadow_logger

            self._shadow = get_shadow_logger()
        except Exception:
            pass  # Shadow logger is optional

    def _recover_from_wal_on_startup(
        self,
    ) -> None:  # verified-by: test_wal_survives_memory_clear
        """
        Recover unprocessed WAL entries on server startup.

        This is the key to zero data loss:
        - Server crashed in degraded mode? WAL has all changes.
        - WAL entries are replayed to Redis on startup.
        """
        if not self._wal_initialized or not self._wal:
            return

        try:
            stats = self._wal.get_stats()
            if stats.total_entries == 0:
                return  # Nothing to recover

            logger.info(
                "resilient_storage.found_wal_entries_check",
                stats=stats.last_sequence,
            )

            # If Redis is unavailable, defer recovery
            if self._mode == ResilientStorageMode.DEGRADED:
                logger.warning("resilient_storage.redis_unavailable_wal_recovery")
                return

            # Replay WAL entries to Redis
            entries = self._wal.recover_unprocessed(
                last_processed_seq=self._last_processed_wal_seq
            )

            recovered_count = 0
            for entry in entries:
                try:
                    self._replay_wal_entry(entry)
                    self._last_processed_wal_seq = entry.sequence
                    recovered_count += 1
                except Exception as e:
                    logger.exception(
                        "resilient_storage.wal_replay_failed_seq",
                        wal_sequence=entry.sequence,
                        _safe_error_message=_safe_error_message(e),
                    )
                    # Continue with next entry

            if recovered_count > 0:
                logger.info(
                    "resilient_storage.recovered_entries_wal",
                    recovered_count=recovered_count,
                )

                # Cleanup processed WAL entries
                self._wal.cleanup_processed(self._last_processed_wal_seq)

        except Exception as e:
            logger.exception(
                "resilient_storage.wal_recovery_failed",
                _safe_error_message=_safe_error_message(e),
            )
            # Recovery failure doesn't prevent server from starting

    def _replay_wal_entry(self, entry: Any) -> None:
        """Replay a single WAL entry to Redis via the dispatch table.

        ``_REPLAY_DISPATCH`` maps a WAL operation name to its handler. A
        ``.get()`` miss preserves the legacy ``unknown_wal_operation``
        WARNING (the replay-side half of the #470 coverage gap). D9's
        guard introspects ``set(self._REPLAY_DISPATCH)`` to assert the
        write vocabulary is a subset of the handled vocabulary.
        """
        if not self._redis or not self._redis_initialized:
            return

        operation = entry.data.get("operation")
        handler = self._REPLAY_DISPATCH.get(operation)
        if handler is None:
            logger.warning(
                "resilient_storage.unknown_wal_operation",
                operation=operation,
            )
            return
        handler(self, entry.data)

    def _replay_set(self, data: dict[str, Any]) -> None:
        """Replay a ``set`` record (also the replay action for ``incr``,
        whose degraded WAL record uses the ``set`` op carrying the
        absolute counter value — see D4)."""
        # Replay handlers are dispatched only from _replay_wal_entry, which
        # guards `if not self._redis ...: return` before calling them.
        assert self._redis is not None
        self._redis.raw_client.set(data["key"], self._redis._serialize(data["value"]))

    def _replay_hset(self, data: dict[str, Any]) -> None:
        """Replay an ``hset`` record."""
        assert self._redis is not None
        mapping = {str(k): str(v) for k, v in data["value"].items()}
        self._redis.raw_client.hset(data["key"], mapping=mapping)

    def _replay_delete(self, data: dict[str, Any]) -> None:
        """Replay a ``delete`` record."""
        assert self._redis is not None
        self._redis.raw_client.delete(data["key"])

    def _replay_hdel(self, data: dict[str, Any]) -> None:
        """Replay an ``hdel`` record."""
        assert self._redis is not None
        field = data.get("field")
        if field:
            self._redis.raw_client.hdel(data["key"], field)

    def _replay_zadd(self, data: dict[str, Any]) -> None:
        """Replay a ``zadd`` record. Idempotent: re-ZADD sets the same
        score; WAL-sequence order preserves cross-call ordering against
        ``zrem`` so the final index state reconstructs correctly."""
        assert self._redis is not None
        self._redis.raw_client.zadd(data["key"], data["value"])

    def _replay_zrem(self, data: dict[str, Any]) -> None:
        """Replay a ``zrem`` record. Idempotent: removing an absent
        member is a no-op."""
        assert self._redis is not None
        members = data.get("members") or []
        if members:
            self._redis.raw_client.zrem(data["key"], *members)

    def _replay_set_blob(self, data: dict[str, Any]) -> None:
        """Replay a ``set_blob`` record: base64-decode the value and raw
        ``set`` it, bypassing ``_serialize`` so the wire payload is the
        exact original bytes."""
        assert self._redis is not None
        self._redis.raw_client.set(data["key"], base64.b64decode(data["value"]))

    # Operation name -> replay handler. The op name is the WAL record's
    # ``operation`` field; keys here form the authoritative replay
    # vocabulary (D9 guard). ``incr`` is absent because it reuses ``set``.
    _REPLAY_DISPATCH: dict[str, Callable[..., None]] = {
        "set": _replay_set,
        "hset": _replay_hset,
        "delete": _replay_delete,
        "hdel": _replay_hdel,
        "zadd": _replay_zadd,
        "zrem": _replay_zrem,
        "set_blob": _replay_set_blob,
    }

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def mode(self) -> ResilientStorageMode:
        """Get current storage mode."""
        return self._mode

    @property
    def is_degraded(self) -> bool:
        """Check if operating in degraded mode."""
        return self._mode != ResilientStorageMode.REDIS

    @property
    def is_redis_available(self) -> bool:
        """Check if Redis is available."""
        return self._redis_initialized and self._mode == ResilientStorageMode.REDIS

    @property
    def degrade_count(self) -> int:
        """Monotonic count of transitions out of REDIS mode.

        A read that fails answers with the process-local memory view instead
        of raising, so a caller cannot tell a substituted result from a real
        one by its shape — an empty page and a non-empty page are both
        reachable from a failed fetch. Sampling this counter before and after
        a multi-page walk turns that into a decidable question: the counter is
        bumped by ``_switch_to_degraded()``, the only edge that leaves REDIS
        mode, so an unchanged sample across a walk that started available
        means every page was served by Redis itself.

        The increment happens *before* the mode write, so a lockless reader
        that observes non-REDIS mode has necessarily already observed the
        bump. Any future transition path that can leave REDIS without passing
        through ``_switch_to_degraded()`` would break that guarantee.
        """
        return self._degrade_count

    # =========================================================================
    # Core Operations
    # =========================================================================

    def get(self, key: str) -> Any | None:
        """
        Get value by key.

        Args:
            key: Key without prefix (prefix is auto-added)

        Returns:
            Value if exists, None otherwise
        """
        self._ensure_redis()
        full_key = self._get_full_key(key)

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                return self._redis.get(full_key)
            except Exception:
                self._switch_to_degraded()
                return self._memory.get(key)
        else:
            return self._memory.get(key)

    def set(self, key: str, value: Any) -> bool:
        """
        Set value.

        In degraded mode, uses WAL-First protocol:
        1. Write to WAL (disk) with fsync
        2. Write to Memory

        Args:
            key: Key without prefix
            value: Value to store

        Returns:
            True on success
        """
        self._ensure_redis()
        full_key = self._get_full_key(key)

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                self._redis.set(full_key, value)
                return True
            except Exception as e:
                self._switch_to_degraded()
                return self._set_degraded(key, full_key, value, e)
        else:
            return self._set_degraded(key, full_key, value, None)

    def _set_degraded(
        self, key: str, full_key: str, value: Any, error: Exception | None
    ) -> bool:
        """
        Set in degraded mode using WAL-First protocol.

        CRITICAL: WAL must be written BEFORE memory!
        This ensures server crash at any point is recoverable.

        The WAL-write + memory-mutation block runs under ``self._lock``
        (D1 #539) so a write landing in the ``_do_recovery`` window is
        mutually exclusive with the locked finalize's clear/flip — closing
        the recovery-window write-visibility race.
        """
        # 1+2. WAL first (disk with fsync), then memory — under the lock.
        with self._lock:
            if self._wal and self._wal_initialized:
                self._wal.write(
                    {
                        "operation": "set",
                        "key": full_key,
                        "value": value,
                        "timestamp": time.time(),
                    }
                )

            self._memory[key] = value

        # 3. Forensic logging (optional)
        if error and self._shadow:
            self._shadow.record_sync_failure(
                service_name=key,
                intended_state=str(value),
                error=error,
                adapter_type="redis",
            )

        return True

    def delete(self, key: str) -> bool:
        """Delete value by key."""
        self._ensure_redis()
        full_key = self._get_full_key(key)

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                self._redis.delete(full_key)
                return True
            except Exception:
                self._switch_to_degraded()

        # Degraded mode: WAL first, memory second — under the lock (D1 #539).
        # Record/mem helpers are shared with batch_write_ops (544 D6); the
        # mem helper handles both the regular memory pop AND the bounded
        # blob-store pop with byte-accumulator decrement (D2 #539), so a
        # degraded delete is visible to a subsequent ``get_blob`` and the
        # accumulator stays exact.
        with self._lock:
            if self._wal and self._wal_initialized:
                self._wal.write(self._wal_record_delete(full_key))

            self._mem_apply_delete(key)
        return True

    # =========================================================================
    # Hash Operations (for CB, DLQ)
    # =========================================================================

    def hget(self, key: str, field: str) -> Any | None:
        """Get hash field value."""
        self._ensure_redis()
        full_key = self._get_full_key(key)

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                result = self._redis.raw_client.hget(full_key, field)
                if result is None:
                    return None
                if isinstance(result, bytes):
                    return result.decode("utf-8")
                return result
            except Exception:
                self._switch_to_degraded()
                return self._memory.get(key, {}).get(field)
        else:
            return self._memory.get(key, {}).get(field)

    def hset(self, key: str, mapping: dict[str, Any]) -> bool:
        """Set hash fields."""
        self._ensure_redis()
        full_key = self._get_full_key(key)

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                # Convert all values to strings for Redis
                str_mapping = {str(k): str(v) for k, v in mapping.items()}
                self._redis.raw_client.hset(full_key, mapping=str_mapping)
                return True
            except Exception as e:
                self._switch_to_degraded()
                return self._hset_degraded(key, full_key, mapping, e)
        else:
            return self._hset_degraded(key, full_key, mapping, None)

    def _hset_degraded(
        self,
        key: str,
        full_key: str,
        mapping: dict[str, Any],
        error: Exception | None,
    ) -> bool:
        """Hash set in degraded mode using WAL-First protocol.

        WAL-write + memory-mutation run under ``self._lock`` (D1 #539) to
        close the ``_do_recovery`` recovery-window write-visibility race.
        """
        # 1+2. WAL first, then memory — under the lock.
        with self._lock:
            if self._wal and self._wal_initialized:
                self._wal.write(
                    {
                        "operation": "hset",
                        "key": full_key,
                        "value": mapping,
                        "timestamp": time.time(),
                    }
                )

            if key not in self._memory:
                self._memory[key] = {}
            self._memory[key].update(mapping)

        return True

    def hgetall(self, key: str) -> dict[str, Any]:
        """Get all hash fields."""
        self._ensure_redis()
        full_key = self._get_full_key(key)

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                result = self._redis.raw_client.hgetall(full_key)
                if not result:
                    return {}
                # Decode bytes
                return {
                    (k.decode() if isinstance(k, bytes) else k): (
                        v.decode() if isinstance(v, bytes) else v
                    )
                    for k, v in result.items()
                }
            except Exception:
                self._switch_to_degraded()
                return self._memory.get(key, {})
        else:
            return self._memory.get(key, {})

    def hdel(self, key: str, field: str) -> bool:
        """Delete hash field."""
        self._ensure_redis()
        full_key = self._get_full_key(key)

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                self._redis.raw_client.hdel(full_key, field)
                return True
            except Exception:
                self._switch_to_degraded()

        # Degraded mode — WAL-write + memory-mutation under the lock (D1 #539).
        with self._lock:
            if self._wal and self._wal_initialized:
                self._wal.write(
                    {
                        "operation": "hdel",
                        "key": full_key,
                        "field": field,
                        "timestamp": time.time(),
                    }
                )

            if key in self._memory and isinstance(self._memory[key], dict):
                self._memory[key].pop(field, None)

        return True

    # =========================================================================
    # List Operations (for history)
    # =========================================================================

    def lpush(self, key: str, *values: Any) -> int:
        """Push values to list head."""
        self._ensure_redis()
        full_key = self._get_full_key(key)

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                from baldur.utils.serialization import fast_dumps_str

                serialized = [fast_dumps_str(v) for v in values]
                return self._redis.raw_client.lpush(full_key, *serialized)
            except Exception:
                self._switch_to_degraded()

        # Degraded mode: accept-loss. lpush is a non-idempotent append, so
        # it is deliberately NOT WAL-backed (replay would duplicate). The
        # in-memory history is also dropped by the D6 list-skip on
        # runtime-recovery. Only non-critical history features use this;
        # DLQ does not. NON-GOAL for zero-loss (D7).
        if key not in self._memory:
            self._memory[key] = []
        for v in reversed(values):
            self._memory[key].insert(0, v)
        return len(self._memory[key])

    def lrange(self, key: str, start: int, end: int) -> list[Any]:
        """Get list range."""
        self._ensure_redis()
        full_key = self._get_full_key(key)

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                from baldur.utils.serialization import fast_loads

                result = self._redis.raw_client.lrange(full_key, start, end)
                return [fast_loads(v) for v in result]
            except Exception:
                self._switch_to_degraded()
                return self._memory.get(key, [])[start : end + 1 if end >= 0 else None]
        else:
            return self._memory.get(key, [])[start : end + 1 if end >= 0 else None]

    def ltrim(self, key: str, start: int, end: int) -> bool:
        """Trim list to specified range."""
        self._ensure_redis()
        full_key = self._get_full_key(key)

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                self._redis.raw_client.ltrim(full_key, start, end)
                return True
            except Exception:
                self._switch_to_degraded()

        # Degraded mode: accept-loss. ltrim is a non-idempotent trim, so it
        # is deliberately NOT WAL-backed (replay would mis-trim). Paired
        # with lpush — see that branch and D7. NON-GOAL for zero-loss.
        if key in self._memory:
            self._memory[key] = self._memory[key][start : end + 1 if end >= 0 else None]
        return True

    # =========================================================================
    # Sorted Set Operations (for DLQ pending queue)
    # =========================================================================

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        """Add members to sorted set with scores."""
        self._ensure_redis()
        full_key = self._get_full_key(key)

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                return self._redis.raw_client.zadd(full_key, mapping)
            except Exception:
                self._switch_to_degraded()

        # Degraded mode: WAL-First, then memory (simple list simulation).
        # One record per call carries the full {member: score} mapping.
        # Record-build + mem-mutate go through the shared helpers so the
        # batched grouped-op path (batch_write_ops, 538 D3) produces
        # byte-identical replay-relevant fields by construction. The
        # WAL-write + memory-mutation run under the lock (D1 #539) to close
        # the recovery-window write-visibility race.
        with self._lock:
            if self._wal and self._wal_initialized:
                self._wal.write(self._wal_record_zadd(full_key, mapping))

            self._mem_apply_zadd(key, mapping)
        return len(mapping)

    def zrange(self, key: str, start: int, end: int) -> list[str]:
        """Get sorted set range by index."""
        self._ensure_redis()
        full_key = self._get_full_key(key)

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                result = self._redis.raw_client.zrange(full_key, start, end)
                return [v.decode() if isinstance(v, bytes) else v for v in result]
            except Exception:
                self._switch_to_degraded()

        # Degraded mode
        items = self._memory.get(key, [])
        end_idx = end + 1 if end >= 0 else None
        return [item["member"] for item in items[start:end_idx]]

    def zrevrange(self, key: str, start: int, end: int) -> list[str]:
        """Get sorted set range by index in descending score order."""
        self._ensure_redis()
        full_key = self._get_full_key(key)

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                result = self._redis.raw_client.zrevrange(full_key, start, end)
                return [v.decode() if isinstance(v, bytes) else v for v in result]
            except Exception:
                self._switch_to_degraded()

        # Degraded mode: _mem_apply_zadd keeps self._memory[key] sorted
        # ascending by score, so reverse it first then apply the same
        # start:end_idx slice convention as zrange.
        items = list(reversed(self._memory.get(key, [])))
        end_idx = end + 1 if end >= 0 else None
        return [item["member"] for item in items[start:end_idx]]

    def zrangebyscore(
        self,
        key: str,
        min_score: float,
        max_score: float,
        *,
        offset: int = 0,
        count: int | None = None,
    ) -> list[str]:
        """Get sorted set members within an inclusive score window, ascending.

        Lets a caller resume a scan from a score rather than from the head of
        the index. A scan that filters on data held outside the score — a
        status kept in the entry blob, say — otherwise has to re-read every
        member it has already passed on each call.
        """
        self._ensure_redis()
        full_key = self._get_full_key(key)

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                result = self._redis.raw_client.zrangebyscore(
                    full_key,
                    min_score,
                    max_score,
                    start=offset,
                    num=count if count is not None else -1,
                )
                return [v.decode() if isinstance(v, bytes) else v for v in result]
            except Exception:
                self._switch_to_degraded()

        # Degraded mode: _mem_apply_zadd keeps self._memory[key] sorted
        # ascending by score, so filtering preserves the order.
        selected = [
            item["member"]
            for item in self._memory.get(key, [])
            if min_score <= item["score"] <= max_score
        ]
        if count is None:
            return selected[offset:]
        return selected[offset : offset + count]

    def zrem(self, key: str, *members: str) -> int:
        """Remove members from sorted set."""
        self._ensure_redis()
        full_key = self._get_full_key(key)

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                return self._redis.raw_client.zrem(full_key, *members)
            except Exception:
                self._switch_to_degraded()

        # Degraded mode: WAL-First, then memory. Ordering relative to
        # zadd is preserved by WAL sequence so the final index is correct.
        # Record/mem helpers are shared with batch_write_ops (544 D6) so
        # the batched path's replay-relevant fields are byte-identical to
        # the per-op path. WAL-write + memory-mutation under the lock
        # (D1 #539).
        members_list = list(members)
        with self._lock:
            if self._wal and self._wal_initialized:
                self._wal.write(self._wal_record_zrem(full_key, members_list))

            return self._mem_apply_zrem(key, members_list)

    def zcard(self, key: str) -> int:
        """Get sorted set cardinality."""
        self._ensure_redis()
        full_key = self._get_full_key(key)

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                return self._redis.raw_client.zcard(full_key)
            except Exception:
                self._switch_to_degraded()

        return len(self._memory.get(key, []))

    def zcount(self, key: str, min_score: float, max_score: float) -> int:
        """Count sorted-set members with score in the inclusive [min, max] range."""
        self._ensure_redis()
        full_key = self._get_full_key(key)

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                return self._redis.raw_client.zcount(full_key, min_score, max_score)
            except Exception:
                self._switch_to_degraded()

        # Degraded mode: _mem_apply_zadd keeps items sorted ascending by score.
        return sum(
            1
            for item in self._memory.get(key, [])
            if min_score <= item["score"] <= max_score
        )

    # =========================================================================
    # Atomic Operations
    # =========================================================================

    def incr(self, key: str) -> int:
        """Atomically increment counter."""
        self._ensure_redis()
        full_key = self._get_full_key(key)

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                return self._redis.raw_client.incr(full_key)
            except Exception:
                self._switch_to_degraded()

        # Degraded mode (not truly atomic but best effort).
        # The WAL write MUST stay under self._lock: only then does
        # WAL-sequence order equal value order, so replaying the
        # highest-sequence "set" yields this process's max counter value.
        # The record reuses the "set" operation because the replay action
        # is set-to-absolute (DRY _replay_set; no extra dispatch entry).
        with self._lock:
            current = self._memory.get(key, 0)
            new_value = int(current) + 1
            if self._wal and self._wal_initialized:
                self._wal.write(
                    {
                        "operation": "set",
                        "key": full_key,
                        "value": new_value,
                        "timestamp": time.time(),
                    }
                )
            self._memory[key] = new_value
            return new_value

    # =========================================================================
    # Blob Operations (raw bytes — for DLQ entry payloads)
    # =========================================================================

    def set_blob(self, key: str, blob: bytes) -> bool:
        """Store raw bytes verbatim under ``key``, bypassing serialization.

        Unlike :meth:`set` (which orjson-encodes the value), the bytes are
        written to Redis as-is so the wire payload is exactly the caller's
        bytes (DLQ stores zlib-compressed orjson blobs this way).

        In degraded mode, WAL-First with a base64-encoded value: the WAL
        record is a single orjson document and JSON strings cannot carry
        raw bytes, so the blob is base64-wrapped on write and decoded by
        :meth:`_replay_set_blob` on replay. The op name ``set_blob`` is
        itself the encoding marker (no separate ``encoding`` field).
        """
        self._ensure_redis()
        full_key = self._get_full_key(key)

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                self._redis.raw_client.set(full_key, blob)
                return True
            except Exception:
                self._switch_to_degraded()

        # Degraded mode: WAL-First (base64 value), then memory. Shared with
        # batch_write_ops (538 D3) via the record/mem helpers below. The
        # WAL-write + memory-mutation run under the lock (D1 #539): the lock
        # closes the recovery-window race AND is the precondition the bounded
        # ``_mem_apply_set_blob`` helper (D2) assumes — its byte-budget
        # eviction must run mutually exclusive with the recovery finalize's
        # ``_blob_memory.clear()`` + accumulator reset.
        with self._lock:
            if self._wal and self._wal_initialized:
                self._wal.write(self._wal_record_set_blob(full_key, blob))

            self._mem_apply_set_blob(key, blob)
        return True

    def get_blob(self, key: str) -> bytes | None:
        """Load raw bytes stored by :meth:`set_blob`.

        Returns the verbatim bytes from Redis (normal mode) or memory
        (degraded mode), without deserialization.
        """
        self._ensure_redis()
        full_key = self._get_full_key(key)

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                return self._redis.raw_client.get(full_key)
            except Exception:
                self._switch_to_degraded()
                # Degraded read from the bounded blob store (D2 #539). An
                # evicted blob returns None — degraded-read-invisible until
                # recovery, never lost (durable in WAL, reconstructed by
                # _do_recovery / _recover_from_wal_on_startup replay).
                return self._blob_memory.get(key)
        else:
            return self._blob_memory.get(key)

    def get_blobs(self, keys: list[str]) -> list[bytes | None]:
        """Batched raw-bytes read — one ``MGET`` per call; callers chunk.

        Mirrors :meth:`get_blob` on the paths that matter so an aggregate
        walk can collapse N round trips into one:

        - ``_ensure_redis()`` runs first, exactly as every sibling primitive
          does. The backend is *constructed* in DEGRADED and promoted only by
          that call, so a mode-check-first read would return all-``None`` on a
          fresh process against a healthy Redis.
        - An empty key list short-circuits to ``[]`` before any command:
          ``MGET`` with zero arguments is a Redis syntax error, and the blanket
          ``except`` would answer it by degrading the whole backend.
        - Normal mode issues one ``raw_client.mget``; on error it degrades and
          serves every key from the bounded blob store, matching ``get_blob``'s
          fallback. Degraded mode serves the blob store directly.

        Read-only: no WAL record, no degraded-batch vocabulary impact. Not a
        drop-in for a loop of ``get_blob`` — ``MGET`` returns ``nil`` for a
        wrong-typed key where ``GET`` raises ``WRONGTYPE``, and a mid-call
        Redis error degrades the whole call to memory rather than returning the
        bytes already fetched. Both are acceptable for an aggregate walk.
        """
        self._ensure_redis()
        if not keys:
            return []
        full_keys = [self._get_full_key(k) for k in keys]

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                return self._redis.raw_client.mget(full_keys)
            except Exception:
                self._switch_to_degraded()
                return [self._blob_memory.get(k) for k in keys]
        else:
            return [self._blob_memory.get(k) for k in keys]

    # =========================================================================
    # Grouped-op transactional write (538 D3)
    # =========================================================================

    # Extracted WAL-record builders and in-memory mutators. set_blob/zadd and
    # batch_write_ops both call these, so the batched path's replay-relevant
    # `data` fields (operation/key/value/members) are identical to the per-op
    # path by construction (no format-drift between single and batched writes).
    # WAL records carry the full key; in-memory mutations use the short key.

    def _wal_record_set_blob(self, full_key: str, blob: bytes) -> dict[str, Any]:
        """Build the WAL record for a ``set_blob`` op (base64-wrapped value)."""
        return {
            "operation": "set_blob",
            "key": full_key,
            "value": base64.b64encode(blob).decode("ascii"),
            "timestamp": time.time(),
        }

    def _wal_record_zadd(
        self, full_key: str, mapping: dict[str, float]
    ) -> dict[str, Any]:
        """Build the WAL record for a ``zadd`` op (full {member: score} map)."""
        return {
            "operation": "zadd",
            "key": full_key,
            "value": mapping,
            "timestamp": time.time(),
        }

    def _wal_record_zrem(self, full_key: str, members: list[str]) -> dict[str, Any]:
        """Build the WAL record for a ``zrem`` op.

        Mirrors the per-op zrem record shape (``operation/key/members/
        timestamp``) so the batched and per-op replay paths produce
        byte-identical replay-relevant fields.
        """
        return {
            "operation": "zrem",
            "key": full_key,
            "members": list(members),
            "timestamp": time.time(),
        }

    def _wal_record_delete(self, full_key: str) -> dict[str, Any]:
        """Build the WAL record for a ``delete`` op.

        Mirrors the per-op delete record shape — ``key`` only, no value
        body — so the batched and per-op replay paths converge.
        """
        return {
            "operation": "delete",
            "key": full_key,
            "timestamp": time.time(),
        }

    def _mem_apply_set_blob(self, key: str, blob: bytes) -> None:
        """Apply a ``set_blob`` mutation to the bounded blob store (D2 #539).

        The single shared in-memory blob write point — called by both
        degraded blob inflows (standalone ``set_blob`` for DLQ update, and
        ``_batch_write_ops_degraded`` for DLQ create, the primary inflow).
        Maintains ``self._blob_memory_bytes`` and evicts the
        least-recently-*written* blobs once the byte budget is exceeded.

        Eviction sheds degraded-read visibility for the oldest blobs — it
        is never data loss: the blob is durably in WAL and reconstructed in
        Redis on recovery. ``get_blob`` does not promote on read, so the
        order is write-recency (for the DLQ workload every meaningful access
        is a SET, so write-recency tracks access-recency).

        Runs under the caller's ``self._lock`` (standalone ``set_blob``
        wraps it per D1; ``_batch_write_ops_degraded`` already holds it), so
        the accumulator + eviction are mutually exclusive with the recovery
        finalize's ``_blob_memory.clear()`` + ``_blob_memory_bytes`` reset.
        """
        # Overwrite (hot path — D4's _update does GET->mutate->SET on every
        # status/retry-count change): subtract the old blob's bytes BEFORE
        # storing the new one, else the accumulator over-counts the same key
        # across repeated updates and evicts live entries early.
        if key in self._blob_memory:
            self._blob_memory_bytes -= len(self._blob_memory[key])

        self._blob_memory[key] = blob
        # A plain assignment does NOT reorder an existing key, so promote it
        # to most-recently-written explicitly.
        self._blob_memory.move_to_end(key)
        self._blob_memory_bytes += len(blob)

        # Evict least-recently-written blobs until within the byte budget.
        # The ``and self._blob_memory`` guard keeps the loop from underflowing
        # (a single blob larger than the cap is evicted within this call —
        # degraded-read-invisible immediately, still durable in WAL).
        cap = self.config.degraded_blob_memory_max_bytes
        while self._blob_memory_bytes > cap and self._blob_memory:
            evicted_key, evicted_blob = self._blob_memory.popitem(last=False)
            self._blob_memory_bytes -= len(evicted_blob)
            if not self._degraded_blob_memory_full_logged:
                logger.warning(
                    "resilient_storage.degraded_blob_memory_full",
                    cap_bytes=cap,
                    blob_memory_bytes=self._blob_memory_bytes,
                )
                self._degraded_blob_memory_full_logged = True
            logger.debug(
                "resilient_storage.degraded_blob_evicted",
                evicted_key=evicted_key,
                blob_memory_bytes=self._blob_memory_bytes,
            )

    def _mem_apply_zadd(self, key: str, mapping: dict[str, float]) -> None:
        """Apply a ``zadd`` mutation to the in-memory store (short key)."""
        if key not in self._memory:
            self._memory[key] = []
        for member, score in mapping.items():
            self._memory[key].append({"member": member, "score": score})
        self._memory[key].sort(key=lambda x: x["score"])

    def _mem_apply_zrem(self, key: str, members: list[str]) -> int:
        """Apply a ``zrem`` mutation to the in-memory store (short key).

        Returns the number of members actually removed so the per-op
        ``zrem`` caller can report the same count Redis would. The
        batched path discards the return value.
        """
        if not members:
            return 0
        if key not in self._memory:
            return 0
        before = len(self._memory[key])
        member_set = set(members)
        self._memory[key] = [
            item for item in self._memory[key] if item["member"] not in member_set
        ]
        return before - len(self._memory[key])

    def _mem_apply_delete(self, key: str) -> None:
        """Apply a ``delete`` mutation to the in-memory + bounded blob store.

        Mirrors the per-op ``delete`` degraded body at :meth:`delete` —
        ``_memory.pop`` plus ``_blob_memory.pop`` with the byte
        accumulator decrement so a batched delete leaves no orphan blob
        entry or accumulator drift.
        """
        self._memory.pop(key, None)
        removed_blob = self._blob_memory.pop(key, None)
        if removed_blob is not None:
            self._blob_memory_bytes -= len(removed_blob)

    def batch_write_ops(self, ops: list[tuple[str, str, Any]]) -> bool:
        """Perform an ordered list of degraded-vocabulary ops as one
        transactional unit (538 D3).

        Each op is ``(op_name, key, value)`` where ``op_name`` is one of
        ``set_blob`` / ``zadd`` / ``zrem`` / ``delete`` and ``key`` is the
        prefix-less component key:
          - ``("set_blob", key, blob)``
          - ``("zadd", key, {member: score})``
          - ``("zrem", key, members)`` — ``members`` is ``list[str]`` or a
            single ``str``
          - ``("delete", key, None)`` — value slot is unused

        Modes:
        - **normal**: buffer the ops on a single ``transaction=False``
          pipeline and issue them as one round-trip (543 D1). On *any*
          pipeline failure ``_switch_to_degraded()`` then re-apply the
          **entire** op list via the degraded path. set_blob/zadd/zrem/
          delete replay is idempotent (set-to-value / zadd-to-score /
          zrem-of-absent = no-op / DEL-of-absent returns 0), so re-applying
          ops that already reached Redis is safe — the entry ends up
          consistently in *either* Redis *or* degraded WAL+memory, never
          split. Under ``transaction=False`` partial server-side
          application is always a *prefix* of the in-order op list;
          blob-first ordering at the sole caller (``dlq.py``) guarantees
          a prefix never leaves an index entry without its blob.
        - **degraded**: build one WAL record per op and write them all with a
          single ``batch_write_entries`` fsync, then apply each in-memory
          mutation. Degraded create drops from 4 fsyncs → 1.

        **Idempotent-op-only invariant (543 D3)**: only replay-idempotent
        ops may be added to this vocabulary. ``set_blob`` (SET-to-value),
        ``zadd`` (ZADD-to-score), ``zrem`` (``_replay_zrem`` — removing an
        absent member is a no-op) and ``delete`` (``_replay_delete`` — DEL
        of an absent key returns 0 with no error) all qualify because
        re-applying them after a partial pipeline application reproduces
        the same final state. A non-idempotent op (``hset`` field-merge,
        ``incr``, list-append) would double-apply on the "re-apply the
        **entire** op list" failure path and silently corrupt the entry.
        The ``else: raise ValueError`` allowlist below is the mechanical
        guard at the call boundary — this invariant governs which ops
        are *eligible* to enter that allowlist.

        Honours the lock-symmetry rule: does NOT call the public
        ``set_blob`` / ``zadd`` / ``zrem`` / ``delete`` (public→public lock
        re-entry); it calls the lockless record/mem helpers directly.

        Disk-full semantics (538 D3): ``batch_write_entries`` raises on
        ENOSPC (unlike the per-op ``_direct_write`` fail-open). The raise
        propagates so the caller's durable fallback chain persists the entry
        rather than letting it live only in memory. WAL is written before
        memory, so a disk-full raise leaves no partial in-memory state.
        """
        self._ensure_redis()

        if self._mode == ResilientStorageMode.REDIS and self._redis:
            try:
                # 543 D1/D2: buffer all ops on one non-transactional
                # pipeline; ``.execute()`` is the sole network round-trip
                # so a 6-op create costs 1 RTT, not N. ``transaction=False``
                # preserves the existing non-atomic failure model (prefix
                # application) that the G2 re-apply contract above is
                # written and tested against.
                with self._redis.raw_client.pipeline(transaction=False) as pipe:
                    for op_name, key, value in ops:
                        full_key = self._get_full_key(key)
                        if op_name == "set_blob":
                            pipe.set(full_key, value)
                        elif op_name == "zadd":
                            pipe.zadd(full_key, value)
                        elif op_name == "zrem":
                            members = [value] if isinstance(value, str) else list(value)
                            if members:
                                pipe.zrem(full_key, *members)
                        elif op_name == "delete":
                            pipe.delete(full_key)
                        else:
                            raise ValueError(f"Unsupported batch op: {op_name}")
                    pipe.execute()
                return True
            except ValueError:
                raise
            except Exception as e:
                # 543 D3/D5: bind the cause and thread it to the degraded
                # path so it can be captured via shadow forensics (the
                # batch path was previously the only Redis-failure→degrade
                # site that dropped the cause).
                self._switch_to_degraded()
                return self._batch_write_ops_degraded(ops, error=e)

        return self._batch_write_ops_degraded(ops)

    def _batch_write_ops_degraded(  # noqa: C901, PLR0912
        self,
        ops: list[tuple[str, str, Any]],
        error: Exception | None = None,
    ) -> bool:
        """Degraded grouped write: one batched WAL fsync, then memory
        mutations (538 D3). WAL-First — a disk-full raise from
        ``batch_write_entries`` leaves no partial in-memory state.

        543 D5: ``error`` carries the Redis exception that triggered the
        degrade transition (normal-mode wrapper passes ``error=e``; the
        already-DEGRADED tail call passes the default ``None``). When
        present, the cause is recorded via ``_shadow.record_sync_failure``
        after the WAL+memory block — fail-open, mirroring
        ``_set_degraded`` — so the degrade visibility gap on the batch
        path matches its per-op siblings.
        """
        with self._lock:
            # 1. WAL-First: build all records, single batch_write_entries fsync.
            if self._wal and self._wal_initialized:
                records: list[dict[str, Any]] = []
                for op_name, key, value in ops:
                    full_key = self._get_full_key(key)
                    if op_name == "set_blob":
                        records.append(self._wal_record_set_blob(full_key, value))
                    elif op_name == "zadd":
                        records.append(self._wal_record_zadd(full_key, value))
                    elif op_name == "zrem":
                        members = [value] if isinstance(value, str) else list(value)
                        records.append(self._wal_record_zrem(full_key, members))
                    elif op_name == "delete":
                        records.append(self._wal_record_delete(full_key))
                    else:
                        raise ValueError(f"Unsupported batch op: {op_name}")
                self._wal.batch_write_entries(records)

            # 2. Memory mutations second (short keys).
            for op_name, key, value in ops:
                if op_name == "set_blob":
                    self._mem_apply_set_blob(key, value)
                elif op_name == "zadd":
                    self._mem_apply_zadd(key, value)
                elif op_name == "zrem":
                    members = [value] if isinstance(value, str) else list(value)
                    self._mem_apply_zrem(key, members)
                elif op_name == "delete":
                    self._mem_apply_delete(key)

        # 3. Forensic logging (optional, outside the lock — mirrors
        #    ``_set_degraded``). Keyed on the ``set_blob`` op's key
        #    (the entry's create-anchor), falling back to the first op.
        #    One record per failed batch, NOT N per-op records: a
        #    degraded create is one operator-visible failure.
        if error and self._shadow:
            blob_key = next(
                (k for op, k, _ in ops if op == "set_blob"),
                ops[0][1] if ops else "",
            )
            try:
                self._shadow.record_sync_failure(
                    service_name=blob_key,
                    intended_state=f"batch_write_ops({len(ops)} ops)",
                    error=error,
                    adapter_type="redis",
                )
            except Exception:
                logger.debug("resilient_storage.shadow_record_failed")

        return True

    # =========================================================================
    # Mode Management
    # =========================================================================

    def _switch_to_degraded(self) -> None:
        """Switch to degraded mode on Redis failure.

        ``_degrade_count`` is bumped *before* the mode write so that a reader
        which observes a non-REDIS mode has already observed the bump — the
        ordering ``degrade_count``'s contract rests on.
        """
        with self._lock:
            if self._mode != ResilientStorageMode.DEGRADED:
                self._degrade_count += 1
                self._mode = ResilientStorageMode.DEGRADED
                logger.critical("resilient_storage.degraded_mode_fallback")

    def check_and_recover(self) -> bool:
        """
        Check Redis health and recover if available.

        Called periodically to attempt recovery from degraded mode.

        Returns:
            True if recovered to Redis mode
        """
        if self._mode != ResilientStorageMode.DEGRADED:
            return False

        # Health gate: ping our own storage Redis directly. This is the only
        # gate — a Django deployment now probes the same Redis it recovers,
        # instead of consulting the Django-cache Redis a separate health
        # checker used to ping (which could be a different instance).
        try:
            if self._redis:
                self._redis.raw_client.ping()
            else:
                return False
        except Exception:
            return False

        # Apply jitter to prevent thundering herd
        jitter = random.uniform(0, self.config.recovery_jitter_max)
        time.sleep(jitter)

        return self._do_recovery()

    def _do_recovery(self) -> bool:
        """
        Perform two-phase recovery from degraded mode to Redis mode
        (D1 #539) so a degraded write landing during recovery is visible
        in Redis afterwards — not WAL-only-until-restart.

        Phase A (lock-free bulk): replay all unprocessed WAL entries, sync
        remaining memory, then cleanup fully-processed WAL files. Degraded
        writes may still land concurrently during this phase (they are
        lock-held only momentarily, not for the whole phase).

        Phase B (locked finalize, ``with self._lock``): a second
        delta-replay catches every write that landed during Phase A (each
        has a WAL ``seq > last_processed``), then a non-raising commit tail
        clears memory + blob memory + resets the byte accumulator and flips
        mode to REDIS. Degraded writers block on ``self._lock`` during the
        finalize, so once the delta-replay runs no further WAL entry can
        appear before ``mode=REDIS`` — closing the recovery-window race.

        ``mode="runtime"`` filters every WAL glob to this worker's PID so
        peer workers' entries are not over-replayed (G4) and their still-
        active WAL files are not deleted (G3).
        """
        try:
            with self._lock:
                self._mode = ResilientStorageMode.RECOVERING

            # --- Phase A: lock-free bulk replay + sync + cleanup ---
            if self._wal and self._wal_initialized:
                entries = self._wal.recover_unprocessed(
                    last_processed_seq=self._last_processed_wal_seq,
                    mode="runtime",
                )

                for entry in entries:
                    try:
                        self._replay_wal_entry(entry)
                        self._last_processed_wal_seq = entry.sequence
                    except Exception as e:
                        logger.exception(
                            "resilient_storage.wal_replay_error",
                            _safe_error_message=_safe_error_message(e),
                        )

            # Sync remaining memory to Redis (with conflict resolution).
            # _sync_memory_to_redis snapshots self._memory under a brief
            # lock internally so a concurrent degraded write cannot mutate
            # the dict mid-iteration.
            self._sync_memory_to_redis()

            # cleanup_processed deletes fully-processed files so the locked
            # delta-read in Phase B scans only the current/recent file.
            if self._wal:
                self._wal.cleanup_processed(
                    self._last_processed_wal_seq,
                    mode="runtime",
                )

            # --- Phase B: locked finalize (delta-replay + commit tail) ---
            with self._lock:
                if self._wal and self._wal_initialized:
                    delta = self._wal.recover_unprocessed(
                        last_processed_seq=self._last_processed_wal_seq,
                        mode="runtime",
                    )
                    for entry in delta:
                        try:
                            self._replay_wal_entry(entry)
                            self._last_processed_wal_seq = entry.sequence
                        except Exception as e:
                            logger.exception(
                                "resilient_storage.wal_replay_error",
                                _safe_error_message=_safe_error_message(e),
                            )

                # Non-raising commit tail: dict/OrderedDict.clear() and the
                # int/enum assignments cannot raise, so they execute as an
                # indivisible group (no partial state). The only raising
                # step is the delta-replay above; on its exception (e.g. a
                # WAL read failure from recover_unprocessed) the tail is
                # skipped and mode rolls back to DEGRADED with memory intact.
                self._memory.clear()
                self._blob_memory.clear()
                self._blob_memory_bytes = 0
                self._mode = ResilientStorageMode.REDIS

            logger.info("resilient_storage.redis_mode_recovered")
            return True

        except Exception as e:
            logger.exception(
                "resilient_storage.recovery_failed",
                _safe_error_message=_safe_error_message(e),
            )
            with self._lock:
                self._mode = ResilientStorageMode.DEGRADED
            return False

    def _sync_memory_to_redis(self) -> None:
        """Sync in-memory data to Redis with drift reconciliation."""
        if not self._redis or not self._redis_initialized:
            return

        try:
            from baldur.adapters.memory.drift_reconciliation import (
                get_drift_reconciler,
            )

            reconciler = get_drift_reconciler()
        except Exception:
            reconciler = None

        # Snapshot under a brief lock so a concurrent degraded write cannot
        # mutate the dict mid-iteration (D1 #539). The per-key Redis I/O
        # below runs outside the lock — only the snapshot is locked.
        with self._lock:
            snapshot = list(self._memory.items())

        for key, value in snapshot:
            full_key = self._get_full_key(key)

            # ZSET (zadd) and blob (set_blob) memory values are
            # authoritatively reconstructed by WAL replay (WAL-First =>
            # memory subset of WAL). Re-syncing them here would clobber
            # the reconstructed ZSET/blob with a wrong-typed STRING
            # (zrange -> WRONGTYPE, get_by_id -> None). List-history
            # (lpush/ltrim) is accept-loss (D7), so dropping it on skip is
            # strictly better than clobbering it to a STRING.
            if isinstance(value, (list, bytes, bytearray)):
                continue

            try:
                if isinstance(value, dict):
                    # Check for drift if reconciler available
                    if reconciler and key.startswith("cb:"):
                        # Circuit breaker state - use reconciliation
                        redis_data = self._redis.raw_client.hgetall(full_key)
                        if redis_data:
                            redis_state = redis_data.get(b"state", b"closed").decode()
                            memory_state = value.get("state", "closed")

                            # Most Restrictive Wins
                            winner_state, _ = reconciler.reconcile(
                                service_name=key,
                                l1_state=memory_state,
                                l2_state=redis_state,
                            )
                            value["state"] = winner_state

                    str_mapping = {str(k): str(v) for k, v in value.items()}
                    self._redis.raw_client.hset(full_key, mapping=str_mapping)
                else:
                    self._redis.set(full_key, value)

            except Exception as e:
                logger.exception(
                    "resilient_storage.sync_error_key",
                    redis_key=key,
                    _safe_error_message=_safe_error_message(e),
                )

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def flush_wal(self) -> None:
        """Flush WAL buffer to disk."""
        if self._wal and self._wal_initialized:
            self._wal.flush()

    def get_stats(self) -> dict[str, Any]:
        """Get storage statistics."""
        stats = {
            "mode": self._mode.value,
            "redis_available": self._redis_initialized,
            "wal_initialized": self._wal_initialized,
            "wal_on_fallback_dir": self._wal_on_fallback_dir,
            "memory_keys": len(self._memory),
            # Degraded blob-store observability (D2 #539). Both are
            # GIL-atomic single reads — lock-free like ``memory_keys``.
            "blob_memory_keys": len(self._blob_memory),
            "blob_memory_bytes": self._blob_memory_bytes,
        }

        if self._wal:
            try:
                wal_stats = self._wal.get_stats()
                stats["wal_entries"] = wal_stats.total_entries
                stats["wal_last_sequence"] = wal_stats.last_sequence
            except Exception:
                pass

        return stats

    def close(self) -> None:
        """Close storage backend and release resources.

        Closes the WAL writer and drains the Redis connection pool (463 D16).
        Called by ``reset_storage_backend(cleanup=True)`` from
        :func:`reset_init_state` so repeated test-fixture ``init()`` cycles
        do not leak file descriptors.
        """
        if self._wal:
            self._wal.close()
        if self._redis is not None:
            self._redis.close()


from baldur.utils.singleton import CLEANUP_CLOSE, make_singleton_factory

get_storage_backend, configure_storage_backend, reset_storage_backend = (
    make_singleton_factory(
        "storage_backend",
        ResilientStorageBackend,
        cleanup_fn=CLEANUP_CLOSE,
    )
)


# Backward-compatibility alias (deprecated)
StorageMode = ResilientStorageMode
