"""RedisDLQRepository blob encoding + atomic acquire unit tests (#502).

Covers the post-#502 read/write path on the Redis DLQ adapter:
    - _encode_entry / _decode_entry (orjson + optional zlib)
    - create() round-trip with the STRING-encoded blob
    - _update() GET → mutate → SET partial-field merge
    - _store_blob / _load_blob thin delegation to backend.set_blob/get_blob
    - backend.set_blob / get_blob REDIS vs DEGRADED mode dispatch (#470 D5)
    - _try_acquire_atomic WATCH/MULTI/EXEC state machine

Test classes:
    TestEntryBlobCodecBehavior        — encode/decode round-trip + magic byte
    TestRedisDLQCreateBlobBehavior    — create() round-trip parity
    TestRedisDLQUpdateBlobBehavior    — _update() partial-field merge + status index
    TestStoreLoadBlobBehavior         — _store_blob/_load_blob delegation + backend mode dispatch
    TestTryAcquireAtomicBehavior      — WATCH/MULTI/EXEC CAS scenarios
"""

from __future__ import annotations

import base64
import itertools
import threading
from collections import OrderedDict
from unittest.mock import MagicMock, patch

from baldur.adapters.redis.dlq import _ZLIB_MAGIC_BYTE, RedisDLQRepository
from baldur.adapters.resilient.backend import ResilientStorageMode
from baldur.interfaces.repositories import FailedOperationStatus
from baldur.settings.resilient_storage import ResilientStorageSettings
from baldur.utils.serialization import fast_loads

# =============================================================================
# Helpers
# =============================================================================


def _make_repo(compression_enabled: bool = True) -> RedisDLQRepository:
    """Build a RedisDLQRepository without running the real __init__.

    Pattern mirrors test_dlq_sub_modules._make_repo. The backend is a
    MagicMock; sub-modules are not wired because the tests here exercise
    the parent class directly.
    """
    backend = MagicMock()
    with patch.object(RedisDLQRepository, "__init__", lambda self, **kw: None):
        repo = RedisDLQRepository.__new__(RedisDLQRepository)
    repo._backend = backend
    repo._key_prefix = "dlq:"
    repo._pending_key = "dlq:pending"
    repo._entry_prefix = "dlq:entry:"
    repo._by_domain_prefix = "dlq:by_domain:"
    repo._status_prefix = "dlq:status:"
    repo._status_domain_prefix = "dlq:status_domain:"
    repo._all_key = "dlq:all"
    repo._domains_key = "dlq:domains"
    repo._known_domains = set()
    # 538 D2: composite-ID identity seam (fixed for deterministic test ids).
    repo._pod_id = "testpod"
    repo._pid = 1
    repo._run_nonce = "testnonce"
    repo._seq_counter = itertools.count()
    # Patch the compression toggle to bypass settings lookup during tests.
    repo._compression_enabled = MagicMock(return_value=compression_enabled)
    return repo


def _sample_entry(**overrides):
    """Minimal entry dict matching the shape created by create()."""
    base = {
        "id": 42,
        "domain": "payment",
        "failure_type": "PG_TIMEOUT",
        "error_message": "boom",
        "error_code": "",
        "status": FailedOperationStatus.PENDING.value,
        "entity_type": "",
        "entity_id": "",
        "entity_refs": {},
        "user_id": None,
        "snapshot_data": {},
        "request_data": {"order_id": 1, "amount": 1000},
        "response_data": {},
        "metadata": {},
        "retry_count": 0,
        "max_retries": 3,
        "next_action_hint": "",
        "recommended_action": "",
        "created_at": "2026-05-14T12:00:00+00:00",
        "updated_at": "2026-05-14T12:00:00+00:00",
        "expires_at": None,
    }
    base.update(overrides)
    return base


# =============================================================================
# TestEntryBlobCodec — _encode_entry / _decode_entry (#502 D5 + D6)
# =============================================================================


