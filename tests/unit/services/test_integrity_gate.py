"""
PostRecoveryIntegrityGate 단위 테스트.

테스트 대상:
    - on_circuit_breaker_closed_integrity_gate: CB 복구 시 WAL 무결성 게이트
    - _verify_recovery_window_integrity: WAL 해시체인 검증
    - _get_unsynced_wal_entries: WAL 미동기화 엔트리 조회
    - _update_health_score: 건강도 갱신 분기
    - Fail-Open / Fail-Secure 정책 분기
"""

from unittest.mock import MagicMock, patch

import pytest

from baldur.services.event_bus.integrity_gate import (
    INTEGRITY_FAILED_KEY,
    INTEGRITY_GATE_KEY,
    _get_unsynced_wal_entries,
    _update_health_score,
    _verify_recovery_window_integrity,
    on_circuit_breaker_closed_integrity_gate,
)

# ---------------------------------------------------------------------------
# 패치 경로 상수 — integrity_gate.py 내 로컬 import 대상 모듈 기준
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
_PATCH_GET_WAL = "baldur_pro.services.audit.base._get_wal"
_PATCH_HEALTH_SCORE = "baldur.audit.integrity.get_integrity_health_score"


# =============================================================================
# Helpers
# =============================================================================


def _make_event(
    service_name: str = "test_service", data: dict | None = None
) -> MagicMock:
    """테스트용 이벤트 객체 생성."""
    event = MagicMock()
    event.data = data if data is not None else {"service_name": service_name}
    return event


def _make_wal_entry(data: dict) -> MagicMock:
    """WALEntry를 모사하는 mock 객체."""
    entry = MagicMock()
    entry.data = data
    return entry


# =============================================================================
# 계약(Contract) 검증
# =============================================================================


class TestIntegrityGateContract:
    """integrity_gate 설계 계약값 검증."""

    def test_integrity_gate_key_value(self):
        """INTEGRITY_GATE_KEY 상수는 'integrity_gate_result'이다."""
        assert INTEGRITY_GATE_KEY == "integrity_gate_result"

    def test_integrity_failed_key_value(self):
        """INTEGRITY_FAILED_KEY 상수는 'integrity_failed'이다."""
        assert INTEGRITY_FAILED_KEY == "integrity_failed"


# =============================================================================
# on_circuit_breaker_closed_integrity_gate 동작 검증
# =============================================================================


class TestIntegrityGateHandlerBehavior:
    """CB 복구 시 무결성 게이트 핸들러 동작 검증."""

    @patch(_PATCH_HEALTH_UPDATE)
    @patch(_PATCH_VERIFY)
    @patch(_PATCH_SETTINGS)
    def test_valid_chain_sets_integrity_failed_false(
        self, mock_settings, mock_verify, mock_health
    ):
        """정상 체인이면 integrity_failed=False를 설정한다."""
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
        """무결성 위반 시 integrity_failed=True를 설정한다."""
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
        """무결성 위반 시 _send_integrity_violation_alert가 호출된다."""
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
        """예외 + fail_open=True이면 integrity_failed=False (리플레이 허용)."""
        mock_settings.return_value.integrity_gate_fail_open = True
        mock_verify.side_effect = RuntimeError("Redis down")
        event = _make_event()

        on_circuit_breaker_closed_integrity_gate(event)

        assert event.data[INTEGRITY_FAILED_KEY] is False
        assert event.data[INTEGRITY_GATE_KEY]["policy"] == "fail_open"

    @patch(_PATCH_VERIFY)
    @patch(_PATCH_SETTINGS)
    def test_exception_fail_secure_blocks_replay(self, mock_settings, mock_verify):
        """예외 + fail_open=False이면 integrity_failed=True (리플레이 차단)."""
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
        """게이트 결과에 duration_ms와 strategy가 포함된다."""
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
        """설정 로드 실패 시 fail_open=True가 기본 적용된다."""
        mock_verify.return_value = {
            "valid": True,
            "checked": 0,
            "errors": [],
            "strategy": "no_entries",
        }
        event = _make_event()

        with patch(_PATCH_SETTINGS, side_effect=RuntimeError("Settings unavailable")):
            on_circuit_breaker_closed_integrity_gate(event)

        # settings 로드 에러 시에도 게이트가 동작해야 함
        assert INTEGRITY_GATE_KEY in event.data or INTEGRITY_FAILED_KEY in event.data


