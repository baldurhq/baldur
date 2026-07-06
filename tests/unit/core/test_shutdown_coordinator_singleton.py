"""
Tests for ShutdownCoordinator singleton and optional request_tracker (395 C0).

Covers:
- get_shutdown_coordinator / reset_shutdown_coordinator singleton lifecycle
- GracefulShutdownCoordinator with request_tracker=None (handler-only mode)
- Thread safety of singleton creation
"""

import threading
from unittest.mock import MagicMock

import pytest

from baldur.core.shutdown_coordinator import (
    GracefulShutdownCoordinator,
    RequestTracker,
    ShutdownHandler,
    ShutdownPhase,
    get_shutdown_coordinator,
    reset_shutdown_coordinator,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset singleton before and after each test."""
    reset_shutdown_coordinator()
    yield
    reset_shutdown_coordinator()


# =============================================================================
# Singleton Lifecycle (§8.10)
# =============================================================================


class TestShutdownCoordinatorSingletonBehavior:
    """get_shutdown_coordinator / reset_shutdown_coordinator 동작 검증."""

    def test_get_returns_same_instance(self):
        """동일 인스턴스를 반환한다."""
        first = get_shutdown_coordinator()
        second = get_shutdown_coordinator()
        assert first is second

    def test_reset_clears_cached_instance(self):
        """reset 후 새 인스턴스가 생성된다."""
        first = get_shutdown_coordinator()
        reset_shutdown_coordinator()
        second = get_shutdown_coordinator()
        assert first is not second

    def test_get_returns_graceful_shutdown_coordinator_type(self):
        """반환 타입이 GracefulShutdownCoordinator이다."""
        coordinator = get_shutdown_coordinator()
        assert isinstance(coordinator, GracefulShutdownCoordinator)

    def test_request_tracker_none_creates_handler_only_mode(self):
        """request_tracker=None으로 handler-only 모드가 생성된다."""
        coordinator = get_shutdown_coordinator(request_tracker=None)
        assert coordinator._tracker is None

    def test_request_tracker_passed_on_first_call_only(self):
        """request_tracker는 첫 호출에서만 적용된다."""
        tracker = RequestTracker()
        first = get_shutdown_coordinator(request_tracker=tracker)
        assert first._tracker is tracker

        # Second call with different tracker — ignored
        other_tracker = RequestTracker()
        second = get_shutdown_coordinator(request_tracker=other_tracker)
        assert second._tracker is tracker  # Still the original

    def test_initial_phase_is_running(self):
        """생성된 코디네이터의 초기 상태는 RUNNING이다."""
        coordinator = get_shutdown_coordinator()
        assert coordinator.phase == ShutdownPhase.RUNNING


# =============================================================================
# Thread Safety (§8.7)
# =============================================================================


class TestShutdownCoordinatorThreadSafetyBehavior:
    """멀티스레드 싱글톤 생성 안전성 검증."""

    def test_concurrent_get_returns_same_instance(self):
        """10개 스레드에서 동시 호출해도 동일 인스턴스를 반환한다."""
        results = []
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            results.append(get_shutdown_coordinator())

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        assert all(r is results[0] for r in results)


# =============================================================================
# Handler-only mode (request_tracker=None) — §8.2 Edge Cases
# =============================================================================


class TestHandlerOnlyModeBehavior:
    """request_tracker=None 상태에서의 드레인/셧다운 동작 검증."""

    def test_get_stats_with_no_tracker_returns_zero_in_flight(self):
        """tracker 없을 때 get_stats()는 in_flight_count=0을 반환한다."""
        coordinator = GracefulShutdownCoordinator(request_tracker=None)
        stats = coordinator.get_stats()
        assert stats.in_flight_count == 0

    def test_is_accepting_requests_with_no_tracker(self):
        """tracker 없어도 is_accepting_requests()는 정상 동작한다."""
        coordinator = GracefulShutdownCoordinator(request_tracker=None)
        assert coordinator.is_accepting_requests() is True

    def test_drain_completes_immediately_with_no_tracker_no_handlers(self):
        """tracker와 handler 모두 없으면 드레인이 즉시 완료된다."""
        coordinator = GracefulShutdownCoordinator(
            request_tracker=None,
            drain_timeout=2.0,
            check_interval=0.05,
        )
        coordinator.initiate_shutdown()
        completed = coordinator.wait_for_shutdown(timeout=3.0)
        assert completed is True
        assert coordinator.phase == ShutdownPhase.TERMINATED

    def test_drain_with_handler_and_no_tracker(self):
        """tracker 없이 handler만 있으면 handler 드레인만 체크한다."""
        handler = MagicMock(spec=ShutdownHandler)
        handler.is_drain_complete.return_value = True

        coordinator = GracefulShutdownCoordinator(
            request_tracker=None,
            drain_timeout=2.0,
            shutdown_handler=handler,
            check_interval=0.05,
        )
        coordinator.initiate_shutdown()
        completed = coordinator.wait_for_shutdown(timeout=3.0)

        assert completed is True
        handler.on_shutdown_start.assert_called_once()
        handler.on_drain_complete.assert_called_once()

    def test_force_shutdown_with_no_tracker_passes_empty_pending(self):
        """tracker 없을 때 force shutdown은 빈 pending_requests를 전달한다."""
        handler = MagicMock(spec=ShutdownHandler)
        handler.is_drain_complete.return_value = False  # Never drain

        coordinator = GracefulShutdownCoordinator(
            request_tracker=None,
            drain_timeout=0.1,  # Very short to force timeout
            shutdown_handler=handler,
            check_interval=0.05,
        )
        coordinator.initiate_shutdown()
        coordinator.wait_for_shutdown(timeout=2.0)

        handler.on_force_shutdown.assert_called_once()
        pending = handler.on_force_shutdown.call_args[0][0]
        assert pending == []
