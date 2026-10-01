"""Unit tests for the admin verify route against a real ledger (804).

``GET /audit/integrity/verify`` answered 400 "modified" on every untouched
trail: it re-hashed query rows in a different schema than the ledger hashed,
and its only test mocked the recorder, so the route never met a ledger. Every
case here drives a real ``HashChainFileAuditLogAdapter`` through a real
``ContinuousAuditRecorder`` and the framework-free handler.

The route vouches for the ledger's recent window: its newest
``ADMIN_VERIFY_WINDOW_ENTRIES`` entries plus a margin, read from the newest
files. Two window shapes used to read as damage on untouched trails — a newest
file whose read reaches the byte cap with an older file behind it, and a
one-file ledger longer than the window — and both are pinned here.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- Exit-path inventory: no chain on the adapter (400 ``no_hash_chain``), a
  window with no chained entry (200), intact (200), failing (400), a ledger
  read error (500).
- Dependency interaction (the window the recorder asks the adapter for).
- Boundary analysis (the byte cap, a window shorter than the ledger).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from baldur.adapters.audit.file_adapter import FileAuditLogAdapter
from baldur.adapters.audit.hashchain_adapter import HashChainFileAuditLogAdapter
from baldur.api.handlers.continuous_audit import (
    continuous_audit_chain_state,
    continuous_audit_integrity_verify,
)
from baldur.audit.config import AuditConfig
from baldur.audit.continuous_audit import (
    ADMIN_VERIFY_WINDOW_ENTRIES,
    ContinuousAuditRecorder,
)
from baldur.audit.integrity import ledger_tail as ledger_tail_module
from baldur.audit.integrity.ledger_tail import LEDGER_TAIL_MIN_LINES
from baldur.audit.integrity.verifier import (
    ISSUE_ENTRY_MODIFIED,
    ISSUE_MISSING_ENTRY,
    NOTE_HEAD_ABSENT,
)
from baldur.interfaces.audit_adapter import AuditAction, AuditEntry
from baldur.interfaces.web_framework import HttpMethod, RequestContext, ResponseContext
from baldur.settings.secrets import reset_secrets_settings
from baldur.utils.serialization import fast_dumps_str, fast_loads

_KEY_ENV = "BALDUR_SECRETS_AUDIT_SIGNING_KEY"
_WRITER_KEY = "admin-route-writer-key"

_ADAPTER_CLOCK = "baldur.adapters.audit.hashchain_adapter.utc_now"
_RECORDER_LOOKUP = "baldur.api.handlers.continuous_audit._recorder"
_DAY_ONE = datetime(2026, 9, 28, 12, tzinfo=UTC)
_DAY_TWO = datetime(2026, 9, 29, 12, tzinfo=UTC)

# The admin window patched down, and a one-file ledger longer than the window
# plus its margin, so the window ends mid-file.
_ADMIN_WINDOW = "baldur.audit.continuous_audit.ADMIN_VERIFY_WINDOW_ENTRIES"
_PATCHED_WINDOW = 10
_LONG_LEDGER_ENTRIES = _PATCHED_WINDOW + LEDGER_TAIL_MIN_LINES + 18


@pytest.fixture(autouse=True)
def _writer_key():
    """Pin the signing key the ledger is written and verified with."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(_KEY_ENV, _WRITER_KEY)
        reset_secrets_settings()
        yield
    reset_secrets_settings()


def _config_entry(target_id: str, *, padding: int = 0) -> AuditEntry:
    details: dict[str, Any] = {"old_value": {"v": 1}, "new_value": {"v": 2}}
    if padding:
        details["new_value"] = {"v": 2, "note": "n" * padding}
    return AuditEntry(
        action=AuditAction.CONFIG_CHANGE,
        target_type="RETRY_CONFIG",
        target_id=target_id,
        actor_id="alice",
        reason="tuning",
        details=details,
    )


def _write(
    adapter: HashChainFileAuditLogAdapter,
    count: int,
    *,
    day: datetime = _DAY_ONE,
    padding: int = 0,
) -> None:
    with patch(_ADAPTER_CLOCK, return_value=day):
        for n in range(count):
            adapter.log(_config_entry(f"{day:%m%d}-{n}", padding=padding))


