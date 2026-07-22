"""
Mock Redis Client for Testing.

두 개의 conftest.py에서 중복 정의되었던 MockRedisClient를 통합합니다.
- tests/unit/audit/hash_chain_core/conftest.py (90줄)
- tests/unit/audit/graceful_degradation/conftest.py (90줄)

이 통합된 구현은 양쪽 기능을 모두 지원합니다.
"""

from __future__ import annotations

import fnmatch
import threading
from typing import Any


class MockPipeline:
    """
    Mock Redis Pipeline.

    Redis 파이프라인을 모방하여 여러 명령을 배치로 실행합니다.
    context manager 프로토콜을 지원합니다.
    """

    def __init__(self, redis: MockRedisClient):
        self._redis = redis
        self._commands: list[tuple] = []

    def get(self, key: str) -> MockPipeline:
        """GET 명령 추가."""
        self._commands.append(("get", key))
        return self

    def set(
        self, key: str, value: Any, ex: int | None = None, nx: bool = False
    ) -> MockPipeline:
        """SET 명령 추가."""
        self._commands.append(("set", key, value, ex, nx))
        return self

    def delete(self, *keys: str) -> MockPipeline:
        """DELETE 명령 추가."""
        decoded_keys = []
        for k in keys:
            if isinstance(k, bytes):
                decoded_keys.append(k.decode("utf-8"))
            else:
                decoded_keys.append(k)
        self._commands.append(("delete", tuple(decoded_keys)))
        return self

    def hset(
        self, key: str, mapping: dict[str, Any] | None = None, **kwargs
    ) -> MockPipeline:
        """HSET 명령 추가."""
        self._commands.append(("hset", key, mapping or kwargs))
        return self

    def hget(self, key: str, field: str) -> MockPipeline:
        """HGET 명령 추가."""
        self._commands.append(("hget", key, field))
        return self

    def hgetall(self, key: str) -> MockPipeline:
        """HGETALL 명령 추가."""
        self._commands.append(("hgetall", key))
        return self

    def exists(self, key: str) -> MockPipeline:
        """EXISTS 명령 추가."""
        self._commands.append(("exists", key))
        return self

    def incr(self, key: str) -> MockPipeline:
        """INCR 명령 추가."""
        self._commands.append(("incr", key))
        return self

    def expire(self, key: str, seconds: int) -> MockPipeline:
        """EXPIRE 명령 추가."""
        self._commands.append(("expire", key, seconds))
        return self

    def zadd(self, key: str, mapping: dict[str, float]) -> MockPipeline:
        """Queue a ZADD command (sorted-set member -> score)."""
        self._commands.append(("zadd", key, dict(mapping)))
        return self

    def zremrangebyscore(
        self, key: str, min_score: float, max_score: float
    ) -> MockPipeline:
        """Queue a ZREMRANGEBYSCORE command (inclusive score range)."""
        self._commands.append(("zremrangebyscore", key, min_score, max_score))
        return self

    def zcard(self, key: str) -> MockPipeline:
        """Queue a ZCARD command (sorted-set cardinality)."""
        self._commands.append(("zcard", key))
        return self

    def execute(self) -> list[Any]:
        """배치된 명령들을 실행하고 결과 반환."""
        results: list[Any] = []
        for cmd in self._commands:
            try:
                if cmd[0] == "get":
                    results.append(self._redis.get(cmd[1]))
                elif cmd[0] == "set":
                    key, value = cmd[1], cmd[2]
                    self._redis.set(key, value)
                    results.append(True)
                elif cmd[0] == "delete":
                    count = 0
                    for k in cmd[1]:
                        count += self._redis.delete(k)
                    results.append(count)
                elif cmd[0] == "hset":
                    self._redis.hset(cmd[1], cmd[2])
                    results.append(len(cmd[2]) if cmd[2] else 0)
                elif cmd[0] == "hget":
                    results.append(self._redis.hget(cmd[1], cmd[2]))
                elif cmd[0] == "hgetall":
                    results.append(self._redis.hgetall(cmd[1]))
                elif cmd[0] == "exists":
                    results.append(self._redis.exists(cmd[1]))
                elif cmd[0] == "incr":
                    results.append(self._redis.incr(cmd[1]))
                elif cmd[0] == "expire":
                    results.append(self._redis.expire(cmd[1], cmd[2]))
                elif cmd[0] == "zadd":
                    results.append(self._redis.zadd(cmd[1], cmd[2]))
                elif cmd[0] == "zremrangebyscore":
                    results.append(self._redis.zremrangebyscore(cmd[1], cmd[2], cmd[3]))
                elif cmd[0] == "zcard":
                    results.append(self._redis.zcard(cmd[1]))
                else:
                    results.append(None)
            except Exception as e:
                results.append(e)
        self._commands = []
        return results

    def __enter__(self) -> MockPipeline:
        return self

    def __exit__(self, *args) -> None:
        pass