class TestEntryBlobCodecBehavior:
    """_encode_entry / _decode_entry round-trip + magic-byte branching."""

    def test_compressed_round_trip_preserves_full_dict(self):
        repo = _make_repo(compression_enabled=True)
        data = _sample_entry(error_message="x" * 200)

        blob = repo._encode_entry(data)
        decoded = repo._decode_entry(blob)

        # All non-default keys survive the round trip.
        for key in ("id", "domain", "failure_type", "status", "request_data"):
            assert decoded[key] == data[key]
        assert decoded["error_message"] == "x" * 200

    def test_uncompressed_round_trip_preserves_full_dict(self):
        repo = _make_repo(compression_enabled=False)
        data = _sample_entry()

        blob = repo._encode_entry(data)

        # First byte is JSON `{` (0x7b), not zlib magic 0x78.
        assert blob[0] != _ZLIB_MAGIC_BYTE
        assert blob[0] == ord("{")

        decoded = repo._decode_entry(blob)
        assert decoded["id"] == 42
        assert decoded["domain"] == "payment"

    def test_compressed_blob_starts_with_zlib_magic(self):
        repo = _make_repo(compression_enabled=True)
        blob = repo._encode_entry(_sample_entry())
        assert blob[0] == _ZLIB_MAGIC_BYTE

    def test_default_valued_keys_are_dropped_on_encode(self):
        """D6 default-drop: empty/None fields shrink the encoded payload."""
        repo = _make_repo(compression_enabled=False)
        # snapshot_data={} (default), error_code='' (default) → dropped.
        data = _sample_entry()

        blob = repo._encode_entry(data)
        on_wire = fast_loads(blob)

        # Dropped keys are absent from the on-wire dict.
        assert "snapshot_data" not in on_wire
        assert "error_code" not in on_wire
        assert "entity_type" not in on_wire
        # Non-default keys survive.
        assert on_wire["domain"] == "payment"
        assert on_wire["request_data"] == {"order_id": 1, "amount": 1000}

    def test_decode_none_returns_empty_dict(self):
        repo = _make_repo()
        assert repo._decode_entry(None) == {}

    def test_decode_empty_bytes_returns_empty_dict(self):
        repo = _make_repo()
        assert repo._decode_entry(b"") == {}

    def test_decode_str_input_is_utf8_decoded(self):
        """Accepts str blobs (Redis decode_responses=True path)."""
        repo = _make_repo(compression_enabled=False)
        blob_bytes = repo._encode_entry(_sample_entry())
        decoded = repo._decode_entry(blob_bytes.decode("utf-8"))
        assert decoded["id"] == 42

    def test_decode_malformed_zlib_returns_empty_dict(self):
        """Magic byte but broken stream → log + empty dict (caller treats as absent)."""
        repo = _make_repo()
        malformed = bytes([_ZLIB_MAGIC_BYTE]) + b"not-real-zlib"
        assert repo._decode_entry(malformed) == {}

    def test_decode_malformed_json_returns_empty_dict(self):
        repo = _make_repo()
        # Non-zlib, non-JSON payload — starts with `{` but truncated.
        assert repo._decode_entry(b"{not json") == {}

    def test_decode_non_dict_payload_returns_empty_dict(self):
        repo = _make_repo()
        # Valid JSON array, but dlq entries are always dicts.
        assert repo._decode_entry(b"[1, 2, 3]") == {}


# =============================================================================
# TestRedisDLQCreateBlob — create() blob round-trip (#502 D5)
# =============================================================================


