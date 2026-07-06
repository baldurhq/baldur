"""
Postmortem Root Cause 필드 테스트 (문서 129).

Google SRE 표준에 맞춘 trigger, detection, resolution, root_cause_hypothesis 필드 생성 테스트.

테스트 항목:
1. extract_trigger_info - 트리거 정보 추출 테스트
2. extract_detection_info - 감지 정보 추출 테스트
3. extract_resolution_info - 해결 정보 추출 테스트
4. generate_root_cause_hypothesis - 근본 원인 가설 생성 테스트
5. build_postmortem_root_cause_fields - 통합 필드 생성 테스트
6. 빈 타임라인 처리 테스트
"""

from baldur.utils.postmortem_root_cause import (
    build_postmortem_root_cause_fields,
    extract_detection_info,
    extract_resolution_info,
    extract_trigger_info,
    generate_root_cause_hypothesis,
)


class TestExtractTriggerInfo:
    """extract_trigger_info 함수 테스트."""

    def test_extracts_trigger_from_opened_event(self):
        """CB OPEN 이벤트에서 트리거 정보 추출 확인."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:01:23+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {
                    "service_name": "database",
                    "error_context": {
                        "error_type": "ConnectionError",
                        "message": "Database connection timeout",
                    },
                },
            },
            {
                "timestamp": "2026-01-27T14:02:00+09:00",
                "event_type": "circuit_breaker_closed",
                "details": {"service_name": "database"},
            },
        ]

        result = extract_trigger_info(timeline)

        assert result is not None
        assert result["event_type"] == "circuit_breaker_opened"
        assert result["service"] == "database"
        assert result["timestamp"] == "2026-01-27T14:01:23+09:00"
        assert result["error_context"]["error_type"] == "ConnectionError"
        assert result["error_context"]["message"] == "Database connection timeout"

    def test_returns_none_for_empty_timeline(self):
        """빈 타임라인에서 None 반환 확인."""
        result = extract_trigger_info([])
        assert result is None

    def test_returns_none_when_no_open_event(self):
        """OPEN 이벤트 없을 때 None 반환 확인."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:02:00+09:00",
                "event_type": "circuit_breaker_closed",
                "details": {"service_name": "database"},
            },
        ]

        result = extract_trigger_info(timeline)
        assert result is None

    def test_handles_missing_error_context(self):
        """error_context 없을 때 None 처리 확인."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:01:23+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "api"},
            },
        ]

        result = extract_trigger_info(timeline)

        assert result is not None
        assert result["service"] == "api"
        assert result["error_context"] is None


class TestExtractDetectionInfo:
    """extract_detection_info 함수 테스트."""

    def test_extracts_detection_from_opened_event(self):
        """CB OPEN 이벤트에서 감지 정보 추출 확인."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:01:24+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {
                    "service_name": "database",
                    "failure_count": 5,
                    "threshold": 5,
                },
            },
        ]

        result = extract_detection_info(timeline)

        assert result is not None
        assert result["method"] == "circuit_breaker_threshold"
        assert result["detected_at"] == "2026-01-27T14:01:24+09:00"
        assert result["detector"] == "CircuitBreakerService"
        assert result["threshold_exceeded"]["failure_count"] == 5
        assert result["threshold_exceeded"]["threshold"] == 5

    def test_uses_threshold_config_fallback(self):
        """threshold_config에서 threshold 값 가져오기 확인."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:01:24+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "api", "failure_count": 3},
            },
        ]
        threshold_config = {"failure_threshold": 3}

        result = extract_detection_info(timeline, threshold_config)

        assert result is not None
        assert result["threshold_exceeded"]["failure_count"] == 3
        assert result["threshold_exceeded"]["threshold"] == 3

    def test_returns_none_for_empty_timeline(self):
        """빈 타임라인에서 None 반환 확인."""
        result = extract_detection_info([])
        assert result is None


class TestExtractResolutionInfo:
    """extract_resolution_info 함수 테스트."""

    def test_extracts_resolution_with_full_recovery_path(self):
        """전체 복구 경로 (OPEN → HALF_OPEN → CLOSED) 추출 확인."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:01:23+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "database"},
            },
            {
                "timestamp": "2026-01-27T14:01:53+09:00",
                "event_type": "circuit_breaker_half_opened",
                "details": {"service_name": "database"},
            },
            {
                "timestamp": "2026-01-27T14:02:25+09:00",
                "event_type": "circuit_breaker_closed",
                "details": {"service_name": "database"},
            },
        ]

        result = extract_resolution_info(timeline)

        assert result is not None
        assert result["method"] == "automatic_recovery"
        assert result["resolved_at"] == "2026-01-27T14:02:25+09:00"
        assert result["recovery_path"] == "OPEN → HALF_OPEN → CLOSED"
        assert result["manual_intervention"] is False

    def test_extracts_resolution_direct_close(self):
        """직접 CLOSED (OPEN → CLOSED) 경로 추출 확인."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:01:23+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "database"},
            },
            {
                "timestamp": "2026-01-27T14:02:00+09:00",
                "event_type": "circuit_breaker_closed",
                "details": {"service_name": "database"},
            },
        ]

        result = extract_resolution_info(timeline)

        assert result is not None
        assert result["recovery_path"] == "OPEN → CLOSED"

    def test_returns_none_when_not_resolved(self):
        """CLOSED 이벤트 없을 때 None 반환 확인."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:01:23+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "database"},
            },
        ]

        result = extract_resolution_info(timeline)
        assert result is None

    def test_returns_none_for_empty_timeline(self):
        """빈 타임라인에서 None 반환 확인."""
        result = extract_resolution_info([])
        assert result is None


