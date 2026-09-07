"""The chain re-anchors to its own ledger instead of re-using numbers.

A chain keeps its **ordering** in one place — a JSON state file locally, a
Redis counter in distributed mode — and the **ledger it orders** in another.
When the source loses or rolls back its state it hands out numbers the ledger
already holds, and the old code appended them silently: one audit file ending
up with two entries carrying the same sequence, each individually verifiable,
detectable only by someone who ran a verification pass afterwards.

Both managers now compare what their source minted against the highest
sequence the ledger already holds, inside the same exclusive section that
minted it. Three things this file pins:

- the **repair rides on the entry**, under its own hash, so the condition is
  on the record rather than only in a log line;
- an unreadable ledger is a write the chain **refuses** — before any mutation
  of the source, because "no ledger" is the one answer that puts 1 inside a
  live one;
- a sustained outage announces itself **once per episode**, not once per
  entry, and the degraded gauge tracks the live posture rather than freezing
  at what the boot probe saw.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.1 Contract (the stamp's shape, the Redis key forms, the lock defaults).
- §8.3 State transition (the four distributed reasons, the three local ones,
  the posture gauge's three edges).
- §8.4 Side effects (the stamp under the hash, the counters, the log volume).
- §8.2 Exception/edge cases (a reader that raises, a release that raises).
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.audit.integrity.ledger_tail import LedgerTail, LedgerTailReader
from baldur.audit.integrity.local_manager import HashChainManager
from baldur.audit.integrity.models import (
    INTEGRITY_RESERVED_KEYS,
    record_source_reset,
    sanitize_integrity_annotations,
)
from baldur.audit.integrity.redis_manager import (
    CHAIN_LOCK_BLOCKING_TIMEOUT_SECONDS,
    CHAIN_LOCK_KEY,
    CHAIN_LOCK_TIMEOUT_SECONDS,
    CHAIN_SEQUENCE_KEY,
    CHAIN_STATE_KEY,
    CHAIN_STATE_WRITE_TIME_RECOVERY,
    RedisHashChainManager,
    build_chain_lock,
    write_chain_state,
)
from baldur.audit.integrity.verifier import verify_audit_log_integrity
from baldur.core.exceptions import AuditError, HashChainSequenceRefusedError
from tests.factories import MockRedisClient
from tests.factories.writable_dir import log_events

_PREFIX = "test:"
_SEQ_KEY = f"{_PREFIX}{CHAIN_SEQUENCE_KEY}"
_STATE_KEY = f"{_PREFIX}{CHAIN_STATE_KEY}"
_LEDGER_NAME = "audit_2026-09-07.jsonl"

# Levels an operator's log pipeline keeps by default. The per-write fallback
# record was demoted to DEBUG precisely so it stops reaching this set.
_OPERATOR_LEVELS = {"warning", "error", "critical"}


def _ledger_path(tmp_path: Path) -> Path:
    return tmp_path / _LEDGER_NAME


def _append(path: Path, entry: dict[str, Any]) -> None:
    """Append one entry the way the adapter's write path does."""
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def _mint(manager: Any, path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Mint one entry through ``manager`` and land it in the ledger."""
    entry = manager.add_integrity(dict(payload))
    _append(path, entry)
    return entry


def _read_ledger(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_state_file(path: Path, sequence: int, previous_hash: str) -> None:
    path.write_text(
        json.dumps({"sequence": sequence, "previous_hash": previous_hash}),
        encoding="utf-8",
    )


def _operator_records(logs: list[dict], name: str) -> list[dict]:
    """Captured records for ``name`` that survive an operator's level filter."""
    return [
        record
        for record in log_events(logs, name)
        if record["log_level"] in _OPERATOR_LEVELS
    ]


@pytest.fixture
def ledger(tmp_path):
    """A reader over an empty ledger directory the tests then fill."""
    return LedgerTailReader(tmp_path)


@pytest.fixture
def redis_client():
    return MockRedisClient()


@pytest.fixture
def distributed_chain(tmp_path, ledger, redis_client):
    """A distributed manager wired exactly the way the adapter wires one."""
    fallback = HashChainManager(
        state_file=tmp_path / ".hash_chain_state.json",
        use_file_lock=True,
        ledger=ledger,
    )
    return RedisHashChainManager(
        redis_client=redis_client,
        key_prefix=_PREFIX,
        fallback_manager=fallback,
        ledger=ledger,
    )


# =============================================================================
# Contract — the shared shape of the extra keys a manager writes
# =============================================================================


class TestIntegrityAnnotationsContract:
    """An annotation may never shadow a field the chain itself owns."""

    def test_the_reserved_key_set_is_the_chains_own_fields(self):
        assert INTEGRITY_RESERVED_KEYS == frozenset(
            {"sequence", "previous_hash", "timestamp", "pod_id", "current_hash"}
        )

    @pytest.mark.parametrize(
        "reserved",
        sorted({"sequence", "previous_hash", "timestamp", "pod_id", "current_hash"}),
    )
    def test_a_reserved_key_is_dropped_from_the_annotations(self, reserved):
        """``current_hash`` in particular is assigned *after* the hash is
        computed, so an annotation under it would be hashed in and then
        overwritten — and the entry would verify as tampered."""
        cleaned = sanitize_integrity_annotations({reserved: "spoofed", "keep": 1})

        assert reserved not in cleaned
        assert cleaned == {"keep": 1}

    @pytest.mark.parametrize("annotations", [None, {}], ids=["none", "empty"])
    def test_nothing_to_merge_is_an_empty_dict(self, annotations):
        assert sanitize_integrity_annotations(annotations) == {}

    def test_the_caller_mapping_is_not_mutated(self):
        original = {"sequence": 9, "degraded": True}

        sanitize_integrity_annotations(original)

        assert original == {"sequence": 9, "degraded": True}


class TestSourceResetRecordContract:
    """One call site per manager, so the log record, the counter and the
    stamp cannot describe the same repair differently."""

    def test_the_stamp_carries_exactly_the_four_repair_facts(self):
        annotation = record_source_reset(
            manager="redis",
            reason="counter_reset",
            observed=1,
            adopted=42,
            ledger_path="/var/log/baldur/audit_2026-09-07.jsonl",
        )

        assert annotation == {
            "source_reset": {
                "manager": "redis",
                "reason": "counter_reset",
                "observed": 1,
                "adopted": 42,
            }
        }

    def test_the_record_is_a_warning_naming_the_ledger_it_re_anchored_to(self):
        with capture_logs() as logs:
            record_source_reset(
                manager="local",
                reason="state_unreadable",
                observed=None,
                adopted=7,
                ledger_path="/tmp/audit_2026-09-07.jsonl",
            )

        records = log_events(logs, "hash_chain.sequence_source_reset")
        assert len(records) == 1
        assert records[0]["log_level"] == "warning"
        assert records[0]["manager"] == "local"
        assert records[0]["reason"] == "state_unreadable"
        assert records[0]["observed"] is None
        assert records[0]["adopted"] == 7
        assert records[0]["ledger_path"] == "/tmp/audit_2026-09-07.jsonl"

    def test_the_metric_child_carries_the_manager_and_reason_pair(self):
        """A labelled counter, so a healthy chain exports nothing and a
        repaired one exports exactly the child that fired."""
        with patch(
            "baldur.metrics.audit_backend_metrics."
            "increment_audit_hash_chain_source_reset",
            autospec=True,
        ) as increment:
            record_source_reset(
                manager="redis",
                reason="state_hash_stale",
                observed=8,
                adopted=8,
                ledger_path="/tmp/x.jsonl",
            )

        increment.assert_called_once_with(manager="redis", reason="state_hash_stale")

    def test_a_raising_metric_helper_does_not_cost_the_repair(self):
        """Fail-open: the repair is the guarantee, the counter is the report."""
        with patch(
            "baldur.metrics.audit_backend_metrics."
            "increment_audit_hash_chain_source_reset",
            autospec=True,
            side_effect=RuntimeError("registry gone"),
        ):
            annotation = record_source_reset(
                manager="local",
                reason="source_behind_ledger",
                observed=2,
                adopted=9,
                ledger_path="/tmp/x.jsonl",
            )

        assert annotation["source_reset"]["adopted"] == 9


class TestHashChainSequenceRefusedErrorContract:
    """The refusal is typed, and says which ledger it could not read."""

    def test_the_error_is_an_audit_error(self):
        assert issubclass(HashChainSequenceRefusedError, AuditError)

    def test_extra_context_names_the_manager_and_the_ledger_selection(self):
        error = HashChainSequenceRefusedError(
            manager="redis",
            log_dir="/var/log/baldur",
            filename_pattern="audit_{date}_worker.jsonl",
            error="permission denied",
        )

        context = error.extra_context()

        assert context["manager"] == "redis"
        assert context["log_dir"] == "/var/log/baldur"
        assert context["filename_pattern"] == "audit_{date}_worker.jsonl"
        assert context["error"] == "permission denied"

    def test_the_default_message_says_what_was_refused_and_why(self):
        assert "refusing to mint" in str(HashChainSequenceRefusedError())


# =============================================================================
# Local manager — the state file is the source, the ledger is the truth
# =============================================================================


class TestLocalLoadStateBehavior:
    """``_load_state`` now answers whether a source backs the counter."""

    def test_a_parsed_state_file_reports_true_and_is_remembered(self, tmp_path):
        state_file = tmp_path / ".hash_chain_state.json"
        _write_state_file(state_file, 5, "hash-5")
        manager = HashChainManager(state_file=state_file, use_file_lock=False)

        assert manager._state_loaded is True
        assert manager._sequence == 5
        assert manager._previous_hash == "hash-5"

    @pytest.mark.parametrize(
        "content", [None, "{not json", ""], ids=["missing", "corrupt", "empty"]
    )
    def test_an_unreadable_source_reports_false_and_leaves_the_counter_alone(
        self, tmp_path, content
    ):
        """The caller decides what an unreadable source means; silently
        keeping a fresh worker's ``0`` is what re-minted from 1."""
        state_file = tmp_path / ".hash_chain_state.json"
        manager = HashChainManager(state_file=state_file, use_file_lock=False)
        manager._sequence = 11
        manager._previous_hash = "hash-11"
        if content is not None:
            state_file.write_text(content, encoding="utf-8")

        loaded = manager._load_state()

        assert loaded is False
        assert manager._state_loaded is False
        assert manager._sequence == 11
        assert manager._previous_hash == "hash-11"

    def test_reloading_the_same_state_file_is_idempotent(self, tmp_path):
        state_file = tmp_path / ".hash_chain_state.json"
        _write_state_file(state_file, 5, "hash-5")
        manager = HashChainManager(state_file=state_file, use_file_lock=False)

        assert manager._load_state() is True
        assert manager._load_state() is True
        assert (manager._sequence, manager._previous_hash) == (5, "hash-5")


class TestLocalSequenceSourceResetBehavior:
    """The local chain re-anchors to the ledger's own tail, in both lock
    modes, whatever shape the state file was lost in."""

    @pytest.mark.parametrize("use_file_lock", [True, False], ids=["locked", "unlocked"])
    @pytest.mark.parametrize(
        ("state_content", "expected_reason"),
        [
            (None, "state_unreadable"),
            ("{truncated", "state_unreadable"),
            ({"sequence": 3, "previous_hash": "stale-hash"}, "source_behind_ledger"),
        ],
        ids=["state_unreadable_missing", "state_unreadable_corrupt", "source_behind"],
    )
    def test_a_source_behind_the_ledger_adopts_the_tail(
        self, tmp_path, ledger, use_file_lock, state_content, expected_reason
    ):
        # Given: a ledger already holding ten entries, and a state file the
        # process cannot use.
        path = _ledger_path(tmp_path)
        seed = HashChainManager(state_file=None, use_file_lock=False)
        for index in range(10):
            _mint(seed, path, {"event_type": f"seed.{index}"})
        tail = LedgerTailReader(tmp_path).read()
        state_file = tmp_path / ".hash_chain_state.json"
        if isinstance(state_content, dict):
            _write_state_file(
                state_file, state_content["sequence"], state_content["previous_hash"]
            )
        elif state_content is not None:
            state_file.write_text(state_content, encoding="utf-8")

        # When
        manager = HashChainManager(
            state_file=state_file, use_file_lock=use_file_lock, ledger=ledger
        )
        entry = manager.add_integrity({"event_type": "after.loss"})

        # Then
        assert tail is not None
        assert entry["integrity"]["sequence"] == tail.sequence + 1
        assert entry["integrity"]["previous_hash"] == tail.current_hash
        assert entry["integrity"]["source_reset"] == {
            "manager": "local",
            "reason": expected_reason,
            "observed": 1 if expected_reason == "state_unreadable" else 4,
            "adopted": tail.sequence,
        }

    @pytest.mark.parametrize("use_file_lock", [True, False], ids=["locked", "unlocked"])
    def test_state_hash_stale_re_anchors_the_hash_and_leaves_the_sequence(
        self, tmp_path, ledger, use_file_lock
    ):
        """The failed-``_save_state`` shape: the counter is at the tail but
        the hash it links to is not the tail's."""
        path = _ledger_path(tmp_path)
        seed = HashChainManager(state_file=None, use_file_lock=False)
        for index in range(4):
            _mint(seed, path, {"event_type": f"seed.{index}"})
        tail = LedgerTailReader(tmp_path).read()
        assert tail is not None
        state_file = tmp_path / ".hash_chain_state.json"
        _write_state_file(state_file, tail.sequence, "a-hash-no-entry-carries")

        manager = HashChainManager(
            state_file=state_file, use_file_lock=use_file_lock, ledger=ledger
        )
        entry = manager.add_integrity({"event_type": "after.loss"})

        assert entry["integrity"]["sequence"] == tail.sequence + 1
        assert entry["integrity"]["previous_hash"] == tail.current_hash
        assert entry["integrity"]["source_reset"]["reason"] == "state_hash_stale"

    @pytest.mark.parametrize("use_file_lock", [True, False], ids=["locked", "unlocked"])
    def test_a_source_level_with_the_ledger_is_left_untouched(
        self, tmp_path, ledger, use_file_lock
    ):
        """The negative half: a healthy chain carries no stamp, so the stamp
        means something when it appears."""
        path = _ledger_path(tmp_path)
        state_file = tmp_path / ".hash_chain_state.json"
        manager = HashChainManager(
            state_file=state_file, use_file_lock=use_file_lock, ledger=ledger
        )
        for index in range(3):
            _mint(manager, path, {"event_type": f"healthy.{index}"})

        entry = _mint(manager, path, {"event_type": "healthy.3"})

        assert entry["integrity"]["sequence"] == 4
        assert "source_reset" not in entry["integrity"]

    def test_highest_not_last_sequence_in_the_ledger_decides_the_next_mint(
        self, tmp_path, ledger
    ):
        """Siblings append after releasing the mint lock, so the ledger's last
        line can be a lower number than one appended just before it. Reading
        the last line here mints a second 150."""
        path = _ledger_path(tmp_path)
        seed = HashChainManager(state_file=None, use_file_lock=False)
        minted = [_mint(seed, path, {"event_type": f"seed.{i}"}) for i in range(150)]
        # Re-append 149 after 150, the out-of-mint-order shape.
        _append(path, minted[148])

        manager = HashChainManager(state_file=None, use_file_lock=False, ledger=ledger)
        manager._sequence = 149
        manager._previous_hash = minted[148]["integrity"]["current_hash"]
        entry = manager.add_integrity({"event_type": "after.loss"})

        assert entry["integrity"]["sequence"] == 151
        assert (
            entry["integrity"]["previous_hash"]
            == (minted[149]["integrity"]["current_hash"])
        )

    @pytest.mark.parametrize("use_file_lock", [True, False], ids=["locked", "unlocked"])
    def test_without_a_ledger_reader_the_chain_behaves_exactly_as_before(
        self, tmp_path, use_file_lock
    ):
        """Every construction site that does not own a ledger — the singleton
        factory, the graceful-degradation manager — keeps today's behavior."""
        path = _ledger_path(tmp_path)
        seed = HashChainManager(state_file=None, use_file_lock=False)
        for index in range(6):
            _mint(seed, path, {"event_type": f"seed.{index}"})

        manager = HashChainManager(
            state_file=tmp_path / ".other_state.json",
            use_file_lock=use_file_lock,
            ledger=None,
        )
        entry = manager.add_integrity({"event_type": "fresh"})

        assert entry["integrity"]["sequence"] == 1
        assert entry["integrity"]["previous_hash"] == HashChainManager.GENESIS_HASH
        assert "source_reset" not in entry["integrity"]

    def test_an_empty_ledger_is_a_fresh_start_not_a_repair(self, tmp_path, ledger):
        manager = HashChainManager(state_file=None, use_file_lock=False, ledger=ledger)

        entry = manager.add_integrity({"event_type": "first"})

        assert entry["integrity"]["sequence"] == 1
        assert "source_reset" not in entry["integrity"]


class TestLocalAnnotationsHashedBehavior:
    """Annotations ride **under** the computed hash, never on top of it."""

    def test_annotations_hashed_into_the_entry_are_detected_when_edited(
        self, tmp_path, ledger
    ):
        path = _ledger_path(tmp_path)
        seed = HashChainManager(state_file=None, use_file_lock=False)
        for index in range(3):
            _mint(seed, path, {"event_type": f"seed.{index}"})
        manager = HashChainManager(state_file=None, use_file_lock=False, ledger=ledger)

        entry = _mint(manager, path, {"event_type": "repaired"})
        assert "source_reset" in entry["integrity"]
        assert verify_audit_log_integrity(path) == (True, [])

        rows = _read_ledger(path)
        rows[-1]["integrity"]["source_reset"]["adopted"] = 999
        path.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
        )

        is_valid, issues = verify_audit_log_integrity(path)
        assert is_valid is False
        assert [issue["type"] for issue in issues] == ["entry_modified"]

    def test_a_caller_annotation_cannot_shadow_the_chains_own_field(
        self, tmp_path, ledger
    ):
        manager = HashChainManager(state_file=None, use_file_lock=False, ledger=ledger)

        entry = manager.add_integrity(
            {"event_type": "x"}, annotations={"sequence": 4242, "degraded": True}
        )

        assert entry["integrity"]["sequence"] == 1
        assert entry["integrity"]["degraded"] is True


