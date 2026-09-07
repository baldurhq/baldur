"""``StartupHashChainSync`` reconciles exactly the keys the writer writes.

Source: ``src/baldur/audit/integrity/sync.py`` — ``from_manager`` and
``_cleanup_pending_sequences``.

The chain's sequence/state keys and its PENDING/ORPHANED keys hang off two
different roots in production: the chain manager is built with a
partition-namespaced prefix while ``PendingSequenceManager`` receives the bare
one. One prefix for both reconciles a key nothing writes — which is what the
boot-time sync was doing, unnoticed, because it derived the key form itself
instead of reading it off the objects that wrote it.

``from_manager`` closes that by taking the client and the chain prefix off the
manager and the bare root from its caller. The bare root has **no default**:
the manager cannot supply it, and inheriting ``__init__``'s fallback would let
a caller silently reconcile the wrong namespace.

The PENDING sweep runs inside ``init()`` under the init lock, so it uses a
batched, capped ``SCAN`` rather than ``KEYS``: on a shared Redis holding
millions of keys, ``KEYS`` is boot time spent blocking the server.

Companion file: ``tests/unit/audit/hash_chain_core/test_startup_sync.py`` —
the file-vs-Redis comparison this class performs around the sweep.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.1 Contract (the exact composed key forms, the two scan constants).
- §8.2 Boundary (the cap, and the entry that trips it).
- §8.5 Dependency interaction (``scan_iter`` arguments; ``keys()`` unused).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.audit.integrity import (
    LedgerTailReader,
    RedisHashChainManager,
    StartupHashChainSync,
)
from baldur.audit.integrity import sync as sync_module
from baldur.audit.integrity.sync import (
    PENDING_SCAN_BATCH,
    PENDING_SCAN_MAX_KEYS,
    PENDING_SCAN_MAX_SECONDS,
)
from baldur.settings.audit_integrity import (
    get_audit_integrity_settings,
    reset_audit_integrity_settings,
)
from tests.factories import MockPipeline, MockRedisClient

# The two namespaces production keeps apart. Hardcoded rather than composed
# from ``chain_namespace_prefix``: recomposing them here would make this file
# agree with any derivation the sync happened to use, which is the drift the
# whole indirection exists to prevent.
_ROOT_PREFIX = "acme:"
_PARTITION = "eu-west"
_CHAIN_PREFIX = "acme:hashchain:eu-west:"

_SEQUENCE_KEY = "acme:hashchain:eu-west:audit:hash_chain:seq"
_PENDING_PATTERN = "acme:audit:hash_chain:pending:*"


@pytest.fixture
def redis_client() -> MockRedisClient:
    return MockRedisClient()


@pytest.fixture
def manager(redis_client) -> RedisHashChainManager:
    """A chain manager built the way the adapter builds it."""
    return RedisHashChainManager(
        redis_client=redis_client,
        key_prefix=_CHAIN_PREFIX,
    )


def _pending_key(seq: int) -> str:
    return f"{_ROOT_PREFIX}audit:hash_chain:pending:{seq}"


def _orphan_key(seq: int) -> str:
    return f"{_ROOT_PREFIX}audit:hash_chain:orphaned:{seq}"


class TestStartupSyncFromManager:
    """What the classmethod reads off the manager, and what it refuses."""

    def test_the_bare_prefix_has_no_default(self, manager, tmp_path):
        """A two-argument call is the drift this signature forbids: the
        manager stores only its own composed prefix, so a defaulted third
        argument would silently reconcile the chain namespace's PENDING keys,
        which nothing writes."""
        with pytest.raises(TypeError):
            StartupHashChainSync.from_manager(manager, LedgerTailReader(tmp_path))

    def test_the_client_comes_off_the_manager(self, manager, redis_client, tmp_path):
        sync = StartupHashChainSync.from_manager(
            manager, LedgerTailReader(tmp_path), _ROOT_PREFIX
        )

        assert sync._redis is redis_client

    def test_the_chain_prefix_comes_off_the_manager(self, manager, tmp_path):
        """Not re-derived from settings — the manager is the writer, so its
        own prefix is the only value guaranteed to match what it wrote."""
        sync = StartupHashChainSync.from_manager(
            manager, LedgerTailReader(tmp_path), _ROOT_PREFIX
        )

        assert sync._key_prefix == _CHAIN_PREFIX

    def test_the_pending_prefix_is_the_bare_root_not_the_chain_one(
        self, manager, tmp_path
    ):
        sync = StartupHashChainSync.from_manager(
            manager, LedgerTailReader(tmp_path), _ROOT_PREFIX
        )

        assert sync._pending_key_prefix == _ROOT_PREFIX

    def test_the_sequence_read_lands_on_the_chain_namespace(
        self, manager, redis_client, tmp_path
    ):
        """The end-to-end pin: the sequence the sync reads is the key the
        chain writer increments."""
        redis_client.set(_SEQUENCE_KEY, 7)
        sync = StartupHashChainSync.from_manager(
            manager, LedgerTailReader(tmp_path), _ROOT_PREFIX
        )

        result = sync.sync()

        assert result["redis_sequence"] == 7

    def test_the_pending_sweep_scans_the_bare_namespace(
        self, manager, redis_client, tmp_path
    ):
        """The other half of the same pin, on the other prefix: a sweep that
        used the chain prefix would examine zero keys and report a clean
        crash recovery on a Redis full of orphans."""
        redis_client.set(_pending_key(3), "expected-hash")
        sync = StartupHashChainSync.from_manager(
            manager, LedgerTailReader(tmp_path), _ROOT_PREFIX
        )

        result = sync.sync()

        assert result["pending_cleaned"] == 1

    def test_the_two_prefixes_stay_distinguishable(
        self, manager, redis_client, tmp_path
    ):
        """A PENDING key written under the chain prefix is not this sweep's —
        the negative half, which a single-prefix implementation would fail."""
        redis_client.set(f"{_CHAIN_PREFIX}audit:hash_chain:pending:9", "x")
        sync = StartupHashChainSync.from_manager(
            manager, LedgerTailReader(tmp_path), _ROOT_PREFIX
        )

        result = sync.sync()

        assert result["pending_cleaned"] == 0

    def test_the_direct_constructor_still_shares_one_prefix(
        self, redis_client, tmp_path
    ):
        """Every test that injects its own fake Redis passes one prefix and
        means it for both namespaces, so the default has to stay."""
        sync = StartupHashChainSync(redis_client, tmp_path, key_prefix="test:")

        assert sync._pending_key_prefix == "test:"


class TestStartupSyncPendingScanContract:
    """The two scan constants are shipped operational values."""

    def test_batch_size_matches_the_event_journal_scan(self):
        """redis-py's default COUNT of 10 turns one command into a round trip
        per ten keys examined."""
        assert PENDING_SCAN_BATCH == 200

    def test_the_sweep_is_capped(self):
        """Uncapped, one oversized keyspace holds up every process start —
        and the cleanup is best-effort by contract, since PENDING keys carry
        their own short TTL and expire unaided."""
        assert PENDING_SCAN_MAX_KEYS == 10_000

    def test_the_sweep_carries_a_wall_clock_ceiling(self):
        """The key cap counts matches, so it cannot bound a walk over a
        keyspace that holds none. Elapsed time is the bound that binds."""
        assert PENDING_SCAN_MAX_SECONDS == 2.0


class TestStartupSyncPendingScan:
    """The sweep's Redis interaction, its cap, and where orphans land."""

    def test_the_sweep_drives_scan_with_the_batch_hint(
        self, manager, redis_client, tmp_path
    ):
        redis_client.set(_pending_key(1), "expected-hash")
        sync = StartupHashChainSync.from_manager(
            manager, LedgerTailReader(tmp_path), _ROOT_PREFIX
        )

        with patch.object(redis_client, "scan", wraps=redis_client.scan) as spy:
            sync.sync()

        assert spy.call_args_list[0].kwargs["match"] == _PENDING_PATTERN
        assert spy.call_args_list[0].kwargs["count"] == PENDING_SCAN_BATCH

    def test_a_keyspace_with_no_matches_is_still_bounded_by_the_deadline(
        self, manager, redis_client, tmp_path, monkeypatch
    ):
        """The bound has to hold on the keyspace it exists for.

        PENDING keys carry a short TTL, so the normal state of a Redis shared
        with an application cache is millions of keys and zero matches. An
        iterator-driven sweep hands control back only per *match*, so a budget
        checked in the loop body never runs at all there and the walk is
        bounded only by the size of the server's keyspace — inside ``init()``,
        under the init lock, in every worker. Driving the cursor is what makes
        the deadline reachable.
        """
        for i in range(2_000):
            redis_client.set(f"app:cache:{i}", "unrelated")
        # Negative, not 0.0: the platform monotonic clock has ~16 ms
        # granularity, so a zero budget can still read as "not yet elapsed"
        # across ten fast round trips and the arm would pass vacuously.
        monkeypatch.setattr(sync_module, "PENDING_SCAN_MAX_SECONDS", -1.0)
        sync = StartupHashChainSync.from_manager(
            manager, LedgerTailReader(tmp_path), _ROOT_PREFIX
        )

        with (
            patch.object(sync_module, "logger") as mock_logger,
            patch.object(redis_client, "scan", wraps=redis_client.scan) as spy,
        ):
            sync.sync()

        assert spy.call_count == 1, (
            "an expired deadline must stop the walk after one round trip; "
            f"took {spy.call_count}"
        )
        capped = [
            call
            for call in mock_logger.warning.call_args_list
            if call.args and call.args[0] == "startup_sync.pending_scan_capped"
        ]
        assert len(capped) == 1
        assert capped[0].kwargs["limit"] == "max_seconds"

    def test_the_sweep_never_calls_keys(self, manager, redis_client, tmp_path):
        """``KEYS`` blocks the whole server for the length of the scan, and
        this runs inside ``init()`` while the init lock is held."""
        redis_client.set(_pending_key(1), "expected-hash")
        sync = StartupHashChainSync.from_manager(
            manager, LedgerTailReader(tmp_path), _ROOT_PREFIX
        )

        with patch.object(redis_client, "keys", wraps=redis_client.keys) as spy:
            sync.sync()

        spy.assert_not_called()

    def test_a_swept_pending_key_is_deleted(self, manager, redis_client, tmp_path):
        sync = StartupHashChainSync.from_manager(
            manager, LedgerTailReader(tmp_path), _ROOT_PREFIX
        )
        redis_client.set(_pending_key(4), "expected-hash")

        sync.sync()

        assert redis_client.get(_pending_key(4)) is None

    def test_the_orphan_key_follows_the_pending_prefix(
        self, manager, redis_client, tmp_path
    ):
        """Orphans are the pending namespace's other half, and the only reader
        of them scans the bare form — an orphan written under the chain prefix
        is a record nothing will ever find."""
        sync = StartupHashChainSync.from_manager(
            manager, LedgerTailReader(tmp_path), _ROOT_PREFIX
        )
        redis_client.set(_pending_key(5), "expected-hash")

        sync.sync()

        assert redis_client.exists(_orphan_key(5)) == 1
        assert redis_client.exists(f"{_CHAIN_PREFIX}audit:hash_chain:orphaned:5") == 0

    def test_the_orphan_ttl_comes_from_settings(
        self, manager, redis_client, tmp_path, monkeypatch
    ):
        """How long a crash artefact is retained is an operator's call, not an
        inline literal at the write site."""
        monkeypatch.setenv("BALDUR_AUDIT_INTEGRITY_ORPHAN_TTL_SECONDS", "7200")
        reset_audit_integrity_settings()
        redis_client.set(_pending_key(6), "expected-hash")
        sync = StartupHashChainSync.from_manager(
            manager, LedgerTailReader(tmp_path), _ROOT_PREFIX
        )

        try:
            assert get_audit_integrity_settings().orphan_ttl_seconds == 7200
            with patch.object(MockPipeline, "set", autospec=True) as mock_set:
                sync.sync()
        finally:
            reset_audit_integrity_settings()

        assert mock_set.call_args.kwargs["ex"] == 7200

    def test_a_malformed_pending_key_does_not_stop_the_sweep(
        self, manager, redis_client, tmp_path
    ):
        """The trailing segment is parsed as an int. One hand-written key must
        not cost the rest of the crash recovery."""
        redis_client.set(f"{_ROOT_PREFIX}audit:hash_chain:pending:notanint", "x")
        redis_client.set(_pending_key(8), "expected-hash")
        sync = StartupHashChainSync.from_manager(
            manager, LedgerTailReader(tmp_path), _ROOT_PREFIX
        )

        result = sync.sync()

        assert result["pending_cleaned"] == 1

    def test_the_sweep_stops_at_the_cap(
        self, manager, redis_client, tmp_path, monkeypatch
    ):
        """One key past the cap is examined and then abandoned, so the boot
        cost stays bounded by the constant rather than by the keyspace."""
        monkeypatch.setattr(sync_module, "PENDING_SCAN_MAX_KEYS", 3)
        for seq in range(5):
            redis_client.set(_pending_key(seq), "expected-hash")
        sync = StartupHashChainSync.from_manager(
            manager, LedgerTailReader(tmp_path), _ROOT_PREFIX
        )

        result = sync.sync()

        assert result["pending_cleaned"] == 3

    def test_a_capped_sweep_says_so_at_warning(
        self, manager, redis_client, tmp_path, monkeypatch
    ):
        """Silently truncated crash recovery reads as a clean one — the
        operator has to be able to tell the difference."""
        monkeypatch.setattr(sync_module, "PENDING_SCAN_MAX_KEYS", 3)
        for seq in range(5):
            redis_client.set(_pending_key(seq), "expected-hash")
        sync = StartupHashChainSync.from_manager(
            manager, LedgerTailReader(tmp_path), _ROOT_PREFIX
        )

        with patch.object(sync_module, "logger") as mock_logger:
            sync.sync()

        capped = [
            call
            for call in mock_logger.warning.call_args_list
            if call.args and call.args[0] == "startup_sync.pending_scan_capped"
        ]
        assert len(capped) == 1
        assert capped[0].kwargs["limit"] == "max_keys"
        assert capped[0].kwargs["examined"] == 3
        assert capped[0].kwargs["cleaned"] == 3

    def test_an_uncapped_sweep_stays_quiet(
        self, manager, redis_client, tmp_path, monkeypatch
    ):
        """The control arm — a WARNING on every boot is one that gets
        filtered out before the truncated boot it exists for."""
        monkeypatch.setattr(sync_module, "PENDING_SCAN_MAX_KEYS", 10)
        for seq in range(5):
            redis_client.set(_pending_key(seq), "expected-hash")
        sync = StartupHashChainSync.from_manager(
            manager, LedgerTailReader(tmp_path), _ROOT_PREFIX
        )

        with patch.object(sync_module, "logger") as mock_logger:
            result = sync.sync()

        assert result["pending_cleaned"] == 5
        assert not [
            call
            for call in mock_logger.warning.call_args_list
            if call.args and call.args[0] == "startup_sync.pending_scan_capped"
        ]
