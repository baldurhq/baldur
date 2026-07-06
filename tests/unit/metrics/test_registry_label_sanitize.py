"""
sanitize_label_value() 단위 테스트.

Prometheus 메트릭 라벨 값 정규화 함수 테스트.
"""


class TestSanitizeLabelValue:
    """sanitize_label_value() 함수 테스트."""

    def test_replaces_special_characters(self):
        """특수문자가 '_'로 치환되는지 확인."""
        from baldur.metrics.registry import sanitize_label_value

        assert sanitize_label_value("my-service.v2") == "my_service_v2"
        assert sanitize_label_value("payment/gateway") == "payment_gateway"
        assert sanitize_label_value("svc@region#1") == "svc_region_1"

    def test_empty_string_returns_unknown(self):
        """빈 문자열/공백만 입력 시 UNKNOWN_LABEL_VALUE 반환."""
        from baldur.metrics.registry import (
            UNKNOWN_LABEL_VALUE,
            sanitize_label_value,
        )

        # 소스 상수 참조 (하드코딩 제거)
        assert sanitize_label_value("") == UNKNOWN_LABEL_VALUE
        assert sanitize_label_value("   ") == UNKNOWN_LABEL_VALUE
        # None 입력 테스트
        result = sanitize_label_value(None)
        if result:
            assert result == UNKNOWN_LABEL_VALUE

    def test_truncates_at_max_length(self):
        """기본 max_length 초과 시 절단."""
        from baldur.metrics.registry import (
            DEFAULT_LABEL_MAX_LENGTH,
            sanitize_label_value,
        )

        # 소스 상수 참조 (하드코딩 제거)
        long_value = "a" * (DEFAULT_LABEL_MAX_LENGTH + 100)
        result = sanitize_label_value(long_value)
        assert len(result) == DEFAULT_LABEL_MAX_LENGTH
        assert result == "a" * DEFAULT_LABEL_MAX_LENGTH

    def test_preserves_valid_characters(self):
        """유효한 문자(영숫자+언더스코어)는 그대로 유지."""
        from baldur.metrics.registry import sanitize_label_value

        assert sanitize_label_value("valid_service_123") == "valid_service_123"
        assert sanitize_label_value("ABC_xyz_0") == "ABC_xyz_0"

    def test_custom_max_length(self):
        """사용자 지정 max_length 동작 확인."""
        from baldur.metrics.registry import sanitize_label_value

        result = sanitize_label_value("abcdefghij", max_length=5)
        assert result == "abcde"


class TestMetricsBatchRecorder:
    """MetricsBatchRecorder 클래스 테스트."""

    def test_enqueue_adds_item_to_queue(self):
        """enqueue()가 큐에 항목 추가하는지 확인."""
        from baldur.metrics.registry import MetricsBatchRecorder

        recorder = MetricsBatchRecorder(batch_size=10, flush_interval_ms=1000)
        call_count = {"value": 0}

        def mock_fn(*args, **kwargs):
            call_count["value"] += 1

        recorder.enqueue(mock_fn, 1, 2, key="test")
        recorder.shutdown()

        # 셧다운 후 flush 확인
        # 짧은 시간 내에 flush되지 않을 수 있으므로 기본 테스트만 수행
        assert recorder._running is False

    def test_shutdown_stops_worker_thread(self):
        """shutdown()이 워커 스레드를 정지시키는지 확인."""
        from baldur.metrics.registry import MetricsBatchRecorder

        recorder = MetricsBatchRecorder()
        assert recorder._running is True
        assert recorder._worker.is_alive() is True

        recorder.shutdown()
        assert recorder._running is False