class TestLocalRefusalBehavior:
    """A ledger that exists but cannot be read is a write the chain refuses."""

    @pytest.mark.parametrize("use_file_lock", [True, False], ids=["locked", "unlocked"])
    def test_an_unreadable_ledger_is_refused_before_the_source_moves(
        self, tmp_path, ledger, use_file_lock
    ):
        state_file = tmp_path / ".hash_chain_state.json"
        manager = HashChainManager(
            state_file=state_file, use_file_lock=use_file_lock, ledger=ledger
        )
        manager._sequence = 12
        manager._previous_hash = "hash-12"

        with patch.object(ledger, "read", side_effect=OSError("disk gone")):
            with pytest.raises(HashChainSequenceRefusedError) as caught:
                manager.add_integrity({"event_type": "x"})

        assert manager._sequence == 12
        assert manager._previous_hash == "hash-12"
        assert caught.value.manager == "local"
        assert caught.value.log_dir == str(tmp_path)
        assert caught.value.filename_pattern == ledger.filename_pattern
        assert "disk gone" in caught.value.error

    def test_the_refused_entry_is_left_without_an_integrity_block(
        self, tmp_path, ledger
    ):
        """The caller's dict must not look half-signed to the write path."""
        manager = HashChainManager(state_file=None, use_file_lock=False, ledger=ledger)
        entry = {"event_type": "x"}

        with patch.object(ledger, "read", side_effect=OSError("disk gone")):
            with pytest.raises(HashChainSequenceRefusedError):
                manager.add_integrity(entry)

        assert "integrity" not in entry


