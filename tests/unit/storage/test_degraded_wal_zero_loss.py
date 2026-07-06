"""Degraded-mode WAL zero-loss coverage for ResilientStorageBackend (#470 / 535).

Covers the write + replay extension that closes the degraded-mode DLQ
zero-loss gap:
    - D1: ``_replay_*`` dispatch handlers (behavior-preserving set/hset/
      delete/hdel + new zadd/zrem/set_blob), exact raw-client call + replay
      idempotency
    - D2/D3/D4: WAL-First ordering in the zadd/zrem/incr degraded branches
      (WAL written before memory); incr records an absolute "set" op
    - D5 set_blob WAL-First base64 record (mode-branch dispatch lives in
      test_dlq_502_blob_codec.py::TestStoreLoadBlobBehavior)
    - D6: ``_sync_memory_to_redis`` skips list/bytes values (and preserves the
      cb-dict drift-reconciliation branch)
    - DLQ create-path crash-recovery (startup) + runtime-recovery round-trips
      against an in-process fake-redis
    - D9: write-vocabulary ⊆ replay-vocabulary regression guard

Test classes:
    TestReplayHandlerBehavior      — _replay_* exact call + idempotency
    TestDegradedWalFirstBehavior   — WAL-before-memory ordering per op
    TestSyncMemorySkipBehavior     — D6 list/bytes skip, cb-dict preserved
    TestDegradedRecoveryRoundTrip  — startup + runtime recovery round-trips
    TestReplayVocabularyGuard      — D9 AST write-vocab subset guard (Contract)
"""

from __future__ import annotations

import ast
import base64
import inspect
import tempfile
import threading
import time
from collections import OrderedDict
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.adapters.resilient.backend import (
    ResilientStorageBackend,
    ResilientStorageMode,
)
from baldur.settings.resilient_storage import ResilientStorageSettings
from tests.factories import FakeRawRedis, FakeRedisAdapter

# =============================================================================
# Fakes / helpers
# =============================================================================


def _replay_backend(raw: object) -> ResilientStorageBackend:
    """Bare backend wired only for the replay handlers (REDIS mode)."""
    backend = ResilientStorageBackend.__new__(ResilientStorageBackend)
    backend._mode = ResilientStorageMode.REDIS
    backend._redis_initialized = True
    adapter = MagicMock()
    adapter._redis = raw
    adapter.raw_client = raw
    adapter._serialize = MagicMock(side_effect=lambda value: value)
    backend._redis = adapter
    return backend


def _wal_first_backend() -> ResilientStorageBackend:
    """Bare degraded backend with a MagicMock WAL for write-ordering tests."""
    backend = ResilientStorageBackend.__new__(ResilientStorageBackend)
    backend._mode = ResilientStorageMode.DEGRADED
    backend._redis = None
    backend._redis_initialized = False
    backend._memory = {}
    # Bounded blob store + accumulator (#539 D2): set_blob routes through
    # _mem_apply_set_blob, which maintains these and reads the byte cap.
    backend._blob_memory = OrderedDict()
    backend._blob_memory_bytes = 0
    backend._degraded_blob_memory_full_logged = False
    backend.config = ResilientStorageSettings(allow_memory_only=True)
    backend._wal = MagicMock()
    backend._wal_initialized = True
    backend._lock = threading.RLock()
    backend._get_full_key = MagicMock(side_effect=lambda key: f"baldur:{key}")
    backend._ensure_redis = MagicMock(return_value=False)
    return backend


def _sync_backend() -> ResilientStorageBackend:
    """Bare backend wired for ``_sync_memory_to_redis`` (RECOVERING mode)."""
    backend = ResilientStorageBackend.__new__(ResilientStorageBackend)
    backend._mode = ResilientStorageMode.RECOVERING
    backend._redis_initialized = True
    backend._memory = {}
    backend._redis = MagicMock()
    backend._redis.raw_client = backend._redis._redis
    # _sync_memory_to_redis snapshots _memory under _lock (#539 D1).
    backend._lock = threading.RLock()
    backend._get_full_key = MagicMock(side_effect=lambda key: f"baldur:{key}")
    return backend


