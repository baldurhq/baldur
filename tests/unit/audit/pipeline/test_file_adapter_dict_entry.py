"""
FileAuditLogAdapter dict entry 처리 버그 수정 테스트.

Bug: AuditSyncWorker._sync_entry_to_adapter()가 adapter.log(entry.data)로
dict를 전달하는데, FileAuditLogAdapter.log()는 entry.to_json()을 호출하여
AttributeError: 'dict' object has no attribute 'to_json' 발생.

수정: log()에서 isinstance(entry, dict) 체크 후 json.dumps() 사용.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from baldur.adapters.audit.file_adapter import FileAuditLogAdapter
from baldur.interfaces.audit_adapter import AuditAction, AuditEntry


class TestFileAdapterDictEntryBehavior:
    """FileAuditLogAdapter.log()가 dict와 AuditEntry 모두 처리하는지 검증."""

    @pytest.fixture
    def adapter(self, tmp_path: Path) -> FileAuditLogAdapter:
        """임시 파일 경로로 어댑터 생성."""
        return FileAuditLogAdapter(tmp_path / "audit.log")

    def _read_log_line(self, adapter: FileAuditLogAdapter) -> dict:
        """로그 파일의 첫 번째 줄을 JSON으로 파싱."""
        file_path = adapter._get_current_file_path()
        content = file_path.read_text(encoding="utf-8").strip()
        return json.loads(content)

    def test_log_dict_entry_writes_valid_json(self, adapter: FileAuditLogAdapter):
        """dict를 전달하면 json.dumps()로 유효한 JSON 기록."""
        entry_dict = {
            "action": "cb_force_open",
            "target_type": "circuit_breaker",
            "target_id": "payment-service",
            "timestamp": "2026-03-26T10:00:00+00:00",
        }

        adapter.log(entry_dict)

        written = self._read_log_line(adapter)
        assert written["action"] == "cb_force_open"
        assert written["target_type"] == "circuit_breaker"
        assert written["target_id"] == "payment-service"

    def test_log_audit_entry_writes_via_to_json(self, adapter: FileAuditLogAdapter):
        """AuditEntry 객체는 기존 to_json() 경로로 기록."""
        entry = AuditEntry(
            action=AuditAction.CB_FORCE_OPEN,
            target_type="circuit_breaker",
            target_id="payment-service",
        )

        adapter.log(entry)

        written = self._read_log_line(adapter)
        assert written["action"] == AuditAction.CB_FORCE_OPEN.value
        assert written["target_type"] == "circuit_breaker"

    def test_log_dict_preserves_all_fields(self, adapter: FileAuditLogAdapter):
        """dict의 모든 필드가 보존되는지 검증."""
        entry_dict = {
            "action": "test_action",
            "actor_id": "user-123",
            "details": {"key": "value", "nested": {"a": 1}},
            "success": True,
        }

        adapter.log(entry_dict)

        written = self._read_log_line(adapter)
        assert written == entry_dict

    def test_log_dict_with_non_serializable_values(self, adapter: FileAuditLogAdapter):
        """dict에 datetime 등 비직렬화 값이 있어도 default=str로 처리."""
        ts = datetime(2026, 3, 26, 10, 0, 0, tzinfo=UTC)
        entry_dict = {
            "action": "test",
            "timestamp": ts,
        }

        adapter.log(entry_dict)

        written = self._read_log_line(adapter)
        assert written["action"] == "test"
        # datetime은 str()로 변환됨
        assert "2026" in written["timestamp"]

    def test_log_empty_dict_writes_empty_json_object(
        self, adapter: FileAuditLogAdapter
    ):
        """빈 dict도 정상적으로 기록."""
        adapter.log({})

        written = self._read_log_line(adapter)
        assert written == {}

    def test_log_dict_does_not_call_to_json(self, adapter: FileAuditLogAdapter):
        """dict 입력 시 to_json() 호출 시도 없이 json.dumps() 사용."""
        entry_dict = {"action": "test"}

        # dict에 to_json이 없으므로 AttributeError 발생하지 않아야 함
        adapter.log(entry_dict)

        file_path = adapter._get_current_file_path()
        assert file_path.exists()
        content = file_path.read_text(encoding="utf-8").strip()
        assert content == json.dumps(entry_dict)

    def test_log_multiple_entries_mixed_types(self, adapter: FileAuditLogAdapter):
        """dict와 AuditEntry를 번갈아 기록해도 정상 동작."""
        dict_entry = {"action": "dict_entry", "seq": 1}
        audit_entry = AuditEntry(
            action=AuditAction.CB_FORCE_OPEN,
            target_type="cb",
        )

        adapter.log(dict_entry)
        adapter.log(audit_entry)

        file_path = adapter._get_current_file_path()
        lines = file_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2

        first = json.loads(lines[0])
        assert first["action"] == "dict_entry"

        second = json.loads(lines[1])
        assert second["action"] == AuditAction.CB_FORCE_OPEN.value
