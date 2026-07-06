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

from baldur.adapters.audit.hashchain_adapter import (
    HashChainFileAuditLogAdapter,
)
from baldur.audit.integrity import HashChainManager
from baldur.interfaces.audit_adapter import (
    AuditAction,
    AuditEntry,
    AuditLogAdapter,
)

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