class TestRedisDLQCreateBlobBehavior:
    """RedisDLQRepository.create() writes a single STRING blob (no HSET)."""

    @staticmethod
    def _created_blob(repo):
        """Extract the set_blob op value from create()'s grouped op (538 D3)."""
        ops = repo._backend.batch_write_ops.call_args[0][0]
        assert ops[0][0] == "set_blob"
        return ops[0][2]

    def test_create_stores_blob_and_indexes(self):
        repo = _make_repo(compression_enabled=False)
        result = repo.create(
            domain="payment",
            failure_type="PG_TIMEOUT",
            error_message="boom",
            request_data={"order_id": 1},
        )

        # 538 D3: create() issues a single transactional grouped op.
        # 541 D6: the global dlq:all index is the 4th op.
        # 544 D1/D2/D3: ops 5 + 6 add the composite (status, domain) index
        # and the domain-registry ZSET.
        repo._backend.batch_write_ops.assert_called_once()
        ops = repo._backend.batch_write_ops.call_args[0][0]
        assert [op[0] for op in ops] == [
            "set_blob",
            "zadd",
            "zadd",
            "zadd",
            "zadd",
            "zadd",
        ]

        # set_blob op carries the entry blob under the dlq:entry: namespace.
        assert ops[0][1] == repo._make_key(result.id)
        decoded = repo._decode_entry(ops[0][2])
        assert decoded["domain"] == "payment"
        assert decoded["failure_type"] == "PG_TIMEOUT"
        assert decoded["request_data"] == {"order_id": 1}
        assert decoded["status"] == FailedOperationStatus.PENDING.value

        # Pending + by_domain + global + composite + domain registry zadds.
        zadd_keys = {op[1] for op in ops if op[0] == "zadd"}
        assert "dlq:pending" in zadd_keys
        assert "dlq:by_domain:payment" in zadd_keys
        assert "dlq:all" in zadd_keys
        assert "dlq:status_domain:pending:payment" in zadd_keys
        assert "dlq:domains" in zadd_keys

        # Returned FailedOperationData carries the composite-id string (538 D2).
        assert result.id == "testpod:1:testnonce:0"
        assert result.domain == "payment"

    def test_create_round_trip_through_load_blob(self):
        """End-to-end: create writes a blob that load+decode recovers verbatim."""
        repo = _make_repo(compression_enabled=True)

        repo.create(
            domain="point",
            failure_type="SHORTAGE",
            request_data={"user_id": 42},
            metadata={"trace_id": "abc"},
        )

        decoded = repo._decode_entry(self._created_blob(repo))
        assert decoded["domain"] == "point"
        assert decoded["request_data"] == {"user_id": 42}
        assert decoded["metadata"] == {"trace_id": "abc"}

    def test_create_with_large_request_data_round_trips(self):
        repo = _make_repo(compression_enabled=True)
        big_payload = {"items": [{"sku": f"x{i}"} for i in range(100)]}
        repo.create(domain="payment", failure_type="t", request_data=big_payload)
        decoded = repo._decode_entry(self._created_blob(repo))
        assert decoded["request_data"] == big_payload

    def test_create_with_special_characters_round_trips(self):
        repo = _make_repo(compression_enabled=False)
        message = 'boom 한글 emoji 🔥 quote " backslash \\'
        repo.create(domain="payment", failure_type="t", error_message=message)
        decoded = repo._decode_entry(self._created_blob(repo))
        assert decoded["error_message"] == message


# =============================================================================
# TestRedisDLQUpdateBlob — _update() GET → mutate → SET (#502 D5)
# =============================================================================


class TestRedisDLQUpdateBlobBehavior:
    """_update() partial-field merge + status index transitions."""

    def test_update_missing_entry_returns_false(self):
        repo = _make_repo()
        with patch.object(repo, "_load_blob", return_value=None):
            assert repo._update(entry_id=999, status="resolved") is False

    def test_update_merges_partial_fields_preserving_others(self):
        repo = _make_repo(compression_enabled=False)
        existing_blob = repo._encode_entry(
            _sample_entry(retry_count=0, metadata={"a": 1})
        )

        captured = {}

        def fake_store(eid, blob):
            captured[eid] = blob

        with (
            patch.object(repo, "_load_blob", return_value=existing_blob),
            patch.object(repo, "_store_blob", side_effect=fake_store),
        ):
            assert repo._update(entry_id=42, retry_count=5) is True

        decoded = repo._decode_entry(captured[42])
        # retry_count updated; other fields preserved.
        assert decoded["retry_count"] == 5
        assert decoded["domain"] == "payment"
        assert decoded["metadata"] == {"a": 1}

    def test_update_metadata_merges_into_existing_dict(self):
        repo = _make_repo(compression_enabled=False)
        existing_blob = repo._encode_entry(_sample_entry(metadata={"a": 1}))
        captured = {}
        with (
            patch.object(repo, "_load_blob", return_value=existing_blob),
            patch.object(
                repo, "_store_blob", side_effect=lambda e, b: captured.setdefault(e, b)
            ),
        ):
            repo._update(entry_id=42, metadata={"b": 2})
        decoded = repo._decode_entry(captured[42])
        assert decoded["metadata"] == {"a": 1, "b": 2}

    def test_update_status_transitions_pending_to_indexed(self):
        """PENDING → RESOLVED: 544 D6 collapses zrem old / zadd new /
        composite zrem / composite zadd / set_blob into a single
        batch_write_ops call (1 RTT)."""
        repo = _make_repo(compression_enabled=False)
        existing_blob = repo._encode_entry(
            _sample_entry(status=FailedOperationStatus.PENDING.value)
        )
        with patch.object(repo, "_load_blob", return_value=existing_blob):
            assert (
                repo._update(entry_id=42, status=FailedOperationStatus.RESOLVED.value)
                is True
            )

        repo._backend.batch_write_ops.assert_called_once()
        ops = repo._backend.batch_write_ops.call_args[0][0]
        zrem_keys = [op[1] for op in ops if op[0] == "zrem"]
        zadd_keys = [op[1] for op in ops if op[0] == "zadd"]
        assert "dlq:pending" in zrem_keys
        assert "dlq:status:resolved" in zadd_keys

    def test_update_status_pending_to_replaying_indexes_replaying(self):
        """541 D6: REPLAYING is in _STATUS_INDEXED, so _update routes the
        PENDING → REPLAYING transition through batch_write_ops with the
        composite (status, domain) pair updated alongside the per-status
        indexes (544 D6)."""
        repo = _make_repo(compression_enabled=False)
        existing_blob = repo._encode_entry(
            _sample_entry(status=FailedOperationStatus.PENDING.value)
        )
        with patch.object(repo, "_load_blob", return_value=existing_blob):
            repo._update(entry_id=42, status=FailedOperationStatus.REPLAYING.value)

        repo._backend.batch_write_ops.assert_called_once()
        ops = repo._backend.batch_write_ops.call_args[0][0]
        zrem_keys = [op[1] for op in ops if op[0] == "zrem"]
        zadd_keys = [op[1] for op in ops if op[0] == "zadd"]
        assert "dlq:pending" in zrem_keys
        assert "dlq:status:replaying" in zadd_keys