# =============================================================================
# Distributed manager — Redis is the source, the ledger is the truth
# =============================================================================


class TestWriteChainStateContract:
    """The one writer of the counter/state pair's shape."""

    def test_the_pair_is_written_under_the_chains_own_key_forms(self, redis_client):
        write_chain_state(
            redis_client, _PREFIX, 42, "tail-hash", synced_from="file_recovery"
        )

        assert int(redis_client.get(_SEQ_KEY)) == 42
        state = redis_client.hgetall(_STATE_KEY)
        assert state[b"previous_hash"] == b"tail-hash"
        assert state[b"sequence"] == b"42"
        assert state[b"synced_from"] == b"file_recovery"
        assert b"updated_at" in state

    def test_the_write_time_recovery_marker_is_distinct_from_the_boot_one(
        self, redis_client
    ):
        """The boot reconciliation writes ``file_recovery``; a repair made on
        the write path must be tellable apart in an incident review."""
        assert CHAIN_STATE_WRITE_TIME_RECOVERY == "write_time_recovery"

        write_chain_state(
            redis_client,
            _PREFIX,
            7,
            "tail-hash",
            synced_from=CHAIN_STATE_WRITE_TIME_RECOVERY,
        )

        assert (
            redis_client.hgetall(_STATE_KEY)[b"synced_from"] == b"write_time_recovery"
        )

    def test_both_commands_go_through_one_pipeline(self, redis_client):
        with patch.object(
            redis_client, "pipeline", wraps=redis_client.pipeline
        ) as pipeline_spy:
            write_chain_state(
                redis_client, _PREFIX, 1, "h", synced_from="file_recovery"
            )

        pipeline_spy.assert_called_once()

    def test_a_failing_client_raises_rather_than_reporting_a_repair(self):
        """The caller decides whether that is a fallback or a failure — it
        cannot decide anything if the write reports success."""

        class _RaisingPipeline:
            def set(self, *args, **kwargs):
                return self

            def hset(self, *args, **kwargs):
                return self

            def execute(self):
                raise ConnectionError("redis gone")

        class _RaisingClient:
            def pipeline(self):
                return _RaisingPipeline()

        with pytest.raises(ConnectionError):
            write_chain_state(
                _RaisingClient(), _PREFIX, 1, "h", synced_from="file_recovery"
            )


