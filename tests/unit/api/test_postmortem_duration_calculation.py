"""
Postmortem Duration 계산 테스트.

테스트 대상:
1. calculate_incident_duration - 인시던트 지속 시간 계산
2. parse_iso_timestamp - ISO 타임스탬프 파싱
3. calculate_time_diff_seconds - 시간 차이 계산
4. find_first_event_by_type / find_last_event_by_type - 이벤트 검색

테스트 케이스:
- 정상: OPEN → HALF_OPEN → CLOSED 순서로 이벤트 존재
- 정상: OPEN → CLOSED (HALF_OPEN 없음)
- 예외: CLOSED 없음 (진행 중 인시던트)
- 예외: 빈 타임라인
- 예외: OPEN 이벤트 없음
"""


class TestCalculateIncidentDuration:
    """calculate_incident_duration 함수 테스트."""

    def test_basic_duration_with_open_and_closed_events(self):
        """OPEN과 CLOSED 이벤트가 있을 때 duration 계산."""
        from baldur.utils.duration import calculate_incident_duration

        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "CIRCUIT_BREAKER_OPENED",
                "details": {"service_name": "test-service"},
            },
            {
                "timestamp": "2026-01-27T14:01:02+09:00",
                "event_type": "CIRCUIT_BREAKER_CLOSED",
                "details": {"service_name": "test-service"},
            },
        ]

        result = calculate_incident_duration(timeline)

        assert result.started_at == "2026-01-27T14:00:00+09:00"
        assert result.resolved_at == "2026-01-27T14:01:02+09:00"
        assert result.duration_seconds == 62.0

    def test_empty_timeline_returns_none_duration(self):
        """빈 타임라인일 때 duration은 None."""
        from baldur.utils.duration import calculate_incident_duration

        result = calculate_incident_duration(
            [], current_time_iso="2026-01-27T14:00:00+09:00"
        )

        assert result.started_at is None
        assert result.resolved_at == "2026-01-27T14:00:00+09:00"
        assert result.duration_seconds is None

    def test_no_open_event_uses_first_event(self):
        """OPEN 이벤트가 없으면 첫 번째 이벤트를 시작점으로 사용."""
        from baldur.utils.duration import calculate_incident_duration

        timeline = [
            {
                "timestamp": "2026-01-27T14:00:30+09:00",
                "event_type": "ERROR_BUDGET_CRITICAL",
                "details": {},
            },
            {
                "timestamp": "2026-01-27T14:01:30+09:00",
                "event_type": "CIRCUIT_BREAKER_CLOSED",
                "details": {},
            },
        ]

        result = calculate_incident_duration(timeline)

        assert result.started_at == "2026-01-27T14:00:30+09:00"
        assert result.resolved_at == "2026-01-27T14:01:30+09:00"
        assert result.duration_seconds == 60.0


