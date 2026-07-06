"""
Self-Audit Logger 단위 테스트.

Tests:
- 이벤트 로깅
- 통계 수집
- 실패 이벤트 분류
- 헬스 체크
- 최근 이벤트 조회
"""

from baldur.audit.self_audit import (
    SelfAuditEvent,
    SelfAuditLogger,
    self_audit,
)


class TestSelfAuditLogger:
    """SelfAuditLogger 테스트."""

    def setup_method(self):
        """각 테스트 전 싱글톤 리셋."""
        SelfAuditLogger.reset_instance()

    def teardown_method(self):
        """각 테스트 후 싱글톤 리셋."""
        SelfAuditLogger.reset_instance()

    def test_singleton(self):
        """싱글톤 패턴 테스트."""
        logger1 = SelfAuditLogger.get_instance()
        logger2 = SelfAuditLogger.get_instance()
        assert logger1 is logger2

    def test_self_audit_function(self):
        """self_audit() 함수 테스트."""
        logger = self_audit()
        assert isinstance(logger, SelfAuditLogger)
        assert logger is SelfAuditLogger.get_instance()

    def test_log_event(self):
        """이벤트 로깅 테스트."""
        logger = self_audit()
        logger.log(SelfAuditEvent.STARTUP, "System started")

        stats = logger.get_stats()
        assert stats.total_events == 1

    def test_log_with_details(self):
        """상세 정보와 함께 로깅."""
        logger = self_audit()
        logger.log(
            SelfAuditEvent.WAL_WRITE_FAILED,
            "Write failed",
            details={"error": "disk full", "path": "/var/log"},
        )

        stats = logger.get_stats()
        assert stats.total_events == 1
        assert stats.failure_events == 1

    def test_failure_event_classification(self):
        """실패 이벤트 분류 테스트."""
        logger = self_audit()

        # 실패 이벤트
        logger.log(SelfAuditEvent.WAL_WRITE_FAILED, "Failed")
        logger.log(SelfAuditEvent.PRIMARY_STORE_FAILED, "Failed")
        logger.log(SelfAuditEvent.CHECKSUM_MISMATCH, "Mismatch")

        # 일반 이벤트
        logger.log(SelfAuditEvent.STARTUP, "Started")
        logger.log(SelfAuditEvent.INITIALIZED, "Initialized")

        stats = logger.get_stats()
        assert stats.total_events == 5
        assert stats.failure_events == 3

    def test_events_by_type(self):
        """이벤트 유형별 카운트."""
        logger = self_audit()

        logger.log(SelfAuditEvent.STARTUP, "Started")
        logger.log(SelfAuditEvent.STARTUP, "Restarted")
        logger.log(SelfAuditEvent.CIRCUIT_OPENED, "Opened")

        stats = logger.get_stats()
        assert stats.events_by_type["startup"] == 2
        assert stats.events_by_type["circuit_opened"] == 1


class TestSelfAuditStats:
    """통계 테스트."""

    def setup_method(self):
        SelfAuditLogger.reset_instance()

    def teardown_method(self):
        SelfAuditLogger.reset_instance()

    def test_initial_stats(self):
        """초기 통계."""
        logger = self_audit()
        stats = logger.get_stats()

        assert stats.total_events == 0
        assert stats.failure_events == 0
        assert stats.events_by_type == {}
        assert stats.last_event_time is None
        assert stats.uptime_seconds >= 0

    def test_uptime_increases(self):
        """uptime 증가."""
        import time

        logger = self_audit()
        stats1 = logger.get_stats()
        time.sleep(0.1)
        stats2 = logger.get_stats()

        assert stats2.uptime_seconds > stats1.uptime_seconds

    def test_last_event_time_updated(self):
        """마지막 이벤트 시간 업데이트."""
        logger = self_audit()
        assert logger.get_stats().last_event_time is None

        logger.log(SelfAuditEvent.STARTUP, "Started")

        assert logger.get_stats().last_event_time is not None


class TestRecentEvents:
    """최근 이벤트 테스트."""

    def setup_method(self):
        SelfAuditLogger.reset_instance()

    def teardown_method(self):
        SelfAuditLogger.reset_instance()

    def test_get_recent_events(self):
        """최근 이벤트 조회."""
        logger = self_audit()

        logger.log(SelfAuditEvent.STARTUP, "Started")
        logger.log(SelfAuditEvent.INITIALIZED, "Initialized")

        events = logger.get_recent_events(limit=10)
        assert len(events) == 2
        assert events[0]["event"] == "startup"
        assert events[1]["event"] == "initialized"

    def test_recent_events_limit(self):
        """최근 이벤트 제한."""
        logger = self_audit()

        for i in range(10):
            logger.log(SelfAuditEvent.STARTUP, f"Event {i}")

        events = logger.get_recent_events(limit=5)
        assert len(events) == 5
        # 가장 최근 5개
        assert "Event 5" in events[0]["message"]

    def test_recent_events_max_stored(self):
        """최대 저장 개수 제한."""
        logger = self_audit()
        logger._max_recent_events = 10

        for i in range(20):
            logger.log(SelfAuditEvent.STARTUP, f"Event {i}")

        events = logger.get_recent_events(limit=100)
        assert len(events) == 10