class TestBuildChainLockContract:
    """Every writer of the pair serializes on this one key."""

    def test_the_lock_hangs_off_the_chains_partition_namespaced_prefix(
        self, redis_client
    ):
        lock = build_chain_lock(redis_client, _PREFIX)

        assert lock._name == f"{_PREFIX}{CHAIN_LOCK_KEY}"
        assert CHAIN_LOCK_KEY == "audit:hash_chain:lock"

    def test_the_shipped_timeouts_are_five_and_ten_seconds(self, redis_client):
        assert CHAIN_LOCK_TIMEOUT_SECONDS == 5.0
        assert CHAIN_LOCK_BLOCKING_TIMEOUT_SECONDS == 10.0

        lock = build_chain_lock(redis_client, _PREFIX)

        assert lock._timeout == timedelta(seconds=5.0)
        assert lock._blocking_timeout == 10.0

    def test_the_boot_rewind_and_the_write_path_build_the_same_key(self, redis_client):
        """Two spellings of the lock key is how a rewind and a mint stop
        excluding each other."""
        from baldur.audit.integrity import sync as sync_module

        manager_lock = build_chain_lock(redis_client, _PREFIX)
        boot_lock = sync_module.build_chain_lock(redis_client, _PREFIX)

        assert manager_lock._name == boot_lock._name