class TestCalculateIncidentDurationDetailed:
    """calculate_incident_duration 함수 세분화 테스트."""

    def test_full_lifecycle_open_halfopen_closed(self):
        """OPEN → HALF_OPEN → CLOSED 전체 라이프사이클 duration 계산."""
        from baldur.utils.duration import calculate_incident_duration

        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "CIRCUIT_BREAKER_OPENED",
                "details": {"service_name": "test-service"},
            },
            {
                "timestamp": "2026-01-27T14:00:30+09:00",
                "event_type": "CIRCUIT_BREAKER_HALF_OPENED",
                "details": {"service_name": "test-service"},
            },
            {
                "timestamp": "2026-01-27T14:01:00+09:00",
                "event_type": "CIRCUIT_BREAKER_CLOSED",
                "details": {"service_name": "test-service"},
            },
        ]

        result = calculate_incident_duration(timeline)

        assert result.started_at == "2026-01-27T14:00:00+09:00"
        assert result.resolved_at == "2026-01-27T14:01:00+09:00"
        assert result.duration_seconds == 60.0
        assert result.downtime_seconds == 30.0  # OPEN → HALF_OPEN
        assert result.validation_seconds == 30.0  # HALF_OPEN → CLOSED

    def test_no_half_open_event_sets_downtime_as_duration(self):
        """HALF_OPEN 없이 직접 CLOSED된 경우 downtime = duration, validation = 0."""
        from baldur.utils.duration import calculate_incident_duration

        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "CIRCUIT_BREAKER_OPENED",
                "details": {},
            },
            {
                "timestamp": "2026-01-27T14:01:30+09:00",
                "event_type": "CIRCUIT_BREAKER_CLOSED",
                "details": {},
            },
        ]

        result = calculate_incident_duration(timeline)

        assert result.duration_seconds == 90.0
        assert result.downtime_seconds == 90.0
        assert result.validation_seconds == 0.0

    def test_no_closed_event_ongoing_incident(self):
        """CLOSED 이벤트가 없는 경우 (진행 중 인시던트)."""
        from baldur.utils.duration import calculate_incident_duration

        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "CIRCUIT_BREAKER_OPENED",
                "details": {},
            },
            {
                "timestamp": "2026-01-27T14:00:30+09:00",
                "event_type": "CIRCUIT_BREAKER_HALF_OPENED",
                "details": {},
            },
        ]

        result = calculate_incident_duration(
            timeline, current_time_iso="2026-01-27T14:01:00+09:00"
        )

        assert result.started_at == "2026-01-27T14:00:00+09:00"
        assert result.resolved_at == "2026-01-27T14:01:00+09:00"
        assert result.duration_seconds == 60.0
        assert result.downtime_seconds == 30.0  # OPEN → HALF_OPEN

    def test_empty_timeline_returns_none_values(self):
        """빈 타임라인은 None 값 반환."""
        from baldur.utils.duration import calculate_incident_duration

        result = calculate_incident_duration(
            [], current_time_iso="2026-01-27T14:00:00+09:00"
        )

        assert result.started_at is None
        assert result.resolved_at is not None
        assert result.duration_seconds is None
        assert result.downtime_seconds is None
        assert result.validation_seconds is None

    def test_multiple_half_open_uses_first_one(self):
        """여러 번 HALF_OPEN 진입 시 첫 번째 HALF_OPEN 사용."""
        from baldur.utils.duration import calculate_incident_duration

        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "CIRCUIT_BREAKER_OPENED",
                "details": {},
            },
            {
                "timestamp": "2026-01-27T14:00:30+09:00",
                "event_type": "CIRCUIT_BREAKER_HALF_OPENED",
                "details": {},
            },
            {
                "timestamp": "2026-01-27T14:00:45+09:00",
                "event_type": "CIRCUIT_BREAKER_OPENED",  # 다시 OPEN
                "details": {},
            },
            {
                "timestamp": "2026-01-27T14:01:00+09:00",
                "event_type": "CIRCUIT_BREAKER_HALF_OPENED",  # 두 번째 HALF_OPEN
                "details": {},
            },
            {
                "timestamp": "2026-01-27T14:01:30+09:00",
                "event_type": "CIRCUIT_BREAKER_CLOSED",
                "details": {},
            },
        ]

        result = calculate_incident_duration(timeline)

        # 첫 번째 HALF_OPEN 사용
        assert result.downtime_seconds == 30.0  # 14:00:00 → 14:00:30

    def test_uses_last_closed_event(self):
        """마지막 CLOSED 이벤트를 종료점으로 사용."""
        from baldur.utils.duration import calculate_incident_duration

        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "CIRCUIT_BREAKER_OPENED",
                "details": {},
            },
            {
                "timestamp": "2026-01-27T14:00:30+09:00",
                "event_type": "CIRCUIT_BREAKER_CLOSED",  # 첫 번째 CLOSED
                "details": {},
            },
            {
                "timestamp": "2026-01-27T14:00:45+09:00",
                "event_type": "CIRCUIT_BREAKER_OPENED",  # 다시 OPEN
                "details": {},
            },
            {
                "timestamp": "2026-01-27T14:02:00+09:00",
                "event_type": "CIRCUIT_BREAKER_CLOSED",  # 마지막 CLOSED
                "details": {},
            },
        ]

        result = calculate_incident_duration(timeline)

        # 마지막 CLOSED 이벤트 사용
        assert result.resolved_at == "2026-01-27T14:02:00+09:00"
        assert result.duration_seconds == 120.0  # 14:00:00 → 14:02:00


