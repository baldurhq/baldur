"""Unit tests for ``HashChainFileAuditLogAdapter`` (#416 Part 6, D6/D22/D23).

Covers:
- D6 dict-schema preservation: the on-disk row matches the previous
  ``LocalFileBackend.write()`` shape (top-level ``timestamp``,
  ``actor``, ``change``, ``metadata`` keys + nested ``integrity``).
- Hash chain continuity across restart instances (state file persists
  ``sequence`` and ``previous_hash`` so the chain resumes).
- D23 partition behavior: empty partition preserves the legacy
  ``audit_{date}.jsonl`` filename and ``.hash_chain_state.json`` state
  file. Non-empty partition splits both filenames so two writers can
  share the same ``log_dir`` without contention.
- ``query()`` against the dict schema: rows are mapped back to H1
  ``AuditEntry`` (D19-A field map).
- ``verify_integrity()`` returns ``(True, [])`` for an unaltered chain
  and ``(False, issues)`` after tampering.
- ``distributed_hash_chain=True`` with no Redis client falls back to
  the local ``HashChainManager`` (warning emitted).
- File-lock setting flows from constructor → ``HashChainManager``.

Reference: 416
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.audit.hashchain_adapter import (
    HashChainFileAuditLogAdapter,
)
from baldur.audit.integrity import HashChainManager
from baldur.interfaces.audit_adapter import (
    AuditAction,
    AuditEntry,
    AuditLogAdapter,
)
from tests.factories import MockRedisClient
from tests.factories.writable_dir import log_events

# =============================================================================
# Helpers
# =============================================================================


def _read_rows(log_dir: Path, glob: str = "audit_*.jsonl") -> list[dict]:
    """Read all JSONL rows from the audit dir for assertions."""
    rows: list[dict] = []
    for f in sorted(log_dir.glob(glob)):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def _make_config_change_entry(target_id: str = "max_retries") -> AuditEntry:
    return AuditEntry(
        action=AuditAction.CONFIG_CHANGE,
        target_type="RETRY_CONFIG",
        target_id=target_id,
        actor_id="alice",
        reason="Tuning",
        details={
            "old_value": {"v": 3},
            "new_value": {"v": 5},
            "source": "api",
        },
    )


# =============================================================================
# Contract — interface + dict schema fields
# =============================================================================


class TestHashChainFileAuditLogAdapterContract:
    """Hardcoded design-doc value checks (D6 schema, D23 filenames)."""

    def test_implements_audit_log_adapter_interface(self):
        """The adapter must satisfy the H1 ``AuditLogAdapter`` ABC."""
        assert issubclass(HashChainFileAuditLogAdapter, AuditLogAdapter)

    def test_default_log_dir_constant(self):
        """The published default log dir is ``logs/audit`` (D11 OSS-safe)."""
        assert HashChainFileAuditLogAdapter.DEFAULT_LOG_DIR == "logs/audit"

    def test_d6_on_disk_dict_schema_top_level_keys(self, tmp_path):
        """A logged entry must serialize to the legacy H2 dict schema:
        ``timestamp``, ``event_type=config_change``, ``actor``, ``change``."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path), enable_hash_chain=False, use_file_lock=False
        )
        try:
            adapter.log(_make_config_change_entry())
        finally:
            adapter.close()

        rows = _read_rows(tmp_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["event_type"] == "config_change"
        assert "timestamp" in row
        assert row["actor"]["user"] == "alice"
        assert row["change"]["config_type"] == "RETRY_CONFIG"
        assert row["change"]["config_key"] == "max_retries"
        assert row["change"]["action"] == AuditAction.CONFIG_CHANGE.value
        assert row["change"]["old_value"] == {"v": 3}
        assert row["change"]["new_value"] == {"v": 5}
        assert row["change"]["reason"] == "Tuning"

    def test_d23_empty_partition_uses_legacy_filename(self, tmp_path):
        """Empty partition produces ``audit_{date}.jsonl`` and
        ``.hash_chain_state.json`` (no partition suffix)."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            partition="",
            enable_hash_chain=True,
            use_file_lock=False,
        )
        try:
            adapter.log(_make_config_change_entry())
        finally:
            adapter.close()

        files = sorted(p.name for p in tmp_path.iterdir())
        # Exactly one audit_*.jsonl file and exactly one state file.
        assert any(f.startswith("audit_") and f.endswith(".jsonl") for f in files)
        # The legacy filename has no second underscore before .jsonl.
        for f in files:
            if f.startswith("audit_") and f.endswith(".jsonl"):
                assert "_.jsonl" not in f
        assert (tmp_path / ".hash_chain_state.json").exists()

    def test_d23_partition_splits_filename_and_state(self, tmp_path):
        """Non-empty partition produces ``audit_{date}_{partition}.jsonl``
        and ``.hash_chain_state.{partition}.json``."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            partition="web",
            enable_hash_chain=True,
            use_file_lock=False,
        )
        try:
            adapter.log(_make_config_change_entry())
        finally:
            adapter.close()

        names = sorted(p.name for p in tmp_path.iterdir())
        # The audit filename embeds the partition.
        assert any(n.startswith("audit_") and n.endswith("_web.jsonl") for n in names)
        # The state filename embeds the partition.
        assert (tmp_path / ".hash_chain_state.web.json").exists()