class MockDistributedLock:
    """
    Mock Distributed Lock.

    분산 락을 모방합니다. graceful_degradation 테스트에서 사용.
    """

    def __init__(self, *args, **kwargs):
        self._acquired = False

    def acquire(self, blocking: bool = True) -> bool:
        """락 획득."""
        self._acquired = True
        return True

    def release(self) -> None:
        """락 해제."""
        self._acquired = False

    def __enter__(self) -> MockDistributedLock:
        self.acquire()
        return self

    def __exit__(self, *args) -> None:
        self.release()


class MockRedisClient:
    """
    테스트용 통합 Mock Redis 클라이언트.

    hash_chain_core와 graceful_degradation의 MockRedisClient를 통합합니다.

    Features:
        - 기본 GET/SET/DELETE 지원
        - Hash 명령 (HGET, HSET, HGETALL)
        - List 명령 (LPUSH, LTRIM)
        - INCR/DECR
        - Pipeline 지원
        - 실패 모드 (should_fail)
        - Thread-safe 연산

    Usage:
        # 정상 모드
        redis = MockRedisClient()
        redis.set("key", "value")
        assert redis.get("key") == b"value"

        # 실패 모드
        failing_redis = MockRedisClient(should_fail=True)
        # 모든 연산이 ConnectionError 발생
    """

    def __init__(self, should_fail: bool = False):
        """
        Mock Redis 클라이언트 초기화.

        Args:
            should_fail: True면 모든 연산에서 ConnectionError 발생
        """
        self._data: dict[str, Any] = {}
        self._hashes: dict[str, dict[str, Any]] = {}
        self._lists: dict[str, list[str]] = {}
        self._zsets: dict[str, dict[str, float]] = {}
        self._should_fail = should_fail
        self._lock = threading.Lock()

    def set_should_fail(self, should_fail: bool) -> None:
        """실패 모드 설정."""
        self._should_fail = should_fail

    def _check_failure(self) -> None:
        """실패 모드 확인 및 예외 발생."""
        if self._should_fail:
            raise ConnectionError("Redis connection failed")

    # =========================================================================
    # String 명령
    # =========================================================================

    def get(self, key: str) -> bytes | None:
        """GET 명령."""
        self._check_failure()
        value = self._data.get(key)
        if value is not None:
            return str(value).encode() if not isinstance(value, bytes) else value
        return None

    def set(
        self,
        key: str,
        value: Any,
        nx: bool = False,
        ex: int | None = None,
        px: int | None = None,
    ) -> bool:
        """
        SET 명령.

        Args:
            key: 키
            value: 값
            nx: True면 키가 없을 때만 설정
            ex: 만료 시간 (초)
            px: 만료 시간 (밀리초)
        """
        self._check_failure()
        with self._lock:
            if nx and key in self._data:
                return False
            self._data[key] = value
            return True

    def delete(self, *keys: str) -> int:
        """DELETE 명령."""
        self._check_failure()
        count = 0
        for key in keys:
            if key in self._data:
                del self._data[key]
                count += 1
            if key in self._hashes:
                del self._hashes[key]
                count += 1
            if key in self._lists:
                del self._lists[key]
                count += 1
            if key in self._zsets:
                del self._zsets[key]
                count += 1
        return count

    def keys(self, pattern: str) -> list[bytes]:
        """KEYS 명령 (패턴 매칭)."""
        self._check_failure()
        matching = [
            k.encode() if isinstance(k, str) else k
            for k in self._data.keys()
            if fnmatch.fnmatch(k, pattern)
        ]
        return matching

    def incr(self, key: str) -> int:
        """INCR 명령."""
        self._check_failure()
        with self._lock:
            current = int(self._data.get(key, 0))
            new_value = current + 1
            self._data[key] = new_value
            return new_value

    def decr(self, key: str) -> int:
        """DECR 명령."""
        self._check_failure()
        with self._lock:
            current = int(self._data.get(key, 0))
            new_value = current - 1
            self._data[key] = new_value
            return new_value

    def expire(self, key: str, seconds: int) -> int:
        """EXPIRE 명령 (Mock: 실제 만료 없음)."""
        self._check_failure()
        return (
            1 if key in self._data or key in self._hashes or key in self._zsets else 0
        )

    def exists(self, *keys: str) -> int:
        """EXISTS 명령."""
        self._check_failure()
        count = 0
        for key in keys:
            if (
                key in self._data
                or key in self._hashes
                or key in self._lists
                or key in self._zsets
            ):
                count += 1
        return count

    # =========================================================================
    # Hash 명령
    # =========================================================================

    def hget(self, key: str, field: str) -> bytes | None:
        """HGET 명령."""
        self._check_failure()
        hash_data = self._hashes.get(key, {})
        value = hash_data.get(field)
        if value is not None:
            return str(value).encode() if not isinstance(value, bytes) else value
        return None

    def hset(self, key: str, mapping: dict[str, Any] | None = None, **kwargs) -> int:
        """HSET 명령."""
        self._check_failure()
        if mapping is None:
            mapping = kwargs
        with self._lock:
            if key not in self._hashes:
                self._hashes[key] = {}
            self._hashes[key].update({str(k): str(v) for k, v in mapping.items()})
            return len(mapping)

    def hgetall(self, key: str) -> dict[bytes, bytes]:
        """HGETALL 명령."""
        self._check_failure()
        hash_data = self._hashes.get(key, {})
        return {
            k.encode() if isinstance(k, str) else k: str(v).encode()
            if not isinstance(v, bytes)
            else v
            for k, v in hash_data.items()
        }

    def hdel(self, key: str, *fields: str) -> int:
        """HDEL 명령."""
        self._check_failure()
        if key not in self._hashes:
            return 0
        count = 0
        for field in fields:
            if field in self._hashes[key]:
                del self._hashes[key][field]
                count += 1
        return count

    # =========================================================================
    # List 명령
    # =========================================================================

    def lpush(self, key: str, *values: str) -> int:
        """LPUSH 명령."""
        self._check_failure()
        if key not in self._lists:
            self._lists[key] = []
        for v in values:
            self._lists[key].insert(0, v)
        return len(self._lists[key])

    def rpush(self, key: str, *values: str) -> int:
        """RPUSH 명령."""
        self._check_failure()
        if key not in self._lists:
            self._lists[key] = []
        for v in values:
            self._lists[key].append(v)
        return len(self._lists[key])

    def ltrim(self, key: str, start: int, end: int) -> bool:
        """LTRIM 명령."""
        self._check_failure()
        if key in self._lists:
            self._lists[key] = self._lists[key][start : end + 1]
        return True

    def lrange(self, key: str, start: int, end: int) -> list[bytes]:
        """LRANGE 명령."""
        self._check_failure()
        if key not in self._lists:
            return []
        if end == -1:
            end = len(self._lists[key])
        else:
            end = end + 1
        return [
            v.encode() if isinstance(v, str) else v for v in self._lists[key][start:end]
        ]

    def llen(self, key: str) -> int:
        """LLEN 명령."""
        self._check_failure()
        return len(self._lists.get(key, []))

    # =========================================================================
    # Sorted Set (ZSET)
    # =========================================================================

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        """ZADD command — add members with scores; returns count of NEW members."""
        self._check_failure()
        with self._lock:
            zset = self._zsets.setdefault(key, {})
            added = sum(1 for member in mapping if member not in zset)
            zset.update(mapping)
            return added

    def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> int:
        """ZREMRANGEBYSCORE command — remove members with min_score <= score <= max_score."""
        self._check_failure()
        with self._lock:
            zset = self._zsets.get(key)
            if not zset:
                return 0
            removed = [
                member
                for member, score in zset.items()
                if min_score <= score <= max_score
            ]
            for member in removed:
                del zset[member]
            return len(removed)

    def zcard(self, key: str) -> int:
        """ZCARD command — number of members in the sorted set."""
        self._check_failure()
        return len(self._zsets.get(key, {}))

    # =========================================================================
    # Pipeline
    # =========================================================================

    def pipeline(self, transaction: bool = True) -> MockPipeline:
        """Pipeline 생성."""
        return MockPipeline(self)

    # =========================================================================
    # Connection
    # =========================================================================

    def ping(self) -> bool:
        """PING 명령."""
        self._check_failure()
        return True

    def flushdb(self) -> bool:
        """FLUSHDB 명령."""
        self._check_failure()
        self._data.clear()
        self._hashes.clear()
        self._lists.clear()
        self._zsets.clear()
        return True

    def close(self) -> None:
        """연결 종료 (Mock: 아무것도 안함)."""
        pass

    # =========================================================================
    # Pub/Sub (Mock)
    # =========================================================================

    def publish(self, channel: str, message: str) -> int:
        """PUBLISH 명령 (Mock: 수신자 0)."""
        self._check_failure()
        return 0

    # =========================================================================
    # Scripting
    # =========================================================================

    def __init_scripts(self):
        """스크립트 저장소 초기화 (lazy init)."""
        if not hasattr(self, "_scripts"):
            self._scripts: dict[str, str] = {}
            self._script_counter = 0

    def script_load(self, script: str) -> str:
        """SCRIPT LOAD 명령 - 스크립트를 로드하고 SHA 반환."""
        self._check_failure()
        self.__init_scripts()
        self._script_counter += 1
        sha = f"sha_{self._script_counter}"
        self._scripts[sha] = script
        return sha

    def eval(self, script: str, numkeys: int, *args) -> Any:
        """
        EVAL 명령 (Lua 스크립트 실행 모의).

        지원 패턴:
        1. Lock release (check-and-delete)
        2. Atomic sequence (INCR + HGET)
        3. Pending commit (EXISTS + DEL)
        """
        self._check_failure()

        # Pattern 1: Atomic sequence reservation (INCR + HGET)
        if "INCR" in script and "HGET" in script:
            seq_key = args[0] if len(args) > 0 else "seq"
            hash_key = args[1] if len(args) > 1 else "hash"
            new_seq = self.incr(seq_key)
            prev_hash = self.hget(hash_key, "previous_hash")
            prev_hash_str = prev_hash.decode() if prev_hash else "GENESIS"
            return [new_seq, prev_hash_str]

        # Pattern 2: Pending commit (EXISTS + DEL)
        if "EXISTS" in script and "DEL" in script:
            pending_key = args[0] if len(args) > 0 else "pending"
            if self.exists(pending_key):
                self.delete(pending_key)
                return {"ok": True}
            return {"err": "PENDING_NOT_FOUND"}

        # Pattern 3: Lock release (check-and-delete)
        if numkeys == 1 and len(args) >= 2:
            key = args[0]
            expected_value = args[1]

            with self._lock:
                current = self._data.get(key)
                if current == expected_value:
                    if key in self._data:
                        del self._data[key]
                    return 1
            return 0

        return 0

    def evalsha(self, sha: str, numkeys: int, *args) -> Any:
        """EVALSHA 명령 - SHA로 스크립트 실행."""
        self._check_failure()
        self.__init_scripts()
        if sha in self._scripts:
            return self.eval(self._scripts[sha], numkeys, *args)
        raise Exception("NOSCRIPT")

    def pexpire(self, key: str, milliseconds: int) -> int:
        """PEXPIRE 명령 (Mock: TTL 설정 없이 존재 여부만 반환)."""
        self._check_failure()
        return 1 if key in self._data or key in self._hashes else 0

    # =========================================================================
    # Utility
    # =========================================================================

    def clear(self) -> None:
        """모든 데이터 초기화."""
        self._data.clear()
        self._hashes.clear()
        self._lists.clear()
        self._zsets.clear()