class TestHelperFunctions:
    """헬퍼 함수 테스트."""

    def test_parse_iso_timestamp_valid(self):
        """유효한 ISO 타임스탬프 파싱."""
        from baldur.utils.duration import parse_iso_timestamp

        result = parse_iso_timestamp("2026-01-27T14:00:00+09:00")
        assert result is not None
        assert result.hour == 14
        assert result.minute == 0

    def test_parse_iso_timestamp_with_z_suffix(self):
        """Z 접미사가 있는 UTC 타임스탬프 파싱."""
        from baldur.utils.duration import parse_iso_timestamp

        result = parse_iso_timestamp("2026-01-27T05:00:00Z")
        assert result is not None
        assert result.hour == 5

    def test_parse_iso_timestamp_invalid(self):
        """잘못된 타임스탬프는 None 반환."""
        from baldur.utils.duration import parse_iso_timestamp

        assert parse_iso_timestamp("invalid") is None
        assert parse_iso_timestamp(None) is None
        assert parse_iso_timestamp("") is None

    def test_calculate_time_diff_seconds(self):
        """두 타임스탬프 간 시간 차이 계산."""
        from baldur.utils.duration import calculate_time_diff_seconds

        diff = calculate_time_diff_seconds(
            "2026-01-27T14:00:00+09:00", "2026-01-27T14:01:30+09:00"
        )
        assert diff == 90.0

    def test_calculate_time_diff_negative_returns_none(self):
        """역순 타임스탬프는 None 반환."""
        from baldur.utils.duration import calculate_time_diff_seconds

        diff = calculate_time_diff_seconds(
            "2026-01-27T14:01:30+09:00", "2026-01-27T14:00:00+09:00"
        )
        assert diff is None

    def test_find_first_event_by_type(self):
        """특정 키워드의 첫 번째 이벤트 찾기."""
        from baldur.utils.duration import find_first_event_by_type

        timeline = [
            {"event_type": "ERROR_BUDGET_CRITICAL", "timestamp": "t1"},
            {"event_type": "CIRCUIT_BREAKER_OPENED", "timestamp": "t2"},
            {"event_type": "CIRCUIT_BREAKER_OPENED", "timestamp": "t3"},
        ]

        event = find_first_event_by_type(timeline, ["opened"])
        assert event["timestamp"] == "t2"

    def test_find_last_event_by_type(self):
        """특정 키워드의 마지막 이벤트 찾기."""
        from baldur.utils.duration import find_last_event_by_type

        timeline = [
            {"event_type": "CIRCUIT_BREAKER_CLOSED", "timestamp": "t1"},
            {"event_type": "ERROR_BUDGET_CRITICAL", "timestamp": "t2"},
            {"event_type": "CIRCUIT_BREAKER_CLOSED", "timestamp": "t3"},
        ]

        event = find_last_event_by_type(timeline, ["closed"])
        assert event["timestamp"] == "t3"


class TestIncidentDurationResult:
    """IncidentDurationResult 클래스 테스트."""

    def test_result_class_attributes(self):
        """IncidentDurationResult 클래스 속성 확인."""
        from baldur.utils.duration import IncidentDurationResult

        result = IncidentDurationResult(
            started_at="2026-01-27T14:00:00+09:00",
            resolved_at="2026-01-27T14:01:00+09:00",
            duration_seconds=60.0,
            downtime_seconds=30.0,
            validation_seconds=30.0,
        )

        assert result.started_at == "2026-01-27T14:00:00+09:00"
        assert result.resolved_at == "2026-01-27T14:01:00+09:00"
        assert result.duration_seconds == 60.0
        assert result.downtime_seconds == 30.0
        assert result.validation_seconds == 30.0

    def test_result_class_optional_fields(self):
        """IncidentDurationResult 선택적 필드 기본값."""
        from baldur.utils.duration import IncidentDurationResult

        result = IncidentDurationResult(
            started_at=None,
            resolved_at="2026-01-27T14:01:00+09:00",
            duration_seconds=None,
        )

        assert result.downtime_seconds is None
        assert result.validation_seconds is None