# =============================================================================
# Behavior — write path, query, verify, hash chain restart
# =============================================================================


class TestHashChainFileAuditLogAdapterBehavior:
    """Behavior tests using the real adapter and on-disk verification."""

    def test_log_then_query_round_trips_h1_entry(self, tmp_path):
        """``query()`` reconstructs the H1 ``AuditEntry`` (D19-A mapping)."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path), enable_hash_chain=False, use_file_lock=False
        )
        try:
            adapter.log(_make_config_change_entry("k1"))
            adapter.log(_make_config_change_entry("k2"))
            results = adapter.query()
        finally:
            adapter.close()

        assert len(results) == 2
        for entry in results:
            assert isinstance(entry, AuditEntry)
            assert entry.action == AuditAction.CONFIG_CHANGE
            assert entry.target_type == "RETRY_CONFIG"
            assert entry.actor_id == "alice"
            assert entry.reason == "Tuning"
            # Round-trip places old/new value back into details.
            assert entry.details["old_value"] == {"v": 3}
            assert entry.details["new_value"] == {"v": 5}
            assert entry.details["source"] == "api"

    def test_query_filter_by_target_id(self, tmp_path):
        """``query(target_id=...)`` filters at the row level."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path), enable_hash_chain=False, use_file_lock=False
        )
        try:
            adapter.log(_make_config_change_entry("k1"))
            adapter.log(_make_config_change_entry("k2"))
            adapter.log(_make_config_change_entry("k3"))
            results = adapter.query(target_id="k2")
        finally:
            adapter.close()

        assert len(results) == 1
        assert results[0].target_id == "k2"

    def test_query_filter_by_action_string(self, tmp_path):
        """Action filter accepts both enum and string variants."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path), enable_hash_chain=False, use_file_lock=False
        )
        try:
            adapter.log(_make_config_change_entry("k1"))
            results_str = adapter.query(action="config_change")
            results_enum = adapter.query(action=AuditAction.CONFIG_CHANGE)
        finally:
            adapter.close()

        assert len(results_str) == 1
        assert len(results_enum) == 1

    def test_query_limit_caps_results(self, tmp_path):
        """``limit`` caps the number of returned entries."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path), enable_hash_chain=False, use_file_lock=False
        )
        try:
            for i in range(5):
                adapter.log(_make_config_change_entry(f"k{i}"))
            results = adapter.query(limit=3)
        finally:
            adapter.close()

        assert len(results) == 3

    def test_hash_chain_continuity_across_restart(self, tmp_path):
        """Restart simulation: a fresh instance picks up the saved
        ``sequence`` and ``previous_hash`` from the state file and
        continues the chain so ``verify_integrity()`` stays True (D6)."""
        # Writer 1 — first batch
        a1 = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path), enable_hash_chain=True, use_file_lock=False
        )
        try:
            for i in range(5):
                a1.log(_make_config_change_entry(f"k{i}"))
        finally:
            a1.close()

        ok1, issues1 = a1.verify_integrity()
        assert ok1, f"First-half chain should verify cleanly: {issues1}"

        # Writer 2 — second batch (new instance, same dir → resumes chain)
        a2 = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path), enable_hash_chain=True, use_file_lock=False
        )
        try:
            for i in range(5, 10):
                a2.log(_make_config_change_entry(f"k{i}"))
        finally:
            a2.close()

        ok2, issues2 = a2.verify_integrity()
        assert ok2, f"Restart-continued chain should verify cleanly: {issues2}"

        # Final sequence in the state file is 10 (5 + 5).
        state = json.loads((tmp_path / ".hash_chain_state.json").read_text())
        assert state["sequence"] == 10

    def test_verify_integrity_detects_tamper(self, tmp_path):
        """Editing the JSONL file invalidates ``verify_integrity()``."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path), enable_hash_chain=True, use_file_lock=False
        )
        try:
            for i in range(3):
                adapter.log(_make_config_change_entry(f"k{i}"))
        finally:
            adapter.close()

        # Tamper: rewrite one row's actor.user.
        log_files = list(tmp_path.glob("audit_*.jsonl"))
        assert log_files
        target = log_files[0]
        original = target.read_text(encoding="utf-8").splitlines()
        tampered = []
        for idx, line in enumerate(original):
            row = json.loads(line)
            if idx == 1:
                row["actor"]["user"] = "evil-mallory"
            tampered.append(json.dumps(row))
        target.write_text("\n".join(tampered) + "\n", encoding="utf-8")

        ok, issues = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path), enable_hash_chain=True, use_file_lock=False
        ).verify_integrity()
        assert ok is False
        assert issues  # at least one issue reported

    def test_partitions_coexist_with_independent_state_files(self, tmp_path):
        """Two partitions sharing the same ``log_dir`` keep independent
        chains and independent files (D23)."""
        web = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            partition="web",
            enable_hash_chain=True,
            use_file_lock=False,
        )
        celery = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            partition="celery",
            enable_hash_chain=True,
            use_file_lock=False,
        )
        try:
            for i in range(3):
                web.log(_make_config_change_entry(f"w{i}"))
                celery.log(_make_config_change_entry(f"c{i}"))
        finally:
            web.close()
            celery.close()

        # Independent state files.
        assert (tmp_path / ".hash_chain_state.web.json").exists()
        assert (tmp_path / ".hash_chain_state.celery.json").exists()

        # Each chain verifies independently.
        ok_web, _ = web.verify_integrity()
        ok_celery, _ = celery.verify_integrity()
        assert ok_web
        assert ok_celery

        # Sequences advance independently — each chain wrote 3 entries.
        web_state = json.loads((tmp_path / ".hash_chain_state.web.json").read_text())
        celery_state = json.loads(
            (tmp_path / ".hash_chain_state.celery.json").read_text()
        )
        assert web_state["sequence"] == 3
        assert celery_state["sequence"] == 3

    def test_query_for_partition_only_matches_partition_files(self, tmp_path):
        """The ``web`` adapter's ``query()`` ignores ``celery`` files."""
        web = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            partition="web",
            enable_hash_chain=False,
            use_file_lock=False,
        )
        celery = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            partition="celery",
            enable_hash_chain=False,
            use_file_lock=False,
        )
        try:
            web.log(_make_config_change_entry("w1"))
            celery.log(_make_config_change_entry("c1"))
            web_results = web.query()
            celery_results = celery.query()
        finally:
            web.close()
            celery.close()

        assert len(web_results) == 1
        assert web_results[0].target_id == "w1"
        assert len(celery_results) == 1
        assert celery_results[0].target_id == "c1"

    def test_partition_field_embedded_in_entry_when_set(self, tmp_path):
        """Non-empty partition adds a ``partition`` top-level key."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            partition="celery",
            enable_hash_chain=False,
            use_file_lock=False,
        )
        try:
            adapter.log(_make_config_change_entry("k1"))
        finally:
            adapter.close()

        rows = _read_rows(tmp_path, glob="audit_*_celery.jsonl")
        assert rows
        assert rows[0]["partition"] == "celery"

    def test_partition_field_absent_when_empty(self, tmp_path):
        """Empty partition omits the ``partition`` key (legacy parity)."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            partition="",
            enable_hash_chain=False,
            use_file_lock=False,
        )
        try:
            adapter.log(_make_config_change_entry("k1"))
        finally:
            adapter.close()

        rows = _read_rows(tmp_path)
        assert rows
        assert "partition" not in rows[0]


