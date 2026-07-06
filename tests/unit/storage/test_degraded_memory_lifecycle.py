"""Degraded-mode memory lifecycle coverage for ResilientStorageBackend (#539).

Covers the two backend-internal follow-ups #539 hardens on the shared
``_memory`` + recovery + lock model:

- **D1 recovery-window write-visibility**: a degraded write that lands in the
  ``_do_recovery`` window must be visible in Redis afterwards (surfaced by the
  locked delta-replay) — not WAL-only-until-restart. The ``_sync_memory_to_redis``
  seam injects a mid-recovery degraded write; the post-recovery fake-redis state
  is asserted across every degraded op type.
- **D2 bounded degraded blob memory**: blobs live in a dedicated bounded
  ``_blob_memory`` OrderedDict capped by a byte budget. The shared
  ``_mem_apply_set_blob`` mutator maintains ``_blob_memory_bytes`` (overwrite
  subtracts the old length + explicit ``move_to_end``) and evicts
  least-recently-written blobs. Eviction sheds degraded-read visibility (durable
  in WAL, reconstructed on recovery), never data loss.

Test classes:
    TestRecoveryWindowVisibilityBehavior — D1 window-write visibility per op type
    TestBoundedBlobMemoryBehavior        — D2/D4 byte budget, eviction, accounting
"""

from __future__ import annotations

import tempfile
import time
from unittest.mock import patch

import pytest

from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.adapters.resilient.backend import (
    ResilientStorageBackend,
    ResilientStorageMode,
)
from baldur.interfaces.repositories import FailedOperationStatus
from baldur.settings.resilient_storage import ResilientStorageSettings
from tests.factories import FakeRawRedis, FakeRedisAdapter


@pytest.fixture
def temp_wal_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def degraded_backend(temp_wal_dir):
    """Real backend in DEGRADED mode with a real temp-dir WAL.

    ``_next_redis_probe`` is pushed far into the future so hot-path
    ``_ensure_redis()`` calls never attempt a real connection (a local dev
    Redis would otherwise flip the backend to REDIS mode and skip the WAL).
    """
    config = ResilientStorageSettings(wal_dir=temp_wal_dir, allow_memory_only=True)
    backend = ResilientStorageBackend(config)
    backend._next_redis_probe = time.monotonic() + 9999.0
    yield backend
    backend.close()


# =============================================================================
# TestRecoveryWindowVisibilityBehavior — D1 window-write visibility (G1)
# =============================================================================


class TestRecoveryWindowVisibilityBehavior:
    """A degraded write landing during the ``_do_recovery`` window must end up
    in Redis after recovery completes, for every op type. The
    ``_sync_memory_to_redis`` seam fires after the lock-free bulk replay but
    before the locked finalize — exactly the race D1 closes via the locked
    delta-replay."""

    @pytest.mark.parametrize("op", ["set", "incr", "hset", "zadd", "zrem", "set_blob"])
    def test_degraded_write_in_recovery_window_visible_in_redis_after_recovery(
        self, degraded_backend, op
    ):
        backend = degraded_backend

        # Given: a removal op needs its target already in the WAL so the bulk
        # phase reconstructs it in Redis before the window delta removes it.
        if op == "zrem":
            backend.zadd("win:zr", {"gone": 1.0, "stay": 2.0})

        # Wire an in-process fake-redis as the now-recovered client. Mode stays
        # DEGRADED until _do_recovery flips it.
        raw = FakeRawRedis()
        backend._redis = FakeRedisAdapter(raw)
        backend._redis_initialized = True

        # When: a degraded write lands in the recovery window.
        def inject_window_write():
            if op == "set":
                backend.set("win:set", "v")
            elif op == "incr":
                backend.incr("win:cnt")
            elif op == "hset":
                backend.hset("win:h", {"f": "v"})
            elif op == "zadd":
                backend.zadd("win:z", {"m": 1.0})
            elif op == "zrem":
                backend.zrem("win:zr", "gone")
            elif op == "set_blob":
                backend.set_blob("win:b", b"\x78window-blob")

        with patch.object(
            backend, "_sync_memory_to_redis", side_effect=inject_window_write
        ):
            assert backend._do_recovery() is True

        # Then: the window write is visible in Redis — surfaced by the locked
        # delta-replay, not stranded WAL-only-until-restart.
        assert backend.mode == ResilientStorageMode.REDIS
        if op == "set":
            assert backend.get("win:set") == "v"
        elif op == "incr":
            assert backend.get("win:cnt") == 1
        elif op == "hset":
            assert backend.hgetall("win:h") == {"f": "v"}
        elif op == "zadd":
            assert "m" in backend.zrange("win:z", 0, -1)
        elif op == "zrem":
            members = backend.zrange("win:zr", 0, -1)
            assert "gone" not in members
            assert "stay" in members
        elif op == "set_blob":
            assert backend.get_blob("win:b") == b"\x78window-blob"


