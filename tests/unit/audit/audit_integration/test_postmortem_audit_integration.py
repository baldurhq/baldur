"""
Postmortem Audit Integration Tests (문서 130).

Postmortem 자동 트리거의 Audit 로깅 테스트.
(수동 API 테스트는 Django가 필요하여 전역 tests 폴더에서 진행)

테스트 항목:
1. 자동 트리거의 _write_to_wal 호출 확인
2. Audit 이벤트 타입 검증
"""

from unittest.mock import patch

import pytest

# Every test here exercises a baldur_pro audit path; skip when PRO is absent
# (the published OSS mirror installs only baldur).
pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


class TestAutoPostmortemAudit:
    """자동 Post-mortem 트리거의 Audit 로깅 테스트."""

    def setup_method(self):
        """테스트 전 설정 리셋."""
        from baldur.settings.api_view import reset_api_view_settings

        reset_api_view_settings()

    def teardown_method(self):
        """테스트 후 설정 리셋."""
        from baldur.settings.api_view import reset_api_view_settings

        reset_api_view_settings()

    def test_auto_postmortem_no_audit_when_disabled(self, monkeypatch):
        """자동 Post-mortem 비활성화 시 Audit이 호출되지 않는지 확인."""
        from baldur.services.event_bus import (
            BaldurEvent,
            EventType,
            _on_circuit_breaker_closed_postmortem,
        )

        monkeypatch.setenv("BALDUR_API_VIEW_XTEST_AUTO_POSTMORTEM_ENABLED", "false")

        event = BaldurEvent(
            event_type=EventType.CIRCUIT_BREAKER_CLOSED,
            data={"service_name": "test_service"},
            source="test",
        )

        wal_calls = []

        def mock_write_to_wal(**kwargs):
            wal_calls.append(kwargs)
            return 1

        with patch(
            "baldur_pro.services.audit.base._write_to_wal",
            side_effect=mock_write_to_wal,
        ):
            _on_circuit_breaker_closed_postmortem(event)

        # 비활성화 시 _write_to_wal이 호출되면 안 됨
        postmortem_calls = [
            c for c in wal_calls if c.get("event_type") == "POSTMORTEM_AUTO_GENERATED"
        ]
        assert len(postmortem_calls) == 0


class TestAuditEventTypes:
    """Audit 이벤트 타입 테스트."""

    def test_xtest_audit_uses_xtest_operation_event_type(self):
        """수동 API가 XTEST_OPERATION 이벤트 타입을 사용하는지 확인."""
        from baldur_pro.services.audit.xtest_audit import log_xtest_operation_audit

        wal_calls = []

        def mock_write_to_wal(**kwargs):
            wal_calls.append(kwargs)
            return 1

        with patch(
            "baldur_pro.services.audit.xtest_audit._write_to_wal",
            side_effect=mock_write_to_wal,
        ):
            log_xtest_operation_audit(
                session_id="test-session",
                action="generate_postmortem",
                component="observability",
                details={"incident_id": "TEST-001"},
                result="success",
                user="test_user",
            )

        assert len(wal_calls) == 1
        assert wal_calls[0]["event_type"] == "XTEST_OPERATION"
        assert wal_calls[0]["source"] == "XTest.observability"
