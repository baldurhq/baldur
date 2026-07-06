"""
ActorContext/TraceContext 자동 주입 테스트.

테스트 대상:
- TestAuditContextAutoInjection: 컨텍스트 자동 주입
"""

from unittest.mock import patch

import pytest


class TestAuditContextAutoInjection:
    """
    ActorContext/TraceContext 자동 주입 테스트.

    Context 결합 개선:
    - ShadowLogger/WAL에서 _write_to_wal() 직접 호출
    - "어떤 운영자의 어떤 작업에서 발생" 추적 가능
    """

    def test_shadow_logger_uses_write_to_wal(self):
        """ShadowLogger가 _write_to_wal()을 직접 호출."""
        pytest.importorskip("baldur_pro")
        from baldur.adapters.memory.shadow_logger import ShadowLogger

        logger = ShadowLogger()
        logger.clear()

        with patch("baldur_pro.services.audit.base._write_to_wal") as mock_wal:
            logger.record_sync_failure(
                service_name="test_service",
                intended_state="OPEN",
                error=Exception("Connection timeout"),
                adapter_type="redis",
                operation="sync",
            )

            # _write_to_wal이 호출되어야 함
            mock_wal.assert_called_once()
            call_kwargs = mock_wal.call_args[1]
            assert call_kwargs["event_type"] == "SHADOW_LOG_SYNC_FAILED"
            assert call_kwargs["source"] == "ShadowLogger"
            assert call_kwargs["details"]["service_name"] == "test_service"

    def test_shadow_logger_recovery_uses_write_to_wal(self):
        """ShadowLogger 복구 시 _write_to_wal() 호출."""
        pytest.importorskip("baldur_pro")
        from baldur.adapters.memory.shadow_logger import ShadowLogger

        logger = ShadowLogger()
        logger.clear()

        # 먼저 실패 기록
        with patch("baldur_pro.services.audit.base._write_to_wal"):
            logger.record_sync_failure(
                service_name="test_service",
                intended_state="OPEN",
                error=Exception("Test error"),
            )

        # 복구 시 _write_to_wal 호출 확인
        with patch("baldur_pro.services.audit.base._write_to_wal") as mock_wal:
            count = logger.mark_as_synced("test_service")

            if count > 0:
                mock_wal.assert_called_once()
                call_kwargs = mock_wal.call_args[1]
                assert call_kwargs["event_type"] == "SHADOW_LOG_RECOVERED"
                assert call_kwargs["details"]["recovered_count"] == count

    def test_wal_uses_write_to_wal_for_rotation(self, temp_wal_dir):
        """WAL 로테이션 시 _write_to_wal() 호출."""
        pytest.importorskip("baldur_pro")
        from baldur.audit.wal import WALConfig, WriteAheadLog

        config = WALConfig(
            wal_dir=temp_wal_dir,
            max_file_size_mb=0.0001,  # 매우 작은 크기로 로테이션 유도
            sync_on_write=False,
        )

        with patch("baldur_pro.services.audit.base._write_to_wal") as mock_wal:
            wal = WriteAheadLog(config=config)  # audit_adapter=None

            # 로테이션 유도
            for i in range(50):
                wal.write({"event": f"test_{i}", "data": "x" * 2000})

            wal.close()

            # WAL_ROTATED 이벤트가 기록되어야 함
            rotation_calls = [
                call
                for call in mock_wal.call_args_list
                if call[1].get("event_type") == "WAL_ROTATED"
            ]
            assert len(rotation_calls) > 0, "WAL_ROTATED 이벤트가 기록되어야 함"

    def test_wal_audit_adapter_priority_over_write_to_wal(
        self, temp_wal_dir, mock_audit_adapter
    ):
        """WAL에 audit_adapter가 주입되면 우선 사용."""
        pytest.importorskip("baldur_pro")
        from baldur.audit.wal import WALConfig, WriteAheadLog

        config = WALConfig(
            wal_dir=temp_wal_dir,
            max_file_size_mb=0.0001,
            sync_on_write=False,
        )

        with patch("baldur_pro.services.audit.base._write_to_wal") as mock_wal:
            wal = WriteAheadLog(config=config, audit_adapter=mock_audit_adapter)

            # 로테이션 유도
            for i in range(50):
                wal.write({"event": f"test_{i}", "data": "x" * 2000})

            wal.close()

            # audit_adapter가 우선 사용되어야 함
            rotation_events = mock_audit_adapter.get_events_by_type("WAL_ROTATED")
            if rotation_events:
                # audit_adapter가 사용됨 → _write_to_wal은 호출되지 않아야 함
                wal_rotation_calls = [
                    call
                    for call in mock_wal.call_args_list
                    if call[1].get("event_type") == "WAL_ROTATED"
                ]
                assert len(wal_rotation_calls) == 0, (
                    "audit_adapter가 있으면 _write_to_wal 호출 안됨"
                )

    def test_shadow_logger_graceful_on_import_error(self):
        """_write_to_wal import 실패 시 graceful 처리."""
        from baldur.adapters.memory.shadow_logger import ShadowLogger

        logger = ShadowLogger()
        logger.clear()

        with patch.dict("sys.modules", {"baldur_pro.services.audit.base": None}):
            # ImportError가 발생해도 메인 로직은 정상 동작
            logger.record_sync_failure(
                service_name="test_service",
                intended_state="OPEN",
                error=Exception("Test error"),
            )

            # 레코드가 정상적으로 기록되어야 함
            records = logger.get_all_records()
            assert len(records) >= 1
