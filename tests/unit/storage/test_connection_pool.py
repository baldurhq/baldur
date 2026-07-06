"""
Stage 26: Connection Pool Tests

Scenarios:
1. Normal pool usage
2. High usage warning (70%+)
3. Critical usage (90%+)
4. Pool exhaustion
5. Connection leak detection
6. Automatic leak cleanup
7. Pool expansion on exhaustion
"""

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

from baldur.core.pool_watchdog import (
    PoolRecoveryAction,
    PoolRecoveryHandler,
    PoolWatchdog,
)
from baldur_pro.services.pool_monitor import (
    ConnectionInfo,
    ConnectionPoolMonitor,
    PoolHealthStatus,
    PoolStats,
    PoolStatsProvider,
)


class MockPoolStatsProvider(PoolStatsProvider):
    """Mock pool stats provider for testing"""

    def __init__(self, stats: PoolStats):
        self._stats = stats

    def get_stats(self) -> PoolStats:
        return self._stats

    def set_stats(self, stats: PoolStats) -> None:
        self._stats = stats


class TestPoolHealthMonitoring:
    """Pool health monitoring tests"""

    def test_healthy_pool(self):
        """정상 풀 상태"""
        stats = PoolStats(
            pool_name="default",
            max_connections=100,
            active_connections=30,
            available_connections=70,
        )
        provider = MockPoolStatsProvider(stats)
        monitor = ConnectionPoolMonitor(stats_provider=provider)

        status, _ = monitor.check_health()

        assert status == PoolHealthStatus.HEALTHY

    def test_warning_threshold(self):
        """70% 이상 사용 시 경고"""
        stats = PoolStats(
            pool_name="default",
            max_connections=100,
            active_connections=75,
            available_connections=25,
        )
        provider = MockPoolStatsProvider(stats)
        monitor = ConnectionPoolMonitor(
            stats_provider=provider,
            warning_threshold=70.0,
        )

        status, _ = monitor.check_health()

        assert status == PoolHealthStatus.WARNING

    def test_critical_threshold(self):
        """90% 이상 사용 시 위험"""
        stats = PoolStats(
            pool_name="default",
            max_connections=100,
            active_connections=95,
            available_connections=5,
        )
        provider = MockPoolStatsProvider(stats)
        monitor = ConnectionPoolMonitor(
            stats_provider=provider,
            critical_threshold=90.0,
        )

        status, _ = monitor.check_health()

        assert status == PoolHealthStatus.CRITICAL

    def test_pool_exhausted(self):
        """풀 고갈 감지"""
        stats = PoolStats(
            pool_name="default",
            max_connections=100,
            active_connections=100,
            available_connections=0,
            waiting_requests=10,
        )
        provider = MockPoolStatsProvider(stats)
        monitor = ConnectionPoolMonitor(stats_provider=provider)

        status, _ = monitor.check_health()

        assert status == PoolHealthStatus.EXHAUSTED

    def test_pool_stats_usage_percent(self):
        """사용량 백분율 계산"""
        stats = PoolStats(
            pool_name="test",
            max_connections=100,
            active_connections=50,
            available_connections=50,
        )

        assert stats.usage_percent == 50.0

    def test_pool_stats_zero_max(self):
        """max_connections가 0일 때"""
        stats = PoolStats(
            pool_name="test",
            max_connections=0,
            active_connections=0,
            available_connections=0,
        )

        assert stats.usage_percent == 0.0


class TestConnectionLeakDetection:
    """Connection leak detection tests"""

    def test_no_leak_normal_usage(self):
        """정상 사용 시 누수 없음"""
        monitor = ConnectionPoolMonitor()

        # 연결 획득
        monitor.on_connection_acquired("conn_1")

        # 즉시 반환
        monitor.on_connection_released("conn_1")

        leaks = monitor.detect_leaks(threshold_seconds=1)

        assert leaks.leak_count == 0

    def test_detect_long_held_connection(self):
        """오래 유지된 연결 감지"""
        monitor = ConnectionPoolMonitor()

        # 과거에 획득한 연결 시뮬레이션
        monitor._active_connections["conn_old"] = ConnectionInfo(
            connection_id="conn_old",
            acquired_at=datetime.now(UTC) - timedelta(minutes=10),
        )

        leaks = monitor.detect_leaks(threshold_seconds=60)  # 1분 임계값

        assert leaks.leak_count == 1
        assert leaks.suspected_leaks[0].connection_id == "conn_old"

    def test_track_connection_with_stack_trace(self):
        """스택 트레이스와 함께 연결 추적"""
        monitor = ConnectionPoolMonitor()

        monitor.on_connection_acquired(
            "conn_1",
            stack_trace="File xyz.py, line 123",
            query_info="SELECT * FROM users",
        )

        assert "conn_1" in monitor._active_connections
        info = monitor._active_connections["conn_1"]
        assert info.stack_trace == "File xyz.py, line 123"
        assert info.query_info == "SELECT * FROM users"

    def test_release_nonexistent_connection(self):
        """존재하지 않는 연결 반환 시도"""
        monitor = ConnectionPoolMonitor()

        # Should not raise exception
        monitor.on_connection_released("nonexistent")

        assert "nonexistent" not in monitor._active_connections


class TestPoolTrendAnalysis:
    """Pool trend analysis tests"""

    def test_insufficient_data(self):
        """데이터 부족 시"""
        monitor = ConnectionPoolMonitor()

        trend = monitor.get_trend()

        assert trend["trend"] == "insufficient_data"

    def test_stable_trend(self):
        """안정적인 트렌드"""
        stats = PoolStats(
            pool_name="default",
            max_connections=100,
            active_connections=30,
            available_connections=70,
        )
        provider = MockPoolStatsProvider(stats)
        monitor = ConnectionPoolMonitor(stats_provider=provider)

        # Record several samples
        for _ in range(15):
            monitor.check_health()

        trend = monitor.get_trend()

        assert trend["trend"] == "stable"


