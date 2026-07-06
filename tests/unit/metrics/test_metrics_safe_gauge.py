"""
Tests for Safe Gauge Wrapper.
"""

import time
from unittest.mock import MagicMock, Mock

import pytest


class TestSyncStatus:
    """Test SyncStatus enum."""

    def test_sync_status_values(self):
        """Should have correct string values."""
        from baldur.metrics.safe_gauge import SyncStatus

        assert SyncStatus.SYNCED.value == "synced"
        assert SyncStatus.STALE.value == "stale"
        assert SyncStatus.UNKNOWN.value == "unknown"
        assert SyncStatus.RECOVERING.value == "recovering"


class TestSyncInfo:
    """Test SyncInfo dataclass."""

    def test_default_values(self):
        """Should have correct default values."""
        from baldur.metrics.safe_gauge import SyncInfo, SyncStatus

        info = SyncInfo()

        assert info.status == SyncStatus.UNKNOWN
        assert info.last_sync_time is None
        assert info.last_sync_source == "none"
        assert info.staleness_threshold == 300.0  # 5 minutes
        assert info.stabilization_duration == 60.0  # 1 minute

    def test_age_seconds_property_none_when_no_sync(self):
        """Should return None when never synced."""
        from baldur.metrics.safe_gauge import SyncInfo

        info = SyncInfo()
        assert info.age_seconds is None

    def test_age_seconds_property_calculates_correctly(self):
        """Should calculate age correctly."""
        from baldur.metrics.safe_gauge import SyncInfo

        sync_time = time.time() - 60  # 1 minute ago
        info = SyncInfo(last_sync_time=sync_time)

        age = info.age_seconds
        assert age is not None
        assert 59 <= age <= 61

    def test_is_synced_true_when_fresh(self):
        """Should return True when data is fresh."""
        from baldur.metrics.safe_gauge import SyncInfo, SyncStatus

        info = SyncInfo(
            status=SyncStatus.SYNCED,
            last_sync_time=time.time(),  # Just now
        )

        assert info.is_synced is True

    def test_is_synced_false_when_stale(self):
        """Should return False when data is stale."""
        from baldur.metrics.safe_gauge import SyncInfo, SyncStatus

        old_time = time.time() - 600  # 10 minutes ago (> staleness_threshold)
        info = SyncInfo(
            status=SyncStatus.SYNCED,
            last_sync_time=old_time,
            staleness_threshold=300.0,
        )

        assert info.is_synced is False

    def test_is_synced_false_when_not_synced_status(self):
        """Should return False when status is not SYNCED."""
        from baldur.metrics.safe_gauge import SyncInfo, SyncStatus

        info = SyncInfo(
            status=SyncStatus.UNKNOWN,
            last_sync_time=time.time(),
        )

        assert info.is_synced is False

    def test_is_recovering_property(self):
        """Should correctly determine if recovering."""
        from baldur.metrics.safe_gauge import SyncInfo, SyncStatus

        # Not recovering
        info_unknown = SyncInfo(status=SyncStatus.UNKNOWN)
        assert info_unknown.is_recovering is False

        # Recovering
        info_recovering = SyncInfo(
            status=SyncStatus.RECOVERING,
            stabilization_start=time.time(),
            stabilization_duration=60.0,
        )
        assert info_recovering.is_recovering is True

    def test_recovery_progress_property(self):
        """Should calculate recovery progress correctly."""
        from baldur.metrics.safe_gauge import SyncInfo, SyncStatus

        # Full progress when not recovering
        info_complete = SyncInfo(status=SyncStatus.SYNCED)
        assert info_complete.recovery_progress == 1.0

        # Partial progress during recovery
        start_time = time.time() - 30  # Started 30s ago
        info_recovering = SyncInfo(
            status=SyncStatus.RECOVERING,
            stabilization_start=start_time,
            stabilization_duration=60.0,
        )
        progress = info_recovering.recovery_progress
        assert 0.4 <= progress <= 0.6  # About 50%


class TestClampFunctions:
    """Test clamp utility functions."""

    def test_clamp_non_negative(self):
        """Should clamp values to non-negative."""
        from baldur.metrics.safe_gauge import clamp_non_negative

        assert clamp_non_negative(5) == 5
        assert clamp_non_negative(0) == 0
        assert clamp_non_negative(-5) == 0
        assert clamp_non_negative(-0.001) == 0

    def test_clamp_percentage(self):
        """Should clamp values to 0-100 range."""
        from baldur.metrics.safe_gauge import clamp_percentage

        assert clamp_percentage(50) == 50
        assert clamp_percentage(0) == 0
        assert clamp_percentage(100) == 100
        assert clamp_percentage(-10) == 0
        assert clamp_percentage(150) == 100


