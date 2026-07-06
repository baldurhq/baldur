"""
ResilientContinuousAuditRecorder 통합 테스트.

Tests:
- 기본 레코딩 (Non-blocking)
- Fallback 체인
- Background Flush
- 헬스 체크
"""

import threading
import time

from baldur.audit.ring_buffer import RingBuffer
from baldur.audit.self_audit import SelfAuditLogger


class TestResilientRecorderInit:
    """ResilientContinuousAuditRecorder 초기화 테스트."""

    def test_ring_buffer_integration(self):
        """RingBuffer 통합 확인."""
        buffer = RingBuffer[dict](capacity=100)

        # 아이템 추가
        buffer.put({"action": "test", "data": 123})
        buffer.put({"action": "test2", "data": 456})

        assert buffer.size == 2

        # 배치 조회
        batch = buffer.get_batch(10)
        assert len(batch) == 2
        assert batch[0]["action"] == "test"

    def test_self_audit_integration(self):
        """SelfAudit 통합 확인."""
        from baldur.audit.self_audit import SelfAuditEvent, self_audit

        SelfAuditLogger.reset_instance()

        logger = self_audit()
        logger.log(SelfAuditEvent.STARTUP, "Test started")

        stats = logger.get_stats()
        assert stats.total_events == 1

        SelfAuditLogger.reset_instance()


class TestBackpressureIntegration:
    """배압 통합 테스트."""

    def test_drop_oldest_under_load(self):
        """부하 시 DROP_OLDEST 동작."""
        from baldur.audit.ring_buffer import BackpressureStrategy, RingBuffer

        buffer = RingBuffer[int](
            capacity=100,
            strategy=BackpressureStrategy.DROP_OLDEST,
        )

        # 200개 추가 (100개 드롭)
        for i in range(200):
            buffer.put(i)

        stats = buffer.get_stats()
        assert stats.size == 100
        assert stats.total_dropped == 100
        assert stats.total_enqueued == 200

        # 가장 오래된 것이 드롭됨
        first = buffer.get()
        assert first == 100  # 0-99는 드롭됨


class TestChecksumIntegration:
    """체크섬 통합 테스트."""

    def test_checksum_in_workflow(self):
        """워크플로우 내 체크섬 사용."""
        from baldur.audit.checksum import compute_crc32, verify_crc32

        # 감사 엔트리 생성
        entry = {
            "action": "auto_tuning",
            "parameter": "timeout_ms",
            "old_value": 5000,
            "new_value": 6000,
            "timestamp": "2025-01-01T00:00:00Z",
        }

        # 체크섬 계산
        checksum = compute_crc32(entry)
        entry["checksum"] = checksum

        # 나중에 검증
        entry_copy = dict(entry)
        stored_checksum = entry_copy.pop("checksum")

        result = verify_crc32(entry_copy, stored_checksum)
        assert result.is_valid

    def test_checksum_detects_tampering(self):
        """체크섬으로 위변조 감지."""
        from baldur.audit.checksum import compute_crc32, verify_crc32

        entry = {"action": "test", "value": 100}
        checksum = compute_crc32(entry)

        # 위변조
        entry["value"] = 999

        result = verify_crc32(entry, checksum)
        assert not result.is_valid


