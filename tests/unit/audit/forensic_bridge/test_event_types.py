"""
AuditEventType 신규 이벤트 타입 테스트.

테스트 대상:
- CorruptionShield 관련 이벤트 타입
- ShadowLogger 관련 이벤트 타입
- WAL 관련 이벤트 타입
- Forensic 관련 이벤트 타입
"""


class TestAuditEventTypeAdditions:
    """AuditEventType 신규 이벤트 타입 테스트."""

    def test_corruption_event_types_exist(self):
        """CorruptionShield 관련 이벤트 타입 존재 확인."""
        from baldur.audit.event_buffer import AuditEventType

        assert hasattr(AuditEventType, "CORRUPTION_DETECTED")
        assert hasattr(AuditEventType, "CORRUPTION_BLOCKED")

        assert AuditEventType.CORRUPTION_DETECTED.value == "corruption_detected"
        assert AuditEventType.CORRUPTION_BLOCKED.value == "corruption_blocked"

    def test_shadow_log_event_types_exist(self):
        """ShadowLogger 관련 이벤트 타입 존재 확인."""
        from baldur.audit.event_buffer import AuditEventType

        assert hasattr(AuditEventType, "SHADOW_LOG_SYNC_FAILED")
        assert hasattr(AuditEventType, "SHADOW_LOG_RECOVERED")

        assert AuditEventType.SHADOW_LOG_SYNC_FAILED.value == "shadow_log_sync_failed"
        assert AuditEventType.SHADOW_LOG_RECOVERED.value == "shadow_log_recovered"

    def test_wal_event_types_exist(self):
        """WAL 관련 이벤트 타입 존재 확인."""
        from baldur.audit.event_buffer import AuditEventType

        assert hasattr(AuditEventType, "WAL_CORRUPTION_DETECTED")
        assert hasattr(AuditEventType, "WAL_RECOVERED")
        assert hasattr(AuditEventType, "WAL_ROTATED")

        assert AuditEventType.WAL_CORRUPTION_DETECTED.value == "wal_corruption_detected"
        assert AuditEventType.WAL_RECOVERED.value == "wal_recovered"
        assert AuditEventType.WAL_ROTATED.value == "wal_rotated"

    def test_forensic_event_types_exist(self):
        """Forensic 관련 이벤트 타입 존재 확인."""
        from baldur.audit.event_buffer import AuditEventType

        assert hasattr(AuditEventType, "FORENSIC_CAPTURE_STARTED")
        assert hasattr(AuditEventType, "FORENSIC_CAPTURE_COMPLETED")
        assert hasattr(AuditEventType, "FORENSIC_ANOMALY_DETECTED")

        assert (
            AuditEventType.FORENSIC_CAPTURE_STARTED.value == "forensic_capture_started"
        )
        assert (
            AuditEventType.FORENSIC_CAPTURE_COMPLETED.value
            == "forensic_capture_completed"
        )
        assert (
            AuditEventType.FORENSIC_ANOMALY_DETECTED.value
            == "forensic_anomaly_detected"
        )


class TestAuditIntegrationEnd2End:
    """End-to-End 통합 테스트."""

    def test_all_new_event_types_are_unique(self):
        """모든 신규 이벤트 타입 값이 고유한지 확인."""
        from baldur.audit.event_buffer import AuditEventType

        values = [e.value for e in AuditEventType]
        assert len(values) == len(set(values)), "중복된 이벤트 타입 값이 있음"

    def test_new_event_types_count(self):
        """신규 이벤트 타입 수 확인."""
        from baldur.audit.event_buffer import AuditEventType

        new_types = [
            "CORRUPTION_DETECTED",
            "CORRUPTION_BLOCKED",
            "SHADOW_LOG_SYNC_FAILED",
            "SHADOW_LOG_RECOVERED",
            "WAL_CORRUPTION_DETECTED",
            "WAL_RECOVERED",
            "WAL_ROTATED",
            "FORENSIC_CAPTURE_STARTED",
            "FORENSIC_CAPTURE_COMPLETED",
            "FORENSIC_ANOMALY_DETECTED",
        ]

        for type_name in new_types:
            assert hasattr(AuditEventType, type_name), f"{type_name}이 없음"
