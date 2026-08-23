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

    def test_meta_event_without_adapter_leaves_the_wal_untouched(self, temp_wal_dir):
        """어댑터가 없으면 메타 이벤트는 자신이 기술하는 WAL을 바꾸지 않는다."""
        from pathlib import Path as _Path

        from baldur.audit.wal import WALConfig, WriteAheadLog

        config = WALConfig(
            wal_dir=temp_wal_dir,
            sync_on_write=False,
            max_files=1000,  # 보존 삭제가 바이트 수를 흔들지 않도록
        )
        wal = WriteAheadLog(config=config)  # audit_adapter=None

        for i in range(5):
            wal.write({"event": f"test_{i}"})
        wal.flush()

        def wal_dir_bytes() -> int:
            return sum(f.stat().st_size for f in _Path(temp_wal_dir).glob("*.wal"))

        entries_before = wal.get_stats().total_entries
        bytes_before = wal_dir_bytes()
        files_before = len(list(_Path(temp_wal_dir).glob("*.wal")))

        # 1) 로테이션 메타 이벤트
        wal._rotate_file()
        assert len(list(_Path(temp_wal_dir).glob("*.wal"))) == files_before, (
            "로테이션은 파일을 닫을 뿐 새 파일을 즉시 만들지 않음"
        )

        # 2) 손상 감지 메타 이벤트 — 실제로 발동해야 단언이 유효함
        target = sorted(_Path(temp_wal_dir).glob("*.wal"))[0]
        with open(target, "r+b") as f:
            f.seek(20)
            f.write(b"CORRUPTED")

        wal2 = WriteAheadLog(config=config)  # audit_adapter=None
        wal2.recover_unprocessed(last_processed_seq=0)
        assert wal2.get_stats().corrupted_entries > 0, (
            "손상 분기가 실제로 실행되어야 함"
        )
        wal2.close()
        wal.close()

        assert wal.get_stats().total_entries == entries_before
        assert wal_dir_bytes() == bytes_before

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