# =============================================================================
# TestBoundedBlobMemoryBehavior — D2/D4 byte budget, eviction, accounting (G2)
# =============================================================================


class TestBoundedBlobMemoryBehavior:
    """``_mem_apply_set_blob`` keeps degraded blob memory inside the byte
    budget by evicting least-recently-written blobs, never drifting the
    accumulator from the stored total.

    Tests set ``degraded_blob_memory_max_bytes`` directly to a small value:
    the Field floor is 1 MiB, so the attribute is assigned post-construction
    (``validate_assignment`` is off) to keep the byte math small and readable.
    The eviction code reads the cap fresh on every call and is cap-value-
    agnostic, so a tiny budget exercises the identical code path.
    """

    def test_sustained_set_blob_writes_keep_blob_memory_within_byte_budget(
        self, degraded_backend
    ):
        backend = degraded_backend
        backend.config.degraded_blob_memory_max_bytes = 1000

        for i in range(20):
            backend.set_blob(f"dlq:entry:{i}", b"\x00" * 200)  # 4000 total >> cap

        assert backend._blob_memory_bytes <= 1000
        assert backend._blob_memory_bytes == sum(
            len(b) for b in backend._blob_memory.values()
        )
        # A byte budget, not zero — eviction is a last resort.
        assert len(backend._blob_memory) >= 1

    def test_sustained_batch_create_writes_keep_blob_memory_within_byte_budget(
        self, degraded_backend
    ):
        backend = degraded_backend
        backend.config.degraded_blob_memory_max_bytes = 1000

        # The primary degraded inflow (DLQ create) funnels through the SAME
        # _mem_apply_set_blob via the grouped batch path, so the byte budget
        # must hold here — not just on the standalone set_blob path.
        for i in range(20):
            backend.batch_write_ops(
                [
                    ("set_blob", f"dlq:entry:{i}", b"\x00" * 200),
                    ("zadd", "dlq:pending", {str(i): float(i)}),
                ]
            )

        assert backend._blob_memory_bytes <= 1000
        assert backend._blob_memory_bytes == sum(
            len(b) for b in backend._blob_memory.values()
        )

    def test_blob_memory_bytes_matches_stored_total_after_mixed_overwrite_evict(
        self, degraded_backend
    ):
        backend = degraded_backend
        backend.config.degraded_blob_memory_max_bytes = 1000

        backend.set_blob("dlq:entry:a", b"\x00" * 300)
        backend.set_blob("dlq:entry:b", b"\x00" * 300)
        # Same-key overwrite with a DIFFERENT length (D4 GET->mutate->SET
        # shape): the overwrite-subtract must remove the old 300 before adding
        # the new 100, else the accumulator over-counts the same key.
        backend.set_blob("dlq:entry:a", b"\x00" * 100)
        # Push the total past the budget to force eviction.
        backend.set_blob("dlq:entry:c", b"\x00" * 300)
        backend.set_blob("dlq:entry:d", b"\x00" * 300)

        # Zero-drift: a naive `+= len(blob)` on overwrite still passes the cap
        # check (too tightly) but fails this invariant — the targeted guard.
        assert backend._blob_memory_bytes == sum(
            len(b) for b in backend._blob_memory.values()
        )
        assert backend._blob_memory_bytes <= 1000

    def test_single_blob_larger_than_cap_evicted_in_call_accumulator_nonnegative(
        self, degraded_backend
    ):
        backend = degraded_backend
        backend.config.degraded_blob_memory_max_bytes = 1000

        backend.set_blob("dlq:entry:big", b"\x00" * 2000)  # own length > cap

        # Evicted within the same call (degraded-read-invisible immediately,
        # still durable in WAL); the `and self._blob_memory` guard keeps the
        # loop from underflowing past an empty dict.
        assert "dlq:entry:big" not in backend._blob_memory
        assert backend._blob_memory_bytes == 0

    def test_evicted_blob_reads_none_degraded_but_reconstructs_in_redis_on_recovery(
        self, degraded_backend
    ):
        backend = degraded_backend
        backend.config.degraded_blob_memory_max_bytes = 500
        original = b"\x78first-entry-payload"

        backend.set_blob("dlq:entry:first", original)
        # Evict "first" by writing newer blobs past the budget.
        for i in range(5):
            backend.set_blob(f"dlq:entry:{i}", b"\x00" * 200)

        # Degraded read after eviction: invisible (None), NOT an error.
        assert backend.get_blob("dlq:entry:first") is None

        # Recovery reconstructs it in Redis from the durable WAL record.
        raw = FakeRawRedis()
        backend._redis = FakeRedisAdapter(raw)
        backend._redis_initialized = True
        assert backend._do_recovery() is True

        assert backend.get_blob("dlq:entry:first") == original

    def test_dlq_update_on_evicted_blob_returns_false(self, degraded_backend):
        backend = degraded_backend
        # Cap below a single entry blob so the create-path blob is evicted
        # in-call (D4: replay worker / operator GET->mutate->SET).
        backend.config.degraded_blob_memory_max_bytes = 30
        repo = RedisDLQRepository(backend)

        entry = repo.create(domain="payment", failure_type="PG_TIMEOUT")

        # The blob was evicted; a degraded update cannot load it, so the
        # GET->mutate->SET update is a no-op returning False.
        assert backend.get_blob(repo._make_key(entry.id)) is None
        assert (
            repo._update(entry.id, status=FailedOperationStatus.RESOLVED.value) is False
        )

    def test_degrade_recover_degrade_cycle_does_not_crash(self, degraded_backend):
        backend = degraded_backend
        backend.config.degraded_blob_memory_max_bytes = 1000

        backend.set_blob("dlq:entry:1", b"\x00" * 300)
        assert backend._blob_memory_bytes == 300

        # Recovery clears _blob_memory AND resets the byte accumulator to 0.
        raw = FakeRawRedis()
        backend._redis = FakeRedisAdapter(raw)
        backend._redis_initialized = True
        assert backend._do_recovery() is True
        assert backend._blob_memory_bytes == 0
        assert len(backend._blob_memory) == 0

        # Re-enter degraded mode and write again. The accumulator must reflect
        # ONLY the new blob — without the reset-on-clear it would retain the
        # stale 300 and drift (or popitem an empty dict on eviction).
        backend._switch_to_degraded()
        backend.set_blob("dlq:entry:2", b"\x00" * 250)

        assert backend._blob_memory_bytes == 250
        assert backend._blob_memory["dlq:entry:2"] == b"\x00" * 250

    def test_first_blob_eviction_emits_warning_once(self, degraded_backend):
        backend = degraded_backend
        backend.config.degraded_blob_memory_max_bytes = 500

        with patch("baldur.adapters.resilient.backend.logger") as mock_logger:
            for i in range(10):  # forces multiple evictions
                backend.set_blob(f"dlq:entry:{i}", b"\x00" * 200)

        full_warnings = [
            call.args[0]
            for call in mock_logger.warning.call_args_list
            if call.args
            and call.args[0] == "resilient_storage.degraded_blob_memory_full"
        ]
        # One-time flag: the "memory full" WARNING fires exactly once per
        # outage despite repeated evictions.
        assert len(full_warnings) == 1

    def test_eviction_warning_flag_resets_on_recovery_success(self, degraded_backend):
        backend = degraded_backend
        backend._degraded_blob_memory_full_logged = True
        backend._recovery_lock.acquire()  # dispatch precondition

        with patch.object(backend, "check_and_recover", return_value=True):
            backend._run_recovery_payload()

        # Reset alongside _degraded_critical_logged so the next sustained
        # outage that sheds blobs re-warns operators.
        assert backend._degraded_blob_memory_full_logged is False

    def test_degraded_delete_removes_blob_key_and_decrements_accumulator(
        self, degraded_backend
    ):
        backend = degraded_backend
        blob = b"\x78payload"
        backend.set_blob("dlq:entry:1", blob)
        assert backend._blob_memory_bytes == len(blob)

        assert backend.delete("dlq:entry:1") is True

        # A blob key lives in _blob_memory, not _memory; delete must pop it
        # there and decrement the accumulator so a subsequent get_blob sees
        # the removal and the accumulator stays exact.
        assert "dlq:entry:1" not in backend._blob_memory
        assert backend._blob_memory_bytes == 0
        assert backend.get_blob("dlq:entry:1") is None

    def test_get_stats_exposes_blob_memory_keys_and_bytes(self, degraded_backend):
        backend = degraded_backend
        backend.set_blob("dlq:entry:1", b"\x00" * 100)
        backend.set_blob("dlq:entry:2", b"\x00" * 150)

        stats = backend.get_stats()

        assert stats["blob_memory_keys"] == 2
        assert stats["blob_memory_bytes"] == 250

    def test_overwrite_promotes_blob_so_older_key_is_evicted_first(
        self, degraded_backend
    ):
        backend = degraded_backend
        # Budget holds exactly two 300-byte blobs, not three.
        backend.config.degraded_blob_memory_max_bytes = 600

        backend.set_blob("dlq:entry:a", b"\x00" * 300)  # order: [a]
        backend.set_blob("dlq:entry:b", b"\x01" * 300)  # order: [a, b]
        # Overwrite "a" — the explicit move_to_end makes it most-recently-
        # written, so "b" becomes the least-recently-written.
        backend.set_blob("dlq:entry:a", b"\x02" * 300)  # order: [b, a]
        # Next write exceeds the budget and evicts the LRW entry ("b"), not "a".
        backend.set_blob("dlq:entry:c", b"\x03" * 300)  # evict b -> [a, c]

        assert backend.get_blob("dlq:entry:a") == b"\x02" * 300
        assert backend.get_blob("dlq:entry:b") is None  # least-recently-written
        assert backend.get_blob("dlq:entry:c") == b"\x03" * 300
        assert backend._blob_memory_bytes == 600