def _scan_wal_write_operations() -> set[str]:
    """AST-scan backend.py for string-literal ``"operation"`` values in every
    WAL-record dict literal — the authoritative write vocabulary (D9 guard).

    538 D3 moved set_blob/zadd record-building into ``_wal_record_*`` helpers
    (fed to ``self._wal.write(...)`` and the batched ``batch_write_entries``
    path), so the record dict is no longer always a literal arg to
    ``self._wal.write``. backend.py uses an ``"operation"`` dict key only for
    WAL records, so scanning every dict literal carrying a string ``"operation"``
    captures the full write vocabulary regardless of inline-vs-helper shape.
    """
    import baldur.adapters.resilient.backend as backend_mod

    tree = ast.parse(inspect.getsource(backend_mod))
    operations: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key_node, value_node in zip(node.keys, node.values, strict=False):
            if (
                isinstance(key_node, ast.Constant)
                and key_node.value == "operation"
                and isinstance(value_node, ast.Constant)
                and isinstance(value_node.value, str)
            ):
                operations.add(value_node.value)

    return operations


@pytest.fixture
def temp_wal_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def degraded_backend(temp_wal_dir):
    """Real backend in DEGRADED mode with a real temp-dir WAL.

    ``_next_redis_probe`` is pushed far into the future so that hot-path
    ``_ensure_redis()`` calls never attempt a real connection (a local dev
    Redis would otherwise flip the backend to REDIS mode and skip the WAL).
    """
    from baldur.settings.resilient_storage import ResilientStorageSettings

    config = ResilientStorageSettings(wal_dir=temp_wal_dir, allow_memory_only=True)
    backend = ResilientStorageBackend(config)
    backend._next_redis_probe = time.monotonic() + 9999.0
    yield backend
    backend.close()


# =============================================================================
# TestReplayHandlerBehavior — _replay_* exact call + idempotency (D1)
# =============================================================================