class TestGenerateRootCauseHypothesis:
    """generate_root_cause_hypothesis 함수 테스트."""

    def test_single_service_failure(self):
        """단일 서비스 장애 가설 생성 확인."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:01:23+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {
                    "service_name": "api_service",
                    "error_context": {"error_type": "UnknownError"},
                },
            },
        ]
        affected = ["api_service"]

        result = generate_root_cause_hypothesis(timeline, affected)

        assert result is not None
        assert "Single service failure" in result
        assert "api_service" in result
        assert "UnknownError" in result

    def test_multi_service_failure(self):
        """다중 서비스 장애 가설 생성 확인."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:01:23+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "database"},
            },
        ]
        affected = ["database", "api", "cache"]

        result = generate_root_cause_hypothesis(timeline, affected)

        assert result is not None
        assert "Possible infrastructure-wide failure" in result
        assert "common cause analysis required" in result

    def test_database_error_pattern(self):
        """DB 관련 에러 패턴 가설 확인."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:01:23+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {
                    "service_name": "user_service",
                    "error_context": {
                        "error_type": "DatabaseError",
                        "message": "PostgreSQL connection refused",
                    },
                },
            },
        ]
        affected = ["user_service"]

        result = generate_root_cause_hypothesis(timeline, affected)

        assert result is not None
        assert "Database connection issue" in result

    def test_timeout_error_pattern(self):
        """Timeout 에러 패턴 가설 확인."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:01:23+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {
                    "service_name": "api",
                    "error_context": {
                        "error_type": "TimeoutError",
                        "message": "Request timed out",
                    },
                },
            },
        ]
        affected = ["api"]

        result = generate_root_cause_hypothesis(timeline, affected)

        assert result is not None
        assert "Network latency or service overload" in result

    def test_returns_none_for_empty_data(self):
        """빈 데이터에서 None 반환 확인."""
        result = generate_root_cause_hypothesis([], [])
        assert result is None