# =============================================================================
# Side effects — masking, file lock propagation, distributed fallback
# =============================================================================


class TestHashChainFileAuditLogAdapterSideEffects:
    """Verifies external side effects beyond return values."""

    def test_ip_address_is_masked_in_persisted_dict(self, tmp_path):
        """GDPR/CCPA: ``ip_address`` is masked before being written."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            enable_hash_chain=False,
            use_file_lock=False,
            mask_ip_addresses=True,
        )
        try:
            adapter.log(
                AuditEntry(
                    action=AuditAction.CONFIG_CHANGE,
                    target_type="AUTH",
                    target_id="login",
                    actor_id="alice",
                    details={"ip_address": "10.20.30.40"},
                )
            )
        finally:
            adapter.close()

        rows = _read_rows(tmp_path)
        assert rows
        masked = rows[0]["actor"]["ip_address"]
        # Octets after the second one are masked.
        assert masked.startswith("10.20.")
        assert "***" in masked

    def test_sensitive_field_masked_in_old_new_value(self, tmp_path):
        """Default sensitive_fields list redacts password/token/etc."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            enable_hash_chain=False,
            use_file_lock=False,
        )
        try:
            adapter.log(
                AuditEntry(
                    action=AuditAction.CONFIG_CHANGE,
                    target_type="AUTH_CONFIG",
                    target_id="creds",
                    actor_id="alice",
                    details={
                        "old_value": {"username": "u1", "password": "old_pw"},
                        "new_value": {"username": "u1", "password": "new_pw"},
                    },
                )
            )
        finally:
            adapter.close()

        rows = _read_rows(tmp_path)
        assert rows[0]["change"]["old_value"]["password"] != "old_pw"
        assert rows[0]["change"]["new_value"]["password"] != "new_pw"
        # Username left intact.
        assert rows[0]["change"]["old_value"]["username"] == "u1"

    def test_use_file_lock_flag_propagates_to_hash_chain_manager(self, tmp_path):
        """Constructor ``use_file_lock`` is forwarded to ``HashChainManager``."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            enable_hash_chain=True,
            use_file_lock=True,
        )
        try:
            assert isinstance(adapter._hash_chain, HashChainManager)
            assert adapter._hash_chain._use_file_lock is True
        finally:
            adapter.close()

    def test_use_file_lock_false_disables_lock_path(self, tmp_path):
        """``use_file_lock=False`` reaches the manager unchanged."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            enable_hash_chain=True,
            use_file_lock=False,
        )
        try:
            assert adapter._hash_chain._use_file_lock is False
        finally:
            adapter.close()

    def test_distributed_mode_without_redis_falls_back_to_local(self, tmp_path):
        """``distributed_hash_chain=True`` with ``redis_client=None`` falls
        back to a plain local ``HashChainManager`` (warning logged)."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            enable_hash_chain=True,
            distributed_hash_chain=True,
            redis_client=None,
            use_file_lock=False,
        )
        try:
            # Falls back to local manager.
            assert isinstance(adapter._hash_chain, HashChainManager)
        finally:
            adapter.close()

    def test_close_persists_state_file(self, tmp_path):
        """``close()`` flushes the hash chain state to disk."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            enable_hash_chain=True,
            use_file_lock=False,
        )
        adapter.log(_make_config_change_entry("k1"))
        adapter.close()
        # State file written and parsable.
        state_file = tmp_path / ".hash_chain_state.json"
        assert state_file.exists()
        state = json.loads(state_file.read_text())
        assert state["sequence"] >= 1
        assert "previous_hash" in state


