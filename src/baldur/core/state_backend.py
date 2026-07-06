"""
State Backend Interface and Implementations.

Provides pluggable state persistence for the baldur system.
Supports both single-server (file) and multi-server (Redis) deployments.

Configuration:
    # Django settings.py
    BALDUR_SYSTEM_CONTROL_BACKEND = "file"  # or "redis"
    BALDUR_SYSTEM_CONTROL_DIR = "/var/lib/baldur/"  # for file backend
    BALDUR_REDIS_URL = "redis://localhost:6379/0"  # for redis backend

    # Or environment variables
    BALDUR_SYSTEM_CONTROL_BACKEND=redis
    BALDUR_REDIS_URL=redis://localhost:6379/0
"""

from __future__ import annotations

import fnmatch
import json
import os
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Generic, TypeVar
from urllib.parse import quote, unquote

import structlog

from baldur.core.file_utils import safe_unlink
from baldur.utils.serialization import fast_dumps_str, fast_loads

logger = structlog.get_logger()

T = TypeVar("T")


class StateBackend(ABC, Generic[T]):
    """
    Abstract base class for state persistence backends.

    Implementations must be thread-safe.
    """

    @abstractmethod
    def get(self, key: str, default: T | None = None) -> T | None:
        """Get state by key."""
        pass

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
        version_field: str = "__occ_version__",
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


