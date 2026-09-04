"""Post-recovery integrity gate — verdict reachability behaviour.

The gate runs ahead of the DLQ replay dispatch on every CB CLOSED and blocks
the replay when it finds a tampered audit chain. The records it collects come
from the write-ahead log, which is written *before* the hash chain is applied,
so they routinely carry no integrity block at all. Running a chain verifier
over such records reports every one of them as broken, which is a statement
about their shape, not about tampering. These tests pin the distinction: no
verdict follows the gate's fail-open/fail-secure policy, while a real chain
that fails verification still blocks.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from baldur.services.event_bus.integrity_gate import (
    INTEGRITY_FAILED_KEY,
    INTEGRITY_GATE_KEY,
    on_circuit_breaker_closed_integrity_gate,
)

_GATE = "baldur.services.event_bus.integrity_gate"
_SETTINGS = "baldur.settings.audit_integrity.get_audit_integrity_settings"

#: A write-ahead record as the WAL actually stores it: the audit payload only.
_UNCHAINED_RECORD = {
    "record_id": "audit-0000000000ab",
    "event_type": "SYSTEM_CONTROL_CHANGED",
    "source": "SystemControl",
    "details": {"action": "archive_dlq"},
    "success": True,
}


def _event():
    return SimpleNamespace(data={"service_name": "payment.charge"})


def _settings(fail_open: bool):
    return SimpleNamespace(integrity_gate_fail_open=fail_open)


class TestUnchainedRecordsBehavior:
    """Records carrying no integrity block yield no verdict, not a violation."""

    def test_fail_open_lets_the_replay_through(self):
        event = _event()
        with (
            patch(
                f"{_GATE}._get_unsynced_wal_entries", return_value=[_UNCHAINED_RECORD]
            ),
            patch(_SETTINGS, return_value=_settings(True)),
        ):
            on_circuit_breaker_closed_integrity_gate(event)

        assert event.data[INTEGRITY_FAILED_KEY] is False
        assert event.data[INTEGRITY_GATE_KEY]["valid"] is None
        assert (
            event.data[INTEGRITY_GATE_KEY]["strategy"]
            == "unverifiable_unchained_records"
        )

    def test_fail_secure_still_blocks_the_replay(self):
        event = _event()
        with (
            patch(
                f"{_GATE}._get_unsynced_wal_entries", return_value=[_UNCHAINED_RECORD]
            ),
            patch(_SETTINGS, return_value=_settings(False)),
        ):
            on_circuit_breaker_closed_integrity_gate(event)

        assert event.data[INTEGRITY_FAILED_KEY] is True

    def test_the_verifier_is_never_asked_to_rank_unchained_records(self):
        """The per-record hash recompute is skipped — it cannot inform a verdict.

        This is the cost half: the collection is every unprocessed WAL record,
        so hashing it runs on the recovery path in proportion to the backlog.
        """
        event = _event()
        with (
            patch(
                f"{_GATE}._get_unsynced_wal_entries",
                return_value=[_UNCHAINED_RECORD] * 50,
            ),
            patch(_SETTINGS, return_value=_settings(True)),
            patch("baldur.audit.integrity.HashChainVerifier.verify_chain") as verify,
        ):
            on_circuit_breaker_closed_integrity_gate(event)

        verify.assert_not_called()
        assert event.data[INTEGRITY_GATE_KEY]["checked"] == 50


class TestChainedRecordsBehavior:
    """A real chain keeps deciding — the fix must not disarm the gate."""

    def test_a_broken_chain_still_blocks_the_replay(self):
        broken = [
            {
                **_UNCHAINED_RECORD,
                "integrity": {"sequence": 7, "previous_hash": "x", "current_hash": "y"},
            },
        ]
        event = _event()
        with (
            patch(f"{_GATE}._get_unsynced_wal_entries", return_value=broken),
            patch(_SETTINGS, return_value=_settings(True)),
        ):
            on_circuit_breaker_closed_integrity_gate(event)

        assert event.data[INTEGRITY_FAILED_KEY] is True
        assert event.data[INTEGRITY_GATE_KEY]["valid"] is False

    def test_an_empty_wal_reports_a_pass(self):
        event = _event()
        with (
            patch(f"{_GATE}._get_unsynced_wal_entries", return_value=[]),
            patch(_SETTINGS, return_value=_settings(True)),
        ):
            on_circuit_breaker_closed_integrity_gate(event)

        assert event.data[INTEGRITY_FAILED_KEY] is False
        assert event.data[INTEGRITY_GATE_KEY]["strategy"] == "no_entries"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