# =============================================================================
# Edge cases — empty / missing dir / corrupt rows
# =============================================================================


class TestHashChainFileAuditLogAdapterEdgeCases:
    """Edge case handling."""

    def test_query_empty_dir_returns_empty(self, tmp_path):
        """No log files → empty query result."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            enable_hash_chain=False,
            use_file_lock=False,
        )
        try:
            assert adapter.query() == []
        finally:
            adapter.close()

    def test_corrupt_row_is_skipped_during_query(self, tmp_path):
        """A non-JSON line is skipped, not raised."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            enable_hash_chain=False,
            use_file_lock=False,
        )
        try:
            adapter.log(_make_config_change_entry("k1"))
            # Append a corrupt line directly.
            files = list(tmp_path.glob("audit_*.jsonl"))
            with open(files[0], "a", encoding="utf-8") as f:
                f.write("not-json-at-all\n")
            results = adapter.query()
        finally:
            adapter.close()

        # Only the valid row survives.
        assert len(results) == 1
        assert results[0].target_id == "k1"

    def test_log_dir_created_on_init(self, tmp_path):
        """Constructor creates the directory tree (parents=True)."""
        nested = tmp_path / "deeply" / "nested" / "audit"
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(nested),
            enable_hash_chain=False,
            use_file_lock=False,
        )
        try:
            assert nested.exists()
            assert nested.is_dir()
        finally:
            adapter.close()


# =============================================================================
# D5 — log() re-raises on write failure (669)
# =============================================================================


