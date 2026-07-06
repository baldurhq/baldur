"""Multi-process-safe DLQ create path coverage (538).

New behavior coverage for the 538 rewrite (the existing-test int->str sweep
was completed inline by ``/execute``). All of it is exercisable in one
process via the injectable identity seam (N ``RedisDLQRepository`` instances
with distinct ``pod_id``/``pid``/``run_nonce``) over a real
``ResilientStorageBackend`` + WAL on a tmpdir, plus an in-process fake redis
for the normal-mode scan — no real subprocess, Redis, or DB.

Test classes:
    TestAllocateIdBehavior     — composite-ID format, monotonic seq,
                                 multi-identity collision absence (D2)
    TestEntryKeyDiscrimination — _make_key / _is_valid_entry_key whitelist
                                 (D6, Contract)
    TestCreateGroupedOp        — create() allocates id + one batch_write_ops
                                 ([set_blob, zadd, zadd]) (D2/D3)
    TestBatchWriteOps          — single-fsync, normal-mode-failure re-apply,
                                 idempotency (D3)
    TestWalRecordDriftGuard    — per-op set_blob/zadd vs batched records carry
                                 identical replay-relevant fields (D3/R3)
    TestByStatusNormalScan     — dlq:entry:* glob returns composite entries,
                                 HASH-typed dlq:compressed:* excluded without
                                 WRONGTYPE (D6/G4)
    TestPostRecoveryNoReuse    — distinct composite keys survive startup-glob
                                 recovery with no last-write-wins collapse (D2)
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.adapters.resilient.backend import (
    ResilientStorageBackend,
    ResilientStorageMode,
)
from baldur.interfaces.repositories import FailedOperationStatus
from baldur.settings.resilient_storage import ResilientStorageSettings

# =============================================================================
# Fakes / helpers
# =============================================================================


class _FakeRaw:
    """In-process dict-backed raw redis stand-in.

    Implements the primitives the create / scan / recovery paths touch with
    real state (set/get/zadd/zrem/zcard/zrange/hset/delete/scan). ``get``
    raises a WRONGTYPE-style error on a HASH-typed key so a test can prove the
    ``dlq:entry:*`` glob never feeds a compressed HASH key to the GET-after-scan.
    """

    def __init__(self, fail_zadd: bool = False) -> None:
        self.kv: dict[str, object] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.hashes: dict[str, dict[str, object]] = {}
        self.get_calls: list[str] = []
        self._fail_zadd = fail_zadd

    def set(self, key: str, value: object) -> bool:
        self.kv[key] = value
        return True

    def get(self, key: str) -> object | None:
        self.get_calls.append(key)
        if key in self.hashes:
            raise RuntimeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )
        return self.kv.get(key)

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        if self._fail_zadd:
            raise ConnectionError("redis down")
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

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            for store in (self.kv, self.zsets, self.hashes):
                if key in store:
                    del store[key]
                    removed += 1
        return removed

    def scan(
        self, cursor: int, match: str | None = None, count: int = 100
    ) -> tuple[int, list[str]]:
        all_keys = list(self.kv) + list(self.zsets) + list(self.hashes)
        if match:
            all_keys = [k for k in all_keys if fnmatch.fnmatch(k, match)]
        return (0, all_keys)

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        """Buffering pipeline that replays buffered ops on ``.execute()``
        against the same fake (543 D4).

        Required because ``batch_write_ops`` (normal-mode REDIS branch)
        issues a single pipelined ``.execute()`` instead of per-op
        client calls; the fake's ``fail_zadd=True`` knob fires at
        ``.execute()``, matching the real pipeline's failure granularity
        (commands buffer client-side; the network round-trip happens on
        ``.execute()``).
        """
        return _FakePipeline(self)


class _FakePipeline:
    """Minimal pipeline stand-in: buffer ``.set``/``.zadd``, replay on
    ``.execute()`` against the owning ``_FakeRaw`` (543 D4)."""

    def __init__(self, raw: _FakeRaw) -> None:
        self._raw = raw
        self._ops: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def __enter__(self) -> _FakePipeline:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Discard any buffered (unexecuted) commands on context exit so a
        # build-time abort leaves no prefix in the fake.
        self._ops.clear()
        return False

    def set(self, key: str, value: object) -> _FakePipeline:
        self._ops.append(("set", (key, value), {}))
        return self

    def zadd(self, key: str, mapping: dict[str, float]) -> _FakePipeline:
        self._ops.append(("zadd", (key, mapping), {}))
        return self

    def zrem(self, key: str, *members: str) -> _FakePipeline:
        # 544 D6: buffer the zrem op so ``batch_write_ops`` can include
        # zrem in its 1-RTT pipeline.
        self._ops.append(("zrem", (key, *members), {}))
        return self

    def delete(self, key: str) -> _FakePipeline:
        # 544 D6: buffer the delete op so ``batch_write_ops`` collapses
        # DLQ delete (blob + 5 zrems) to a single round trip.
        self._ops.append(("delete", (key,), {}))
        return self

    def execute(self) -> list[object]:
        results: list[object] = []
        try:
            for name, args, kwargs in self._ops:
                method = getattr(self._raw, name)
                results.append(method(*args, **kwargs))
        finally:
            self._ops.clear()
        return results


class _FakeAdapter:
    """Minimal RedisCacheAdapter stand-in wrapping a ``_FakeRaw``."""

    def __init__(self, raw: _FakeRaw) -> None:
        self._redis = raw

    @property
    def raw_client(self) -> _FakeRaw:
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


def _mock_backend_repo(
    pod_id: str = "pod-a",
    pid: int = 100,
    run_nonce: str = "nonce0",
    key_prefix: str = "",
) -> tuple[RedisDLQRepository, MagicMock]:
    """Construct a repo over a MagicMock backend with an injected identity.

    Uses the real ``__init__`` (which never touches the backend during
    construction) so the identity seam is wired exactly as in production.
    """
    backend = MagicMock()
    backend.config.key_prefix = key_prefix
    repo = RedisDLQRepository(backend, pod_id=pod_id, pid=pid, run_nonce=run_nonce)
    return repo, backend


@pytest.fixture
def backend_factory():
    """Yield a factory building real ``ResilientStorageBackend`` instances on
    fresh tmpdir WALs. ``use_dynamic_prefix=False`` pins the static
    ``baldur:`` key_prefix so full keys are deterministic; in-memory mutations
    still use the short component key. Created backends are closed + their dirs
    removed on teardown (Windows-safe — no open WAL handle blocks the rmtree)."""
    created: list[ResilientStorageBackend] = []
    dirs: list[str] = []

    def make(*, redis_raw: _FakeRaw | None = None) -> ResilientStorageBackend:
        wal_dir = tempfile.mkdtemp()
        dirs.append(wal_dir)
        settings = ResilientStorageSettings(
            wal_dir=wal_dir,
            allow_memory_only=True,
            use_dynamic_prefix=False,
        )
        backend = ResilientStorageBackend(settings)
        # Push the first-init probe far out so hot-path _ensure_redis() never
        # attempts a real local-dev Redis connection (which would flip to REDIS).
        backend._next_redis_probe = time.monotonic() + 9999.0
        if redis_raw is not None:
            backend._redis = _FakeAdapter(redis_raw)
            backend._redis_initialized = True
            backend._mode = ResilientStorageMode.REDIS
        created.append(backend)
        return backend

    yield make

    for backend in created:
        try:
            backend.close()
        except Exception:
            pass
    for wal_dir in dirs:
        shutil.rmtree(wal_dir, ignore_errors=True)


def _strip_volatile(record: dict[str, object]) -> dict[str, object]:
    """Drop the wall-clock ``timestamp`` so two WAL records can be compared on
    their replay-relevant fields only (operation/key/value/members)."""
    return {k: v for k, v in record.items() if k != "timestamp"}


# =============================================================================
# TestAllocateIdBehavior — composite-ID format + collision absence (D2)
# =============================================================================


class TestAllocateIdBehavior:
    """_allocate_id() emits a process-namespaced composite token that is
    collide-free across uncoordinated worker processes."""

    def test_allocate_id_format_is_pod_pid_nonce_seq(self):
        repo, _ = _mock_backend_repo(pod_id="pod-a", pid=100, run_nonce="nonce0")

        assert repo._allocate_id() == "pod-a:100:nonce0:0"

    def test_allocate_id_seq_is_monotonic_per_process(self):
        # State transition: the per-process seq counter advances by 1 each call.
        repo, _ = _mock_backend_repo(pod_id="pod-a", pid=100, run_nonce="nonce0")

        ids = [repo._allocate_id() for _ in range(5)]

        seqs = [int(entry_id.rsplit(":", 1)[1]) for entry_id in ids]
        assert seqs == [0, 1, 2, 3, 4]

    def test_allocate_id_default_identity_uses_runtime_pid_and_random_nonce(self):
        # Given no injected identity, pid defaults to os.getpid() and the nonce
        # is a 64-bit secrets.token_hex(8) (16 hex chars).
        repo = RedisDLQRepository(MagicMock())

        _pod, pid, nonce, seq = repo._allocate_id().rsplit(":", 3)

        assert pid == str(os.getpid())
        assert seq == "0"
        assert len(nonce) == 16
        int(nonce, 16)  # hex-parseable — raises if not

    @pytest.mark.parametrize(
        ("identity_a", "identity_b"),
        [
            # same host, distinct pid (gunicorn -w N)
            (("pod-1", 11, "nonce"), ("pod-1", 22, "nonce")),
            # multi-pod, distinct pod_id
            (("pod-a", 11, "nonce"), ("pod-b", 11, "nonce")),
            # restart with pid reuse (e.g. app as pid 1), distinct run_nonce
            (("pod-1", 1, "nonceA"), ("pod-1", 1, "nonceB")),
        ],
    )
    def test_allocate_id_distinct_identities_allocate_disjoint_ids(
        self, identity_a, identity_b
    ):
        repo_a, _ = _mock_backend_repo(*identity_a)
        repo_b, _ = _mock_backend_repo(*identity_b)

        ids_a = {repo_a._allocate_id() for _ in range(50)}
        ids_b = {repo_b._allocate_id() for _ in range(50)}

        assert ids_a.isdisjoint(ids_b)
        assert len(ids_a) == len(ids_b) == 50

    def test_allocate_id_restart_same_pid_does_not_reuse_without_nonce_collision(
        self,
    ):
        # Two processes sharing pod_id AND pid (container pid-1 restart) still
        # never collide because run_nonce disambiguates the namespace.
        repo_old, _ = _mock_backend_repo("pod-1", 1, "old-run")
        repo_new, _ = _mock_backend_repo("pod-1", 1, "new-run")

        # Both allocate from seq 0 — the bare {pod}:{pid}:{seq} would collide.
        old_id = repo_old._allocate_id()
        new_id = repo_new._allocate_id()

        assert old_id == "pod-1:1:old-run:0"
        assert new_id == "pod-1:1:new-run:0"
        assert old_id != new_id


# =============================================================================
# TestEntryKeyDiscrimination — _make_key / _is_valid_entry_key (D6, Contract)
# =============================================================================


class TestEntryKeyDiscrimination:
    """Positive-match whitelist: a key is an entry iff it lives under the
    dedicated ``dlq:entry:`` namespace. Hardcoded against the D6 contract."""

    def test_make_key_wraps_id_in_entry_namespace(self):
        repo, _ = _mock_backend_repo()

        assert repo._make_key("pod-a:100:nonce0:0") == "dlq:entry:pod-a:100:nonce0:0"

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            # composite entry keys — valid
            ("dlq:entry:pod-a:100:nonce0:0", True),
            # pod segment merely RESEMBLES a reserved word — still valid
            # (pod_id sits in the 3rd segment, never the discriminator position)
            ("dlq:entry:pending-1:1:nonce:0", True),
            # special-key families — every one excluded
            ("dlq:pending", False),
            ("dlq:status:resolved", False),
            ("dlq:by_domain:payment", False),
            ("dlq:id_seq", False),
            ("dlq:compressed:abc123", False),
            ("dlq:audit:2026-05-27", False),
            # legacy bare-int orphan — excluded (intentional, D5)
            ("dlq:42", False),
        ],
    )
    def test_is_valid_entry_key_whitelist(self, key, expected):
        repo, _ = _mock_backend_repo(key_prefix="")

        assert repo._is_valid_entry_key(key) is expected

    def test_is_valid_entry_key_strips_backend_prefix(self):
        # The backend key_prefix is stripped before matching the relative key.
        repo, _ = _mock_backend_repo(key_prefix="baldur:")

        assert repo._is_valid_entry_key("baldur:dlq:entry:pod:1:n:0") is True
        assert repo._is_valid_entry_key("baldur:dlq:pending") is False
        assert repo._is_valid_entry_key("baldur:dlq:compressed:x") is False


# =============================================================================
# TestCreateGroupedOp — create() allocates id + one batch_write_ops (D2/D3)
# =============================================================================


class TestCreateGroupedOp:
    """create() allocates a composite id and issues a single grouped op
    [set_blob, zadd(pending), zadd(by_domain), zadd(global), zadd(composite),
    zadd(domain registry)] — 6 ops (541 D6 added the global dlq:all index;
    544 D1/D2/D3 added the composite (status, domain) ZSET and the
    domain registry ZSET) all riding a single batch_write_ops call."""

    def test_create_issues_single_batch_write_ops_with_six_ops(self):
        repo, backend = _mock_backend_repo(pod_id="pod-a", pid=100, run_nonce="nonce0")
        repo._compression_enabled = MagicMock(return_value=False)

        result = repo.create(
            domain="payment",
            failure_type="PG_TIMEOUT",
            error_message="boom",
            request_data={"order_id": 1},
        )

        backend.batch_write_ops.assert_called_once()
        ops = backend.batch_write_ops.call_args.args[0]
        assert len(ops) == 6

        entry_id = "pod-a:100:nonce0:0"
        # op 0: blob under the dlq:entry: namespace
        assert ops[0][0] == "set_blob"
        assert ops[0][1] == f"dlq:entry:{entry_id}"
        assert isinstance(ops[0][2], bytes)
        # op 1: pending index zadd
        assert ops[1][0] == "zadd"
        assert ops[1][1] == "dlq:pending"
        assert list(ops[1][2].keys()) == [entry_id]
        # op 2: by-domain index zadd
        assert ops[2][0] == "zadd"
        assert ops[2][1] == "dlq:by_domain:payment"
        assert list(ops[2][2].keys()) == [entry_id]
        # op 3: global index zadd (541 D6)
        assert ops[3][0] == "zadd"
        assert ops[3][1] == "dlq:all"
        assert list(ops[3][2].keys()) == [entry_id]
        # op 4: composite (status, domain) zadd (544 D1/D2)
        assert ops[4][0] == "zadd"
        assert ops[4][1] == "dlq:status_domain:pending:payment"
        assert list(ops[4][2].keys()) == [entry_id]
        # op 5: domain-registry zadd (544 D3) — keyed by domain, not entry_id
        assert ops[5][0] == "zadd"
        assert ops[5][1] == "dlq:domains"
        assert list(ops[5][2].keys()) == ["payment"]

        # The four entry-id-keyed zadds share the same created_at-epoch
        # score (541 D6, extended to the composite by 544 D1).
        assert (
            ops[1][2][entry_id]
            == ops[2][2][entry_id]
            == ops[3][2][entry_id]
            == ops[4][2][entry_id]
        )

        # The returned DTO carries the composite str id and PENDING status.
        assert result.id == entry_id
        assert result.domain == "payment"
        assert result.status == FailedOperationStatus.PENDING.value

    def test_create_blob_op_decodes_to_the_entry(self):
        repo, backend = _mock_backend_repo(pod_id="pod-a", pid=100, run_nonce="nonce0")
        repo._compression_enabled = MagicMock(return_value=False)

        repo.create(domain="point", failure_type="SHORTAGE")

        ops = backend.batch_write_ops.call_args.args[0]
        decoded = repo._decode_entry(ops[0][2])
        assert decoded["id"] == "pod-a:100:nonce0:0"
        assert decoded["domain"] == "point"
        assert decoded["status"] == FailedOperationStatus.PENDING.value

    def test_create_consecutive_calls_advance_the_seq(self):
        repo, backend = _mock_backend_repo(pod_id="pod-a", pid=100, run_nonce="nonce0")
        repo._compression_enabled = MagicMock(return_value=False)

        first = repo.create(domain="d", failure_type="t")
        second = repo.create(domain="d", failure_type="t")

        assert first.id == "pod-a:100:nonce0:0"
        assert second.id == "pod-a:100:nonce0:1"


# =============================================================================
# TestBatchWriteOps — single fsync, failure re-apply, idempotency (D3)
# =============================================================================


class TestBatchWriteOps:
    """ResilientStorageBackend.batch_write_ops grouped transactional write."""

    _OPS = [
        ("set_blob", "dlq:entry:x", b"\x78blob-bytes"),
        ("zadd", "dlq:pending", {"x": 1.0}),
        ("zadd", "dlq:by_domain:d", {"x": 1.0}),
    ]

    def test_degraded_batch_uses_single_fsync_not_per_op_writes(self, backend_factory):
        backend = backend_factory()  # DEGRADED, real tmpdir WAL

        with (
            patch.object(
                backend._wal,
                "batch_write_entries",
                wraps=backend._wal.batch_write_entries,
            ) as batch_spy,
            patch.object(backend._wal, "write", wraps=backend._wal.write) as write_spy,
        ):
            backend.batch_write_ops(self._OPS)

        # One batched fsync for all 3 ops; no per-op write on the create path.
        assert batch_spy.call_count == 1
        write_spy.assert_not_called()
        # All 3 records went into the single batch call.
        assert len(batch_spy.call_args.args[0]) == 3

    def test_degraded_batch_applies_all_memory_mutations(self, backend_factory):
        backend = backend_factory()

        backend.batch_write_ops(self._OPS)

        # Blob (bytes) lives in the bounded blob store (#539 D2); both zset
        # lists (degraded shape) stay in _memory.
        assert backend._blob_memory["dlq:entry:x"] == b"\x78blob-bytes"
        assert backend._memory["dlq:pending"] == [{"member": "x", "score": 1.0}]
        assert backend._memory["dlq:by_domain:d"] == [{"member": "x", "score": 1.0}]

    def test_normal_mode_mid_op_failure_reapplies_all_ops_to_degraded(
        self, backend_factory
    ):
        # 543 D1/D3: pipeline buffers all 3 ops; ``.execute()`` replays them
        # in order against ``_FakeRaw`` and the first ``.zadd`` call raises
        # (fail_zadd=True), so ``set_blob`` lands as a *prefix* in
        # ``_FakeRaw.kv`` before the failure surfaces. The wrapper then
        # switches to degraded and re-applies the ENTIRE op list — the entry
        # must end up fully in degraded WAL+memory, never split.
        raw = _FakeRaw(fail_zadd=True)
        backend = backend_factory(redis_raw=raw)
        # 543 D5: wire a MagicMock shadow so we can assert the cause was
        # captured (the wrapper now threads ``error=e`` to the degraded
        # path). ``backend_factory`` does not wire a shadow by default.
        backend._shadow = MagicMock()
        full_blob_key = backend._get_full_key("dlq:entry:x")

        with patch.object(
            backend._wal,
            "batch_write_entries",
            wraps=backend._wal.batch_write_entries,
        ) as batch_spy:
            backend.batch_write_ops(self._OPS)

        assert backend.mode == ResilientStorageMode.DEGRADED
        # (review 2a) The ``set_blob`` prefix reached Redis before
        # ``.execute()`` failed on the first ``zadd`` — proving this test
        # exercises the genuine prefix-reached-Redis split risk, not a
        # clean all-or-nothing failure.
        assert raw.kv == {full_blob_key: b"\x78blob-bytes"}
        assert raw.zsets == {}
        # The whole op list (incl. the blob that already reached Redis) is in
        # degraded memory — no split. The blob lives in the bounded blob
        # store (#539 D2); the zset lists stay in _memory.
        assert backend._blob_memory["dlq:entry:x"] == b"\x78blob-bytes"
        assert backend._memory["dlq:pending"] == [{"member": "x", "score": 1.0}]
        assert backend._memory["dlq:by_domain:d"] == [{"member": "x", "score": 1.0}]
        # Degraded re-apply still uses one batched fsync for all 3 records.
        assert batch_spy.call_count == 1
        assert len(batch_spy.call_args.args[0]) == 3
        # (543 D5/1b) Exactly one shadow record per failed batch, keyed on
        # the entry (``set_blob``) key, carrying the caught ConnectionError.
        assert backend._shadow.record_sync_failure.call_count == 1
        shadow_call = backend._shadow.record_sync_failure.call_args
        assert shadow_call.kwargs["service_name"] == "dlq:entry:x"
        assert shadow_call.kwargs["adapter_type"] == "redis"
        assert isinstance(shadow_call.kwargs["error"], ConnectionError)

    def test_normal_mode_reapply_is_idempotent(self, backend_factory):
        # 543 D3: set-to-value / zadd-to-score replay is idempotent under
        # the pipeline — applying the same op list twice in normal mode
        # leaves identical Redis state (no double-count, no split).
        raw = _FakeRaw()
        backend = backend_factory(redis_raw=raw)

        backend.batch_write_ops(self._OPS)
        kv_after_first = dict(raw.kv)
        zsets_after_first = {k: dict(v) for k, v in raw.zsets.items()}

        backend.batch_write_ops(self._OPS)

        assert raw.kv == kv_after_first
        assert {k: dict(v) for k, v in raw.zsets.items()} == zsets_after_first

    def test_normal_mode_pipelines_batch_as_single_execute(self, backend_factory):
        # 543 D1: every op in the batch must buffer on one pipeline and
        # ride a single ``.execute()`` — proves the create is 1 RTT, not
        # N. ``_FakeRaw``'s ``.execute()`` delegates to ``self.set/zadd``,
        # so this single-RTT proof needs a MagicMock raw client.
        backend = backend_factory()
        raw_client = MagicMock()
        pipe = MagicMock()
        pipe.__enter__.return_value = pipe
        pipe.__exit__.return_value = False
        raw_client.pipeline.return_value = pipe
        backend._redis = _FakeAdapter(raw_client)
        backend._redis_initialized = True
        backend._mode = ResilientStorageMode.REDIS

        backend.batch_write_ops(self._OPS)

        # One pipeline, non-transactional, one ``.execute()``.
        raw_client.pipeline.assert_called_once_with(transaction=False)
        assert pipe.execute.call_count == 1
        # Per-op client calls are NOT made directly on the raw client —
        # they only buffer on the pipeline mock.
        raw_client.set.assert_not_called()
        raw_client.zadd.assert_not_called()
        # (review 2b) Each op was buffered once on the pipeline with the
        # correct full key/value, in order, count == len(ops).
        full = backend._get_full_key
        pipe.set.assert_called_once_with(full("dlq:entry:x"), b"\x78blob-bytes")
        assert pipe.zadd.call_count == 2
        pipe.zadd.assert_any_call(full("dlq:pending"), {"x": 1.0})
        pipe.zadd.assert_any_call(full("dlq:by_domain:d"), {"x": 1.0})
        # set+zadd buffer count == op count → no over-/under-buffering.
        assert pipe.set.call_count + pipe.zadd.call_count == len(self._OPS)

    def test_normal_mode_unsupported_op_raises_without_degrading(self, backend_factory):
        # 543 D3: an unsupported op trips ``ValueError`` during pipeline
        # *build* — before ``.execute()`` — so no buffered command reaches
        # Redis and mode stays REDIS (strictly cleaner than the old loop,
        # which had already written ops 0..k-1 before hitting the
        # unsupported op at k).
        raw = _FakeRaw()
        backend = backend_factory(redis_raw=raw)
        ops_with_bad_tail = [
            ("set_blob", "dlq:entry:x", b"\x78blob-bytes"),
            ("zadd", "dlq:pending", {"x": 1.0}),
            ("hset", "dlq:bad", {"a": 1}),
        ]

        with pytest.raises(ValueError, match="Unsupported batch op"):
            backend.batch_write_ops(ops_with_bad_tail)

        assert backend.mode == ResilientStorageMode.REDIS
        # Buffered commands were discarded on build-abort — no prefix
        # leaked to Redis.
        assert raw.kv == {}
        assert raw.zsets == {}

    def test_unsupported_op_name_raises_value_error(self, backend_factory):
        # DEGRADED-mode path (no Redis wired): unsupported op still raises
        # ValueError during the WAL record-build loop.
        backend = backend_factory()

        with pytest.raises(ValueError, match="Unsupported batch op"):
            backend.batch_write_ops([("hset", "dlq:bad", {"a": 1})])

    # 544 D6: zrem AND delete enter the batch_write_ops vocabulary so
    # ``_update`` status transitions and ``delete`` collapse to 1 RTT.

    def test_normal_mode_zrem_in_batch_is_pipelined(self, backend_factory):
        """A batch carrying a ``zrem`` op buffers on the same pipeline and
        rides a single ``.execute()`` -- no per-op client call."""
        backend = backend_factory()
        raw_client = MagicMock()
        pipe = MagicMock()
        pipe.__enter__.return_value = pipe
        pipe.__exit__.return_value = False
        raw_client.pipeline.return_value = pipe
        backend._redis = _FakeAdapter(raw_client)
        backend._redis_initialized = True
        backend._mode = ResilientStorageMode.REDIS

        backend.batch_write_ops(
            [
                ("set_blob", "dlq:entry:x", b"blob"),
                ("zrem", "dlq:pending", ["x"]),
                ("zrem", "dlq:status:resolved", ["x"]),
            ]
        )

        raw_client.pipeline.assert_called_once_with(transaction=False)
        assert pipe.execute.call_count == 1
        # zrem buffered on the pipeline twice with the right full keys.
        assert pipe.zrem.call_count == 2
        full = backend._get_full_key
        pipe.zrem.assert_any_call(full("dlq:pending"), "x")
        pipe.zrem.assert_any_call(full("dlq:status:resolved"), "x")
        # No direct raw client zrem calls -- only buffered on the pipeline.
        raw_client.zrem.assert_not_called()

    def test_normal_mode_zrem_accepts_string_or_list_members(self, backend_factory):
        """``zrem`` ops can pass a single str or a list of strs; both
        unpack to ``pipe.zrem(full_key, *members)``."""
        backend = backend_factory()
        raw_client = MagicMock()
        pipe = MagicMock()
        pipe.__enter__.return_value = pipe
        pipe.__exit__.return_value = False
        raw_client.pipeline.return_value = pipe
        backend._redis = _FakeAdapter(raw_client)
        backend._redis_initialized = True
        backend._mode = ResilientStorageMode.REDIS

        backend.batch_write_ops(
            [
                ("zrem", "dlq:pending", "x"),  # single str
                ("zrem", "dlq:by_domain:payment", ["a", "b"]),  # list
            ]
        )

        full = backend._get_full_key
        pipe.zrem.assert_any_call(full("dlq:pending"), "x")
        pipe.zrem.assert_any_call(full("dlq:by_domain:payment"), "a", "b")

    def test_normal_mode_delete_in_batch_is_pipelined(self, backend_factory):
        """A batch carrying a ``delete`` op buffers on the same pipeline
        and rides a single ``.execute()`` -- proves DLQ.delete is 1 RTT."""
        backend = backend_factory()
        raw_client = MagicMock()
        pipe = MagicMock()
        pipe.__enter__.return_value = pipe
        pipe.__exit__.return_value = False
        raw_client.pipeline.return_value = pipe
        backend._redis = _FakeAdapter(raw_client)
        backend._redis_initialized = True
        backend._mode = ResilientStorageMode.REDIS

        backend.batch_write_ops(
            [
                ("delete", "dlq:entry:x", None),
                ("zrem", "dlq:pending", ["x"]),
                ("zrem", "dlq:by_domain:payment", ["x"]),
            ]
        )

        raw_client.pipeline.assert_called_once_with(transaction=False)
        assert pipe.execute.call_count == 1
        full = backend._get_full_key
        pipe.delete.assert_called_once_with(full("dlq:entry:x"))
        # No direct raw client delete call -- only pipeline buffered.
        raw_client.delete.assert_not_called()

    def test_degraded_zrem_in_batch_applies_memory_mutation(self, backend_factory):
        """Degraded batch with a zrem replays through ``_mem_apply_zrem``
        so the in-memory degraded ZSET shrinks as Redis would."""
        backend = backend_factory()  # DEGRADED
        # Seed an existing zset via the per-op zadd.
        backend.zadd("dlq:pending", {"x": 1.0, "y": 2.0})

        backend.batch_write_ops([("zrem", "dlq:pending", ["x"])])

        # Member x removed; y remains.
        remaining = [item["member"] for item in backend._memory["dlq:pending"]]
        assert "x" not in remaining
        assert "y" in remaining

    def test_degraded_delete_in_batch_applies_memory_mutation(self, backend_factory):
        """Degraded batch with a delete pops both _memory and _blob_memory
        and decrements the blob-byte accumulator."""
        backend = backend_factory()  # DEGRADED
        blob = b"\x78compressed"
        backend.set_blob("dlq:entry:x", blob)
        assert backend._blob_memory_bytes == len(blob)

        backend.batch_write_ops([("delete", "dlq:entry:x", None)])

        assert "dlq:entry:x" not in backend._blob_memory
        assert backend._blob_memory_bytes == 0


# =============================================================================
# TestWalRecordDriftGuard — per-op vs batched records identical (D3/R3)
# =============================================================================


class TestWalRecordDriftGuard:
    """The per-op set_blob/zadd path and the batched batch_write_ops path feed
    the SAME _wal_record_* helpers, so their replay-relevant fields are
    identical by construction — a future per-op change cannot desync the
    batched path's recovery records."""

    def test_set_blob_record_matches_between_per_op_and_batched(self, backend_factory):
        backend = backend_factory()
        blob = b"\x78compressed-payload"

        with patch.object(backend._wal, "write") as write_spy:
            backend.set_blob("dlq:entry:x", blob)
        per_op_record = write_spy.call_args.args[0]

        with patch.object(backend._wal, "batch_write_entries") as batch_spy:
            backend.batch_write_ops([("set_blob", "dlq:entry:x", blob)])
        batched_record = batch_spy.call_args.args[0][0]

        assert _strip_volatile(per_op_record) == _strip_volatile(batched_record)
        # Sanity: it really is the set_blob op carrying the base64-wrapped value.
        assert per_op_record["operation"] == "set_blob"
        assert per_op_record["key"] == backend._get_full_key("dlq:entry:x")

    def test_zadd_record_matches_between_per_op_and_batched(self, backend_factory):
        backend = backend_factory()
        mapping = {"x": 12.5}

        with patch.object(backend._wal, "write") as write_spy:
            backend.zadd("dlq:pending", mapping)
        per_op_record = write_spy.call_args.args[0]

        with patch.object(backend._wal, "batch_write_entries") as batch_spy:
            backend.batch_write_ops([("zadd", "dlq:pending", mapping)])
        batched_record = batch_spy.call_args.args[0][0]

        assert _strip_volatile(per_op_record) == _strip_volatile(batched_record)
        assert per_op_record["operation"] == "zadd"
        assert per_op_record["key"] == backend._get_full_key("dlq:pending")
        assert per_op_record["value"] == mapping

    def test_zrem_record_matches_between_per_op_and_batched(self, backend_factory):
        """544 D6: per-op ``zrem`` and batched ``zrem`` produce the same
        WAL record shape so the batched path's replay is byte-identical
        to the per-op path."""
        backend = backend_factory()
        members = ["x", "y"]

        with patch.object(backend._wal, "write") as write_spy:
            backend.zrem("dlq:pending", *members)
        per_op_record = write_spy.call_args.args[0]

        with patch.object(backend._wal, "batch_write_entries") as batch_spy:
            backend.batch_write_ops([("zrem", "dlq:pending", members)])
        batched_record = batch_spy.call_args.args[0][0]

        assert _strip_volatile(per_op_record) == _strip_volatile(batched_record)
        assert per_op_record["operation"] == "zrem"
        assert per_op_record["key"] == backend._get_full_key("dlq:pending")
        assert per_op_record["members"] == members

    def test_delete_record_matches_between_per_op_and_batched(self, backend_factory):
        """544 D6: per-op ``delete`` and batched ``delete`` produce the
        same WAL record shape -- ``key`` only, no ``value`` body."""
        backend = backend_factory()

        with patch.object(backend._wal, "write") as write_spy:
            backend.delete("dlq:entry:x")
        per_op_record = write_spy.call_args.args[0]

        with patch.object(backend._wal, "batch_write_entries") as batch_spy:
            backend.batch_write_ops([("delete", "dlq:entry:x", None)])
        batched_record = batch_spy.call_args.args[0][0]

        assert _strip_volatile(per_op_record) == _strip_volatile(batched_record)
        assert per_op_record["operation"] == "delete"
        assert per_op_record["key"] == backend._get_full_key("dlq:entry:x")
        # No ``value`` body on the delete record -- the replay handler only
        # needs the key.
        assert "value" not in per_op_record