# =============================================================================
# TestStoreLoadBlob — delegation + backend.set_blob/get_blob dispatch (#470 D5)
# =============================================================================


def _backend_for_blob(mode: ResilientStorageMode, redis_client=None, wal=None):
    """Build a real ResilientStorageBackend instance (bypassing __init__)
    with only the attributes set_blob/get_blob touch — no real WAL/Redis.
    """
    from baldur.adapters.resilient.backend import ResilientStorageBackend

    backend = ResilientStorageBackend.__new__(ResilientStorageBackend)
    backend._mode = mode
    backend._redis = MagicMock() if redis_client else None
    if redis_client and backend._redis is not None:
        backend._redis.raw_client = redis_client
    backend._memory = {}
    # Bounded blob store + accumulator (#539 D2): degraded set_blob/get_blob
    # route through _blob_memory under _lock with the byte cap from config.
    backend._blob_memory = OrderedDict()
    backend._blob_memory_bytes = 0
    backend._degraded_blob_memory_full_logged = False
    backend.config = ResilientStorageSettings(allow_memory_only=True)
    backend._lock = threading.RLock()
    backend._wal = wal
    backend._wal_initialized = wal is not None
    backend._get_full_key = MagicMock(side_effect=lambda k: f"baldur:{k}")
    backend._ensure_redis = MagicMock(return_value=(mode == ResilientStorageMode.REDIS))

    def _switch():
        backend._mode = ResilientStorageMode.DEGRADED

    backend._switch_to_degraded = MagicMock(side_effect=_switch)
    return backend