class FakeRawRedis:
    """In-process dict-backed stand-in for the raw redis client.

    Distinct from :class:`MockRedisClient`: this fake stores values
    *verbatim* (no bytes encoding on ``get``) and implements sorted-set
    primitives (``zadd``/``zrem``/``zcard``/``zrange``). Those two
    properties are what ``ResilientStorageBackend``'s replay handlers and
    recovery sync exercise — the backend's ``get_blob`` returns raw bytes
    verbatim and the DLQ index lives in a ZSET — so the encoding-and-
    list-only ``MockRedisClient`` is unsuitable.

    Implements only the primitives the replay handlers + recovery sync
    touch (set/get/delete, zadd/zrem/zcard/zrange, hset/hgetall/hdel) with
    real state so replay idempotency and recovery round-trips can be
    asserted without external infra.
    """

    def __init__(self) -> None:
        self.kv: dict[str, object] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.hashes: dict[str, dict[str, object]] = {}

    def set(self, key: str, value: object) -> bool:
        self.kv[key] = value
        return True

    def get(self, key: str) -> object | None:
        return self.kv.get(key)

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            for store in (self.kv, self.zsets, self.hashes):
                if key in store:
                    del store[key]
                    removed += 1
        return removed

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        zset = self.zsets.setdefault(key, {})
        added = sum(1 for member in mapping if member not in zset)
        zset.update(mapping)
        return added

    def zrem(self, key: str, *members: str) -> int:
        zset = self.zsets.get(key, {})
        return sum(1 for member in members if zset.pop(member, None) is not None)

    def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    def zrange(self, key: str, start: int, end: int) -> list[str]:
        zset = self.zsets.get(key, {})
        ordered = sorted(zset, key=lambda member: zset[member])
        end_idx = end + 1 if end >= 0 else len(ordered) + end + 1
        return ordered[start:end_idx]

    def hset(self, key: str, mapping: dict[str, object] | None = None) -> int:
        store = self.hashes.setdefault(key, {})
        store.update(mapping or {})
        return len(mapping or {})

    def hgetall(self, key: str) -> dict[str, object]:
        return self.hashes.get(key, {})

    def hdel(self, key: str, field: str) -> int:
        return 1 if self.hashes.get(key, {}).pop(field, None) is not None else 0