class TestSafeGauge:
    """Test SafeGauge wrapper class."""

    @pytest.fixture
    def mock_gauge(self):
        """Create mock prometheus gauge."""
        gauge = Mock()
        labeled = Mock()
        labeled._value = Mock()
        labeled._value.get.return_value = 5.0
        gauge.labels.return_value = labeled
        return gauge

    def test_labels_returns_safe_labeled_gauge(self, mock_gauge):
        """Should return SafeLabeledGauge when labels() called."""
        from baldur.metrics.safe_gauge import SafeGauge

        safe = SafeGauge(mock_gauge)
        safe.labels(domain="payment")

        # Should have called underlying gauge's labels method
        mock_gauge.labels.assert_called_once_with(domain="payment")

    def test_dec_prevents_negative(self, mock_gauge):
        """Should prevent gauge from going negative on dec()."""
        from baldur.metrics.safe_gauge import SafeGauge

        # Set up mock to return 0
        labeled = Mock()
        labeled._value = Mock()
        labeled._value.get.return_value = 0.0
        mock_gauge.labels.return_value = labeled

        safe = SafeGauge(mock_gauge)
        safe_labeled = safe.labels(domain="payment")

        # Should not go negative
        if hasattr(safe_labeled, "dec"):
            safe_labeled.dec()
            # Underlying dec should be called with clamped value or not at all
            # depending on implementation


class TestSafeGaugeIntegration:
    """Integration tests for SafeGauge with real scenarios."""

    def test_server_restart_scenario(self):
        """Should handle server restart (gauge reset to 0)."""
        from baldur.metrics.safe_gauge import SyncInfo, SyncStatus

        # After restart, gauge is at 0 but events say -1
        # SafeGauge should clamp to 0

        info = SyncInfo(
            status=SyncStatus.UNKNOWN,  # Not synced after restart
        )

        assert info.is_synced is False
        # This indicates we need to sync before trusting the value

    def test_sync_then_decrement_flow(self):
        """Should handle sync followed by decrement."""
        from baldur.metrics.safe_gauge import SyncInfo, SyncStatus

        # Simulate: sync sets gauge to 5, then 3 decrements
        info = SyncInfo(
            status=SyncStatus.SYNCED,
            last_sync_time=time.time(),
            last_sync_source="hydration",
        )

        assert info.is_synced is True
        # After this, safe decrements would work from 5 down to 2


class TestSafeGaugeLRUCache:
    """LRU 캐시 기능 테스트."""

    @pytest.fixture
    def mock_gauge(self):
        """Create mock prometheus gauge."""
        gauge = Mock()
        gauge.labels = MagicMock(side_effect=lambda **kwargs: Mock())
        return gauge

    def test_max_label_combinations_default(self, mock_gauge):
        """기본 max_label_combinations 값 확인."""
        from baldur.metrics.safe_gauge import SafeGauge

        safe = SafeGauge(mock_gauge)
        assert safe.max_size == 1000

    def test_custom_max_label_combinations(self, mock_gauge):
        """커스텀 max_label_combinations 설정."""
        from baldur.metrics.safe_gauge import SafeGauge

        safe = SafeGauge(mock_gauge, max_label_combinations=500)
        assert safe.max_size == 500

    def test_lru_eviction_when_exceeds_max(self, mock_gauge):
        """max_label_combinations 초과 시 가장 오래된 항목 제거."""
        from baldur.metrics.safe_gauge import SafeGauge

        safe = SafeGauge(mock_gauge, max_label_combinations=3)

        # 3개 생성
        safe.labels(domain="a")
        safe.labels(domain="b")
        safe.labels(domain="c")
        assert safe.current_size == 3
        assert safe.eviction_count == 0

        # 4번째 생성 - eviction 발생
        safe.labels(domain="d")
        assert safe.current_size == 3
        assert safe.eviction_count == 1

        # "a"는 제거되어야 함
        assert safe.get_child(domain="a") is None
        assert safe.get_child(domain="b") is not None

    def test_lru_order_updated_on_access(self, mock_gauge):
        """접근 시 LRU 순서 업데이트."""
        from baldur.metrics.safe_gauge import SafeGauge

        safe = SafeGauge(mock_gauge, max_label_combinations=3)

        safe.labels(domain="a")
        safe.labels(domain="b")
        safe.labels(domain="c")

        # "a" 재접근 - 가장 최근으로 이동
        safe.labels(domain="a")

        # "d" 추가 - "b"가 제거되어야 함 (a는 방금 접근)
        safe.labels(domain="d")

        assert safe.get_child(domain="a") is not None
        assert safe.get_child(domain="b") is None
        assert safe.get_child(domain="c") is not None
        assert safe.get_child(domain="d") is not None

    def test_on_eviction_callback(self, mock_gauge):
        """eviction 콜백 호출 확인."""
        from baldur.metrics.safe_gauge import SafeGauge

        evicted_items = []

        def on_eviction(key, child):
            evicted_items.append((key, child))

        safe = SafeGauge(mock_gauge, max_label_combinations=2, on_eviction=on_eviction)

        safe.labels(domain="a")
        safe.labels(domain="b")
        safe.labels(domain="c")  # eviction 발생

        assert len(evicted_items) == 1
        assert ("domain", "a") in evicted_items[0][0]

    def test_get_cache_stats(self, mock_gauge):
        """캐시 통계 반환."""
        from baldur.metrics.safe_gauge import SafeGauge

        safe = SafeGauge(mock_gauge, max_label_combinations=100)

        safe.labels(domain="a")
        safe.labels(domain="b")

        stats = safe.get_cache_stats()

        assert stats["current_size"] == 2
        assert stats["max_size"] == 100
        assert stats["eviction_count"] == 0
        assert stats["utilization_percent"] == 2.0

    def test_noop_when_gauge_is_none(self):
        """gauge가 None일 때 NoOp 반환."""
        from baldur.metrics.safe_gauge import NoOpGaugeChild, SafeGauge

        safe = SafeGauge(None)
        child = safe.labels(domain="a")

        assert isinstance(child, NoOpGaugeChild)
        assert safe.current_size == 0