class TestHealthCheck:
    """헬스 체크 테스트."""

    def setup_method(self):
        SelfAuditLogger.reset_instance()

    def teardown_method(self):
        SelfAuditLogger.reset_instance()

    def test_initial_healthy(self):
        """초기 상태는 healthy."""
        logger = self_audit()
        assert logger.is_healthy()

    def test_failure_rate_zero_when_no_events(self):
        """이벤트 없을 때 실패율 0."""
        logger = self_audit()
        assert logger.get_failure_rate() == 0.0

    def test_failure_rate_calculation(self):
        """실패율 계산."""
        logger = self_audit()

        # 5개 일반 이벤트
        for _ in range(5):
            logger.log(SelfAuditEvent.STARTUP, "OK")

        # 5개 실패 이벤트
        for _ in range(5):
            logger.log(SelfAuditEvent.WAL_WRITE_FAILED, "Failed")

        assert logger.get_failure_rate() == 0.5

    def test_is_healthy_with_low_failure_rate(self):
        """낮은 실패율은 healthy."""
        logger = self_audit()

        for _ in range(9):
            logger.log(SelfAuditEvent.STARTUP, "OK")
        logger.log(SelfAuditEvent.WAL_WRITE_FAILED, "Failed")

        # 10% 실패율 = 기본 임계값 (10%)과 같음
        assert logger.is_healthy(max_failure_rate=0.1)

    def test_is_healthy_with_high_failure_rate(self):
        """높은 실패율은 unhealthy."""
        logger = self_audit()

        for _ in range(5):
            logger.log(SelfAuditEvent.STARTUP, "OK")
        for _ in range(5):
            logger.log(SelfAuditEvent.WAL_WRITE_FAILED, "Failed")

        # 50% 실패율 > 10%
        assert not logger.is_healthy(max_failure_rate=0.1)

    def test_is_healthy_custom_threshold(self):
        """사용자 정의 임계값."""
        logger = self_audit()

        for _ in range(6):
            logger.log(SelfAuditEvent.STARTUP, "OK")
        for _ in range(4):
            logger.log(SelfAuditEvent.WAL_WRITE_FAILED, "Failed")

        # 40% 실패율
        assert logger.is_healthy(max_failure_rate=0.5)
        assert not logger.is_healthy(max_failure_rate=0.3)


class TestSelfAuditEvent:
    """SelfAuditEvent enum 테스트."""

    def test_all_events_have_string_values(self):
        """모든 이벤트가 문자열 값을 가짐."""
        for event in SelfAuditEvent:
            assert isinstance(event.value, str)

    def test_failure_events_in_enum(self):
        """실패 이벤트들이 enum에 정의됨."""
        failure_events = [
            SelfAuditEvent.WAL_WRITE_FAILED,
            SelfAuditEvent.PRIMARY_STORE_FAILED,
            SelfAuditEvent.FALLBACK_FAILED,
            SelfAuditEvent.CHECKSUM_MISMATCH,
        ]
        for event in failure_events:
            assert event in SelfAuditLogger.FAILURE_EVENTS

    def test_lifecycle_events(self):
        """라이프사이클 이벤트."""
        assert SelfAuditEvent.STARTUP.value == "startup"
        assert SelfAuditEvent.SHUTDOWN.value == "shutdown"
        assert SelfAuditEvent.INITIALIZED.value == "initialized"


class TestErrorHandling:
    """에러 처리 테스트."""

    def setup_method(self):
        SelfAuditLogger.reset_instance()

    def teardown_method(self):
        SelfAuditLogger.reset_instance()

    def test_log_never_raises(self):
        """log()는 절대 예외를 발생시키지 않음."""
        logger = self_audit()

        # 다양한 입력에도 예외 발생하지 않음
        logger.log(SelfAuditEvent.STARTUP, "Normal")
        logger.log(SelfAuditEvent.STARTUP, None)  # type: ignore
        logger.log(SelfAuditEvent.STARTUP, "", details={"key": None})

        # 검증: 일부는 기록되었음
        assert logger.get_stats().total_events >= 1
