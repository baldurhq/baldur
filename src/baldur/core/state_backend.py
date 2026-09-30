"""
State Backend Interface and Implementations.

Provides pluggable state persistence for the baldur system.
Supports both single-server (file) and multi-server (Redis) deployments.

Configuration:
    # Django settings.py
    BALDUR_SYSTEM_CONTROL_BACKEND = "file"  # or "redis"
    BALDUR_SYSTEM_CONTROL_DIR = "/var/lib/baldur/"  # for file backend
    BALDUR_REDIS_URL = "redis://your-redis:6379/0"  # for redis backend

    # Or environment variables
    BALDUR_SYSTEM_CONTROL_BACKEND=redis
    BALDUR_REDIS_URL=redis://your-redis:6379/0

    # With no backend set, a named Redis URL selects "redis"; otherwise "file".
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Generic, TypeVar
from urllib.parse import quote, unquote

import structlog

from baldur.core.file_utils import safe_unlink
from baldur.core.process_utils import fork_safe_lock
from baldur.settings.redis import DEFAULT_REDIS_URL
from baldur.utils.serialization import fast_dumps_str, fast_loads

logger = structlog.get_logger()

T = TypeVar("T")

#: Field every versioned write stamps with the stored version it replaced + 1.
OCC_VERSION_FIELD: Final = "__occ_version__"

#: Field every versioned write stamps with the identity of the change that
#: wrote it. The token alone identifies a change in the store: field values do
#: not, because a change can write values that were already stored.
OCC_WRITER_FIELD: Final = "__occ_writer__"

#: Attempts ``update_versioned`` makes before classifying the change by a
#: read-back. Each lost attempt means another writer committed in between.
DEFAULT_VERSIONED_WRITE_ATTEMPTS: Final = 5


class StateBackend(ABC, Generic[T]):
    """
    Abstract base class for state persistence backends.

    Implementations must be thread-safe.
    """

    @abstractmethod
    def get(self, key: str, default: T | None = None) -> T | None:
        """Get state by key."""
        pass

    def get_strict(self, key: str) -> T | None:
        """Get state by key, telling a read failure apart from absence.

        Returns ``None`` when the key is absent and raises when the backend
        cannot answer. Every shipped backend overrides this. A custom backend
        that does not inherits this default, which delegates to :meth:`get` —
        its read failures then read as absence, exactly as its ``get`` reports
        them.
        """
        return self.get(key)

    @abstractmethod
    def set(self, key: str, value: T, *, ttl_seconds: int | None = None) -> None:
        """Set state by key with optional TTL."""
        pass

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete state by key. Returns True if existed."""
        pass

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Check if key exists."""
        pass

    @abstractmethod
    def get_all(self, pattern: str = "*") -> dict[str, T]:
        """Get all states matching pattern."""
        pass

    @abstractmethod
    def compare_and_set(  # verified-by: test_compare_and_set_conformance
        self,
        key: str,
        expected_version: int,
        new_value: T,
        *,
        version_field: str = OCC_VERSION_FIELD,
    ) -> bool:
        """Atomically store ``new_value`` iff the stored version matches.

        Optimistic-concurrency primitive. Reads the value at ``key`` and its
        ``version_field`` (a missing key, or a value lacking the field, is
        treated as version ``0``), and **iff** that stored version equals
        ``expected_version`` stores ``new_value`` — which the caller has
        already stamped with ``version_field = expected_version + 1``.

        Returns ``True`` when the value was stored, ``False`` on a version
        mismatch (a concurrent writer won the race). A backend error (Redis
        down, file I/O failure) propagates rather than returning ``False`` —
        the write then fails closed exactly as :meth:`set` does, never
        silently falling back to a blind overwrite that would reintroduce the
        lost-update clobber.

        A raise does not prove that nothing was stored: a reply can be lost
        after the store applied the write. A caller that must know re-reads
        the key (:func:`update_versioned` does).

        Implementations perform the read-compare-store atomically with respect
        to other writers of the same key.
        """
        pass

    def close(self) -> None:
        """Release backend resources. Idempotent. Default no-op."""
        pass


from typing import Protocol, runtime_checkable


@runtime_checkable
class ListCapableBackend(Protocol):
    """
    Protocol for backends that support atomic list operations.

    Follows BatchDetectable pattern (interfaces/ml_strategy.py).
    RedisStateBackend implements via RPUSH+LTRIM+EXPIRE (O(1) atomic).
    MemoryStateBackend implements via threading.Lock + list.
    """

    def push_limit(
        self, key: str, value: Any, max_len: int, ttl_seconds: int | None = None
    ) -> int:
        """Atomically append value and trim list to max_len. Returns pre-trim length."""
        ...

    def list_range(self, key: str, start: int, end: int) -> list[Any]:
        """Return elements from start to end (inclusive)."""
        ...


#: Suffix of a write's temp file. Deliberately not ``.tmp``: a process on the
#: previous release promotes every ``*.tmp`` whose ``.json`` is missing, and it
#: would turn a new-release temp into a stray key.
_PARTIAL_SUFFIX: Final = ".partial"

#: Suffix of the per-key lock file every access of that key locks.
_LOCK_SUFFIX: Final = ".lock"

#: The writer part of a temp name: ``<pid>-<thread id>``.
_WRITER_PART_PATTERN: Final = re.compile(r"\d+-\d+")

#: A rename onto a file another process has open (an antivirus or indexer scan
#: of the fresh file) fails on Windows with ``PermissionError``. Retried a few
#: times inside the key's lock before the write is reported as failed.
_RENAME_RETRY_ATTEMPTS: Final = 5
_RENAME_RETRY_DELAY_SECONDS: Final = 0.02


class FileStateBackend(StateBackend[dict[str, Any]]):
    """
    File-based state backend for single-server deployments.

    Features:
    - JSON file storage
    - Atomic writes (a temp file unique to the writer, then rename)
    - Safe across processes and threads: every access of a key holds that
      key's OS file lock, so processes sharing the directory never interleave
      writes or both win one version
    - Survives process restarts

    Limitations:
    - Not shared across servers
    - No TTL support (ignored)
    - A relative directory resolves against each process's working directory

    Usage:
        backend = FileStateBackend("/var/lib/baldur/state")
        backend.set("system_control", {"enabled": True})
        state = backend.get("system_control")
    """

    def __init__(self, directory: str | Path):
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._lock = fork_safe_lock()
        self._remove_dead_writer_temps()
        logger.info(
            "state_backend.file_backend_initialized",
            directory=self.directory,
        )

    @property
    def directory(self) -> str:
        """The store's directory as an absolute path."""
        return str(self._directory.resolve())

    def _remove_dead_writer_temps(self) -> None:
        """Remove every temp file a writer left behind, never promoting one.

        A live writer holds its key's lock until its rename, so a temp seen
        under that lock belongs to a writer that died mid-write — and a value
        that writer never confirmed to its caller must not become state.
        """
        for temp_path in self._directory.glob(f"*{_PARTIAL_SUFFIX}"):
            parts = temp_path.name.rsplit(".", 2)
            if len(parts) != 3 or not _WRITER_PART_PATTERN.fullmatch(parts[1]):
                continue
            key = self._decode_key_from_filename(parts[0])
            try:
                with self._key_lock(key):
                    removed = safe_unlink(temp_path)
                if removed:
                    logger.debug(
                        "state_backend.dead_writer_temp_removed",
                        temp_file=temp_path.name,
                    )
            except Exception as e:
                logger.warning(
                    "state_backend.dead_writer_temp_remove_failed",
                    temp_file=temp_path.name,
                    error=e,
                )

    def _encode_key_for_filename(self, key: str) -> str:
        return quote(key, safe="_.-")

    def _decode_key_from_filename(self, stem: str) -> str:
        return unquote(stem)

    def _get_file_path(self, key: str) -> Path:
        safe_key = self._encode_key_for_filename(key)
        return self._directory / f"{safe_key}.json"

    @contextmanager
    def _key_lock(self, key: str) -> Iterator[None]:
        """Hold ``key``'s cross-process lock (and this process's lock).

        The lock file is opened without truncation and never written; it
        persists beside the state file by design. An OS lock dies with its
        holder, so a killed process cannot wedge a key. Acquisition is bounded
        by the lock primitive's timeout and raises before anything is read or
        written.
        """
        # def-body import: the lock primitive lives in the audit checkpoint
        # package, which this module must not load at import time.
        from baldur.audit.checkpoint.file_lock import lock_file, unlock_file

        lock_path = (
            self._directory / f"{self._encode_key_for_filename(key)}{_LOCK_SUFFIX}"
        )
        with self._lock, open(lock_path, "a+b") as handle:
            lock_file(handle, blocking=True)
            try:
                yield
            finally:
                unlock_file(handle)

    def get(
        self, key: str, default: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        try:
            value = self.get_strict(key)
        except Exception as e:
            logger.warning(
                "state_backend.error_reading",
                state_key=key,
                error=e,
            )
            return default
        return default if value is None else value

    def get_strict(self, key: str) -> dict[str, Any] | None:
        with self._key_lock(key):
            return self._read_raw(key)

    def _write_atomic(self, key: str, value: dict[str, Any]) -> None:
        """Write ``value`` via a writer-unique temp file + rename. Caller holds the key lock.

        Shared by :meth:`set` and :meth:`compare_and_set`. On failure removes
        its own temp file and re-raises so the error propagates.
        """
        file_path = self._get_file_path(key)
        temp_file = self._directory / (
            f"{self._encode_key_for_filename(key)}."
            f"{os.getpid()}-{threading.get_ident()}{_PARTIAL_SUFFIX}"
        )
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(value, f, indent=2, default=str)
                # fsync before rename: an atomic rename is not a durable write —
                # without flushing file contents to disk a power loss mid-write can
                # leave a 0-byte / torn file even after replace() returns. Covers
                # every state write (config, pending, system_control). Directory
                # fsync for rename durability is POSIX-only (cross-platform target),
                # left as an optional follow-up.
                f.flush()
                os.fsync(f.fileno())
            self._replace_with_retry(temp_file, file_path)
        except Exception:
            safe_unlink(temp_file)
            raise

    @staticmethod
    def _replace_with_retry(source: Path, target: Path) -> None:
        """Rename ``source`` onto ``target``, retrying a brief Windows sharing clash."""
        for attempt in range(_RENAME_RETRY_ATTEMPTS):
            try:
                source.replace(target)
                return
            except PermissionError:
                if attempt == _RENAME_RETRY_ATTEMPTS - 1:
                    raise
                time.sleep(_RENAME_RETRY_DELAY_SECONDS)

    def _read_raw(self, key: str) -> dict[str, Any] | None:
        """Read+parse the stored JSON, or ``None`` if absent. Caller holds the key lock.

        Errors propagate (no swallow-to-default) — the strict read and
        :meth:`compare_and_set` rely on a failure surfacing rather than
        reading as absence.
        """
        file_path = self._get_file_path(key)
        if file_path.exists():
            with open(file_path, encoding="utf-8") as f:
                return json.load(f)
        return None

    def set(
        self, key: str, value: dict[str, Any], *, ttl_seconds: int | None = None
    ) -> None:
        try:
            with self._key_lock(key):
                self._write_atomic(key, value)
        except Exception as e:
            logger.exception(
                "state_backend.error_writing",
                state_key=key,
                error=e,
            )
            raise

    def compare_and_set(
        self,
        key: str,
        expected_version: int,
        new_value: dict[str, Any],
        *,
        version_field: str = OCC_VERSION_FIELD,
    ) -> bool:
        """Version-CAS under the key's cross-process lock + atomic rename write."""
        with self._key_lock(key):
            existing = self._read_raw(key)
            current_version = (
                existing.get(version_field, 0) if isinstance(existing, dict) else 0
            )
            if current_version != expected_version:
                return False
            self._write_atomic(key, new_value)
            return True

    def delete(self, key: str) -> bool:
        file_path = self._get_file_path(key)
        with self._key_lock(key):
            return safe_unlink(file_path)

    def exists(self, key: str) -> bool:
        return self._get_file_path(key).exists()

    def get_all(self, pattern: str = "*") -> dict[str, dict[str, Any]]:
        result = {}
        for file_path in self._directory.glob("*.json"):
            raw_key = self._decode_key_from_filename(file_path.stem)
            if pattern == "*" or fnmatch.fnmatchcase(raw_key, pattern):
                try:
                    with self._key_lock(raw_key):
                        value = self._read_raw(raw_key)
                except Exception as e:
                    logger.warning(
                        "state_backend.error_reading",
                        state_key=raw_key,
                        error=e,
                    )
                    continue
                if value is not None:
                    result[raw_key] = value
        return result


class RedisStateBackend(StateBackend[dict[str, Any]]):
    """
    Redis-based state backend for multi-server deployments.

    Features:
    - Shared state across all servers
    - TTL support
    - Atomic operations
    - High availability (with Redis Sentinel/Cluster)

    Requirements:
    - redis package: pip install redis

    Usage:
        backend = RedisStateBackend("redis://your-redis:6379/0")
        backend.set("system_control", {"enabled": True}, ttl_seconds=3600)
        state = backend.get("system_control")
    """

    def __init__(
        self,
        redis_url: str = DEFAULT_REDIS_URL,
        key_prefix: str = "baldur:state:",
        scan_batch_size: int = 100,
        max_scan_keys: int = 10000,
    ):
        self._key_prefix = key_prefix
        self._redis_url = redis_url
        self._scan_batch_size = scan_batch_size
        self._max_scan_keys = max_scan_keys
        self._client: Any = None
        self._lock = fork_safe_lock()
        self._initialize_client()

    def _initialize_client(self) -> None:
        """Admit the server on the bounded probe budget, then build the client.

        Construction still fails when Redis does not answer: consumers whose
        ``get`` swallows errors rely on a backend that exists having answered
        once. The probe bounds how long a blackholed host holds the caller.
        """
        try:
            from baldur.adapters.redis.connection_factory import (
                get_redis_connection_factory,
            )

            factory = get_redis_connection_factory()
            factory.probe(self._redis_url)
            self._client = factory.create(self._redis_url, decode_responses=True)
            logger.info(
                "state_backend.redis_backend_connected",
                redis_url=self._redis_url,
            )
        except ImportError:
            logger.exception("state_backend.redis_import_error")
            raise
        except Exception as e:
            logger.exception(
                "state_backend.redis_connection_failed",
                error=e,
            )
            raise

    def _make_key(self, key: str) -> str:
        return f"{self._key_prefix}{key}"

    def get(
        self, key: str, default: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        try:
            data = self._client.get(self._make_key(key))
            if data:
                return fast_loads(data)
        except Exception as e:
            logger.warning(
                "state_backend.redis_get_failed",
                state_key=key,
                error=e,
            )
        return default

    def get_strict(self, key: str) -> dict[str, Any] | None:
        data = self._client.get(self._make_key(key))
        if not data:
            return None
        return fast_loads(data)

    def set(
        self, key: str, value: dict[str, Any], *, ttl_seconds: int | None = None
    ) -> None:
        try:
            data = fast_dumps_str(value, default=str)
            if ttl_seconds:
                self._client.setex(self._make_key(key), ttl_seconds, data)
            else:
                self._client.set(self._make_key(key), data)
        except Exception as e:
            logger.exception(
                "state_backend.redis_set_error",
                state_key=key,
                error=e,
            )
            raise

    def delete(self, key: str) -> bool:
        try:
            return self._client.delete(self._make_key(key)) > 0
        except Exception as e:
            logger.exception(
                "state_backend.redis_delete_error",
                state_key=key,
                error=e,
            )
            return False

    def exists(self, key: str) -> bool:
        try:
            return self._client.exists(self._make_key(key)) > 0
        except Exception as e:
            logger.warning(
                "state_backend.redis_exists_failed",
                state_key=key,
                error=e,
            )
            return False

    def get_all(
        self,
        pattern: str = "*",
        max_keys: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        """
        Get all states matching pattern with safety limits.

        Args:
            pattern: Key pattern to match
            max_keys: Maximum number of keys to return (default from settings)
                      Set to prevent DoS via unbounded iteration

        Returns:
            Dictionary of matching states
        """
        result = {}
        limit = max_keys if max_keys is not None else self._max_scan_keys

        try:
            full_pattern = self._make_key(pattern)
            count = 0
            for key in self._client.scan_iter(
                match=full_pattern, count=self._scan_batch_size
            ):
                if count >= limit:
                    logger.warning(
                        "state_backend.reached_limit_results_incomplete",
                        limit=limit,
                    )
                    break
                short_key = key.removeprefix(self._key_prefix)
                data = self._client.get(key)
                if data:
                    result[short_key] = fast_loads(data)
                    count += 1
        except Exception as e:
            logger.exception(
                "state_backend.redis_scan_error",
                error=e,
            )
        return result

    def compare_and_set(
        self,
        key: str,
        expected_version: int,
        new_value: dict[str, Any],
        *,
        version_field: str = OCC_VERSION_FIELD,
    ) -> bool:
        """Version-CAS via one ``WATCH``/``MULTI``/``EXEC`` round.

        Version extraction stays in Python (no Lua/cjson): the watched ``GET``
        is parsed, the version compared, and the conditional ``SET`` executed
        in a ``MULTI`` block. A ``WatchError`` answers ``False`` without a
        second round: the key changed under the watch, and only the caller can
        decide what to write on the state that is now stored — re-checking the
        version alone would write a value computed for the old state over a
        write that kept the version (a writer that does not stamp one). redis-py
        also reports an ``EXEC`` whose reply timed out as ``WatchError``, so a
        caller that must know re-reads the key. Connection/parse errors
        propagate.
        """
        try:
            from redis import WatchError
        except ImportError as e:  # redis is always present when this backend exists
            raise RuntimeError("RedisStateBackend requires the redis package") from e

        full_key = self._make_key(key)
        serialized = fast_dumps_str(new_value, default=str)
        with self._client.pipeline() as pipe:
            try:
                pipe.watch(full_key)
                raw = pipe.get(full_key)
                current_version = 0
                if raw:
                    stored = fast_loads(raw)
                    if isinstance(stored, dict):
                        current_version = stored.get(version_field, 0)
                if current_version != expected_version:
                    pipe.unwatch()
                    return False
                pipe.multi()
                pipe.set(full_key, serialized)
                pipe.execute()
                return True
            except WatchError:
                return False

    def close(self) -> None:
        """Close Redis connection pool."""
        if self._client is not None:
            try:
                self._client.close()
                logger.info("state_backend.redis_connection_closed")
            except Exception as e:
                logger.warning("state_backend.redis_close_failed", error=e)
            self._client = None

    # ListCapableBackend implementation
    def push_limit(
        self, key: str, value: Any, max_len: int, ttl_seconds: int | None = None
    ) -> int:
        """Atomically append value and trim list to max_len via RPUSH+LTRIM+EXPIRE."""
        full_key = self._make_key(key)
        try:
            pipe = self._client.pipeline()
            pipe.rpush(full_key, fast_dumps_str(value, default=str))
            pipe.ltrim(full_key, -max_len, -1)
            if ttl_seconds:
                pipe.expire(full_key, ttl_seconds)
            results = pipe.execute()
            return results[0]  # RPUSH returns new length
        except Exception as e:
            logger.warning("state_backend.redis_push_limit_failed", key=key, error=e)
            return 0

    def list_range(self, key: str, start: int, end: int) -> list[Any]:
        """Return elements from start to end (inclusive) via LRANGE."""
        full_key = self._make_key(key)
        try:
            raw_items = self._client.lrange(full_key, start, end)
            result = []
            for item in raw_items:
                try:
                    result.append(fast_loads(item))
                except Exception:
                    result.append(item)
            return result
        except Exception as e:
            logger.warning("state_backend.redis_list_range_failed", key=key, error=e)
            return []


class MemoryStateBackend(StateBackend[dict[str, Any]]):
    """
    In-memory state backend for testing.

    WARNING: State is lost on process restart.
    Use only for testing.
    """

    def __init__(self):
        # Heterogeneous: dict[str, Any] for state values + list[Any] for ListCapableBackend
        self._store: dict[str, Any] = {}
        self._lock = fork_safe_lock()
        logger.info("state_backend.memory_backend_initialized_testing")

    def get(
        self, key: str, default: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        with self._lock:
            return self._store.get(key, default)

    def get_strict(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            return self._store.get(key)

    def set(
        self, key: str, value: dict[str, Any], *, ttl_seconds: int | None = None
    ) -> None:
        with self._lock:
            self._store[key] = value

    def delete(self, key: str) -> bool:
        with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    def exists(self, key: str) -> bool:
        with self._lock:
            return key in self._store

    def get_all(self, pattern: str = "*") -> dict[str, dict[str, Any]]:
        with self._lock:
            if pattern == "*":
                return dict(self._store)
            return {
                k: v for k, v in self._store.items() if fnmatch.fnmatchcase(k, pattern)
            }

    def compare_and_set(
        self,
        key: str,
        expected_version: int,
        new_value: dict[str, Any],
        *,
        version_field: str = OCC_VERSION_FIELD,
    ) -> bool:
        """Version-CAS under the existing ``threading.Lock`` (compare-then-set)."""
        with self._lock:
            existing = self._store.get(key)
            current_version = (
                existing.get(version_field, 0) if isinstance(existing, dict) else 0
            )
            if current_version != expected_version:
                return False
            self._store[key] = new_value
            return True

    # ListCapableBackend implementation
    def push_limit(
        self, key: str, value: Any, max_len: int, ttl_seconds: int | None = None
    ) -> int:
        """Atomically append value and trim list to max_len. Returns pre-trim length."""
        with self._lock:
            existing = self._store.get(key)
            lst: list[Any] = existing if isinstance(existing, list) else []
            lst.append(value)
            pre_trim_len = len(lst)
            if len(lst) > max_len:
                lst = lst[-max_len:]
            self._store[key] = lst
            return pre_trim_len

    def list_range(self, key: str, start: int, end: int) -> list[Any]:
        """Return elements from start to end (inclusive)."""
        with self._lock:
            existing = self._store.get(key)
            if not isinstance(existing, list):
                return []
            lst: list[Any] = existing
            return lst[start : end + 1] if end >= 0 else lst[start:]


# =============================================================================
# Versioned writes
# =============================================================================


class VersionedWriteOutcome(StrEnum):
    """How a versioned change ended."""

    #: The store holds this change (written now, or already there).
    COMMITTED = "committed"
    #: The writer's own preconditions rejected the stored state; nothing written.
    DECLINED = "declined"
    #: The store does not hold this change and never will.
    NOT_APPLIED = "not_applied"
    #: The write may or may not have landed; a later read of the key decides it.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Declined:
    """A mutate answer: the stored state rejects this change, for ``reason``."""

    reason: str


class _AlreadySatisfied:
    """A mutate answer: the stored state already carries this change."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "ALREADY_SATISFIED"


#: A mutate answer: the stored state already carries this change, nothing to write.
ALREADY_SATISFIED: Final = _AlreadySatisfied()

#: What a mutate returns: the new value, :class:`Declined`, or
#: :data:`ALREADY_SATISFIED`.
MutateAnswer = dict[str, Any] | Declined | _AlreadySatisfied


@dataclass(frozen=True)
class VersionedWriteResult:
    """The outcome of :func:`update_versioned`.

    Attributes:
        outcome: How the change ended.
        token: The change's writer token (stamped in the store when written).
        before: The stored state the committing (or last) attempt read — what
            a committed change replaced. Every side effect of a committed
            change is computed from this state.
        after: The stored state carrying the change (committed), or the state
            that declined it. ``None`` for the other outcomes.
        decline_reason: The mutate's reason when declined.
        error: The backend error behind a not-applied or unknown outcome.
        read_stored: Whether any attempt read the key successfully — when
            False, ``before`` says nothing about the store.
    """

    outcome: VersionedWriteOutcome
    token: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    decline_reason: str | None = None
    error: BaseException | None = None
    read_stored: bool = False

    @property
    def committed(self) -> bool:
        """Whether the store holds this change."""
        return self.outcome is VersionedWriteOutcome.COMMITTED


def stored_version(stored: Any) -> int:
    """The version a stored value carries; ``0`` for an absent or unstamped value."""
    if not isinstance(stored, dict):
        return 0
    try:
        return int(stored.get(OCC_VERSION_FIELD, 0))
    except (TypeError, ValueError):
        return 0


def carries_writer_token(stored: Any, token: str) -> bool:
    """Whether ``stored`` was written by the change identified by ``token``."""
    return isinstance(stored, dict) and stored.get(OCC_WRITER_FIELD) == token


def new_writer_token() -> str:
    """A fresh writer token identifying one change."""
    return uuid.uuid4().hex


def update_versioned(
    backend: StateBackend,
    key: str,
    mutate: Callable[[dict[str, Any] | None], MutateAnswer],
    *,
    token: str | None = None,
    max_attempts: int = DEFAULT_VERSIONED_WRITE_ATTEMPTS,
) -> VersionedWriteResult:
    """Apply one change to ``key`` through strict read → mutate → versioned write.

    ``mutate`` is called on every attempt with the freshly read stored state
    (``None`` when absent) and re-evaluates the writer's own preconditions
    there. It returns the new value (without version fields — this function
    stamps ``__occ_version__`` and the writer token), :class:`Declined`, or
    :data:`ALREADY_SATISFIED`. A lost conditional write re-reads and re-runs
    the mutate; the change's constants belong in the caller's closure, fixed
    once before the first attempt.

    A raise does not mean "not written" (a lost reply, a rename that landed
    and then raised), so every raise — and exhausted attempts — is classified
    by one strict read-back: the stored value carries this change's token →
    committed; its version is still the one expected → not applied; the
    read-back fails or shows anything else → unknown.

    Args:
        backend: The store holding ``key``.
        key: The state key.
        mutate: The change, re-evaluated on every attempt's stored state.
        token: The change's writer token; a fresh one when omitted.
        max_attempts: Conditional-write attempts before the read-back.

    Returns:
        The change's outcome, never raising for a backend error.
    """
    change_token = token or new_writer_token()
    previous_read: dict[str, Any] | None = None
    wrote = False
    expected = 0
    for _ in range(max_attempts):
        try:
            stored = backend.get_strict(key)
        except Exception as e:
            # Nothing of this change was sent yet → not applied. After a lost
            # conditional write it may have landed (a timed-out EXEC is
            # reported as a lost watch), so it cannot be told.
            outcome = (
                VersionedWriteOutcome.UNKNOWN
                if wrote
                else VersionedWriteOutcome.NOT_APPLIED
            )
            return VersionedWriteResult(
                outcome, change_token, before=previous_read, error=e, read_stored=wrote
            )
        if carries_writer_token(stored, change_token):
            # An earlier attempt landed although it was reported lost.
            return VersionedWriteResult(
                VersionedWriteOutcome.COMMITTED,
                change_token,
                before=previous_read,
                after=stored,
                read_stored=True,
            )
        answer = mutate(stored)
        if isinstance(answer, Declined):
            return VersionedWriteResult(
                VersionedWriteOutcome.DECLINED,
                change_token,
                before=stored,
                after=stored,
                decline_reason=answer.reason,
                read_stored=True,
            )
        if isinstance(answer, _AlreadySatisfied):
            return VersionedWriteResult(
                VersionedWriteOutcome.COMMITTED,
                change_token,
                before=stored,
                after=stored,
                read_stored=True,
            )
        expected = stored_version(stored)
        new_value = dict(answer)
        new_value[OCC_VERSION_FIELD] = expected + 1
        new_value[OCC_WRITER_FIELD] = change_token
        previous_read = stored
        wrote = True
        try:
            if backend.compare_and_set(key, expected, new_value):
                return VersionedWriteResult(
                    VersionedWriteOutcome.COMMITTED,
                    change_token,
                    before=stored,
                    after=new_value,
                    read_stored=True,
                )
        except Exception as e:
            return _classify_by_read_back(
                backend, key, change_token, expected, previous_read, e
            )
    return _classify_by_read_back(
        backend, key, change_token, expected, previous_read, None
    )


def _classify_by_read_back(
    backend: StateBackend,
    key: str,
    token: str,
    expected: int,
    before: dict[str, Any] | None,
    error: BaseException | None,
) -> VersionedWriteResult:
    """Decide a raised or exhausted versioned write by one strict read-back."""
    try:
        read_back = backend.get_strict(key)
    except Exception as e:
        return VersionedWriteResult(
            VersionedWriteOutcome.UNKNOWN,
            token,
            before=before,
            error=error or e,
            read_stored=True,
        )
    if carries_writer_token(read_back, token):
        return VersionedWriteResult(
            VersionedWriteOutcome.COMMITTED,
            token,
            before=before,
            after=read_back,
            read_stored=True,
        )
    if stored_version(read_back) == expected:
        return VersionedWriteResult(
            VersionedWriteOutcome.NOT_APPLIED,
            token,
            before=before,
            error=error,
            read_stored=True,
        )
    return VersionedWriteResult(
        VersionedWriteOutcome.UNKNOWN,
        token,
        before=before,
        error=error,
        read_stored=True,
    )


@dataclass(frozen=True)
class PendingChange:
    """A change whose write ended unknown, decided by the next successful read.

    Attributes:
        token: The change's writer token.
        description: What the change was, for its log lines.
        on_committed: Runs the change's side effects once a read shows its
            token, with that read's stored state.
        origin_pid: The process that made the change. A ``fork()`` child
            inherits the record but never decides it: the side effects of one
            change run in one process.
    """

    token: str
    description: str
    on_committed: Callable[[dict[str, Any]], None]
    origin_pid: int = field(default_factory=os.getpid)


def settle_pending_changes(
    pending: list[PendingChange], stored: dict[str, Any] | None
) -> tuple[list[PendingChange], list[PendingChange]]:
    """Split ``pending`` into (committed, not applied) by the stored writer token.

    A change that landed and was overwritten before this read reads as not
    applied: its token is gone, and nothing else identifies it. A record
    inherited across ``fork()`` is in neither list — it is dropped, because the
    process that made the change decides it.
    """
    pid = os.getpid()
    own = [p for p in pending if p.origin_pid == pid]
    committed = [p for p in own if carries_writer_token(stored, p.token)]
    not_applied = [p for p in own if not carries_writer_token(stored, p.token)]
    return committed, not_applied


# =============================================================================
# Backend Factory
# =============================================================================


def _create_state_backend() -> StateBackend:
    from baldur.settings.system_control import get_system_control_settings

    settings = get_system_control_settings()

    if settings.backend == "redis":
        backend = RedisStateBackend(
            redis_url=settings.redis_url,
            key_prefix=settings.redis_key_prefix,
            scan_batch_size=settings.redis_scan_batch_size,
            max_scan_keys=settings.redis_max_scan_keys,
        )
        if settings.backend_was_derived:
            _warn_if_file_state_stranded(settings.state_dir)
        return backend
    if settings.backend == "memory":
        return MemoryStateBackend()
    return FileStateBackend(directory=settings.state_dir)


def _warn_if_file_state_stranded(directory: str) -> None:
    """Warn once when a derived Redis store leaves file-store state unread.

    The state is not migrated: a deployment that relied on the file store keeps
    it by setting the backend explicitly.
    """
    try:
        path = Path(directory)
        stranded = path.is_dir() and any(path.glob("*.json"))
    except OSError:
        return
    if stranded:
        logger.warning(
            "state_backend.file_state_not_migrated",
            directory=str(path.resolve()),
            hint="set BALDUR_SYSTEM_CONTROL_BACKEND=file to keep using it",
        )


from baldur.utils.singleton import make_singleton_factory

get_state_backend, configure_state_backend, reset_state_backend = (
    make_singleton_factory("state_backend", _create_state_backend)
)
