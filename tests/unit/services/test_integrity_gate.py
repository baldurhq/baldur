"""
PostRecoveryIntegrityGate unit tests.

Under test:
    - on_circuit_breaker_closed_integrity_gate: WAL integrity gate on CB recovery
    - _verify_recovery_window_integrity: WAL hash-chain verification
    - _get_unsynced_wal_entries: unsynced WAL entry lookup
    - _update_health_score: health score update branches
    - Fail-Open / Fail-Secure policy branches
"""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.services.event_bus.integrity_gate import (
    INTEGRITY_FAILED_KEY,
    INTEGRITY_GATE_KEY,
    _get_unsynced_wal_entries,
    _update_health_score,
    _verify_recovery_window_integrity,
    on_circuit_breaker_closed_integrity_gate,
)

# ---------------------------------------------------------------------------
# Patch path constants — the modules integrity_gate.py imports locally
# ---------------------------------------------------------------------------
_PATCH_SETTINGS = "baldur.settings.audit_integrity.get_audit_integrity_settings"
_PATCH_VERIFY = (
    "baldur.services.event_bus.integrity_gate._verify_recovery_window_integrity"
)
_PATCH_HEALTH_UPDATE = "baldur.services.event_bus.integrity_gate._update_health_score"
_PATCH_ALERT = (
    "baldur.services.event_bus.integrity_gate._send_integrity_violation_alert"
)
_PATCH_VERIFIER_CLS = "baldur.audit.integrity.HashChainVerifier"
_PATCH_GET_ENTRIES = (
    "baldur.services.event_bus.integrity_gate._get_unsynced_wal_entries"
)
_PRO_WAL_MODULE = "baldur_pro.services.audit.base"
_PATCH_GET_WAL = f"{_PRO_WAL_MODULE}._get_wal"
_PATCH_HEALTH_SCORE = "baldur.audit.integrity.get_integrity_health_score"


# =============================================================================
# Helpers
# =============================================================================


def _make_event(
    service_name: str = "test_service", data: dict | None = None
) -> MagicMock:
    """Build an event object for the test."""
    event = MagicMock()
    event.data = data if data is not None else {"service_name": service_name}
    return event


def _make_wal_entry(data: dict) -> MagicMock:
    """A mock standing in for a WALEntry."""
    entry = MagicMock()
    entry.data = data
    return entry


# =============================================================================
# Contract
# =============================================================================


class TestIntegrityGateContract:
    """integrity_gate design contract values."""

    def test_integrity_gate_key_value(self):
        """The INTEGRITY_GATE_KEY constant is 'integrity_gate_result'."""
        assert INTEGRITY_GATE_KEY == "integrity_gate_result"

    def test_integrity_failed_key_value(self):
        """The INTEGRITY_FAILED_KEY constant is 'integrity_failed'."""
        assert INTEGRITY_FAILED_KEY == "integrity_failed"


# =============================================================================
# on_circuit_breaker_closed_integrity_gate behavior
# =============================================================================