class TestHashChainLogReraise:
    """669 D5: a write failure must be **re-raised**, not swallowed.

    Pre-fix, ``log()`` caught and logged the failure without re-raising, so a
    failed central write looked like success to the recovery-replay sync
    worker — which then advanced its cursor and deleted the WAL entry (silent
    audit loss for a compliance-grade store). D5 re-raises after logging so the
    worker holds the cursor and preserves the WAL (668-D1 zero-loss).
    """

    def _adapter(self, tmp_path: Path) -> HashChainFileAuditLogAdapter:
        return HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            enable_hash_chain=False,
            use_file_lock=False,
        )

    def test_write_dict_failure_is_reraised(self, tmp_path):
        """A failure in the on-disk write (``_write_dict``) propagates out of
        ``log()`` rather than being swallowed."""
        adapter = self._adapter(tmp_path)
        try:
            with patch.object(
                adapter, "_write_dict", side_effect=RuntimeError("simulated disk full")
            ):
                with pytest.raises(RuntimeError, match="simulated disk full"):
                    adapter.log(_make_config_change_entry())
        finally:
            adapter.close()

    def test_entry_to_event_dict_failure_is_reraised(self, tmp_path):
        """A failure while shaping the entry (``_entry_to_event_dict``) also
        propagates — any failure in the write path surfaces, not only I/O."""
        adapter = self._adapter(tmp_path)
        try:
            with patch.object(
                adapter, "_entry_to_event_dict", side_effect=ValueError("bad entry")
            ):
                with pytest.raises(ValueError, match="bad entry"):
                    adapter.log(_make_config_change_entry())
        finally:
            adapter.close()

    def test_file_open_failure_is_reraised_not_swallowed(self, tmp_path):
        """A file-open failure (permission denied, ENOSPC, fd exhaustion)
        surfaces out of ``log()`` instead of being silently swallowed.

        Regression: ``_ensure_file_open()`` catches the open error and returns
        ``False``, and ``_write_dict`` used to ``return`` on that — so ``log()``
        saw no exception and looked like a SUCCESS to the recovery-replay sync
        worker, which then advanced its cursor and deleted the WAL entry
        (silent audit loss for a compliance store). The write must raise.
        """
        adapter = self._adapter(tmp_path)
        try:
            with patch.object(adapter, "_ensure_file_open", return_value=False):
                with pytest.raises(OSError):
                    adapter.log(_make_config_change_entry())
        finally:
            adapter.close()

    def test_log_failed_is_emitted_then_reraised(self, tmp_path):
        """State transition on failure: ``hash_chain_file_audit.log_failed`` is
        logged AND the exception is re-raised (the operator sees it AND the
        caller can distinguish a lost write from a real one)."""
        adapter = self._adapter(tmp_path)
        try:
            with (
                patch.object(adapter, "_write_dict", side_effect=RuntimeError("boom")),
                patch("baldur.adapters.audit.hashchain_adapter.logger") as mock_logger,
            ):
                with pytest.raises(RuntimeError):
                    adapter.log(_make_config_change_entry())

            events = [c.args[0] for c in mock_logger.exception.call_args_list if c.args]
            assert "hash_chain_file_audit.log_failed" in events
        finally:
            adapter.close()

    def test_healthy_write_does_not_raise(self, tmp_path):
        """Negative case: a successful write is silent (no raise) and persists
        the row — the re-raise only fires on failure."""
        adapter = self._adapter(tmp_path)
        try:
            adapter.log(_make_config_change_entry("healthy"))
        finally:
            adapter.close()

        rows = _read_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["change"]["config_key"] == "healthy"


# =============================================================================
# Open-failure recovery
# =============================================================================


class TestOpenFailureRecoveryBehavior:
    """A transient open failure must not silence the adapter permanently.

    The rotation branch records the file it is switching to. Recording it
    before the open succeeded made a failed open indistinguishable from an
    open one: the next call saw the target already recorded, skipped the
    branch, and reported success while holding no handle, so every later
    write died on the handle assertion for the life of the process.
    """

    def _adapter(self, tmp_path):
        return HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path), enable_hash_chain=False, use_file_lock=False
        )

    def test_a_failed_open_is_not_reported_as_an_open_file(self, tmp_path):
        adapter = self._adapter(tmp_path)
        try:
            with patch("builtins.open", side_effect=OSError("no space left on device")):
                assert adapter._ensure_file_open() is False
                assert adapter._file_handle is None
                # Still false on the retry: the failure is the condition, not a
                # one-shot that flips the adapter into a phantom-open state.
                assert adapter._ensure_file_open() is False
        finally:
            adapter.close()

    def test_writes_resume_once_the_condition_clears(self, tmp_path):
        adapter = self._adapter(tmp_path)
        try:
            with patch("builtins.open", side_effect=OSError("too many open files")):
                try:
                    adapter.log(_make_config_change_entry("during_outage"))
                except Exception:
                    pass
            adapter.log(_make_config_change_entry("after_recovery"))
        finally:
            adapter.close()

        rows = _read_rows(tmp_path)
        assert len(rows) == 1, f"the post-recovery write must land; got {rows}"

    def test_a_missing_log_directory_is_recreated_on_the_next_write(self, tmp_path):
        log_dir = tmp_path / "audit"
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(log_dir), enable_hash_chain=False, use_file_lock=False
        )
        try:
            adapter.log(_make_config_change_entry("before"))
            adapter._close_file()
            for f in log_dir.iterdir():
                f.unlink()
            log_dir.rmdir()
            adapter._current_file = None

            adapter.log(_make_config_change_entry("after"))
        finally:
            adapter.close()

        assert log_dir.is_dir()
        assert len(_read_rows(log_dir)) == 1