class TestReplayHandlerBehavior:
    """Each dispatch handler issues the exact raw-client call; the new
    sorted-set / blob handlers are idempotent under repeated replay."""

    # --- behavior-preserving handlers (extracted by D1) ---

    def test_replay_set_serializes_value_then_raw_sets(self):
        raw = MagicMock()
        backend = _replay_backend(raw)

        backend._replay_set({"key": "baldur:dlq:id_seq", "value": 7})

        backend._redis._serialize.assert_called_once_with(7)
        raw.set.assert_called_once_with("baldur:dlq:id_seq", 7)

    def test_replay_hset_stringifies_mapping(self):
        raw = MagicMock()
        backend = _replay_backend(raw)

        backend._replay_hset(
            {"key": "baldur:cb:svc", "value": {"state": "open", "count": 5}}
        )

        raw.hset.assert_called_once_with(
            "baldur:cb:svc", mapping={"state": "open", "count": "5"}
        )

    def test_replay_delete_issues_raw_delete(self):
        raw = MagicMock()
        backend = _replay_backend(raw)

        backend._replay_delete({"key": "baldur:k"})

        raw.delete.assert_called_once_with("baldur:k")

    def test_replay_hdel_with_field_issues_raw_hdel(self):
        raw = MagicMock()
        backend = _replay_backend(raw)

        backend._replay_hdel({"key": "baldur:h", "field": "f1"})

        raw.hdel.assert_called_once_with("baldur:h", "f1")

    def test_replay_hdel_without_field_is_noop(self):
        raw = MagicMock()
        backend = _replay_backend(raw)

        backend._replay_hdel({"key": "baldur:h"})

        raw.hdel.assert_not_called()

    # --- new handlers (zadd / zrem / set_blob) ---

    def test_replay_zadd_issues_raw_zadd_with_mapping(self):
        raw = MagicMock()
        backend = _replay_backend(raw)

        backend._replay_zadd({"key": "baldur:dlq:pending", "value": {"1": 100.0}})

        raw.zadd.assert_called_once_with("baldur:dlq:pending", {"1": 100.0})

    def test_replay_zrem_issues_raw_zrem_with_members(self):
        raw = MagicMock()
        backend = _replay_backend(raw)

        backend._replay_zrem({"key": "baldur:dlq:pending", "members": ["1", "2"]})

        raw.zrem.assert_called_once_with("baldur:dlq:pending", "1", "2")

    def test_replay_zrem_empty_members_is_noop(self):
        raw = MagicMock()
        backend = _replay_backend(raw)

        backend._replay_zrem({"key": "baldur:dlq:pending", "members": []})

        raw.zrem.assert_not_called()

    def test_replay_set_blob_base64_decodes_then_raw_sets(self):
        raw = MagicMock()
        backend = _replay_backend(raw)
        original = b"\x78\x9c-compressed-blob"

        backend._replay_set_blob(
            {
                "key": "baldur:dlq:1",
                "value": base64.b64encode(original).decode("ascii"),
            }
        )

        raw.set.assert_called_once_with("baldur:dlq:1", original)

    # --- idempotency (replay twice -> identical final state) ---

    def test_replay_set_idempotent_on_repeat(self):
        raw = FakeRawRedis()
        backend = _replay_backend(raw)
        data = {"key": "baldur:dlq:id_seq", "value": 5}

        backend._replay_set(data)
        backend._replay_set(data)

        assert raw.kv["baldur:dlq:id_seq"] == 5

    def test_replay_zadd_idempotent_on_repeat(self):
        raw = FakeRawRedis()
        backend = _replay_backend(raw)
        data = {"key": "baldur:dlq:pending", "value": {"1": 100.0, "2": 200.0}}

        backend._replay_zadd(data)
        after_first = dict(raw.zsets["baldur:dlq:pending"])
        backend._replay_zadd(data)
        after_second = dict(raw.zsets["baldur:dlq:pending"])

        assert after_first == after_second == {"1": 100.0, "2": 200.0}

    def test_replay_zrem_idempotent_on_repeat(self):
        raw = FakeRawRedis()
        raw.zadd("baldur:dlq:pending", {"1": 100.0, "2": 200.0})
        backend = _replay_backend(raw)
        data = {"key": "baldur:dlq:pending", "members": ["1"]}

        backend._replay_zrem(data)
        after_first = dict(raw.zsets["baldur:dlq:pending"])
        backend._replay_zrem(data)  # "1" already gone — no-op
        after_second = dict(raw.zsets["baldur:dlq:pending"])

        assert after_first == after_second == {"2": 200.0}

    def test_replay_set_blob_idempotent_on_repeat(self):
        raw = FakeRawRedis()
        backend = _replay_backend(raw)
        original = b"\x78compressed-blob"
        data = {
            "key": "baldur:dlq:1",
            "value": base64.b64encode(original).decode("ascii"),
        }

        backend._replay_set_blob(data)
        backend._replay_set_blob(data)

        assert raw.kv["baldur:dlq:1"] == original


# =============================================================================
# TestDegradedWalFirstBehavior — WAL written before memory (D2/D3/D4/D5)
# =============================================================================