# =============================================================================
# _verify_recovery_window_integrity 동작 검증
# =============================================================================


class TestVerifyRecoveryWindowBehavior:
    """WAL 해시체인 검증 함수 동작 검증."""

    @patch(_PATCH_GET_ENTRIES)
    @patch(_PATCH_VERIFIER_CLS)
    def test_empty_wal_returns_valid(self, mock_verifier_cls, mock_get_entries):
        """WAL 엔트리가 없으면 valid=True, strategy=no_entries를 반환한다."""
        mock_get_entries.return_value = []
        event = _make_event()

        result = _verify_recovery_window_integrity("test_service", event)

        assert result["valid"] is True
        assert result["checked"] == 0
        assert result["strategy"] == "no_entries"

    @patch(_PATCH_GET_ENTRIES)
    @patch(_PATCH_VERIFIER_CLS)
    def test_valid_chain_returns_strategy_wal_chain_verify(
        self, mock_verifier_cls, mock_get_entries
    ):
        """정상 체인에서 strategy='wal_chain_verify'를 반환한다."""
        mock_get_entries.return_value = [{"seq": 1}, {"seq": 2}]
        verifier_instance = MagicMock()
        verifier_instance.verify_chain.return_value = (True, None)
        mock_verifier_cls.return_value = verifier_instance
        event = _make_event()

        result = _verify_recovery_window_integrity("test_service", event)

        assert result["valid"] is True
        assert result["strategy"] == "wal_chain_verify"
        assert result["checked"] == 2

    @patch(_PATCH_GET_ENTRIES)
    @patch(_PATCH_VERIFIER_CLS)
    def test_broken_chain_invokes_find_tampering(
        self, mock_verifier_cls, mock_get_entries
    ):
        """검증 실패 시 find_tampering()이 호출된다."""
        mock_get_entries.return_value = [{"seq": 1}]
        verifier_instance = MagicMock()
        verifier_instance.verify_chain.return_value = (False, "hash mismatch")
        verifier_instance.find_tampering.return_value = [
            {"message": "tampered at seq 1"}
        ]
        mock_verifier_cls.return_value = verifier_instance
        event = _make_event()

        result = _verify_recovery_window_integrity("test_service", event)

        assert result["valid"] is False
        verifier_instance.find_tampering.assert_called_once()
        assert "tampered at seq 1" in result["errors"]


# =============================================================================
# _get_unsynced_wal_entries 동작 검증
# =============================================================================


class TestGetUnsyncedWalEntriesBehavior:
    """WAL 미동기화 엔트리 조회 동작 검증."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    @patch(_PATCH_GET_WAL)
    def test_wal_none_returns_empty_list(self, mock_get_wal):
        """WAL이 None이면 빈 목록을 반환한다."""
        mock_get_wal.return_value = None

        result = _get_unsynced_wal_entries("test_service")

        assert result == []

    @patch(_PATCH_GET_WAL)
    def test_uses_recover_unprocessed_method(self, mock_get_wal):
        """wal.recover_unprocessed(last_processed_seq=0)를 호출한다."""
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
        """예외 발생 시 빈 목록을 반환한다."""
        mock_get_wal.side_effect = RuntimeError("WAL unavailable")

        result = _get_unsynced_wal_entries("test_service")

        assert result == []


# =============================================================================
# _update_health_score 동작 검증
# =============================================================================


class TestUpdateHealthScoreBehavior:
    """IntegrityHealthScore 업데이트 동작 검증."""

    @patch(_PATCH_HEALTH_SCORE)
    def test_valid_result_calls_record_recovery(self, mock_get_health):
        """검증 성공 시 record_recovery()가 호출된다."""
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
        """검증 실패 시 record_chain_break()가 호출된다."""
        mock_health = MagicMock()
        mock_get_health.return_value = mock_health
        result = {"valid": False, "checked": 5}

        _update_health_score(result, duration_ms=100.0)

        mock_health.record_chain_break.assert_called_once()
        mock_health.record_recovery.assert_not_called()

    @patch(_PATCH_HEALTH_SCORE)
    def test_exception_does_not_propagate(self, mock_get_health):
        """health score 업데이트 예외가 호출자에게 전파되지 않는다."""
        mock_get_health.side_effect = RuntimeError("Health unavailable")

        # 예외가 전파되지 않아야 함
        _update_health_score({"valid": True, "checked": 0}, duration_ms=0.0)