# =============================================================================
# TestByStatusNormalScan — dlq:entry:* glob, WRONGTYPE-free (D6/G4)
# =============================================================================


class TestByStatusIndexServed:
    """541 D6: every status except PENDING is index-served, so by_status for
    ``replaying`` reads the dlq:status:replaying index ZSET (O(limit)) instead
    of globbing the dlq:entry:* namespace. The index holds only entry ids, so
    special-family keys are excluded by construction (no WRONGTYPE risk)."""

    def _entry_blob(self, repo: RedisDLQRepository, entry_id: str, domain: str):
        data = {
            "id": entry_id,
            "domain": domain,
            "failure_type": "PG_TIMEOUT",
            "status": FailedOperationStatus.REPLAYING.value,
            "created_at": "2026-05-27T00:00:00+00:00",
            "updated_at": "2026-05-27T00:00:00+00:00",
        }
        return repo._encode_entry(data)

    def _seed_replaying(self, raw, backend, repo, entry_id, domain="d"):
        """Stage an entry blob AND its replaying-index membership."""
        raw.set(
            backend._get_full_key(repo._make_key(entry_id)),
            self._entry_blob(repo, entry_id, domain),
        )
        raw.zadd(
            backend._get_full_key(
                repo._status_key(FailedOperationStatus.REPLAYING.value)
            ),
            {entry_id: 1.0},
        )

    @pytest.mark.parametrize(
        "ids",
        [
            # single pod
            ["pod-a:1:n:0", "pod-a:1:n:1"],
            # multi-pod
            ["pod-a:1:n:0", "pod-b:1:n:0"],
        ],
    )
    def test_by_status_returns_composite_entries_via_index(self, backend_factory, ids):
        raw = _FakeRaw()
        backend = backend_factory(redis_raw=raw)
        repo = RedisDLQRepository(backend, pod_id="pod-a", pid=1, run_nonce="n")
        repo._compression_enabled = MagicMock(return_value=False)

        for entry_id in ids:
            self._seed_replaying(raw, backend, repo, entry_id)
        # A HASH-typed compressed key alongside — the index path never reads it.
        compressed_key = backend._get_full_key("dlq:compressed:abc")
        raw.hset(compressed_key, {"orig_count": "5"})

        results = repo.get_by_status(FailedOperationStatus.REPLAYING.value)

        assert sorted(r.id for r in results) == sorted(ids)
        assert all(r.status == FailedOperationStatus.REPLAYING.value for r in results)
        # The compressed HASH key was never read (index holds only entry ids).
        assert compressed_key not in raw.get_calls

    def test_by_status_excludes_special_family_keys(self, backend_factory):
        raw = _FakeRaw()
        backend = backend_factory(redis_raw=raw)
        repo = RedisDLQRepository(backend, pod_id="pod-a", pid=1, run_nonce="n")
        repo._compression_enabled = MagicMock(return_value=False)

        # One indexed entry + several special-family STRING keys that are NOT
        # in the replaying index and so can never be returned as entries.
        self._seed_replaying(raw, backend, repo, "pod-a:1:n:0")
        raw.set(backend._get_full_key("dlq:pending"), b"not-an-entry")
        raw.set(backend._get_full_key("dlq:status:resolved"), b"not-an-entry")
        raw.set(backend._get_full_key("dlq:id_seq"), b"7")

        results = repo.get_by_status(FailedOperationStatus.REPLAYING.value)

        assert [r.id for r in results] == ["pod-a:1:n:0"]