def _recorder_over(adapter: Any) -> ContinuousAuditRecorder:
    return ContinuousAuditRecorder(
        audit_adapter=adapter, config=AuditConfig(hash_seed="route-test-seed")
    )


def _get(handler, recorder: ContinuousAuditRecorder, path: str) -> ResponseContext:
    with patch(_RECORDER_LOOKUP, return_value=recorder):
        return handler(RequestContext(method=HttpMethod.GET, path=path))


def _verify(recorder: ContinuousAuditRecorder) -> ResponseContext:
    return _get(continuous_audit_integrity_verify, recorder, "/audit/integrity/verify")


def _alter_sequence(log_dir: Path, sequence: int) -> None:
    """Edit the payload of the ledger entry at ``sequence``, its hash kept."""
    for path in sorted(log_dir.glob("audit_*.jsonl")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            row = fast_loads(line)
            if row["integrity"]["sequence"] == sequence:
                row["change"]["reason"] = "TAMPERED"
                lines[index] = fast_dumps_str(row)
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                return
    raise AssertionError(f"sequence {sequence} is not in {log_dir}")


@pytest.fixture
def ledger(tmp_path) -> HashChainFileAuditLogAdapter:
    """A file hash-chain adapter holding five untouched entries."""
    adapter = HashChainFileAuditLogAdapter(log_dir=str(tmp_path / "audit"))
    _write(adapter, 5)
    adapter.close()
    return adapter


def _single_writer_adapter(tmp_path: Path, **options: Any):
    """A file hash-chain adapter for a test's one writer.

    The cross-process file lock is for siblings sharing a volume; a test that
    writes a long ledger from one thread pays it on every entry for nothing.
    """
    return HashChainFileAuditLogAdapter(
        log_dir=str(tmp_path / "audit"), use_file_lock=False, **options
    )


@pytest.fixture
def long_one_file_ledger(tmp_path) -> HashChainFileAuditLogAdapter:
    """An untouched ``audit_all.jsonl`` longer than the patched admin window."""
    adapter = _single_writer_adapter(tmp_path, rotate_daily=False)
    _write(adapter, _LONG_LEDGER_ENTRIES)
    adapter.close()
    return adapter


class TestContinuousAuditVerifyBehavior:
    """The admin verify and state routes over a real file hash-chain adapter."""

    def test_untouched_trail_answers_200_over_the_recent_window(self, ledger):
        response = _verify(_recorder_over(ledger))

        assert response.status_code == 200
        body = response.body
        assert body["verified"] is True
        assert body["issues"] == []
        assert body["scope"] == "recent_window"
        assert (body["first_sequence"], body["last_sequence"]) == (1, 5)
        assert (body["total_entries"], body["verified_entries"]) == (5, 5)

    def test_altered_entry_answers_400_with_entry_modified_at_that_sequence_only(
        self, ledger
    ):
        _alter_sequence(ledger.log_dir, 3)

        response = _verify(_recorder_over(ledger))

        assert response.status_code == 400
        issues = response.body["issues"]
        assert [(issue["type"], issue["sequence"]) for issue in issues] == [
            (ISSUE_ENTRY_MODIFIED, 3)
        ]
        assert response.body["error"] == issues[0]["message"]

    def test_state_route_reports_the_ledger_chains_sequence(self, ledger):
        # The recorder recorded nothing itself: only the adapter's chain is at 5.
        response = _get(
            continuous_audit_chain_state,
            _recorder_over(ledger),
            "/audit/integrity/state",
        )

        assert response.status_code == 200
        assert response.body["chain_state"]["sequence"] == 5

    def test_verify_asks_the_adapter_for_the_admin_window(self, ledger):
        recorder = _recorder_over(ledger)

        with patch.object(
            ledger, "verify_trail", wraps=ledger.verify_trail
        ) as verify_trail:
            recorder.verify_integrity()

        verify_trail.assert_called_once_with(window=ADMIN_VERIFY_WINDOW_ENTRIES)

    def test_adapter_without_a_chain_answers_no_hash_chain(self, tmp_path):
        recorder = _recorder_over(FileAuditLogAdapter(tmp_path / "audit.jsonl"))

        response = _verify(recorder)

        assert response.status_code == 400
        assert response.body["verified"] is None
        assert response.body["reason"] == "no_hash_chain"

    def test_chain_state_without_a_chain_is_the_no_hash_chain_marker(self, tmp_path):
        recorder = _recorder_over(FileAuditLogAdapter(tmp_path / "audit.jsonl"))

        assert recorder.get_chain_state() == {
            "sequence": None,
            "previous_hash": None,
            "source": "no_hash_chain",
        }

    def test_window_with_no_chained_entry_answers_200_with_nothing_verified(
        self, tmp_path
    ):
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path / "audit"), enable_hash_chain=False
        )
        _write(adapter, 3)
        adapter.close()

        response = _verify(_recorder_over(adapter))

        assert response.status_code == 200
        assert response.body["verified"] is True
        assert response.body["verified_entries"] == 0
        assert response.body["message"] == "No entries with integrity information found"

    def test_ledger_read_error_answers_500(self, ledger):
        recorder = _recorder_over(ledger)

        with patch.object(
            ledger, "verify_trail", autospec=True, side_effect=OSError("unreadable")
        ):
            response = _verify(recorder)

        assert response.status_code == 500

    def test_window_capped_in_the_newest_file_reports_no_missing_entry(self, tmp_path):
        # Given an older daily file of more lines than the margin, read whole,
        # and a newest file larger than the byte cap (patched down to the
        # first read's size), both untouched
        cap = ledger_tail_module.LEDGER_TAIL_INITIAL_WINDOW_BYTES
        adapter = _single_writer_adapter(tmp_path)
        _write(adapter, LEDGER_TAIL_MIN_LINES + 8, day=_DAY_ONE)
        _write(adapter, 30, day=_DAY_TWO, padding=1000)
        adapter.close()
        older, newest = sorted(adapter.log_dir.glob("audit_*.jsonl"))
        assert older.stat().st_size < cap < newest.stat().st_size

        # When the route verifies the window
        with patch.object(ledger_tail_module, "LEDGER_TAIL_MAX_WINDOW_BYTES", cap):
            response = _verify(_recorder_over(adapter))

        # Then the window ended at the capped file: read on into the older
        # file, the capped file's unread middle would sit between two reads as
        # a stretch of removed entries
        body = response.body
        assert response.status_code == 200
        assert ISSUE_MISSING_ENTRY not in [issue["type"] for issue in body["issues"]]
        assert NOTE_HEAD_ABSENT not in [note["type"] for note in body["notes"]]
        assert body["first_sequence"] > LEDGER_TAIL_MIN_LINES + 8

    def test_one_file_window_shorter_than_the_ledger_reports_no_head_note(
        self, long_one_file_ledger
    ):
        # When the route verifies the recent window of an untouched one-file
        # ledger longer than the (patched) window
        with patch(_ADMIN_WINDOW, _PATCHED_WINDOW):
            response = _verify(_recorder_over(long_one_file_ledger))

        # Then the unread head is not reported: it was not read, not removed
        assert response.status_code == 200
        assert response.body["notes"] == []
        assert response.body["first_sequence"] > 1
        assert response.body["last_sequence"] == _LONG_LEDGER_ENTRIES

    def test_one_file_window_altered_entry_inside_the_window_is_entry_modified(
        self, long_one_file_ledger
    ):
        altered = _LONG_LEDGER_ENTRIES - 5
        _alter_sequence(long_one_file_ledger.log_dir, altered)

        with patch(_ADMIN_WINDOW, _PATCHED_WINDOW):
            response = _verify(_recorder_over(long_one_file_ledger))

        assert response.status_code == 400
        assert [
            (issue["type"], issue["sequence"]) for issue in response.body["issues"]
        ] == [(ISSUE_ENTRY_MODIFIED, altered)]
