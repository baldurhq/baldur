"""
RingBuffer + WAL 통합 후 RequestAuditBuffer 테스트.

테스트 항목:
1. 용량 내 이벤트 추가
2. 용량 초과 시 DROP_OLDEST 전략
3. 고부하에서 Non-blocking 확인
4. 하위 호환성 (events 속성, truncated_count)
5. 버퍼 통계 (stats 속성)
6. WAL 통합 (선택적)
"""

import time

import pytest

from baldur.audit.event_buffer import (
    AuditEvent,
    AuditEventType,
    RequestAuditBuffer,
)


class TestRequestAuditBufferWithRingBuffer:
    """RingBuffer 통합 후 RequestAuditBuffer 테스트."""

    def test_add_event_within_capacity(self):
        """용량 내 이벤트 추가 시 모두 저장."""
        buffer = RequestAuditBuffer(max_events=100)

        for i in range(50):
            event = AuditEvent(
                event_type=AuditEventType.DLQ_STORE,
                source="test",
                details={"idx": i},
            )
            result = buffer.add_event(event)
            assert result is True

        assert len(buffer.events) == 50
        assert buffer.stats["total_dropped"] == 0

    def test_add_event_exceeds_capacity_drop_oldest(self):
        """용량 초과 시 DROP_OLDEST 전략으로 오래된 이벤트 제거."""
        buffer = RequestAuditBuffer(max_events=10)

        # 15개 추가 (5개 DROP)
        for i in range(15):
            event = AuditEvent(
                event_type=AuditEventType.DLQ_STORE,
                source="test",
                details={"idx": i},
            )
            buffer.add_event(event)

        # 최신 10개만 남음
        assert len(buffer.events) == 10
        assert buffer.stats["total_dropped"] == 5

        # 가장 오래된 것(0-4)이 DROP됨, 5-14만 남음
        assert buffer.events[0].details["idx"] == 5
        assert buffer.events[-1].details["idx"] == 14

    def test_non_blocking_under_load(self):
        """고부하에서 Non-blocking 확인 (10,000개 이벤트)."""
        buffer = RequestAuditBuffer(max_events=10000)

        start = time.time()
        for _i in range(10000):
            event = AuditEvent(
                event_type=AuditEventType.DLQ_STORE,
                source="test",
            )
            buffer.add_event(event)
        elapsed = time.time() - start

        # 10,000개 추가가 1초 이내 (충분히 여유 있게 설정)
        assert elapsed < 1.0, f"10k events took {elapsed}s (should be < 1.0s)"
        assert buffer.event_count() == 10000

    def test_backward_compatibility_events_property(self):
        """하위 호환성: events 속성으로 이벤트 접근."""
        buffer = RequestAuditBuffer(max_events=100)

        event = AuditEvent(
            event_type=AuditEventType.CB_STATE_CHANGE,
            source="test",
        )
        buffer.add_event(event)

        # 기존 코드처럼 events 속성 접근 가능
        assert len(buffer.events) == 1
        assert buffer.events[0].event_type == AuditEventType.CB_STATE_CHANGE

    def test_backward_compatibility_truncated_count(self):
        """하위 호환성: truncated_count 속성 접근."""
        buffer = RequestAuditBuffer(max_events=5)

        for _i in range(10):
            buffer.add_event(
                AuditEvent(
                    event_type=AuditEventType.DLQ_STORE,
                    source="test",
                )
            )

        # truncated_count = dropped count
        assert buffer.truncated_count == 5
        assert buffer.is_truncated is True

    def test_stats_property(self):
        """버퍼 통계 속성 확인."""
        buffer = RequestAuditBuffer(max_events=10)

        for _i in range(15):
            buffer.add_event(
                AuditEvent(
                    event_type=AuditEventType.DLQ_STORE,
                    source="test",
                )
            )

        stats = buffer.stats
        assert stats["capacity"] == 10
        assert stats["size"] == 10
        assert stats["total_enqueued"] == 15
        assert stats["total_dropped"] == 5
        assert stats["drop_rate"] == pytest.approx(5 / 15, rel=0.01)
        assert stats["wal_enabled"] is False  # 기본값은 비활성화

    def test_add_method_convenience(self):
        """add() 편의 메서드 동작 확인."""
        buffer = RequestAuditBuffer(max_events=100)

        event = buffer.add(
            event_type=AuditEventType.CONFIG_CHANGE,
            source="test_source",
            details={"key": "value"},
        )

        assert event is not None
        assert event.event_type == AuditEventType.CONFIG_CHANGE
        assert event.source == "test_source"
        assert buffer.has_events() is True

    def test_get_events_by_type(self):
        """특정 유형 이벤트 필터링."""
        buffer = RequestAuditBuffer(max_events=100)

        buffer.add_event(
            AuditEvent(
                event_type=AuditEventType.DLQ_STORE,
                source="test",
            )
        )
        buffer.add_event(
            AuditEvent(
                event_type=AuditEventType.CB_STATE_CHANGE,
                source="test",
            )
        )
        buffer.add_event(
            AuditEvent(
                event_type=AuditEventType.DLQ_STORE,
                source="test",
            )
        )

        dlq_events = buffer.get_events_by_type(AuditEventType.DLQ_STORE)
        assert len(dlq_events) == 2

    def test_get_failed_events(self):
        """실패 이벤트 필터링."""
        buffer = RequestAuditBuffer(max_events=100)

        buffer.add_event(
            AuditEvent(
                event_type=AuditEventType.DLQ_STORE,
                source="test",
                success=True,
            )
        )
        buffer.add_event(
            AuditEvent(
                event_type=AuditEventType.CB_REJECTION,
                source="test",
                success=False,
                error_message="Circuit breaker open",
            )
        )

        failed = buffer.get_failed_events()
        assert len(failed) == 1
        assert failed[0].success is False

    def test_has_event_from_source(self):
        """특정 source 이벤트 존재 확인."""
        buffer = RequestAuditBuffer(max_events=100)

        buffer.add_event(
            AuditEvent(
                event_type=AuditEventType.DLQ_STORE,
                source="MiddlewareA",
            )
        )

        assert buffer.has_event_from_source("MiddlewareA") is True
        assert buffer.has_event_from_source("MiddlewareB") is False

    def test_clear_buffer(self):
        """버퍼 초기화."""
        buffer = RequestAuditBuffer(max_events=100)

        for _i in range(10):
            buffer.add_event(
                AuditEvent(
                    event_type=AuditEventType.DLQ_STORE,
                    source="test",
                )
            )

        assert buffer.event_count() == 10

        buffer.clear()

        assert buffer.event_count() == 0
        assert buffer.has_events() is False
        # 통계도 초기화됨
        assert buffer.stats["total_enqueued"] == 0

    def test_to_dict(self):
        """버퍼 직렬화."""
        buffer = RequestAuditBuffer(max_events=10)
        buffer.request_id = "test-request-123"

        buffer.add_event(
            AuditEvent(
                event_type=AuditEventType.DLQ_STORE,
                source="test",
            )
        )

        result = buffer.to_dict()

        assert result["request_id"] == "test-request-123"
        assert result["event_count"] == 1
        assert len(result["events"]) == 1
        assert "buffer_stats" in result

    def test_to_dict_with_truncation(self):
        """truncation 정보 포함된 직렬화."""
        buffer = RequestAuditBuffer(max_events=5)

        for _i in range(10):
            buffer.add_event(
                AuditEvent(
                    event_type=AuditEventType.DLQ_STORE,
                    source="test",
                )
            )

        result = buffer.to_dict()

        assert result["truncated"] is True
        assert result["truncated_count"] == 5
        assert result["max_events"] == 5

    def test_max_events_property(self):
        """max_events 속성 확인."""
        buffer = RequestAuditBuffer(max_events=500)
        assert buffer.max_events == 500

    def test_request_metadata(self):
        """요청 메타데이터 설정."""
        buffer = RequestAuditBuffer(max_events=100)

        buffer.set_request_metadata(
            path="/api/test",
            method="POST",
            user_id="user-123",
        )

        result = buffer.to_dict()
        assert result["path"] == "/api/test"
        assert result["method"] == "POST"
        assert result["user_id"] == "user-123"


