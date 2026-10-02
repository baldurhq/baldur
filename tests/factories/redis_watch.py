"""A Redis DLQ backend whose raw client honours WATCH / MULTI / EXEC.

``FakeSortedSetBackend`` keeps real score ordering for the index reads; this
subclass adds the raw-client seam the DLQ lifecycle's compare-and-set writes go
through (``_try_acquire_atomic``, ``return_replay_attempt``,
``release_stale_replaying``), with the transaction semantics those writes
depend on:

- commands before ``multi()`` run at once (a watched ``get`` reads the key);
- commands after ``multi()`` are queued and applied together by ``execute()``;
- ``execute()`` raises ``redis.WatchError`` when a watched key was written
  after its ``watch()`` — by anyone, as a concurrent client would.

Two hooks inject that concurrent client: ``after_watch`` runs once right after
the next ``watch()``, ``after_get`` once right after the next watched ``get``.
Each receives the key and the backend, and usually rewrites the entry.

``redis_up`` is what ``ensure_redis()`` answers: False sends every lifecycle
write down its degraded read-modify-write path.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from tests.factories.redis import FakeSortedSetBackend

__all__ = ["WatchingRedisBackend"]

Hook = Callable[[str, "WatchingRedisBackend"], None]


class WatchingRedisBackend(FakeSortedSetBackend):
    """``FakeSortedSetBackend`` plus a WATCH-capable raw client."""

    def __init__(self) -> None:
        super().__init__()
        self.versions: dict[str, int] = defaultdict(int)
        self.redis_up = True
        self.is_degraded = False
        self.config = SimpleNamespace(key_prefix="")
        self.after_watch: Hook | None = None
        self.after_get: Hook | None = None
        self.watch_conflicts = 0

    # -- the seams the repository reads ------------------------------------

    def ensure_redis(self) -> bool:
        return self.redis_up

    @property
    def raw_redis_client(self) -> _RawClient:
        return _RawClient(self)

    @staticmethod
    def _get_full_key(key: str) -> str:
        return key

    # -- writes bump the key's version (what WATCH compares) ---------------

    def set_blob(self, key: str, value: bytes) -> None:
        super().set_blob(key, value)
        self.versions[key] += 1

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.versions[key] += 1
        return super().zadd(key, mapping)

    def zrem(self, key: str, members) -> int:
        self.versions[key] += 1
        return super().zrem(key, members)

    def members(self, key: str) -> set[str]:
        """The members of one sorted set (empty when it does not exist)."""
        return set(self.zsets.get(key, {}))


class _RawClient:
    def __init__(self, backend: WatchingRedisBackend) -> None:
        self._backend = backend

    def pipeline(self, transaction: bool = True) -> _Pipeline:
        return _Pipeline(self._backend)


class _Pipeline:
    """One WATCH / MULTI / EXEC round, over the backend's own maps."""

    def __init__(self, backend: WatchingRedisBackend) -> None:
        self._backend = backend
        self._watched: dict[str, int] = {}
        self._queued: list[tuple[str, tuple[Any, ...]]] | None = None

    def __enter__(self) -> _Pipeline:
        return self

    def __exit__(self, *exc: object) -> None:
        self._watched.clear()
        self._queued = None

    def watch(self, *keys: str) -> None:
        for key in keys:
            self._watched[key] = self._backend.versions[key]
        hook, self._backend.after_watch = self._backend.after_watch, None
        if hook is not None:
            hook(keys[0], self._backend)

    def unwatch(self) -> None:
        self._watched.clear()

    def get(self, key: str) -> bytes | None:
        if self._queued is not None:
            raise RuntimeError("get inside MULTI is not modelled")
        value = self._backend.blobs.get(key)
        hook, self._backend.after_get = self._backend.after_get, None
        if hook is not None:
            hook(key, self._backend)
        return value

    def multi(self) -> None:
        self._queued = []

    def set(self, key: str, value: bytes) -> None:
        self._queue("set_blob", key, value)

    def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self._queue("zadd", key, mapping)

    def zrem(self, key: str, *members: str) -> None:
        self._queue("zrem", key, list(members))

    def execute(self) -> list[Any]:
        import redis

        if any(
            self._backend.versions[key] != version
            for key, version in self._watched.items()
        ):
            self._backend.watch_conflicts += 1
            self._watched.clear()
            self._queued = None
            raise redis.WatchError("watched key changed")
        results = [getattr(self._backend, op)(*args) for op, args in self._queued or []]
        self._watched.clear()
        self._queued = None
        return results

    def _queue(self, op: str, *args: Any) -> None:
        if self._queued is None:
            raise RuntimeError(f"{op} outside MULTI is not modelled")
        self._queued.append((op, args))
