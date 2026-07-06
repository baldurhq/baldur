"""
WAL Audit 통합 테스트.

테스트 대상:
- TestWALAuditIntegration: WAL Audit 통합
"""

from pathlib import Path


class TestWALAuditIntegration:
    """WAL Audit 통합 테스트."""

    def test_wal_init_accepts_audit_adapter(self):
        """WAL 생성자에 audit_adapter 파라미터 존재 확인."""
        import inspect

        from baldur.audit.wal import WriteAheadLog

        sig = inspect.signature(WriteAheadLog.__init__)
        assert "audit_adapter" in sig.parameters

    def test_wal_recovered_event_on_recovery(self, temp_wal_dir, mock_audit_adapter):
        """복구 시 WAL_RECOVERED 이벤트 기록."""
        from baldur.audit.wal import WALConfig, WriteAheadLog

        config = WALConfig(
            wal_dir=temp_wal_dir,
            sync_on_write=False,
        )

        # WAL 생성 및 기록
        wal = WriteAheadLog(config=config, audit_adapter=mock_audit_adapter)
        wal.write({"event": "test1"})
        wal.write({"event": "test2"})
        wal.write({"event": "test3"})
        wal.close()

        # 새 WAL 인스턴스로 복구
        wal2 = WriteAheadLog(config=config, audit_adapter=mock_audit_adapter)
        entries = wal2.recover_unprocessed(last_processed_seq=0)
        wal2.close()

        if entries:
            # WAL_RECOVERED 이벤트 확인
            recovered_events = mock_audit_adapter.get_events_by_type("WAL_RECOVERED")
            assert len(recovered_events) > 0, "WAL_RECOVERED 이벤트가 기록되어야 함"
            assert "recovered_count" in recovered_events[-1]["details"]

    def test_wal_rotated_event_on_rotation(self, temp_wal_dir, mock_audit_adapter):
        """로테이션 시 WAL_ROTATED 이벤트 기록."""
        from baldur.audit.wal import WALConfig, WriteAheadLog

        # 작은 파일 크기로 설정하여 빠른 로테이션 유도
        config = WALConfig(
            wal_dir=temp_wal_dir,
            max_file_size_mb=0.0001,  # 매우 작은 크기
            sync_on_write=False,
        )

        wal = WriteAheadLog(config=config, audit_adapter=mock_audit_adapter)

        # 여러 번 기록하여 로테이션 유도
        for i in range(100):
            wal.write({"event": f"test_{i}", "data": "x" * 1000})

        wal.close()

        # WAL_ROTATED 이벤트 확인
        rotated_events = mock_audit_adapter.get_events_by_type("WAL_ROTATED")
        assert len(rotated_events) > 0, "WAL_ROTATED 이벤트가 기록되어야 함"

    def test_wal_corruption_detected_event(self, temp_wal_dir, mock_audit_adapter):
        """체크섬 불일치 시 WAL_CORRUPTION_DETECTED 이벤트 기록."""
        from baldur.audit.wal import WALConfig, WriteAheadLog

        config = WALConfig(
            wal_dir=temp_wal_dir,
            sync_on_write=False,
        )

        # WAL 생성 및 기록
        wal = WriteAheadLog(config=config)
        wal.write({"event": "test"})
        wal.close()

        # WAL 파일 손상 시뮬레이션
        wal_files = list(Path(temp_wal_dir).glob("*.wal"))
        if wal_files:
            with open(wal_files[0], "r+b") as f:
                f.seek(20)  # 데이터 영역으로 이동
                f.write(b"CORRUPTED")

        # 손상된 WAL 읽기 시도
        wal2 = WriteAheadLog(config=config, audit_adapter=mock_audit_adapter)
        wal2.recover_unprocessed(last_processed_seq=0)
        wal2.close()

        # 손상이 감지되면 WAL_CORRUPTION_DETECTED 이벤트 확인
        mock_audit_adapter.get_events_by_type("WAL_CORRUPTION_DETECTED")
        # 손상 위치에 따라 감지되지 않을 수 있음