class TestDistributedSequenceSourceResetBehavior:
    """The two arms — the sequence arm and the hash arm — and what each one
    is allowed to touch."""

    def test_a_distributed_counter_reset_mid_run_continues_from_the_tail(
        self, tmp_path, distributed_chain, redis_client
    ):
        # Given: three entries already served by Redis.
        path = _ledger_path(tmp_path)
        for index in range(3):
            _mint(distributed_chain, path, {"event_type": f"seed.{index}"})
        tail = LedgerTailReader(tmp_path).read()
        assert tail is not None
        assert tail.sequence == 3

        # When: Redis loses both keys — a restart without persistence, a
        # failover to a blank replica, an ``allkeys-*`` eviction.
        redis_client.delete(_SEQ_KEY)
        redis_client.delete(_STATE_KEY)
        entry = _mint(distributed_chain, path, {"event_type": "after.loss"})

        # Then
        assert entry["integrity"]["sequence"] == tail.sequence + 1
        assert entry["integrity"]["previous_hash"] == tail.current_hash
        assert entry["integrity"]["source_reset"] == {
            "manager": "redis",
            "reason": "counter_reset",
            "observed": 1,
            "adopted": tail.sequence,
        }
        assert int(redis_client.get(_SEQ_KEY)) == tail.sequence + 1

    def test_no_second_genesis_anchor_appears_after_a_counter_reset(
        self, tmp_path, distributed_chain, redis_client
    ):
        """The old categorization: a second GENESIS mid-file, which a
        verification pass reports as ``chain_broken`` at sequence 1."""
        path = _ledger_path(tmp_path)
        for index in range(3):
            _mint(distributed_chain, path, {"event_type": f"seed.{index}"})
        redis_client.delete(_SEQ_KEY)
        redis_client.delete(_STATE_KEY)
        for index in range(3):
            _mint(distributed_chain, path, {"event_type": f"after.{index}"})

        anchors = [
            row["integrity"]
            for row in _read_ledger(path)
            if row["integrity"]["previous_hash"] == HashChainManager.GENESIS_HASH
        ]

        assert [anchor["sequence"] for anchor in anchors] == [1]

    def test_sequences_are_strictly_increasing_across_the_whole_ledger(
        self, tmp_path, distributed_chain, redis_client
    ):
        """The other half of the old categorization: a repeated number, which
        is indistinguishable from a real repeat once it is on disk."""
        path = _ledger_path(tmp_path)
        for index in range(3):
            _mint(distributed_chain, path, {"event_type": f"seed.{index}"})
        redis_client.set(_SEQ_KEY, 1)
        for index in range(3):
            _mint(distributed_chain, path, {"event_type": f"after.{index}"})

        sequences = [row["integrity"]["sequence"] for row in _read_ledger(path)]

        assert sequences == sorted(set(sequences))
        assert verify_audit_log_integrity(path) == (True, [])

    @pytest.mark.parametrize(
        ("counter", "state_hash", "expected_reason", "sequence_moves"),
        [
            (1, None, "counter_reset", True),
            (5, "tail-hash", "source_behind_ledger", True),
            (10, None, "state_hash_lost", False),
            (10, "a-hash-no-entry-carries", "state_hash_stale", False),
        ],
        ids=[
            "counter_reset",
            "source_behind",
            "state_hash_lost",
            "state_hash_stale",
        ],
    )
    def test_each_loss_shape_re_anchors_the_field_it_owns(
        self, distributed_chain, counter, state_hash, expected_reason, sequence_moves
    ):
        """The sequence arm moves the counter; the hash arm never does."""
        tail = LedgerTail(10, "tail-hash", Path("/tmp/audit_2026-09-07.jsonl"))
        minted = counter + 1 if counter != 1 else 1

        annotations, sequence, previous_hash = distributed_chain._reconcile_with_ledger(
            tail, minted, state_hash, _SEQ_KEY
        )

        assert annotations["source_reset"]["reason"] == expected_reason
        assert annotations["source_reset"]["observed"] == minted
        assert annotations["source_reset"]["adopted"] == tail.sequence
        assert previous_hash == tail.current_hash
        if sequence_moves:
            assert sequence == tail.sequence + 1
        else:
            assert sequence == minted

    def test_a_sibling_mint_in_flight_above_the_tail_is_left_alone(
        self, distributed_chain
    ):
        """The ledger tail lags the last mint by the entries still between
        their mint and their append. That is not a lost source."""
        tail = LedgerTail(10, "tail-hash", Path("/tmp/audit_2026-09-07.jsonl"))

        annotations, sequence, previous_hash = distributed_chain._reconcile_with_ledger(
            tail, 14, "a-peers-hash", _SEQ_KEY
        )

        assert annotations == {}
        assert sequence == 14
        assert previous_hash == "a-peers-hash"

    def test_a_missing_state_hash_is_replaced_even_above_the_tail(
        self, distributed_chain
    ):
        """GENESIS mid-chain is certainly wrong however far ahead the counter
        is, so the hash arm still fires."""
        tail = LedgerTail(10, "tail-hash", Path("/tmp/audit_2026-09-07.jsonl"))

        annotations, sequence, previous_hash = distributed_chain._reconcile_with_ledger(
            tail, 14, None, _SEQ_KEY
        )

        assert annotations["source_reset"]["reason"] == "state_hash_lost"
        assert sequence == 14
        assert previous_hash == "tail-hash"

    def test_a_fresh_ledger_is_never_a_repair(self, distributed_chain):
        annotations, sequence, previous_hash = distributed_chain._reconcile_with_ledger(
            None, 1, None, _SEQ_KEY
        )

        assert annotations == {}
        assert sequence == 1
        assert previous_hash == RedisHashChainManager.GENESIS_HASH