class TestBuildPostmortemRootCauseFields:
    """build_postmortem_root_cause_fields 함수 테스트."""

    def test_builds_all_fields(self):
        """모든 root cause 필드 생성 확인."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:01:23+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {
                    "service_name": "database",
                    "failure_count": 5,
                    "threshold": 5,
                    "error_context": {
                        "error_type": "ConnectionError",
                        "message": "Connection timeout",
                    },
                },
            },
            {
                "timestamp": "2026-01-27T14:01:53+09:00",
                "event_type": "circuit_breaker_half_opened",
                "details": {"service_name": "database"},
            },
            {
                "timestamp": "2026-01-27T14:02:25+09:00",
                "event_type": "circuit_breaker_closed",
                "details": {"service_name": "database"},
            },
        ]
        affected = ["database"]

        result = build_postmortem_root_cause_fields(timeline, affected)

        # 모든 필드 존재 확인
        assert "trigger" in result
        assert "detection" in result
        assert "resolution" in result
        assert "root_cause_hypothesis" in result

        # trigger 검증
        assert result["trigger"]["event_type"] == "circuit_breaker_opened"
        assert result["trigger"]["service"] == "database"

        # detection 검증
        assert result["detection"]["method"] == "circuit_breaker_threshold"

        # resolution 검증
        assert result["resolution"]["method"] == "automatic_recovery"
        assert result["resolution"]["recovery_path"] == "OPEN → HALF_OPEN → CLOSED"

        # root_cause_hypothesis 검증
        assert result["root_cause_hypothesis"] is not None
        assert "database" in result["root_cause_hypothesis"]

    def test_handles_empty_timeline(self):
        """빈 타임라인 처리 확인."""
        result = build_postmortem_root_cause_fields([], [])

        assert result["trigger"] is None
        assert result["detection"] is None
        assert result["resolution"] is None
        assert result["root_cause_hypothesis"] is None

    def test_handles_incomplete_timeline(self):
        """불완전한 타임라인 (CLOSED 없음) 처리 확인."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:01:23+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "api"},
            },
        ]
        affected = ["api"]

        result = build_postmortem_root_cause_fields(timeline, affected)

        # trigger, detection은 있어야 함
        assert result["trigger"] is not None
        assert result["detection"] is not None

        # resolution은 CLOSED 없으므로 None
        assert result["resolution"] is None

        # root_cause_hypothesis는 있어야 함
        assert result["root_cause_hypothesis"] is not None


class TestPostmortemDataIntegration:
    """_generate_postmortem_data 통합 테스트.

    Note: observability 모듈은 Django 의존성이 있으므로,
    순수 유틸리티 함수 테스트로 대체합니다.
    """

    def test_build_fields_integrates_with_postmortem_structure(self):
        """build_postmortem_root_cause_fields가 Post-mortem 구조와 통합 가능 확인."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:01:23+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {
                    "service_name": "database",
                    "error_context": {"error_type": "ConnectionError"},
                },
            },
            {
                "timestamp": "2026-01-27T14:02:25+09:00",
                "event_type": "circuit_breaker_closed",
                "details": {"service_name": "database"},
            },
        ]
        affected = ["database"]

        # 실제 observability.py가 호출하는 것과 동일한 함수 호출
        from baldur.utils.postmortem_root_cause import (
            build_postmortem_root_cause_fields,
        )

        result = build_postmortem_root_cause_fields(timeline, affected)

        # Post-mortem에 추가될 필드들 검증
        assert "trigger" in result
        assert "detection" in result
        assert "resolution" in result
        assert "root_cause_hypothesis" in result

        # trigger 필드 상세 확인
        assert result["trigger"]["event_type"] == "circuit_breaker_opened"
        assert result["trigger"]["service"] == "database"

        # detection 필드 상세 확인
        assert result["detection"]["method"] == "circuit_breaker_threshold"

        # resolution 필드 상세 확인
        assert result["resolution"]["method"] == "automatic_recovery"

        # root_cause_hypothesis 확인 (connection 키워드로 DB 패턴 매칭)
        assert "Database connection issue" in result["root_cause_hypothesis"]