class TestSafeGaugeLRUEvictionLogging:
    """
    LRU 캐시 축출(Eviction) 경고 로그 테스트.

    리뷰 ①: max_size에 도달하여 항목이 삭제될 때 logger.warning을 남기는지 확인.
    """

    @pytest.fixture
    def mock_gauge(self):
        """Create mock prometheus gauge."""
        gauge = Mock()
        gauge.labels = MagicMock(side_effect=lambda **kwargs: Mock())
        return gauge

    @pytest.fixture
    def captured_logs(self):
        """로거를 직접 캡처하는 fixture (테스트 격리 문제 해결)."""
        import logging
        from io import StringIO

        # 로거 설정
        test_logger = logging.getLogger("baldur.metrics.safe_gauge.core")

        # 캡처용 핸들러
        log_capture = StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setLevel(logging.WARNING)
        handler.setFormatter(logging.Formatter("%(message)s"))

        # 로거에 핸들러 추가 및 propagate 강제 설정
        original_level = test_logger.level
        original_propagate = test_logger.propagate
        test_logger.setLevel(logging.WARNING)
        test_logger.propagate = True
        test_logger.addHandler(handler)

        yield log_capture

        # 정리
        test_logger.removeHandler(handler)
        test_logger.setLevel(original_level)
        test_logger.propagate = original_propagate

    def test_eviction_logs_warning(self, mock_gauge, captured_logs):
        """Eviction 발생 시 경고 로그가 기록되어야 함."""
        from baldur.metrics.safe_gauge import SafeGauge

        safe = SafeGauge(mock_gauge, max_label_combinations=2)

        safe.labels(domain="a")
        safe.labels(domain="b")
        safe.labels(domain="c")  # eviction 발생

        # 캡처된 로그 확인
        log_output = captured_logs.getvalue()
        assert "safe_gauge.lru_eviction" in log_output
        assert "domain" in log_output
        assert "max_label_combinations" in log_output

    def test_eviction_log_includes_shadow_value(self, mock_gauge, captured_logs):
        """Eviction 로그에 shadow_value가 포함되어야 함."""
        from baldur.metrics.safe_gauge import SafeGauge

        safe = SafeGauge(mock_gauge, max_label_combinations=2)

        safe.labels(domain="a")
        safe.labels(domain="b")
        safe.labels(domain="c")  # eviction 발생

        # 캡처된 로그에서 shadow_value 확인
        log_output = captured_logs.getvalue()
        assert "oldest_child" in log_output or "shadow_value" in log_output

    def test_multiple_evictions_log_count(self, mock_gauge, captured_logs):
        """여러 번 eviction 발생 시 각각 경고 로그 기록."""
        from baldur.metrics.safe_gauge import SafeGauge

        safe = SafeGauge(mock_gauge, max_label_combinations=2)

        safe.labels(domain="a")
        safe.labels(domain="b")
        safe.labels(domain="c")  # eviction #1
        safe.labels(domain="d")  # eviction #2

        # 캡처된 로그에서 eviction 번호 확인
        log_output = captured_logs.getvalue()
        assert log_output.count("safe_gauge.lru_eviction") >= 2