class TestDistributedFallbackContinuationBehavior:
    """A fallback entry continues the chain instead of forking it."""

    def test_fallback_continues_the_chain_from_the_ledger_tail(
        self, tmp_path, distributed_chain, redis_client
    ):
        path = _ledger_path(tmp_path)
        for index in range(3):
            _mint(distributed_chain, path, {"event_type": f"seed.{index}"})
        tail = LedgerTailReader(tmp_path).read()
        assert tail is not None

        redis_client.set_should_fail(True)
        entry = _mint(distributed_chain, path, {"event_type": "degraded"})

        assert entry["integrity"]["degraded"] is True
        assert entry["integrity"]["fallback_source"] == "local"
        assert entry["integrity"]["sequence"] == tail.sequence + 1
        assert entry["integrity"]["previous_hash"] == tail.current_hash

    def test_fallback_entry_verifies_instead_of_reading_as_tampered(
        self, tmp_path, distributed_chain, redis_client
    ):
        """The pre-existing fork: the stamps were added *after* the hash was
        computed, so every fallback entry verified as modified."""
        path = _ledger_path(tmp_path)
        for index in range(3):
            _mint(distributed_chain, path, {"event_type": f"seed.{index}"})
        redis_client.set_should_fail(True)
        _mint(distributed_chain, path, {"event_type": "degraded"})
        redis_client.set_should_fail(False)
        recovered = _mint(distributed_chain, path, {"event_type": "recovered"})

        assert recovered["integrity"]["source_reset"]["manager"] == "redis"
        assert recovered["integrity"]["source_reset"]["reason"] == (
            "source_behind_ledger"
        )
        assert verify_audit_log_integrity(path) == (True, [])

    def test_editing_a_fallback_stamp_on_disk_breaks_the_entry(
        self, tmp_path, distributed_chain, redis_client
    ):
        """The stamps sit under ``current_hash``, which is the whole point of
        passing them into the local manager rather than adding them after."""
        path = _ledger_path(tmp_path)
        _mint(distributed_chain, path, {"event_type": "seed"})
        redis_client.set_should_fail(True)
        _mint(distributed_chain, path, {"event_type": "degraded"})

        rows = _read_ledger(path)
        rows[-1]["integrity"]["fallback_source"] = "none"
        path.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
        )

        is_valid, issues = verify_audit_log_integrity(path)
        assert is_valid is False
        assert issues[0]["type"] == "entry_modified"