class TestRequestAuditBufferWALIntegration:
    """WAL 통합 테스트 (선택적 기능)."""

    def test_wal_disabled_by_default(self):
        """WAL은 기본적으로 비활성화."""
        buffer = RequestAuditBuffer(max_events=100)

        assert buffer._wal_enabled is False
        assert buffer._wal is None

    def test_wal_enabled_with_parameter(self):
        """enable_wal=True 시 WAL 활성화 시도."""
        import tempfile

        from baldur.audit.wal import WALConfig, WriteAheadLog

        with tempfile.TemporaryDirectory() as tmpdir:
            wal = WriteAheadLog(
                config=WALConfig(
                    wal_dir=tmpdir,
                    sync_on_write=False,  # 테스트용
                )
            )

            buffer = RequestAuditBuffer(
                max_events=100,
                enable_wal=True,
                wal_instance=wal,
            )

            assert buffer._wal_enabled is True
            assert buffer._wal is not None

    def test_wal_records_events(self):
        """WAL 활성화 시 이벤트가 디스크에 기록."""
        import tempfile

        from baldur.audit.wal import WALConfig, WriteAheadLog

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            wal = WriteAheadLog(
                config=WALConfig(
                    wal_dir=tmpdir,
                    sync_on_write=False,
                )
            )

            buffer = RequestAuditBuffer(
                max_events=100,
                enable_wal=True,
                wal_instance=wal,
            )

            # 이벤트 추가
            for i in range(5):
                buffer.add_event(
                    AuditEvent(
                        event_type=AuditEventType.DLQ_STORE,
                        source="test",
                        details={"idx": i},
                    )
                )

            # WAL 시퀀스 추적 확인
            assert len(buffer._wal_sequences) == 5

            # 메모리 버퍼에도 존재
            assert buffer.event_count() == 5

            # WAL 닫기 (Windows 파일 잠금 해제)
            wal.close()

    def test_wal_stats_in_buffer_stats(self):
        """WAL 활성화 시 stats에 WAL 정보 포함."""
        import tempfile

        from baldur.audit.wal import WALConfig, WriteAheadLog

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            wal = WriteAheadLog(
                config=WALConfig(
                    wal_dir=tmpdir,
                    sync_on_write=False,
                )
            )

            buffer = RequestAuditBuffer(
                max_events=100,
                enable_wal=True,
                wal_instance=wal,
            )

            buffer.add_event(
                AuditEvent(
                    event_type=AuditEventType.DLQ_STORE,
                    source="test",
                )
            )

            stats = buffer.stats
            assert stats["wal_enabled"] is True
            assert stats["wal_sequences_count"] == 1

            # WAL 닫기 (Windows 파일 잠금 해제)
            wal.close()