class FileStateBackend(StateBackend[dict[str, Any]]):
    """
    File-based state backend for single-server deployments.

    Features:
    - JSON file storage
    - Atomic writes (temp file + rename)
    - Thread-safe
    - Survives process restarts

    Limitations:
    - Not shared across servers
    - No TTL support (ignored)

    Usage:
        backend = FileStateBackend("/var/lib/baldur/state")
        backend.set("system_control", {"enabled": True})
        state = backend.get("system_control")
    """

    def __init__(self, directory: str | Path):
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._recover_orphan_tmp_files()
        logger.info(
            "state_backend.file_backend_initialized",
            directory=self._directory,
        )

    def _recover_orphan_tmp_files(self) -> None:
        """
        Recover orphan .tmp files left by interrupted atomic writes.

        On startup, find .tmp files whose corresponding .json does not exist
        and rename them to .json to recover the data. If .json already exists,
        the .tmp is stale and should be removed.
        """
        for tmp_path in self._directory.glob("*.tmp"):
            json_path = tmp_path.with_suffix(".json")
            try:
                if not json_path.exists():
                    # .json missing → .tmp has the latest data, recover it
                    tmp_path.replace(json_path)
                    logger.info(
                        "state_backend.recovered_orphan_tmp",
                        tmp_path=tmp_path.name,
                        json_path=json_path.name,
                    )
                else:
                    # .json exists → .tmp is stale, remove it
                    safe_unlink(tmp_path)
                    logger.debug(
                        "state_backend.removed_stale_tmp",
                        tmp_path=tmp_path.name,
                    )
            except Exception as e:
                logger.warning(
                    "state_backend.recover_failed",
                    tmp_path=tmp_path.name,
                    error=e,
                )

    def _encode_key_for_filename(self, key: str) -> str:
        return quote(key, safe="_.-")

    def _decode_key_from_filename(self, stem: str) -> str:
        return unquote(stem)

    def _get_file_path(self, key: str) -> Path:
        safe_key = self._encode_key_for_filename(key)
        return self._directory / f"{safe_key}.json"

    def get(
        self, key: str, default: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        file_path = self._get_file_path(key)
        with self._lock:
            try:
                if file_path.exists():
                    with open(file_path, encoding="utf-8") as f:
                        return json.load(f)
                # Fallback: check for orphan .tmp if .json is missing
                tmp_path = file_path.with_suffix(".tmp")
                if tmp_path.exists():
                    try:
                        tmp_path.replace(file_path)
                        logger.info(
                            "state_backend.recovered_orphan_tmp_read",
                            tmp_path=tmp_path.name,
                        )
                        with open(file_path, encoding="utf-8") as f:
                            return json.load(f)
                    except Exception as recover_err:
                        logger.warning(
                            "state_backend.recover_tmp_failed",
                            state_key=key,
                            recover_err=recover_err,
                        )
            except Exception as e:
                logger.warning(
                    "state_backend.error_reading",
                    state_key=key,
                    error=e,
                )
        return default

    def _write_atomic(self, key: str, value: dict[str, Any]) -> None:
        """Atomically write ``value`` via temp file + rename. Caller holds ``self._lock``.

        Shared by :meth:`set` and :meth:`compare_and_set`. On failure cleans up
        the orphan ``.tmp`` and re-raises so the error propagates.
        """
        file_path = self._get_file_path(key)
        temp_file = file_path.with_suffix(".tmp")
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
            temp_file.replace(file_path)
        except Exception:
            # Clean up orphan .tmp to avoid stale data on next read
            safe_unlink(temp_file)
            raise

    def _read_raw(self, key: str) -> dict[str, Any] | None:
        """Read+parse the stored JSON, or ``None`` if absent. Caller holds ``self._lock``.

        Unlike :meth:`get`, errors propagate (no swallow-to-default) — used by
        :meth:`compare_and_set`, whose contract requires backend errors to
        surface rather than silently degrade to a blind overwrite.
        """
        file_path = self._get_file_path(key)
        if file_path.exists():
            with open(file_path, encoding="utf-8") as f:
                return json.load(f)
        return None

    def set(
        self, key: str, value: dict[str, Any], *, ttl_seconds: int | None = None
    ) -> None:
        with self._lock:
            try:
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
        version_field: str = "__occ_version__",
    ) -> bool:
        """Version-CAS under the per-process lock + atomic temp-rename write.

        Cross-host File CAS is out of reach (per-process ``threading.Lock``
        only) — not the config-write topology, which funnels PUTs through the
        single admin process on File/single-host (multi-pod uses Redis).
        """
        with self._lock:
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
        with self._lock:
            return safe_unlink(file_path)

    def exists(self, key: str) -> bool:
        return self._get_file_path(key).exists()

    def get_all(self, pattern: str = "*") -> dict[str, dict[str, Any]]:
        result = {}
        with self._lock:
            for file_path in self._directory.glob("*.json"):
                raw_key = self._decode_key_from_filename(file_path.stem)
                if pattern == "*" or fnmatch.fnmatchcase(raw_key, pattern):
                    try:
                        with open(file_path, encoding="utf-8") as f:
                            result[raw_key] = json.load(f)
                    except Exception as e:
                        logger.warning(
                            "state_backend.error_reading",
                            state_key=raw_key,
                            error=e,
                        )
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
        backend = RedisStateBackend("redis://localhost:6379/0")
        backend.set("system_control", {"enabled": True}, ttl_seconds=3600)
        state = backend.get("system_control")
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        key_prefix: str = "baldur:state:",
        scan_batch_size: int = 100,
        max_scan_keys: int = 10000,
    ):
        self._key_prefix = key_prefix
        self._redis_url = redis_url
        self._scan_batch_size = scan_batch_size
        self._max_scan_keys = max_scan_keys
        self._client: Any = None
        self._lock = threading.Lock()
        self._initialize_client()

    def _initialize_client(self) -> None:
        try:
            from baldur.adapters.redis.connection_factory import (
                get_redis_connection_factory,
            )

            self._client = get_redis_connection_factory().create(
                self._redis_url, decode_responses=True
            )
            # Test connection
            self._client.ping()
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
        version_field: str = "__occ_version__",
    ) -> bool:
        """Version-CAS via ``WATCH``/``MULTI``/``EXEC`` with bounded retry.

        Mirrors the cache adapter's ``cas_dict_field`` mechanism specialized to
        a version field. Version extraction stays in Python (no Lua/cjson): the
        watched ``GET`` is parsed, the version compared, and the conditional
        ``SET`` executed in a ``MULTI`` block. A ``WatchError`` (the key changed
        mid-transaction — i.e. a concurrent writer) is retried a bounded number
        of times, then resolves to ``False``. Connection/parse errors propagate.
        """
        try:
            from redis import WatchError
        except ImportError as e:  # redis is always present when this backend exists
            raise RuntimeError("RedisStateBackend requires the redis package") from e

        full_key = self._make_key(key)
        serialized = fast_dumps_str(new_value, default=str)
        max_attempts = 5
        for _ in range(max_attempts):
            try:
                with self._client.pipeline() as pipe:
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
                # Key changed between WATCH and EXEC → concurrent writer; retry.
                continue
        # Contention persisted past the retry bound → treat as a lost CAS.
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
        self._lock = threading.Lock()
        logger.info("state_backend.memory_backend_initialized_testing")

    def get(
        self, key: str, default: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        with self._lock:
            return self._store.get(key, default)

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
        version_field: str = "__occ_version__",
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
# Backend Factory
# =============================================================================


def _create_state_backend() -> StateBackend:
    from baldur.settings.system_control import get_system_control_settings

    settings = get_system_control_settings()

    if settings.backend == "redis":
        return RedisStateBackend(
            redis_url=settings.redis_url or "redis://localhost:6379/0",
            key_prefix=settings.redis_key_prefix,
            scan_batch_size=settings.redis_scan_batch_size,
            max_scan_keys=settings.redis_max_scan_keys,
        )
    if settings.backend == "memory":
        return MemoryStateBackend()
    return FileStateBackend(directory=settings.state_dir)


from baldur.utils.singleton import make_singleton_factory

get_state_backend, configure_state_backend, reset_state_backend = (
    make_singleton_factory("state_backend", _create_state_backend)
)