class TestDistributedExitPathBehavior:
    """Every exit of ``add_integrity``, and what each one is allowed to do."""

    def test_a_redis_answer_returns_the_entry_and_counts_a_redis_write(
        self, tmp_path, distributed_chain
    ):
        entry = distributed_chain.add_integrity({"event_type": "x"})

        assert entry["integrity"]["sequence"] == 1
        assert distributed_chain._stats["redis_writes"] == 1
        assert distributed_chain._stats["fallback_writes"] == 0

    def test_a_refused_read_is_re_raised_ahead_of_the_fallback(
        self, tmp_path, distributed_chain, ledger, redis_client
    ):
        """A source that cannot see the ledger is exactly the source that must
        not mint — routing it into the fallback would mint anyway."""
        with patch.object(ledger, "read", side_effect=OSError("disk gone")):
            with pytest.raises(HashChainSequenceRefusedError) as caught:
                distributed_chain.add_integrity({"event_type": "x"})

        assert caught.value.manager == "redis"
        assert redis_client.get(_SEQ_KEY) is None
        assert distributed_chain._stats["fallback_writes"] == 0

    def test_a_refused_read_consumes_no_sequence_from_a_live_counter(
        self, tmp_path, distributed_chain, ledger, redis_client
    ):
        redis_client.set(_SEQ_KEY, 99)

        with patch.object(ledger, "read", side_effect=OSError("disk gone")):
            with pytest.raises(HashChainSequenceRefusedError):
                distributed_chain.add_integrity({"event_type": "x"})

        assert int(redis_client.get(_SEQ_KEY)) == 99

    def test_a_lock_release_that_raises_does_not_displace_the_refusal(
        self, distributed_chain, ledger
    ):
        """Redis gone after the acquire: the release raises inside ``finally``
        and would otherwise replace the refusal with a ConnectionError, which
        the generic handler then routes into the fallback."""

        class _ReleaseRaisingLock:
            def acquire(self, blocking: bool = True) -> bool:
                return True

            def release(self) -> None:
                raise ConnectionError("redis gone")

        with (
            patch(
                "baldur.audit.integrity.redis_manager.build_chain_lock",
                autospec=True,
                return_value=_ReleaseRaisingLock(),
            ),
            patch.object(ledger, "read", side_effect=OSError("disk gone")),
        ):
            with pytest.raises(HashChainSequenceRefusedError):
                distributed_chain.add_integrity({"event_type": "x"})

    def test_a_refusal_raised_inside_the_fallback_propagates_and_counts_nothing(
        self, tmp_path, distributed_chain, ledger, redis_client
    ):
        """The fallback's own guard ran and refused; nothing was written, so
        nothing may be counted as a fallback write."""
        redis_client.set_should_fail(True)

        with patch.object(ledger, "read", side_effect=OSError("disk gone")):
            with pytest.raises(HashChainSequenceRefusedError) as caught:
                distributed_chain.add_integrity({"event_type": "x"})

        assert caught.value.manager == "local"
        assert distributed_chain._stats["fallback_writes"] == 0

    def test_a_failed_lock_acquisition_falls_back_and_counts_a_lock_failure(
        self, tmp_path, distributed_chain
    ):
        class _UnacquirableLock:
            def acquire(self, blocking: bool = True) -> bool:
                return False

            def release(self) -> None:  # pragma: no cover - never reached
                raise AssertionError("released a lock that was never acquired")

        with patch(
            "baldur.audit.integrity.redis_manager.build_chain_lock",
            autospec=True,
            return_value=_UnacquirableLock(),
        ):
            entry = distributed_chain.add_integrity({"event_type": "x"})

        assert entry["integrity"]["degraded"] is True
        assert distributed_chain._stats["lock_failures"] == 1
        assert distributed_chain._stats["fallback_writes"] == 1

    def test_a_manager_without_a_fallback_still_marks_the_entry_degraded(
        self, redis_client, ledger
    ):
        redis_client.set_should_fail(True)
        manager = RedisHashChainManager(
            redis_client=redis_client, key_prefix=_PREFIX, ledger=ledger
        )

        entry = manager.add_integrity({"event_type": "x"})

        assert entry["integrity"]["sequence"] == -1
        assert entry["integrity"]["fallback_source"] == "none"


class TestPosturePublicationBehavior:
    """The gauge answers "is this chain distributed right now", not "what did
    the admission probe see at construction"."""

    def test_the_first_successful_write_publishes_a_healthy_gauge(
        self, distributed_chain
    ):
        """``_published_degraded`` starts at ``None``, so the first write
        always publishes — that is what turns a construction-time ``1`` into
        ``0`` once Redis answers."""
        with patch(
            "baldur.metrics.audit_backend_metrics.set_audit_distributed_chain_degraded",
            autospec=True,
        ) as gauge:
            distributed_chain.add_integrity({"event_type": "x"})

        gauge.assert_called_once_with(False)

    def test_the_gauge_follows_the_posture_in_both_directions(
        self, distributed_chain, redis_client
    ):
        with patch(
            "baldur.metrics.audit_backend_metrics.set_audit_distributed_chain_degraded",
            autospec=True,
        ) as gauge:
            distributed_chain.add_integrity({"event_type": "healthy"})
            redis_client.set_should_fail(True)
            distributed_chain.add_integrity({"event_type": "degraded"})
            redis_client.set_should_fail(False)
            distributed_chain.add_integrity({"event_type": "recovered"})

        assert [call.args[0] for call in gauge.call_args_list] == [False, True, False]

    def test_an_unchanged_posture_does_not_re_publish_the_gauge(
        self, distributed_chain, redis_client
    ):
        redis_client.set_should_fail(True)

        with patch(
            "baldur.metrics.audit_backend_metrics.set_audit_distributed_chain_degraded",
            autospec=True,
        ) as gauge:
            for _ in range(5):
                distributed_chain.add_integrity({"event_type": "degraded"})

        gauge.assert_called_once_with(True)

    def test_a_raising_gauge_publisher_does_not_cost_the_write(self, distributed_chain):
        """Fail-open — the metric helper is never on the write's critical
        path, prometheus_client absent or not."""
        with patch(
            "baldur.metrics.audit_backend_metrics.set_audit_distributed_chain_degraded",
            autospec=True,
            side_effect=RuntimeError("registry gone"),
        ):
            entry = distributed_chain.add_integrity({"event_type": "x"})

        assert entry["integrity"]["sequence"] == 1

    def test_a_healthy_process_never_logs_a_restore_it_did_not_earn(
        self, distributed_chain
    ):
        """``redis_restored`` after the first healthy write of a process would
        read as an incident that never happened."""
        with capture_logs() as logs:
            distributed_chain.add_integrity({"event_type": "x"})

        assert log_events(logs, "redis_hash_chain.redis_restored") == []
        assert log_events(logs, "redis_hash_chain.fallback_entered") == []