class FakeRedisAdapter:
    """Minimal stand-in for ``RedisCacheAdapter`` wrapping a :class:`FakeRawRedis`.

    Exposes the ``_redis`` raw client, ``_serialize`` (identity — the
    counter value is never read back through deserialization in these
    tests), and the high-level ``set``/``get``/``delete`` used by
    ``ResilientStorageBackend._sync_memory_to_redis``'s scalar branch.
    """

    def __init__(self, raw: FakeRawRedis) -> None:
        self._redis = raw

    @property
    def raw_client(self) -> FakeRawRedis:
        """Mirror ``RedisCacheAdapter.raw_client`` — the underlying raw client."""
        return self._redis

    def _serialize(self, value: object) -> object:
        return value

    def set(self, key: str, value: object) -> bool:
        return self._redis.set(key, value)

    def get(self, key: str) -> object | None:
        return self._redis.get(key)

    def delete(self, key: str) -> int:
        return self._redis.delete(key)

    def close(self) -> None:  # noqa: D401 - teardown no-op
        pass


class FakeSortedSetBackend:
    """In-process stand-in that preserves real score ordering.

    Only the operations the compression module reaches for are implemented.
    ``blob_reads`` counts *entry* fetches so a test can assert the ascending
    walk stops early instead of reading the whole index. The namespace's
    structural blobs — the migration marker and its watermark — are excluded:
    they are control-flow reads of fixed cost, and counting them would make
    every walk-cost assertion depend on how often the marker is consulted.

    ``degrade_count`` and ``is_redis_available`` mirror the real backend's
    degradation seam so a test can express "the backend dropped out mid-walk"
    without threads. Two knobs drive the sequences the design's verified-scan
    invariant exists for:

    ``blob_fetch_degrades_on_call``
        The listed ``get_blobs`` calls (1-based) answer all-``None`` and bump
        the counter, while availability reads keep reporting True — the
        recovery-daemon re-promotion race, where a point-in-time mode check
        after the fetch cannot tell the failed chunk from an empty one.

    ``blob_fetch_serves_stale_on_call``
        The same degrade, but the process-local store still holds the
        payloads: the call answers with content rather than ``None`` and bumps
        the counter. This is the shape a caller cannot detect at all by
        looking at the result — the payloads are plausible and possibly
        stale, and acting on them transitions entries that should not have
        transitioned.

    ``peer_degrade_after_availability_read``
        A peer thread degrades immediately after the first availability read
        reports True: the counter bumps once, every subsequent read is served
        from an empty process-local view, and background recovery re-promotes
        before the closing availability read. Nothing bumps the counter while
        already degraded, so a caller that samples the epoch *after* its
        availability read gets a post-bump value that still matches at the end
        — the whole scan looks clean. Only a sample taken first sees the
        pre-bump value.
    """

    _CONTROL_BLOB_KEYS = frozenset(
        {
            "dlq:compressed:status_index_ready",
            "dlq:compressed:backfill_watermark",
        }
    )

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.blob_reads = 0
        self.get_blobs_calls = 0
        self.blob_fetch_degrades_on_call: set[int] = set()
        self.blob_fetch_serves_stale_on_call: set[int] = set()
        self.peer_degrade_after_availability_read = False
        self._is_redis_available = True
        self._serving_memory_view = False
        self._degrade_count = 0

    # -- degradation seam --------------------------------------------------

    @property
    def degrade_count(self) -> int:
        return self._degrade_count

    @degrade_count.setter
    def degrade_count(self, value: int) -> None:
        self._degrade_count = value

    @property
    def is_redis_available(self) -> bool:
        """Report availability, optionally letting a peer thread degrade first.

        The first read reports True and then applies the pending degrade, so
        the transition lands strictly *after* that read — the position from
        which a later epoch sample can no longer detect it. Reads after that
        one report True again: the recovery daemon re-promotes the mode while
        the walk is still running, which is what makes an end-of-scan mode
        check insufficient on its own.
        """
        if self.peer_degrade_after_availability_read:
            self.peer_degrade_after_availability_read = False
            self._degrade_count += 1
            self._serving_memory_view = True
        elif self._serving_memory_view:
            self._serving_memory_view = False
        return self._is_redis_available

    @is_redis_available.setter
    def is_redis_available(self, value: bool) -> None:
        self._is_redis_available = value

    def _reads_served_locally(self) -> bool:
        """True while reads come from the process-local view, not Redis."""
        return self._serving_memory_view or not self._is_redis_available

    # -- blob store --------------------------------------------------------

    def set_blob(self, key: str, value: bytes) -> None:
        self.blobs[key] = value

    def get_blob(self, key: str) -> bytes | None:
        if key not in self._CONTROL_BLOB_KEYS:
            self.blob_reads += 1
        return self.blobs.get(key)

    def get_blobs(self, keys: list[str]) -> list[bytes | None]:
        # Batched read must charge the same per-key cost as get_blob, so a
        # cost assertion cannot be passed by accident (see D8).
        self.get_blobs_calls += 1
        if self.get_blobs_calls in self.blob_fetch_degrades_on_call:
            # Degrade + serve the process-local view, then let background
            # recovery re-promote before the caller inspects the results.
            self._degrade_count += 1
            return [None for _ in keys]
        if self.get_blobs_calls in self.blob_fetch_serves_stale_on_call:
            # Same degrade, but the local store still holds the payloads, so
            # the response is indistinguishable from a healthy one.
            self._degrade_count += 1
            return [self.get_blob(k) for k in keys]
        if self._reads_served_locally():
            return [None for _ in keys]
        return [self.get_blob(k) for k in keys]

    # -- sorted sets -------------------------------------------------------

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        """Add members, returning the count that were new — as Redis does.

        The backfill's completion criterion sums exactly this value, so a
        fake that always reported zero would let a migration conclude on a
        walk that had just filed thousands of entries.
        """
        zset = self.zsets.setdefault(key, {})
        new = sum(1 for member in mapping if member not in zset)
        zset.update(mapping)
        return new

    def zrem(self, key: str, members) -> int:
        """Remove members from a sorted set (list or single-str, as the batch
        ops emit them). Returns the count actually removed, like Redis."""
        zset = self.zsets.get(key)
        if not zset:
            return 0
        if isinstance(members, str):
            members = [members]
        removed = 0
        for m in members:
            if zset.pop(m, None) is not None:
                removed += 1
        return removed

    def _sorted_items(self, key: str) -> list[tuple[str, float]]:
        members = self.zsets.get(key, {})
        return sorted(members.items(), key=lambda kv: (kv[1], kv[0]))

    def _ordered(self, key: str) -> list[str]:
        return [m for m, _ in self._sorted_items(key)]

    def zrange(self, key: str, start: int, end: int) -> list[str]:
        if self._reads_served_locally():
            return []
        ids = self._ordered(key)
        return ids[start:] if end < 0 else ids[start : end + 1]

    def zrangebyscore(
        self,
        key: str,
        min_score: float,
        max_score: float,
        *,
        offset: int = 0,
        count: int | None = None,
    ) -> list[str]:
        if self._reads_served_locally():
            return []
        selected = [
            m for m, s in self._sorted_items(key) if min_score <= s <= max_score
        ]
        if count is None:
            return selected[offset:]
        return selected[offset : offset + count]

    def zrevrange(self, key: str, start: int, end: int) -> list[str]:
        if self._reads_served_locally():
            return []
        ids = list(reversed(self._ordered(key)))
        return ids[start:] if end < 0 else ids[start : end + 1]

    def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    def batch_write_ops(self, ops: list[tuple]) -> None:
        for op in ops:
            getattr(self, op[0])(*op[1:])