class TestIntegrityGateHandlerBehavior:
    """The integrity gate handler on CB recovery."""

    @patch(_PATCH_HEALTH_UPDATE)
    @patch(_PATCH_VERIFY)
    @patch(_PATCH_SETTINGS)
    def test_valid_chain_sets_integrity_failed_false(
        self, mock_settings, mock_verify, mock_health
    ):
        """A valid chain sets integrity_failed=False."""
        mock_settings.return_value.integrity_gate_fail_open = True
        mock_verify.return_value = {
            "valid": True,
            "checked": 10,
            "errors": [],
            "strategy": "wal_chain_verify",
        }
        event = _make_event()

        on_circuit_breaker_closed_integrity_gate(event)

        assert event.data[INTEGRITY_FAILED_KEY] is False
        assert event.data[INTEGRITY_GATE_KEY]["valid"] is True

    @patch(_PATCH_ALERT)
    @patch(_PATCH_HEALTH_UPDATE)
    @patch(_PATCH_VERIFY)
    @patch(_PATCH_SETTINGS)
    def test_broken_chain_sets_integrity_failed_true(
        self, mock_settings, mock_verify, mock_health, mock_alert
    ):
        """An integrity violation sets integrity_failed=True."""
        mock_settings.return_value.integrity_gate_fail_open = True
        mock_verify.return_value = {
            "valid": False,
            "checked": 10,
            "errors": ["Chain break at seq 5"],
            "strategy": "wal_chain_verify",
        }
        event = _make_event()

        on_circuit_breaker_closed_integrity_gate(event)

        assert event.data[INTEGRITY_FAILED_KEY] is True
        assert event.data[INTEGRITY_GATE_KEY]["valid"] is False

    @patch(_PATCH_ALERT)
    @patch(_PATCH_HEALTH_UPDATE)
    @patch(_PATCH_VERIFY)
    @patch(_PATCH_SETTINGS)
    def test_broken_chain_invokes_alert(
        self, mock_settings, mock_verify, mock_health, mock_alert
    ):
        """An integrity violation calls _send_integrity_violation_alert."""
        mock_settings.return_value.integrity_gate_fail_open = True
        mock_verify.return_value = {
            "valid": False,
            "checked": 5,
            "errors": ["hash mismatch"],
            "strategy": "wal_chain_verify",
        }
        event = _make_event()

        on_circuit_breaker_closed_integrity_gate(event)

        mock_alert.assert_called_once()

    @patch(_PATCH_VERIFY)
    @patch(_PATCH_SETTINGS)
    def test_exception_fail_open_allows_replay(self, mock_settings, mock_verify):
        """An exception with fail_open=True sets integrity_failed=False (replay allowed)."""
        mock_settings.return_value.integrity_gate_fail_open = True
        mock_verify.side_effect = RuntimeError("Redis down")
        event = _make_event()

        on_circuit_breaker_closed_integrity_gate(event)

        assert event.data[INTEGRITY_FAILED_KEY] is False
        assert event.data[INTEGRITY_GATE_KEY]["policy"] == "fail_open"

    @patch(_PATCH_VERIFY)
    @patch(_PATCH_SETTINGS)
    def test_exception_fail_secure_blocks_replay(self, mock_settings, mock_verify):
        """An exception with fail_open=False sets integrity_failed=True (replay blocked)."""
        mock_settings.return_value.integrity_gate_fail_open = False
        mock_verify.side_effect = RuntimeError("Redis down")
        event = _make_event()

        on_circuit_breaker_closed_integrity_gate(event)

        assert event.data[INTEGRITY_FAILED_KEY] is True
        assert event.data[INTEGRITY_GATE_KEY]["policy"] == "fail_secure"

    @patch(_PATCH_HEALTH_UPDATE)
    @patch(_PATCH_VERIFY)
    @patch(_PATCH_SETTINGS)
    def test_gate_result_contains_duration_and_strategy(
        self, mock_settings, mock_verify, mock_health
    ):
        """The gate result carries duration_ms and strategy."""
        mock_settings.return_value.integrity_gate_fail_open = True
        mock_verify.return_value = {
            "valid": True,
            "checked": 3,
            "errors": [],
            "strategy": "wal_chain_verify",
        }
        event = _make_event()

        on_circuit_breaker_closed_integrity_gate(event)

        gate_result = event.data[INTEGRITY_GATE_KEY]
        assert "duration_ms" in gate_result
        assert gate_result["strategy"] == "wal_chain_verify"
        assert isinstance(gate_result["duration_ms"], float)

    @patch(_PATCH_VERIFY)
    def test_settings_load_failure_defaults_to_fail_open(self, mock_verify):
        """A settings load failure applies fail_open=True by default."""
        mock_verify.return_value = {
            "valid": True,
            "checked": 0,
            "errors": [],
            "strategy": "no_entries",
        }
        event = _make_event()

        with patch(_PATCH_SETTINGS, side_effect=RuntimeError("Settings unavailable")):
            on_circuit_breaker_closed_integrity_gate(event)

        # The gate must still run when settings fail to load
        assert INTEGRITY_GATE_KEY in event.data or INTEGRITY_FAILED_KEY in event.data


# =============================================================================
# _verify_recovery_window_integrity behavior
# =============================================================================


class TestVerifyRecoveryWindowBehavior:
    """The WAL hash-chain verification function."""

    @patch(_PATCH_GET_ENTRIES)
    @patch(_PATCH_VERIFIER_CLS)
    def test_empty_wal_returns_valid(self, mock_verifier_cls, mock_get_entries):
        """No WAL entries returns valid=True, strategy=no_entries."""
        mock_get_entries.return_value = []
        result = _verify_recovery_window_integrity("test_service")

        assert result["valid"] is True
        assert result["checked"] == 0
        assert result["strategy"] == "no_entries"

    @patch(_PATCH_GET_ENTRIES)
    @patch(_PATCH_VERIFIER_CLS)
    def test_valid_chain_returns_strategy_wal_chain_verify(
        self, mock_verifier_cls, mock_get_entries
    ):
        """A valid chain returns strategy='wal_chain_verify'."""
        mock_get_entries.return_value = [
            {"seq": 1, "integrity": {"hash": "h1"}},
            {"seq": 2, "integrity": {"hash": "h2"}},
        ]
        verifier_instance = MagicMock()
        verifier_instance.verify_chain.return_value = (True, None)
        mock_verifier_cls.return_value = verifier_instance
        result = _verify_recovery_window_integrity("test_service")

        assert result["valid"] is True
        assert result["strategy"] == "wal_chain_verify"
        assert result["checked"] == 2

    @patch(_PATCH_GET_ENTRIES)
    @patch(_PATCH_VERIFIER_CLS)
    def test_broken_chain_invokes_find_tampering(
        self, mock_verifier_cls, mock_get_entries
    ):
        """A failed verification calls find_tampering()."""
        mock_get_entries.return_value = [{"seq": 1, "integrity": {"hash": "h1"}}]
        verifier_instance = MagicMock()
        verifier_instance.verify_chain.return_value = (False, "hash mismatch")
        verifier_instance.find_tampering.return_value = [
            {"message": "tampered at seq 1"}
        ]
        mock_verifier_cls.return_value = verifier_instance
        result = _verify_recovery_window_integrity("test_service")

        assert result["valid"] is False
        verifier_instance.find_tampering.assert_called_once()
        assert "tampered at seq 1" in result["errors"]


