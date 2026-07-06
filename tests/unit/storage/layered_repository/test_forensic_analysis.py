"""
Shadow Log Forensic 분석 테스트.
"""

from datetime import UTC, datetime, timedelta


class TestShadowLogForensicAnalysis:
    """Shadow Log Forensic 분석 테스트."""

    def setup_method(self):
        """각 테스트 전 ShadowLogger 초기화."""
        from baldur.adapters.memory.circuit_breaker import get_shadow_logger

        shadow_logger = get_shadow_logger()
        shadow_logger.clear()

    def test_analyze_empty_log(self):
        """빈 로그 분석."""
        from baldur.adapters.memory.circuit_breaker import get_shadow_logger

        shadow_logger = get_shadow_logger()

        # When: 빈 상태에서 분석
        analysis = shadow_logger.analyze_l2_failures()

        # Then: 기본값 반환
        assert analysis["unsynced_count"] == 0
        assert analysis["affected_services"] == []
        assert analysis["failure_timeline"] == []
        assert analysis["time_range"] is None
        assert "No L2 failures recorded" in analysis["recommendations"][0]

    def test_analyze_with_failures(self):
        """실패 기록이 있을 때 분석."""
        from baldur.adapters.memory.circuit_breaker import get_shadow_logger

        shadow_logger = get_shadow_logger()

        # Given: 여러 실패 기록
        shadow_logger.record_sync_failure(
            "payment-gateway",
            "open",
            Exception("Connection refused"),
            adapter_type="redis",
            operation="sync",
        )
        shadow_logger.record_sync_failure(
            "order-service",
            "closed",
            Exception("Timeout"),
            adapter_type="redis",
            operation="update",
        )
        shadow_logger.record_sync_failure(
            "payment-gateway",
            "half_open",
            Exception("Connection reset"),
            adapter_type="redis",
            operation="sync",
        )

        # When: 분석 수행
        analysis = shadow_logger.analyze_l2_failures()

        # Then: 정확한 분석 결과
        assert analysis["unsynced_count"] == 3
        assert len(analysis["affected_services"]) == 2
        assert "payment-gateway" in analysis["affected_services"]
        assert "order-service" in analysis["affected_services"]

        # 타임라인 검증
        assert len(analysis["failure_timeline"]) == 3
        for entry in analysis["failure_timeline"]:
            assert "service" in entry
            assert "state" in entry
            assert "time" in entry
            assert "adapter" in entry

        # 어댑터별 통계
        assert analysis["by_adapter"]["redis"] == 3

        # 작업별 통계
        assert analysis["by_operation"]["sync"] == 2
        assert analysis["by_operation"]["update"] == 1

        # 시간 범위
        assert analysis["time_range"] is not None
        assert "start" in analysis["time_range"]
        assert "end" in analysis["time_range"]

    def test_analyze_generates_recommendations(self):
        """권장 조치 생성 테스트."""
        from baldur.adapters.memory.circuit_breaker import get_shadow_logger

        shadow_logger = get_shadow_logger()

        # Given: 미동기화 기록 존재
        shadow_logger.record_sync_failure(
            "test-service", "open", Exception("Error"), adapter_type="redis"
        )

        # When: 분석 수행
        analysis = shadow_logger.analyze_l2_failures()

        # Then: sync 권장 조치 포함
        assert len(analysis["recommendations"]) >= 1
        assert any("Sync" in r for r in analysis["recommendations"])

    def test_analyze_multiple_services_recommendation(self):
        """다수 서비스 영향 시 권장 조치."""
        from baldur.adapters.memory.circuit_breaker import get_shadow_logger

        shadow_logger = get_shadow_logger()

        # Given: 4개 이상 서비스 영향
        for i in range(5):
            shadow_logger.record_sync_failure(
                f"service-{i}", "open", Exception(f"Error {i}"), adapter_type="redis"
            )

        # When: 분석 수행
        analysis = shadow_logger.analyze_l2_failures()

        # Then: 인프라 점검 권장
        assert any("infrastructure" in r.lower() for r in analysis["recommendations"])

    def test_get_records_by_service(self):
        """서비스별 기록 조회."""
        from baldur.adapters.memory.circuit_breaker import get_shadow_logger

        shadow_logger = get_shadow_logger()

        # Given: 여러 서비스 실패
        shadow_logger.record_sync_failure("svc-a", "open", Exception("e1"))
        shadow_logger.record_sync_failure("svc-b", "open", Exception("e2"))
        shadow_logger.record_sync_failure("svc-a", "closed", Exception("e3"))

        # When: svc-a 기록만 조회
        records = shadow_logger.get_records_by_service("svc-a")

        # Then: svc-a 기록만 반환
        assert len(records) == 2
        assert all(r.service_name == "svc-a" for r in records)

    def test_get_records_by_time_range(self):
        """시간 범위별 기록 조회."""
        from baldur.adapters.memory.circuit_breaker import get_shadow_logger

        shadow_logger = get_shadow_logger()

        # Given: 시간 차이가 있는 기록
        now = datetime.now(UTC)

        shadow_logger.record_sync_failure("svc-a", "open", Exception("e1"))

        # When: 현재 시간 범위로 조회
        start_time = now - timedelta(minutes=1)
        end_time = now + timedelta(minutes=1)
        records = shadow_logger.get_records_by_time_range(start_time, end_time)

        # Then: 범위 내 기록 반환
        assert len(records) >= 1
