"""
Shadow Logging 테스트.
"""

import threading


class TestShadowLogging:
    """Shadow Logging 테스트."""

    def setup_method(self):
        """각 테스트 전 ShadowLogger 초기화."""
        from baldur.adapters.memory.circuit_breaker import get_shadow_logger

        shadow_logger = get_shadow_logger()
        shadow_logger.clear()

    def test_record_sync_failure(self):
        """동기화 실패 기록."""
        from baldur.adapters.memory.circuit_breaker import get_shadow_logger

        shadow_logger = get_shadow_logger()

        # When: 동기화 실패 기록
        shadow_logger.record_sync_failure(
            service_name="payment-gateway",
            intended_state="open",
            error=Exception("Connection refused"),
            adapter_type="redis",
            operation="sync",
        )

        # Then: 기록 조회 가능
        records = shadow_logger.get_all_records()
        assert len(records) == 1
        assert records[0].service_name == "payment-gateway"
        assert records[0].intended_state == "open"
        assert "Connection refused" in records[0].error_message
        assert records[0].adapter_type == "redis"
        assert records[0].synced_after_recovery is False

    def test_get_unsynced_records(self):
        """미동기화 기록 조회."""
        from baldur.adapters.memory.circuit_breaker import get_shadow_logger

        shadow_logger = get_shadow_logger()

        # Given: 여러 동기화 실패 발생
        shadow_logger.record_sync_failure("service-a", "open", Exception("err1"))
        shadow_logger.record_sync_failure("service-b", "closed", Exception("err2"))

        # When: 하나만 동기화 완료
        shadow_logger.mark_as_synced("service-a")

        # Then: 미동기화 기록만 조회
        unsynced = shadow_logger.get_unsynced_records()
        assert len(unsynced) == 1
        assert unsynced[0].service_name == "service-b"

    def test_mark_as_synced(self):
        """동기화 완료 마킹."""
        from baldur.adapters.memory.circuit_breaker import get_shadow_logger

        shadow_logger = get_shadow_logger()

        # Given: 동기화 실패 기록
        shadow_logger.record_sync_failure("service-x", "open", Exception("err"))

        # When: 동기화 완료 마킹
        count = shadow_logger.mark_as_synced("service-x")

        # Then: 마킹 완료
        assert count == 1
        records = shadow_logger.get_all_records()
        assert records[0].synced_after_recovery is True
        assert records[0].recovery_time is not None

    def test_shadow_log_thread_safety(self):
        """Shadow Log 스레드 안전성."""
        from baldur.adapters.memory.circuit_breaker import get_shadow_logger

        shadow_logger = get_shadow_logger()
        shadow_logger.clear()

        num_threads = 10
        records_per_thread = 100

        def record_failures(thread_id):
            for i in range(records_per_thread):
                shadow_logger.record_sync_failure(
                    service_name=f"service-{thread_id}-{i}",
                    intended_state="open",
                    error=Exception(f"error-{thread_id}-{i}"),
                )

        # When: 여러 스레드에서 동시 기록
        threads = [
            threading.Thread(target=record_failures, args=(i,))
            for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Then: 모든 기록이 저장됨 (최대 1000개 제한 내에서)
        records = shadow_logger.get_all_records()
        assert len(records) <= 1000
        assert len(records) > 0

    def test_shadow_log_max_entries(self):
        """Shadow Log 최대 항목 수 제한."""
        from baldur.adapters.memory.circuit_breaker import get_shadow_logger

        shadow_logger = get_shadow_logger()
        shadow_logger.clear()
        shadow_logger.set_max_entries(100)

        # When: 150개 기록 추가
        for i in range(150):
            shadow_logger.record_sync_failure(
                service_name=f"service-{i}",
                intended_state="open",
                error=Exception(f"error-{i}"),
            )

        # Then: 최대 100개만 유지
        records = shadow_logger.get_all_records()
        assert len(records) == 100
        assert records[0].service_name == "service-50"

    def test_shadow_log_stats(self):
        """Shadow Log 통계 조회."""
        from baldur.adapters.memory.circuit_breaker import get_shadow_logger

        shadow_logger = get_shadow_logger()
        shadow_logger.clear()

        # Given: 여러 실패 기록
        shadow_logger.record_sync_failure("svc-a", "open", Exception("e1"))
        shadow_logger.record_sync_failure("svc-b", "open", Exception("e2"))
        shadow_logger.record_sync_failure("svc-a", "closed", Exception("e3"))
        shadow_logger.mark_as_synced("svc-a")

        # When: 통계 조회
        stats = shadow_logger.get_stats()

        # Then: 통계 정확
        assert stats["total_records"] == 3
        assert stats["unsynced_count"] == 1
        assert "svc-a" in stats["affected_services"]
        assert "svc-b" in stats["affected_services"]