class TestHashChainAdapterPropertiesContract:
    """The construction facts the boot-time reconciliation reads back.

    A startup step that reconciles the Redis chain against the files needs
    three things the adapter alone knows: the directory it *resolved* to (the
    factory routes the requested one through the writable-directory resolver,
    so settings can name a different path), the chain manager it actually
    built (a settings flag can read ``True`` on a process that fell back), and
    the bare Redis key root its ``PendingSequenceManager`` was built with (the
    chain manager's own prefix adds a partition segment the pending namespace
    does not). Re-deriving any of the three is how a reconciliation ends up
    reading a key the writer never writes.
    """

    def test_log_dir_reports_the_directory_actually_written_to(self, tmp_path):
        resolved = tmp_path / "resolved-elsewhere"
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(resolved),
            enable_anchor_backup=False,
        )

        assert adapter.log_dir == resolved

    def test_hash_chain_manager_is_the_local_one_when_nothing_is_distributed(
        self, tmp_path
    ):
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path / "audit"),
            enable_anchor_backup=False,
        )

        assert isinstance(adapter.hash_chain_manager, HashChainManager)

    def test_hash_chain_manager_is_none_when_the_chain_is_disabled(self, tmp_path):
        """``None`` is a third state the reconciliation gate has to absorb —
        not every audit adapter carries a chain."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path / "audit"),
            enable_hash_chain=False,
            enable_anchor_backup=False,
        )

        assert adapter.hash_chain_manager is None

    def test_redis_key_prefix_returns_the_constructor_argument(self, tmp_path):
        """A non-default root: an installation that renamed its Redis
        namespace must not have the reconciliation guess the shipped one."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path / "audit"),
            redis_key_prefix="acme:",
            enable_anchor_backup=False,
        )

        assert adapter.redis_key_prefix == "acme:"

    def test_redis_key_prefix_defaults_to_the_shipped_root(self, tmp_path):
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path / "audit"),
            enable_anchor_backup=False,
        )

        assert adapter.redis_key_prefix == "baldur:"

    @pytest.mark.parametrize(
        "name",
        ["log_dir", "hash_chain_manager", "redis_key_prefix"],
    )
    def test_the_construction_facts_are_read_only(self, tmp_path, name):
        """They describe what was built, not what is wanted — a writable
        attribute would let a caller move the reconciliation off the objects
        that actually wrote the keys."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path / "audit"),
            enable_anchor_backup=False,
        )

        with pytest.raises(AttributeError):
            setattr(adapter, name, "anything")


# =============================================================================
# The ledger the adapter owns — who reads it, and exactly which files
# =============================================================================


def _ledger_line(tmp_path: Path, target_id: str) -> str:
    """Produce one genuine adapter row, for placing under a chosen filename."""
    scratch = tmp_path / f"seed-{target_id}"
    seed = HashChainFileAuditLogAdapter(
        log_dir=str(scratch), enable_anchor_backup=False
    )
    seed.log(_make_config_change_entry(target_id))
    seed.close()
    lines = [
        line
        for path in sorted(scratch.glob("audit_*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return lines[0]


def _place_ledger(log_dir: Path, filename: str, line: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / filename
    path.write_text(line + "\n", encoding="utf-8")
    return path


class TestAdapterLedgerWiringContract:
    """Only the adapter knows which files it writes, so every manager it
    builds holds the adapter's own reader.

    A chain manager that cannot see the ledger mints from a source that may
    have lost its state — which is the whole condition this wiring closes.
    """

    def test_the_local_manager_holds_the_adapters_reader(self, tmp_path):
        adapter = HashChainFileAuditLogAdapter(log_dir=str(tmp_path))

        assert adapter.hash_chain_manager._ledger is adapter.ledger_tail_reader

    def test_the_distributed_manager_holds_the_adapters_reader(self, tmp_path):
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            distributed_hash_chain=True,
            redis_client=MockRedisClient(),
        )

        assert adapter.hash_chain_manager._ledger is adapter.ledger_tail_reader

    def test_the_distributed_fallback_holds_the_same_reader(self, tmp_path):
        """The fallback appends to the same ledger — that is what makes a
        fallback entry continue the chain instead of carrying a local-only
        number that collides with the Redis-minted ones."""
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            distributed_hash_chain=True,
            redis_client=MockRedisClient(),
        )

        fallback = adapter.hash_chain_manager._fallback

        assert fallback is not None
        assert fallback._ledger is adapter.ledger_tail_reader

    def test_the_reader_is_built_from_this_adapters_own_construction_facts(
        self, tmp_path
    ):
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path), partition="worker", rotate_daily=False
        )

        reader = adapter.ledger_tail_reader

        assert reader.log_dir == adapter.log_dir
        assert reader.filename_pattern == "audit_{date}_worker.jsonl"
        assert reader.rotate_daily is False
        assert reader.filename_regex.fullmatch("audit_all_worker.jsonl")

    def test_the_exposed_filename_shape_is_the_readers_own(self, tmp_path):
        adapter = HashChainFileAuditLogAdapter(log_dir=str(tmp_path))

        assert (
            adapter.ledger_filename_regex is adapter.ledger_tail_reader.filename_regex
        )


class TestAdapterExactFileSelectionBehavior:
    """``query()`` and ``verify_integrity()`` walk the adapter's exact files.

    A glob over ``audit_*.jsonl`` also matches every partitioned sibling, and
    a partition ``worker`` also matches ``celery_worker`` — so a default
    adapter used to answer with another writer's rows and verify another
    writer's chain.
    """

    def test_the_default_partition_ignores_partitioned_siblings(self, tmp_path):
        log_dir = tmp_path / "audit"
        _place_ledger(log_dir, "audit_2026-09-07.jsonl", _ledger_line(tmp_path, "mine"))
        _place_ledger(
            log_dir, "audit_2026-09-07_worker.jsonl", _ledger_line(tmp_path, "theirs")
        )
        adapter = HashChainFileAuditLogAdapter(log_dir=str(log_dir))

        results = adapter.query()

        assert [entry.target_id for entry in results] == ["mine"]

    def test_a_partition_named_worker_ignores_celery_worker(self, tmp_path):
        """Substring matching is what made these two the same partition."""
        log_dir = tmp_path / "audit"
        _place_ledger(
            log_dir, "audit_2026-09-07_worker.jsonl", _ledger_line(tmp_path, "mine")
        )
        _place_ledger(
            log_dir,
            "audit_2026-09-07_celery_worker.jsonl",
            _ledger_line(tmp_path, "theirs"),
        )
        adapter = HashChainFileAuditLogAdapter(log_dir=str(log_dir), partition="worker")

        results = adapter.query()

        assert [entry.target_id for entry in results] == ["mine"]

    def test_an_operator_filename_pattern_override_is_honored(self, tmp_path):
        """The override used to be ignored entirely: the walk was hardcoded to
        the default glob, so an operator's own naming read as an empty ledger
        and the source had nothing to compare against."""
        log_dir = tmp_path / "audit"
        _place_ledger(
            log_dir, "ledger_2026-09-07.ndjson", _ledger_line(tmp_path, "mine")
        )
        _place_ledger(
            log_dir, "audit_2026-09-07.jsonl", _ledger_line(tmp_path, "theirs")
        )
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(log_dir), filename_pattern="ledger_{date}.ndjson"
        )

        results = adapter.query()

        assert [entry.target_id for entry in results] == ["mine"]

    def test_an_unreadable_dir_yields_no_rows_and_says_so(self, tmp_path):
        """An empty answer with no record is indistinguishable from an empty
        ledger, which is what a swallowed directory error produced."""
        adapter = HashChainFileAuditLogAdapter(log_dir=str(tmp_path))
        adapter.log(_make_config_change_entry())

        with (
            patch.object(
                Path, "iterdir", autospec=True, side_effect=PermissionError("denied")
            ),
            capture_logs() as logs,
        ):
            results = adapter.query()

        assert results == []
        assert len(log_events(logs, "hash_chain_file_audit.query_failed")) == 1

    def test_an_unreadable_dir_never_verifies_as_a_clean_chain(self, tmp_path):
        """``(True, [])`` on a directory nobody could read is the shape that
        lets an integrity dashboard stay green through a permissions fault."""
        adapter = HashChainFileAuditLogAdapter(log_dir=str(tmp_path))
        adapter.log(_make_config_change_entry())

        with patch.object(
            Path, "iterdir", autospec=True, side_effect=PermissionError("denied")
        ):
            is_valid, issues = adapter.verify_integrity()

        assert is_valid is False
        assert [issue["type"] for issue in issues] == ["verify_error"]

    def test_verify_integrity_ignores_a_partitioned_siblings_chain(self, tmp_path):
        """A sibling's file is a separate chain; reading it as this adapter's
        reports tampering that never happened."""
        log_dir = tmp_path / "audit"
        adapter = HashChainFileAuditLogAdapter(log_dir=str(log_dir))
        adapter.log(_make_config_change_entry())
        adapter.close()
        tampered = json.loads(_ledger_line(tmp_path, "theirs"))
        tampered["actor"]["actor_id"] = "mallory"
        _place_ledger(log_dir, "audit_2026-09-07_worker.jsonl", json.dumps(tampered))

        assert adapter.verify_integrity() == (True, [])


class TestAdapterPendingReservationSkipBehavior:
    """A degraded entry was sequenced by the local fallback, so the Redis-side
    reservation has nothing to protect — and every leg of it would log its own
    ERROR against the same dead client, once per entry."""

    def _degraded_adapter(self, tmp_path, redis_client):
        return HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path),
            distributed_hash_chain=True,
            redis_client=redis_client,
            enable_anchor_backup=False,
        )

    def test_all_three_reservation_legs_are_pending_skipped_for_a_degraded_entry(
        self, tmp_path
    ):
        redis_client = MockRedisClient()
        adapter = self._degraded_adapter(tmp_path, redis_client)
        redis_client.set_should_fail(True)

        with (
            patch.object(adapter._pending_manager, "reserve_sequence") as reserve,
            patch.object(adapter._pending_manager, "commit_sequence") as commit,
            patch.object(adapter._pending_manager, "abort_sequence") as abort,
        ):
            adapter.log(_make_config_change_entry())

        reserve.assert_not_called()
        commit.assert_not_called()
        abort.assert_not_called()

    def test_a_healthy_entry_still_reserves_and_commits(self, tmp_path):
        """The positive half — without it the skip could be an always-off
        reservation and every case above would still pass."""
        redis_client = MockRedisClient()
        adapter = self._degraded_adapter(tmp_path, redis_client)

        with (
            patch.object(
                adapter._pending_manager, "reserve_sequence", return_value=True
            ) as reserve,
            patch.object(
                adapter._pending_manager, "commit_sequence", return_value=True
            ) as commit,
        ):
            adapter.log(_make_config_change_entry())

        reserve.assert_called_once()
        assert reserve.call_args[0][0] == 1
        commit.assert_called_once_with(1)

    def test_the_degraded_sentinel_entry_is_pending_skipped_too(self, tmp_path):
        """Without a fallback manager the entry carries ``sequence: -1``,
        which is truthy — only the ``degraded`` stamp keeps it out of the
        reservation."""
        redis_client = MockRedisClient()
        adapter = self._degraded_adapter(tmp_path, redis_client)
        # The no-fallback shape: reachable through the factory when no state
        # file is available to the distributed manager.
        adapter.hash_chain_manager._fallback = None
        redis_client.set_should_fail(True)

        with patch.object(adapter._pending_manager, "reserve_sequence") as reserve:
            adapter.log(_make_config_change_entry())

        reserve.assert_not_called()
        rows = _read_rows(tmp_path)
        assert rows[-1]["integrity"]["sequence"] == -1

    def test_a_sustained_outage_logs_no_reservation_failure_per_entry(self, tmp_path):
        """Log volume used to scale with the audit rate exactly while the
        operator most needs to read the log — one ERROR per entry per leg."""
        redis_client = MockRedisClient()
        adapter = self._degraded_adapter(tmp_path, redis_client)
        redis_client.set_should_fail(True)

        with capture_logs() as logs:
            for index in range(50):
                adapter.log(_make_config_change_entry(f"target-{index}"))

        failures = [
            record["event"]
            for record in logs
            if record["event"].startswith("pending_seq.")
            and record["event"].endswith("_failed")
        ]
        assert failures == []
        assert len(_read_rows(tmp_path)) == 50


class TestAdapterCloseBehavior:
    """Shutdown persists through the manager's own entry point."""

    def test_close_does_not_roll_back_a_state_file_a_sibling_advanced(self, tmp_path):
        """Worker A's in-process counter is stale the moment worker B writes.
        Writing it over the shared file on close hands the next boot a source
        below the ledger — the exact condition the write-time guard repairs."""
        adapter = HashChainFileAuditLogAdapter(log_dir=str(tmp_path))
        adapter.log(_make_config_change_entry())
        state_file = tmp_path / ".hash_chain_state.json"
        sibling_advanced = {"sequence": 150, "previous_hash": "sibling-hash"}
        state_file.write_text(json.dumps(sibling_advanced), encoding="utf-8")
        adapter.hash_chain_manager._sequence = 100

        adapter.close()

        assert json.loads(state_file.read_text()) == sibling_advanced

    def test_close_persists_the_in_process_counter_when_single_writer(self, tmp_path):
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path), use_file_lock=False
        )
        adapter.log(_make_config_change_entry())
        adapter.hash_chain_manager._sequence = 100
        adapter.hash_chain_manager._previous_hash = "hash-100"

        adapter.close()

        state_file = tmp_path / ".hash_chain_state.json"
        assert json.loads(state_file.read_text())["sequence"] == 100