class TestStoreLoadBlobBehavior:
    """_store_blob/_load_blob thin delegation + backend.set_blob/get_blob
    REDIS vs DEGRADED mode dispatch (#470 D5)."""

    # --- Thin delegation (dlq.py) ---

    def test_store_blob_delegates_to_backend_set_blob(self):
        repo = _make_repo()
        repo._backend = MagicMock()

        repo._store_blob("42", b"\x78raw-bytes")

        repo._backend.set_blob.assert_called_once_with("dlq:entry:42", b"\x78raw-bytes")

    def test_load_blob_delegates_to_backend_get_blob(self):
        repo = _make_repo()
        repo._backend = MagicMock()
        repo._backend.get_blob.return_value = b"\x78compressed"

        assert repo._load_blob("42") == b"\x78compressed"
        repo._backend.get_blob.assert_called_once_with("dlq:entry:42")

    # --- backend.set_blob mode dispatch ---

    def test_set_blob_redis_mode_writes_raw_bytes(self):
        redis_client = MagicMock()
        backend = _backend_for_blob(ResilientStorageMode.REDIS, redis_client)

        backend.set_blob("dlq:42", b"\x78raw-bytes")

        redis_client.set.assert_called_once_with("baldur:dlq:42", b"\x78raw-bytes")
        # Memory not touched in REDIS mode.
        assert backend._memory == {}

    def test_set_blob_degraded_mode_wal_first_then_memory(self):
        wal = MagicMock()
        backend = _backend_for_blob(ResilientStorageMode.DEGRADED, wal=wal)

        backend.set_blob("dlq:42", b"raw-bytes")

        # Blob store holds the raw bytes (#539 D2); _memory untouched.
        assert backend._blob_memory == {"dlq:42": b"raw-bytes"}
        assert backend._memory == {}
        # WAL-First: a base64-wrapped set_blob record was written.
        wal.write.assert_called_once()
        record = wal.write.call_args[0][0]
        assert record["operation"] == "set_blob"
        assert record["key"] == "baldur:dlq:42"
        assert base64.b64decode(record["value"]) == b"raw-bytes"

    def test_set_blob_switches_to_degraded_on_redis_exception(self):
        """Redis raises → switch_to_degraded() + memory fallback."""
        redis_client = MagicMock()
        redis_client.set.side_effect = RuntimeError("redis-down")
        backend = _backend_for_blob(ResilientStorageMode.REDIS, redis_client)

        backend.set_blob("dlq:42", b"raw-bytes")

        backend._switch_to_degraded.assert_called_once()
        assert backend._blob_memory == {"dlq:42": b"raw-bytes"}

    # --- backend.get_blob mode dispatch ---

    def test_get_blob_redis_mode_returns_raw_bytes(self):
        redis_client = MagicMock()
        redis_client.get.return_value = b"\x78compressed"
        backend = _backend_for_blob(ResilientStorageMode.REDIS, redis_client)

        assert backend.get_blob("dlq:42") == b"\x78compressed"
        redis_client.get.assert_called_once_with("baldur:dlq:42")

    def test_get_blob_degraded_mode_reads_memory(self):
        backend = _backend_for_blob(ResilientStorageMode.DEGRADED)
        # #539 D2: degraded blobs live in the bounded blob store.
        backend._blob_memory["dlq:42"] = b"degraded-bytes"

        assert backend.get_blob("dlq:42") == b"degraded-bytes"

    def test_get_blob_falls_back_to_memory_on_redis_exception(self):
        redis_client = MagicMock()
        redis_client.get.side_effect = RuntimeError("redis-down")
        backend = _backend_for_blob(ResilientStorageMode.REDIS, redis_client)
        backend._blob_memory["dlq:42"] = b"fallback"

        assert backend.get_blob("dlq:42") == b"fallback"
        backend._switch_to_degraded.assert_called_once()


# =============================================================================
# TestTryAcquireAtomic — WATCH/MULTI/EXEC state machine (#502 D5)
# =============================================================================


def _encode_entry_for_test(data: dict, *, compression: bool = False) -> bytes:
    """Encode an entry using the repository's real codec (compression off by default)."""
    repo = _make_repo(compression_enabled=compression)
    return repo._encode_entry(data)


def _decode_entry_for_test(blob: bytes) -> dict:
    repo = _make_repo()
    return repo._decode_entry(blob)