# =============================================================================
# TestPostRecoveryNoReuse — distinct composite keys survive recovery (D2)
# =============================================================================


class TestPostRecoveryNoReuse:
    """Two simulated worker processes (distinct identities) create entries
    during a Redis outage into one shared WAL; on startup-glob recovery every
    distinct composite key is restored with no last-write-wins collapse — the
    failure mode the old per-process int ID_SEQ caused."""

    def test_distinct_composite_keys_survive_startup_recovery(self, backend_factory):
        backend = backend_factory()  # DEGRADED, shared tmpdir WAL

        # Two processes that share pod_id AND pid (restart with pid reuse) but
        # differ only by run_nonce — the bare {pod}:{pid}:{seq} scheme would
        # have both allocate "...:0"/"...:1" and collapse on recovery.
        repo_a = RedisDLQRepository(backend, pod_id="pod-1", pid=1, run_nonce="runA")
        repo_b = RedisDLQRepository(backend, pod_id="pod-1", pid=1, run_nonce="runB")

        entries_a = [
            repo_a.create(domain="payment", failure_type="A0"),
            repo_a.create(domain="payment", failure_type="A1"),
        ]
        entries_b = [
            repo_b.create(domain="point", failure_type="B0"),
            repo_b.create(domain="point", failure_type="B1"),
        ]
        backend.flush_wal()

        # Simulate crash + restart: memory lost, process reconnects to Redis.
        raw = _FakeRaw()
        backend._memory.clear()
        backend._redis = _FakeAdapter(raw)
        backend._redis_initialized = True
        backend._mode = ResilientStorageMode.REDIS

        backend._recover_from_wal_on_startup()

        # All 4 distinct composite keys restored — none overwritten.
        entry_keys = [k for k in raw.kv if "dlq:entry:" in k]
        assert len(entry_keys) == 4

        # Each entry is individually retrievable with its own domain/type —
        # proves no two collapsed onto a shared key (last-write-wins).
        for entry in entries_a:
            recovered = repo_a.get_by_id(entry.id)
            assert recovered is not None
            assert recovered.domain == "payment"
            assert recovered.failure_type == entry.failure_type
        for entry in entries_b:
            recovered = repo_b.get_by_id(entry.id)
            assert recovered is not None
            assert recovered.domain == "point"
            assert recovered.failure_type == entry.failure_type

        # Pending index carries all 4 distinct members.
        assert backend.zcard(repo_a.PENDING_KEY) == 4

    def test_same_pid_distinct_nonce_allocate_disjoint_keys(self, backend_factory):
        backend = backend_factory()
        repo_a = RedisDLQRepository(backend, pod_id="pod-1", pid=1, run_nonce="runA")
        repo_b = RedisDLQRepository(backend, pod_id="pod-1", pid=1, run_nonce="runB")

        entry_a = repo_a.create(domain="d", failure_type="t")
        entry_b = repo_b.create(domain="d", failure_type="t")

        # Same seq (0) on both, but the entry keys differ by run_nonce.
        assert entry_a.id != entry_b.id
        assert repo_a._make_key(entry_a.id) != repo_b._make_key(entry_b.id)