class TestFallbackCountingBehavior:
    """The counter and ``_stats`` count the same thing: entries that exist."""

    def test_the_metric_counts_one_per_fallback_entry(
        self, tmp_path, distributed_chain, redis_client
    ):
        redis_client.set_should_fail(True)

        with patch(
            "baldur.metrics.audit_backend_metrics."
            "increment_audit_hash_chain_fallback_write",
            autospec=True,
        ) as increment:
            for _ in range(4):
                distributed_chain.add_integrity({"event_type": "degraded"})

        assert increment.call_count == 4
        assert distributed_chain._stats["fallback_writes"] == 4

    def test_a_refusing_fallback_counts_nothing(
        self, distributed_chain, ledger, redis_client
    ):
        redis_client.set_should_fail(True)

        with patch(
            "baldur.metrics.audit_backend_metrics."
            "increment_audit_hash_chain_fallback_write",
            autospec=True,
        ) as increment:
            with patch.object(ledger, "read", side_effect=OSError("disk gone")):
                with pytest.raises(HashChainSequenceRefusedError):
                    distributed_chain.add_integrity({"event_type": "x"})

        increment.assert_not_called()
        assert distributed_chain._stats["fallback_writes"] == 0

    def test_a_raising_counter_helper_does_not_cost_the_entry(
        self, distributed_chain, redis_client
    ):
        redis_client.set_should_fail(True)

        with patch(
            "baldur.metrics.audit_backend_metrics."
            "increment_audit_hash_chain_fallback_write",
            autospec=True,
            side_effect=RuntimeError("registry gone"),
        ):
            entry = distributed_chain.add_integrity({"event_type": "x"})

        assert entry["integrity"]["degraded"] is True
        assert distributed_chain._stats["fallback_writes"] == 1


class TestSustainedOutageLogVolumeBehavior:
    """Log volume scales with the episode, not with the audit rate."""

    def test_a_sustained_outage_announces_itself_once_not_once_per_entry(
        self, tmp_path, distributed_chain, redis_client
    ):
        # Given: a ledger already holding Redis-served entries, so the
        # fallback manager has a tail to adopt on its first degraded write.
        path = _ledger_path(tmp_path)
        for index in range(3):
            _mint(distributed_chain, path, {"event_type": f"seed.{index}"})
        redis_client.set_should_fail(True)

        # When
        with capture_logs() as logs:
            for _ in range(50):
                _mint(distributed_chain, path, {"event_type": "degraded"})

        # Then
        assert len(_operator_records(logs, "redis_hash_chain.fallback_entered")) == 1
        assert len(_operator_records(logs, "hash_chain.sequence_source_reset")) == 1
        assert (
            _operator_records(logs, "redis_hash_chain.redis_failed_using_fallback")
            == []
        )
        assert distributed_chain._stats["fallback_writes"] == 50

    def test_a_sustained_outage_on_an_empty_ledger_adopts_nothing(
        self, tmp_path, distributed_chain, redis_client
    ):
        """Nothing to adopt is not a repair — a fresh install's outage must
        not look like a source that lost its state."""
        path = _ledger_path(tmp_path)
        redis_client.set_should_fail(True)

        with capture_logs() as logs:
            for _ in range(50):
                _mint(distributed_chain, path, {"event_type": "degraded"})

        assert len(_operator_records(logs, "redis_hash_chain.fallback_entered")) == 1
        assert _operator_records(logs, "hash_chain.sequence_source_reset") == []

    def test_the_recovery_after_a_sustained_outage_is_announced_once(
        self, tmp_path, distributed_chain, redis_client
    ):
        path = _ledger_path(tmp_path)
        for index in range(3):
            _mint(distributed_chain, path, {"event_type": f"seed.{index}"})
        redis_client.set_should_fail(True)
        for _ in range(50):
            _mint(distributed_chain, path, {"event_type": "degraded"})
        redis_client.set_should_fail(False)

        with capture_logs() as logs:
            for _ in range(10):
                _mint(distributed_chain, path, {"event_type": "recovered"})

        restored = log_events(logs, "redis_hash_chain.redis_restored")
        assert len(restored) == 1
        assert restored[0]["log_level"] == "info"
        assert len(_operator_records(logs, "hash_chain.sequence_source_reset")) == 1
        assert verify_audit_log_integrity(path) == (True, [])