def _make_atomic_lifecycle(
    *,
    blob_after_watch: bytes | None,
    exec_result=("zrem-ok", "set-ok"),
):
    """Wire a RedisDLQLifecycle with a duck-typed mock repo + mock pipeline.

    The repo is a MagicMock (not the real class) because RedisDLQRepository
    exposes PENDING_KEY and _raw_redis_client as read-only properties; the
    lifecycle only consumes them as plain attributes so duck-typing is safe.
    """
    from baldur.adapters.redis.dlq import RedisDLQRepository
    from baldur.adapters.redis.dlq_lifecycle import RedisDLQLifecycle

    repo = MagicMock()
    repo._key_prefix = "dlq:"
    repo._pending_key = "dlq:pending"
    repo.PENDING_KEY = "dlq:pending"
    repo._make_key = MagicMock(side_effect=lambda eid: f"dlq:{eid}")
    repo._backend._get_full_key = MagicMock(side_effect=lambda k: f"ns:{k}")
    repo._ensure_redis_available = MagicMock(return_value=True)
    # Use the real codec for encode/decode so tests exercise the actual
    # blob format. _compression_enabled is short-circuited to False so
    # round-trip is plain orjson.
    repo._compression_enabled = MagicMock(return_value=False)
    repo._encode_entry = RedisDLQRepository._encode_entry.__get__(repo)
    repo._decode_entry = RedisDLQRepository._decode_entry.__get__(repo)
    repo._to_data = MagicMock(side_effect=lambda d: d)  # short-circuit DTO conversion

    pipe = MagicMock()
    pipe.get.return_value = blob_after_watch
    pipe.execute.return_value = exec_result

    pipeline_ctx = MagicMock()
    pipeline_ctx.__enter__.return_value = pipe
    pipeline_ctx.__exit__.return_value = False

    raw_client = MagicMock()
    raw_client.pipeline.return_value = pipeline_ctx
    repo._raw_redis_client = raw_client

    lifecycle = RedisDLQLifecycle(repo)
    return lifecycle, repo, pipe


class TestTryAcquireAtomicBehavior:
    """_try_acquire_atomic WATCH/MULTI/EXEC scenarios per D5."""

    def test_acquire_success_transitions_to_replaying_and_increments_retry(self):
        entry = _sample_entry(
            id=10,
            status=FailedOperationStatus.PENDING.value,
            retry_count=0,
            max_retries=3,
        )
        blob = _encode_entry_for_test(entry)
        lifecycle, _, pipe = _make_atomic_lifecycle(blob_after_watch=blob)

        result = lifecycle._try_acquire_atomic(10, max_retries=3, domain_out=[])

        assert pipe.multi.called
        assert pipe.set.called
        assert pipe.execute.called
        assert result is not None
        assert result["status"] == FailedOperationStatus.REPLAYING.value
        assert result["retry_count"] == 1

    def test_acquire_returns_none_when_entry_not_found(self):
        lifecycle, _, pipe = _make_atomic_lifecycle(blob_after_watch=None)

        assert lifecycle._try_acquire_atomic(99, max_retries=3, domain_out=[]) is None
        pipe.unwatch.assert_called_once()
        pipe.multi.assert_not_called()

    def test_acquire_returns_none_when_status_mismatch(self):
        blob = _encode_entry_for_test(
            _sample_entry(status=FailedOperationStatus.REPLAYING.value)
        )
        lifecycle, _, pipe = _make_atomic_lifecycle(blob_after_watch=blob)

        assert lifecycle._try_acquire_atomic(10, max_retries=3, domain_out=[]) is None
        pipe.unwatch.assert_called_once()
        pipe.multi.assert_not_called()

    def test_acquire_returns_none_when_max_retries_exceeded(self):
        blob = _encode_entry_for_test(_sample_entry(retry_count=3, max_retries=3))
        lifecycle, _, pipe = _make_atomic_lifecycle(blob_after_watch=blob)

        assert lifecycle._try_acquire_atomic(10, max_retries=3, domain_out=[]) is None
        pipe.unwatch.assert_called_once()
        pipe.multi.assert_not_called()

    def test_acquire_retries_on_watch_conflict_then_succeeds(self):
        """EXEC returning None triggers WATCH-loop retry; second pass succeeds."""
        blob = _encode_entry_for_test(_sample_entry())
        lifecycle, _, pipe = _make_atomic_lifecycle(blob_after_watch=blob)
        pipe.execute.side_effect = [None, ("zrem-ok", "set-ok")]

        result = lifecycle._try_acquire_atomic(10, max_retries=3, domain_out=[])

        assert result is not None
        assert pipe.execute.call_count == 2

    def test_acquire_falls_back_to_python_when_redis_unavailable(self):
        """No raw client → degraded Python read-modify-write path."""
        from baldur.adapters.redis.dlq_lifecycle import RedisDLQLifecycle

        repo = MagicMock()
        repo._ensure_redis_available = MagicMock(return_value=False)
        repo._raw_redis_client = None
        lifecycle = RedisDLQLifecycle(repo)
        with patch.object(
            lifecycle, "_try_acquire_python", return_value="fallback"
        ) as fb:
            result = lifecycle._try_acquire_atomic(10, max_retries=3, domain_out=[])

        assert result == "fallback"
        fb.assert_called_once()
