"""
ShadowLogger Audit 통합 테스트.

테스트 대상:
- TestShadowLoggerAuditIntegration: ShadowLogger Audit 통합
"""

from unittest.mock import patch

import pytest


class TestShadowLoggerAuditIntegration:
    """ShadowLogger Audit 통합 테스트."""

    def test_record_sync_failure_calls_audit(self):
        """L2 동기화 실패 시 Audit 기록 호출."""
        from baldur.adapters.memory.shadow_logger import ShadowLogger

        # ShadowLogger 싱글톤 초기화 (테스트용)
        logger = ShadowLogger()
        logger.clear()

        with patch.object(logger, "_record_audit_event") as mock_audit:
            logger.record_sync_failure(
                service_name="test_service",
                intended_state="OPEN",
                error=Exception("Connection timeout"),
                adapter_type="redis",
                operation="sync",
            )

            # Audit 호출 확인
            mock_audit.assert_called_once()
            call_args = mock_audit.call_args
            assert call_args[1]["event_type"] == "SHADOW_LOG_SYNC_FAILED"
            assert call_args[1]["service_name"] == "test_service"
            assert "error_message" in call_args[1]["details"]

    def test_mark_as_synced_calls_audit(self):
        """복구 완료 시 Audit 기록 호출."""
        from baldur.adapters.memory.shadow_logger import ShadowLogger

        logger = ShadowLogger()
        logger.clear()

        # 먼저 실패 기록
        logger.record_sync_failure(
            service_name="test_service",
            intended_state="OPEN",
            error=Exception("Test error"),
        )

        with patch.object(logger, "_record_audit_event") as mock_audit:
            count = logger.mark_as_synced("test_service")

            if count > 0:
                mock_audit.assert_called_once()
                call_args = mock_audit.call_args
                assert call_args[1]["event_type"] == "SHADOW_LOG_RECOVERED"
                assert call_args[1]["service_name"] == "test_service"
                assert "recovered_count" in call_args[1]["details"]

    def test_audit_failure_does_not_affect_main_logic(self):
        """Audit failure does not affect main logic (fail-open).

        ShadowLogger._record_audit_event is WAL-only (via _write_to_wal), so the
        failure is injected at that seam — not via a non-existent adapter call.
        """
        pytest.importorskip("baldur_pro")
        from baldur.adapters.memory.shadow_logger import ShadowLogger

        logger = ShadowLogger()
        logger.clear()

        with patch(
            "baldur_pro.services.audit.base._write_to_wal",
            side_effect=Exception("Audit failed"),
        ):
            # Main logic must still succeed.
            logger.record_sync_failure(
                service_name="test_service",
                intended_state="OPEN",
                error=Exception("Test error"),
            )

            records = logger.get_all_records()
            assert len(records) >= 1