class TestDegradedWalFirstBehavior:
    """Degraded zadd/zrem/incr/set_blob write the WAL record BEFORE mutating
    memory so a crash between the two steps is recoverable."""

    def test_zadd_degraded_wal_first_then_memory(self):
        backend = _wal_first_backend()
        observed = {}

        def record(rec):
            observed["key_in_memory_at_write"] = "pending" in backend._memory
            observed["record"] = rec

        backend._wal.write.side_effect = record

        backend.zadd("pending", {"1": 100.0})

        # WAL-First: memory untouched at the moment WAL.write fired.
        assert observed["key_in_memory_at_write"] is False
        # Memory mutated afterward.
        assert backend._memory["pending"] == [{"member": "1", "score": 100.0}]
        assert observed["record"]["operation"] == "zadd"
        assert observed["record"]["key"] == "baldur:pending"
        assert observed["record"]["value"] == {"1": 100.0}

    def test_zrem_degraded_wal_first_then_memory(self):
        backend = _wal_first_backend()
        backend._memory["pending"] = [
            {"member": "1", "score": 100.0},
            {"member": "2", "score": 200.0},
        ]
        observed = {}

        def record(rec):
            observed["members_at_write"] = [
                item["member"] for item in backend._memory["pending"]
            ]
            observed["record"] = rec

        backend._wal.write.side_effect = record

        backend.zrem("pending", "1")

        # At WAL-write time member "1" is still present (memory not yet mutated).
        assert "1" in observed["members_at_write"]
        assert observed["record"]["operation"] == "zrem"
        assert observed["record"]["members"] == ["1"]
        # Memory mutated after the WAL write.
        assert [item["member"] for item in backend._memory["pending"]] == ["2"]

    def test_incr_degraded_wal_first_set_op(self):
        backend = _wal_first_backend()
        observed = {}

        def record(rec):
            observed["value_at_write"] = backend._memory.get("counter")
            observed["record"] = rec

        backend._wal.write.side_effect = record

        result = backend.incr("counter")

        assert result == 1
        # WAL-First: memory still holds the pre-increment value (absent -> None).
        assert observed["value_at_write"] is None
        assert backend._memory["counter"] == 1
        # D4: incr reuses the "set" op carrying the absolute counter value.
        assert observed["record"]["operation"] == "set"
        assert observed["record"]["key"] == "baldur:counter"
        assert observed["record"]["value"] == 1

    def test_incr_degraded_records_carry_absolute_values(self):
        backend = _wal_first_backend()

        for _ in range(3):
            backend.incr("counter")

        records = [call.args[0] for call in backend._wal.write.call_args_list]
        # Each record carries the absolute counter; the highest-sequence record
        # (last) equals the max counter — blind set-to-absolute replay then
        # reconstructs the final value (D4).
        assert [rec["value"] for rec in records] == [1, 2, 3]
        assert all(rec["operation"] == "set" for rec in records)

    def test_set_blob_degraded_wal_first_base64(self):
        backend = _wal_first_backend()
        observed = {}

        def record(rec):
            # #539 D2: blobs live in the bounded _blob_memory store, not
            # _memory. WAL-First means the key is absent at write time.
            observed["key_in_memory_at_write"] = "dlq:1" in backend._blob_memory
            observed["record"] = rec

        backend._wal.write.side_effect = record

        backend.set_blob("dlq:1", b"\x78raw-bytes")

        assert observed["key_in_memory_at_write"] is False
        # Blob store holds the raw bytes verbatim.
        assert backend._blob_memory["dlq:1"] == b"\x78raw-bytes"
        # D5: set_blob op with a base64-wrapped value (op name is the marker).
        assert observed["record"]["operation"] == "set_blob"
        assert observed["record"]["key"] == "baldur:dlq:1"
        assert base64.b64decode(observed["record"]["value"]) == b"\x78raw-bytes"


# =============================================================================
# TestSyncMemorySkipBehavior — D6 list/bytes skip, cb-dict preserved
# =============================================================================


class TestSyncMemorySkipBehavior:
    """``_sync_memory_to_redis`` must skip list (zadd / lpush shape) and bytes
    (set_blob) values — WAL replay already reconstructed them; re-syncing
    would clobber the ZSET/blob with a wrong-typed STRING. Scalar and dict
    values are still synced."""

    def test_sync_memory_skips_list_and_bytes_values(self):
        backend = _sync_backend()
        backend._memory = {
            "scalar": "v",
            "zset_shape": [{"member": "1", "score": 1.0}],  # zadd memory shape
            "blob": b"\x78bytes",  # set_blob memory shape
            "history": ["a", "b"],  # lpush memory shape
        }

        with patch(
            "baldur.adapters.memory.drift_reconciliation.get_drift_reconciler",
            side_effect=Exception("no reconciler"),
        ):
            backend._sync_memory_to_redis()

        # Scalar synced via the high-level adapter set.
        backend._redis.set.assert_called_once_with("baldur:scalar", "v")

        raw = backend._redis._redis
        # No raw set/zadd for the list/bytes keys — they were skipped.
        synced_set_keys = [call.args[0] for call in raw.set.call_args_list]
        assert "baldur:blob" not in synced_set_keys
        assert "baldur:zset_shape" not in synced_set_keys
        assert "baldur:history" not in synced_set_keys
        raw.zadd.assert_not_called()

    def test_sync_memory_preserves_cb_dict_drift_reconciliation(self):
        backend = _sync_backend()
        backend._memory = {"cb:svc": {"state": "open", "failure_count": 3}}
        reconciler = MagicMock()
        reconciler.reconcile.return_value = ("open", "memory")
        backend._redis._redis.hgetall.return_value = {b"state": b"closed"}

        with patch(
            "baldur.adapters.memory.drift_reconciliation.get_drift_reconciler",
            return_value=reconciler,
        ):
            backend._sync_memory_to_redis()

        # Dict value is NOT skipped: drift reconciled + hset to Redis.
        reconciler.reconcile.assert_called_once()
        backend._redis._redis.hset.assert_called_once()
        assert backend._redis._redis.hset.call_args.args[0] == "baldur:cb:svc"