class TestPoolWatchdogRecovery:
    """Pool watchdog recovery tests"""

    def test_alert_on_warning(self):
        """경고 시 알림 발송"""
        stats = PoolStats(
            pool_name="default",
            max_connections=100,
            active_connections=75,
            available_connections=25,
        )
        provider = MockPoolStatsProvider(stats)
        monitor = ConnectionPoolMonitor(stats_provider=provider)

        alert_callback = Mock()
        watchdog = PoolWatchdog(
            monitor=monitor,
            alert_callback=alert_callback,
        )

        result = watchdog.check_and_recover()

        assert result.action == PoolRecoveryAction.ALERT_ONLY
        assert alert_callback.called

    def test_close_leaked_connections(self):
        """누수 연결 자동 종료"""
        stats = PoolStats(
            pool_name="default",
            max_connections=100,
            active_connections=50,
            available_connections=50,
        )
        provider = MockPoolStatsProvider(stats)
        monitor = ConnectionPoolMonitor(stats_provider=provider)

        # 누수 연결 추가
        monitor._active_connections["leaked_conn"] = ConnectionInfo(
            connection_id="leaked_conn",
            acquired_at=datetime.now(UTC) - timedelta(minutes=10),
        )

        recovery_handler = Mock(spec=PoolRecoveryHandler)
        recovery_handler.close_connection.return_value = True

        watchdog = PoolWatchdog(
            monitor=monitor,
            recovery_handler=recovery_handler,
            auto_close_leaked=True,
        )

        result = watchdog.check_and_recover()

        assert result.action == PoolRecoveryAction.CLOSE_LEAKED
        assert result.connections_closed == 1

    def test_expand_pool_on_exhaustion(self):
        """고갈 시 풀 확장"""
        stats = PoolStats(
            pool_name="default",
            max_connections=100,
            active_connections=100,
            available_connections=0,
            waiting_requests=5,
        )
        provider = MockPoolStatsProvider(stats)
        monitor = ConnectionPoolMonitor(stats_provider=provider)

        recovery_handler = Mock(spec=PoolRecoveryHandler)
        recovery_handler.expand_pool.return_value = True

        watchdog = PoolWatchdog(
            monitor=monitor,
            recovery_handler=recovery_handler,
            auto_expand=True,
            max_expansion=10,
        )

        result = watchdog.check_and_recover()

        assert result.action == PoolRecoveryAction.EXPAND_POOL
        assert result.success is True

    def test_circuit_break_on_max_expansion(self):
        """최대 확장 후 서킷 브레이크"""
        stats = PoolStats(
            pool_name="default",
            max_connections=100,
            active_connections=100,
            available_connections=0,
            waiting_requests=5,
        )
        provider = MockPoolStatsProvider(stats)
        monitor = ConnectionPoolMonitor(stats_provider=provider)

        recovery_handler = Mock(spec=PoolRecoveryHandler)
        recovery_handler.expand_pool.return_value = True

        watchdog = PoolWatchdog(
            monitor=monitor,
            recovery_handler=recovery_handler,
            auto_expand=True,
            max_expansion=5,
        )

        # 이미 max expansion에 도달했다고 가정
        watchdog._expanded_by = 5

        result = watchdog.check_and_recover()

        assert result.action == PoolRecoveryAction.CIRCUIT_BREAK
        assert result.success is False

    def test_healthy_pool_no_action(self):
        """정상 풀은 액션 없음"""
        stats = PoolStats(
            pool_name="default",
            max_connections=100,
            active_connections=20,
            available_connections=80,
        )
        provider = MockPoolStatsProvider(stats)
        monitor = ConnectionPoolMonitor(stats_provider=provider)

        watchdog = PoolWatchdog(monitor=monitor)

        result = watchdog.check_and_recover()

        assert result.action == PoolRecoveryAction.NONE
        assert result.success is True

    def test_shrink_pool_after_recovery(self):
        """복구 후 풀 축소"""
        stats = PoolStats(
            pool_name="default",
            max_connections=110,  # 확장된 상태
            active_connections=20,
            available_connections=90,
        )
        provider = MockPoolStatsProvider(stats)
        monitor = ConnectionPoolMonitor(stats_provider=provider)

        recovery_handler = Mock(spec=PoolRecoveryHandler)
        recovery_handler.shrink_pool.return_value = True

        watchdog = PoolWatchdog(
            monitor=monitor,
            recovery_handler=recovery_handler,
            auto_expand=True,
        )
        watchdog._expanded_by = 10  # 이전에 10개 확장

        result = watchdog.check_and_recover()

        assert result.success is True
        assert watchdog._expanded_by == 0  # 원래대로 복원

    def test_alert_callback_exception_handling(self):
        """알림 콜백 예외 처리"""
        stats = PoolStats(
            pool_name="default",
            max_connections=100,
            active_connections=75,
            available_connections=25,
        )
        provider = MockPoolStatsProvider(stats)
        monitor = ConnectionPoolMonitor(stats_provider=provider)

        # 예외를 발생시키는 콜백
        def failing_alert(msg, status):
            raise RuntimeError("Alert failed")

        watchdog = PoolWatchdog(
            monitor=monitor,
            alert_callback=failing_alert,
        )

        # 예외가 발생해도 정상 동작해야 함
        result = watchdog.check_and_recover()

        assert result.action == PoolRecoveryAction.ALERT_ONLY