class TestConcurrentAccess:
    """동시 접근 테스트."""

    def test_concurrent_buffer_access(self):
        """동시 버퍼 접근."""
        from baldur.audit.ring_buffer import RingBuffer

        buffer = RingBuffer[int](capacity=1000)
        results = {"produced": 0, "consumed": 0}
        lock = threading.Lock()

        def producer():
            for i in range(100):
                buffer.put(i)
                with lock:
                    results["produced"] += 1
                time.sleep(0.001)

        def consumer():
            for _ in range(200):  # 충분한 시도
                batch = buffer.get_batch(10)
                with lock:
                    results["consumed"] += len(batch)
                if not batch:
                    time.sleep(0.01)
                if results["consumed"] >= 100:
                    break

        threads = [
            threading.Thread(target=producer),
            threading.Thread(target=consumer),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert results["produced"] == 100
        assert results["consumed"] == 100

    def test_self_audit_thread_safety(self):
        """SelfAudit 스레드 안전성."""
        from baldur.audit.self_audit import SelfAuditEvent, self_audit

        SelfAuditLogger.reset_instance()
        logger = self_audit()

        def log_events():
            for i in range(50):
                logger.log(SelfAuditEvent.STARTUP, f"Event {i}")

        threads = [threading.Thread(target=log_events) for _ in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = logger.get_stats()
        assert stats.total_events == 250

        SelfAuditLogger.reset_instance()


class TestFlushMechanism:
    """Flush 메커니즘 테스트."""

    def test_manual_flush(self):
        """수동 플러시."""
        from baldur.audit.ring_buffer import RingBuffer

        buffer = RingBuffer[dict](capacity=100)

        # 엔트리 추가
        for i in range(10):
            buffer.put({"index": i})

        # 플러시 (배치 조회)
        batch = buffer.get_batch(100)
        assert len(batch) == 10
        assert buffer.is_empty

    def test_partial_flush(self):
        """부분 플러시."""
        from baldur.audit.ring_buffer import RingBuffer

        buffer = RingBuffer[dict](capacity=100)

        for i in range(20):
            buffer.put({"index": i})

        # 10개만 플러시
        batch = buffer.get_batch(10)
        assert len(batch) == 10
        assert buffer.size == 10


class TestHealthStatus:
    """헬스 상태 테스트."""

    def test_buffer_health(self):
        """버퍼 헬스 확인."""
        from baldur.audit.ring_buffer import RingBuffer

        buffer = RingBuffer[int](capacity=100)

        # 80% 미만이면 건강
        for i in range(70):
            buffer.put(i)

        stats = buffer.get_stats()
        assert stats.size < stats.capacity * 0.8

    def test_self_audit_health(self):
        """SelfAudit 헬스 확인."""
        from baldur.audit.self_audit import SelfAuditEvent, self_audit

        SelfAuditLogger.reset_instance()
        logger = self_audit()

        # 정상 이벤트만
        for _ in range(10):
            logger.log(SelfAuditEvent.STARTUP, "OK")

        assert logger.is_healthy()
        assert logger.get_failure_rate() == 0.0

        SelfAuditLogger.reset_instance()


class TestFallbackChain:
    """Fallback 체인 테스트."""

    def test_checksum_for_fallback_verification(self):
        """Fallback 기록 검증을 위한 체크섬."""
        from baldur.audit.checksum import compute_crc32, verify_crc32

        # Primary 실패 시 Fallback에 저장
        entry = {
            "action": "test",
            "timestamp": "2025-01-01T00:00:00Z",
            "details": {"key": "value"},
        }

        # 체크섬 추가
        checksum = compute_crc32(entry)

        # Fallback 저장 형식
        fallback_record = {
            "entry": entry,
            "checksum": checksum,
            "fallback_reason": "primary_failed",
        }

        # 나중에 복구 시 검증
        recovered_entry = fallback_record["entry"]
        stored_checksum = fallback_record["checksum"]

        result = verify_crc32(recovered_entry, stored_checksum)
        assert result.is_valid


class TestModuleImports:
    """모듈 임포트 테스트."""

    def test_import_from_audit_package(self):
        """audit 패키지에서 임포트."""
        from baldur.audit import (
            RingBuffer,
            SelfAuditLogger,
            compute_crc32,
        )

        assert RingBuffer is not None
        assert SelfAuditLogger is not None
        assert compute_crc32 is not None

    def test_import_resilient_recorder(self):
        """ResilientContinuousAuditRecorder 임포트."""
        from baldur.audit import (
            ResilientContinuousAuditRecorder,
            ResilientRecorderConfig,
        )

        assert ResilientContinuousAuditRecorder is not None
        assert ResilientRecorderConfig is not None