# =============================================================================
# TestDegradedRecoveryRoundTrip — startup + runtime recovery (D2/D5/D6)
# =============================================================================


class TestDegradedRecoveryRoundTrip:
    """A DLQ entry created during a Redis outage is fully recoverable on both
    recovery paths — payload blob present AND pending index present — using a
    real temp-dir WAL + an in-process fake-redis."""

    def test_startup_recovery_restores_dlq_entry_from_wal(self, degraded_backend):
        backend = degraded_backend
        repo = RedisDLQRepository(backend)

        # Create a DLQ entry while degraded: incr + set_blob + zadd x2 -> WAL.
        entry = repo.create(
            domain="payment",
            failure_type="PG_TIMEOUT",
            error_message="boom",
            request_data={"order_id": 1},
        )
        backend.flush_wal()

        # Simulate crash + restart: memory lost, process reconnects to Redis.
        raw = FakeRawRedis()
        backend._memory.clear()
        backend._redis = FakeRedisAdapter(raw)
        backend._redis_initialized = True
        backend._mode = ResilientStorageMode.REDIS

        backend._recover_from_wal_on_startup()

        # Payload + index recovered purely from WAL (memory was cleared).
        recovered = repo.get_by_id(entry.id)
        assert recovered is not None
        assert recovered.domain == "payment"
        assert recovered.failure_type == "PG_TIMEOUT"
        assert recovered.request_data == {"order_id": 1}
        assert backend.zcard(repo.PENDING_KEY) > 0

    def test_runtime_recovery_restores_dlq_entry_without_clobber(
        self, degraded_backend
    ):
        backend = degraded_backend
        repo = RedisDLQRepository(backend)

        entry = repo.create(
            domain="point",
            failure_type="SHORTAGE",
            request_data={"user_id": 42},
        )
        backend.flush_wal()

        # Runtime recovery: Redis returns while the worker stays alive.
        raw = FakeRawRedis()
        backend._redis = FakeRedisAdapter(raw)
        backend._redis_initialized = True

        assert backend._do_recovery() is True
        assert backend.mode == ResilientStorageMode.REDIS

        # D6: the post-replay _sync_memory_to_redis did NOT clobber the blob
        # (bytes) or the pending ZSET (list) that replay reconstructed.
        recovered = repo.get_by_id(entry.id)
        assert recovered is not None
        assert recovered.domain == "point"
        assert recovered.request_data == {"user_id": 42}
        assert backend.zcard(repo.PENDING_KEY) > 0


# =============================================================================
# TestReplayVocabularyGuard — D9 write-vocab subset of replay-vocab (Contract)
# =============================================================================


class TestReplayVocabularyGuard:
    """D9 regression guard: every WAL operation backend.py *writes* must have
    a replay handler, else a degraded write silently drops on recovery."""

    def test_scan_finds_expected_write_vocabulary(self):
        # Contract: the degraded write vocabulary stated in D1/D9.
        assert {
            "set",
            "hset",
            "delete",
            "hdel",
            "zadd",
            "zrem",
            "set_blob",
        } <= _scan_wal_write_operations()

    def test_write_vocabulary_is_subset_of_replay_vocabulary(self):
        write_ops = _scan_wal_write_operations()
        replay_ops = set(ResilientStorageBackend._REPLAY_DISPATCH)
        assert write_ops <= replay_ops

    @pytest.mark.parametrize("operation", sorted(_scan_wal_write_operations()))
    def test_each_written_operation_has_replay_handler(self, operation):
        # "incr" is absent from the write vocab because it reuses the "set" op.
        assert operation in ResilientStorageBackend._REPLAY_DISPATCH