# =============================================================================
# _get_unsynced_wal_entries behavior
# =============================================================================


class TestGetUnsyncedWalEntriesBehavior:
    """The unsynced WAL entry lookup."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    @patch(_PATCH_GET_WAL)
    def test_wal_none_returns_empty_list(self, mock_get_wal):
        """A None WAL returns an empty list."""
        mock_get_wal.return_value = None

        result = _get_unsynced_wal_entries("test_service")

        assert result == []

    @patch(_PATCH_GET_WAL)
    def test_uses_recover_unprocessed_method(self, mock_get_wal):
        """Calls wal.recover_unprocessed(last_processed_seq=0)."""
        mock_wal = MagicMock()
        mock_wal.recover_unprocessed.return_value = [
            _make_wal_entry({"event_type": "TEST", "seq": 1}),
        ]
        mock_get_wal.return_value = mock_wal

        result = _get_unsynced_wal_entries("test_service")

        mock_wal.recover_unprocessed.assert_called_once_with(last_processed_seq=0)
        assert len(result) == 1
        assert result[0]["event_type"] == "TEST"

    @patch(_PATCH_GET_WAL)
    def test_exception_returns_empty_list(self, mock_get_wal):
        """An exception returns an empty list."""
        mock_get_wal.side_effect = RuntimeError("WAL unavailable")

        result = _get_unsynced_wal_entries("test_service")

        assert result == []


# =============================================================================
# PRO absence is a tier fact, not a fault (OSS install)
# =============================================================================


class TestWalUnavailableWithoutPro:
    """An OSS install has no write-ahead log to read.

    Every other test in this file needs PRO installed to patch the WAL. That
    left the OSS path — the shipped default — untested, and its ImportError
    was reported as a WARNING naming an internal module on every circuit
    breaker recovery. These tests pin the tier split: absence is DEBUG, a
    genuine read failure stays WARNING.
    """

    @staticmethod
    def _without_pro_wal():
        """Force the WAL import to fail whether or not PRO is installed."""
        return patch.dict(sys.modules, {_PRO_WAL_MODULE: None})

    def test_absence_returns_empty_list(self):
        with self._without_pro_wal():
            assert _get_unsynced_wal_entries("test_service") == []

    def test_absence_is_debug_not_warning(self):
        with self._without_pro_wal(), capture_logs() as logs:
            _get_unsynced_wal_entries("test_service")

        levels = {entry["event"]: entry["log_level"] for entry in logs}
        assert levels.get("integrity_gate.wal_unavailable") == "debug"
        assert "integrity_gate.wal_read_failed" not in levels

    def test_genuine_read_failure_is_still_warning(self):
        def _raise_on_read():
            raise RuntimeError("WAL unavailable")

        stub = types.ModuleType(_PRO_WAL_MODULE)
        stub._get_wal = _raise_on_read

        with patch.dict(sys.modules, {_PRO_WAL_MODULE: stub}), capture_logs() as logs:
            assert _get_unsynced_wal_entries("test_service") == []

        levels = {entry["event"]: entry["log_level"] for entry in logs}
        assert levels.get("integrity_gate.wal_read_failed") == "warning"
        assert "integrity_gate.wal_unavailable" not in levels


# =============================================================================
# _update_health_score behavior
# =============================================================================


class TestUpdateHealthScoreBehavior:
    """IntegrityHealthScore updates."""

    @patch(_PATCH_HEALTH_SCORE)
    def test_valid_result_calls_record_recovery(self, mock_get_health):
        """A successful verification calls record_recovery()."""
        mock_health = MagicMock()
        mock_get_health.return_value = mock_health
        result = {"valid": True, "checked": 10}

        _update_health_score(result, duration_ms=50.0)

        mock_health.record_recovery.assert_called_once_with(
            event_type="post_recovery_gate_ok",
            sequences_affected=10,
            recovery_time_ms=50.0,
        )
        mock_health.record_chain_break.assert_not_called()

    @patch(_PATCH_HEALTH_SCORE)
    def test_invalid_result_calls_record_chain_break(self, mock_get_health):
        """A failed verification calls record_chain_break()."""
        mock_health = MagicMock()
        mock_get_health.return_value = mock_health
        result = {"valid": False, "checked": 5}

        _update_health_score(result, duration_ms=100.0)

        mock_health.record_chain_break.assert_called_once()
        mock_health.record_recovery.assert_not_called()

    @patch(_PATCH_HEALTH_SCORE)
    def test_exception_does_not_propagate(self, mock_get_health):
        """A health score update exception does not reach the caller."""
        mock_get_health.side_effect = RuntimeError("Health unavailable")

        # The exception must not propagate
        _update_health_score({"valid": True, "checked": 0}, duration_ms=0.0)
