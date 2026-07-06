"""
Stage 27: Graceful Shutdown Tests

Scenarios:
1. Normal request tracking
2. Graceful shutdown with drain
3. Timeout and forced shutdown
4. Signal handling
5. Request context manager
"""

import threading
import time
from unittest.mock import Mock

from baldur.core.request_context import (
    RequestLifecycleContext,
    track_request,
)
from baldur.core.shutdown_coordinator import (
    GracefulShutdownCoordinator,
    RequestState,
    RequestTracker,
    ShutdownHandler,
    ShutdownPhase,
)


class TestRequestTracking:
    """Request tracking tests"""

    def test_track_request_lifecycle(self):
        """Request tracking lifecycle"""
        tracker = RequestTracker()

        # Start a request
        request = tracker.start_request(
            request_id="req_001",
            endpoint="/api/orders",
            method="POST",
        )

        assert request.state == RequestState.IN_PROGRESS
        assert tracker.get_pending_count() == 1

        # Complete the request
        tracker.end_request("req_001", success=True)

        assert tracker.get_pending_count() == 0

    def test_multiple_concurrent_requests(self):
        """Concurrent multi-request tracking"""
        tracker = RequestTracker()

        # Start several requests
        for i in range(10):
            tracker.start_request(f"req_{i}")

        assert tracker.get_pending_count() == 10

        # Complete some of them
        for i in range(5):
            tracker.end_request(f"req_{i}")

        assert tracker.get_pending_count() == 5

    def test_abort_all_requests(self):
        """Abort all requests"""
        tracker = RequestTracker()

        for i in range(5):
            tracker.start_request(f"req_{i}")

        aborted, completed_count = tracker.abort_all()

        assert len(aborted) == 5
        assert all(r.state == RequestState.ABORTED for r in aborted)
        assert completed_count == 0

    def test_request_with_metadata(self):
        """Track a request with metadata"""
        tracker = RequestTracker()

        request = tracker.start_request(
            request_id="req_001",
            endpoint="/api/payments",
            method="POST",
            metadata={"user_id": 123, "amount": 1000},
        )

        assert request.metadata["user_id"] == 123
        assert request.metadata["amount"] == 1000

    def test_end_nonexistent_request(self):
        """Ending a nonexistent request returns None"""
        tracker = RequestTracker()

        result = tracker.end_request("nonexistent")

        assert result is None

    def test_completed_count(self):
        """Completed request count"""
        tracker = RequestTracker()

        for i in range(3):
            tracker.start_request(f"req_{i}")

        for i in range(3):
            tracker.end_request(f"req_{i}")

        assert tracker.completed_count == 3

    def test_request_duration(self):
        """Request duration"""
        tracker = RequestTracker()

        request = tracker.start_request("req_001")
        time.sleep(0.1)

        assert request.duration_seconds >= 0.1


class TestGracefulShutdown:
    """Graceful shutdown coordinator tests"""

    def test_normal_shutdown_no_pending(self):
        """Normal shutdown with no pending requests"""
        # Migrated off the removed on_shutdown_complete seam (597 D5):
        # completion is observed via the phase and the handler contract.
        tracker = RequestTracker()
        handler = Mock(spec=ShutdownHandler)
        handler.is_drain_complete.return_value = True

        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            drain_timeout=5.0,
            shutdown_handler=handler,
        )

        assert coordinator.is_accepting_requests() is True

        coordinator.initiate_shutdown()
        coordinator.wait_for_shutdown(timeout=2.0)

        assert coordinator.phase == ShutdownPhase.TERMINATED
        handler.on_drain_complete.assert_called_once()

    def test_drain_pending_requests(self):
        """Drain pending requests"""
        tracker = RequestTracker()

        # Start a request
        tracker.start_request("req_001")

        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            drain_timeout=5.0,
        )

        # Initiate shutdown
        coordinator.initiate_shutdown()

        assert coordinator.is_accepting_requests() is False
        assert coordinator.is_shutting_down() is True

        # Complete the request
        tracker.end_request("req_001")

        # Wait for shutdown
        coordinator.wait_for_shutdown(timeout=2.0)

        assert coordinator.phase == ShutdownPhase.TERMINATED
        stats = coordinator.get_stats()
        assert stats.aborted_count == 0

    def test_timeout_forces_shutdown(self):
        """Force shutdown on timeout"""
        tracker = RequestTracker()

        # A request that never completes
        tracker.start_request("slow_req")

        handler = Mock(spec=ShutdownHandler)

        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            drain_timeout=0.5,  # Short timeout
            shutdown_handler=handler,
        )

        coordinator.initiate_shutdown()
        coordinator.wait_for_shutdown(timeout=2.0)

        assert coordinator.phase == ShutdownPhase.TERMINATED
        assert handler.on_force_shutdown.called

        stats = coordinator.get_stats()
        assert stats.aborted_count == 1

    def test_reject_new_requests_during_drain(self):
        """Reject new requests during drain"""
        tracker = RequestTracker()

        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            drain_timeout=5.0,
        )

        assert coordinator.is_accepting_requests() is True

        coordinator.initiate_shutdown()

        assert coordinator.is_accepting_requests() is False

    def test_shutdown_stats_during_drain(self):
        """Stats during drain"""
        tracker = RequestTracker()
        tracker.start_request("req_001")

        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            drain_timeout=10.0,
        )

        coordinator.initiate_shutdown()
        time.sleep(0.1)

        stats = coordinator.get_stats()

        assert stats.phase == ShutdownPhase.DRAINING
        assert stats.in_flight_count == 1
        assert stats.remaining_drain_time is not None
        assert stats.remaining_drain_time < 10.0

    def test_double_initiate_shutdown(self):
        """Duplicate shutdown request is ignored"""
        tracker = RequestTracker()

        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            drain_timeout=5.0,
        )

        coordinator.initiate_shutdown()
        coordinator.initiate_shutdown()  # The second call must be ignored

        coordinator.wait_for_shutdown(timeout=2.0)

        assert coordinator.phase == ShutdownPhase.TERMINATED

    def test_shutdown_handler_callbacks(self):
        """Shutdown handler callbacks"""
        tracker = RequestTracker()

        class TestHandler(ShutdownHandler):
            def __init__(self):
                self.start_called = False
                self.drain_called = False
                self.force_called = False

            def on_shutdown_start(self):
                self.start_called = True

            def on_drain_complete(self):
                self.drain_called = True

            def on_force_shutdown(self, pending):
                self.force_called = True

        handler = TestHandler()

        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            drain_timeout=5.0,
            shutdown_handler=handler,
        )

        coordinator.initiate_shutdown()
        coordinator.wait_for_shutdown(timeout=2.0)

        assert handler.start_called is True
        assert handler.drain_called is True
        assert handler.force_called is False  # No requests, so no force shutdown


class TestRequestLifecycleContext:
    """Request context manager tests"""

    def test_context_manager_tracking(self):
        """Track a request via context manager"""
        tracker = RequestTracker()

        with RequestLifecycleContext(tracker, endpoint="/api/test") as ctx:
            assert tracker.get_pending_count() == 1
            ctx.set_metadata("user_id", 123)

        assert tracker.get_pending_count() == 0

    def test_context_manager_exception_handling(self):
        """Mark as failed on exception"""
        tracker = RequestTracker()

        try:
            with RequestLifecycleContext(tracker, request_id="fail_req"):
                raise ValueError("Test error")
        except ValueError:
            pass

        # The request ended but is recorded as failed
        assert tracker.get_pending_count() == 0

    def test_track_request_helper(self):
        """track_request helper function"""
        tracker = RequestTracker()

        with track_request(tracker, endpoint="/api/orders") as ctx:
            assert ctx.request_id is not None
            assert tracker.get_pending_count() == 1

        assert tracker.get_pending_count() == 0

    def test_context_mark_failed(self):
        """Failure marking"""
        tracker = RequestTracker()

        with RequestLifecycleContext(tracker, request_id="req_001") as ctx:
            ctx.mark_failed()

        # The request ended
        assert tracker.get_pending_count() == 0

    def test_context_set_metadata(self):
        """Metadata setting"""
        tracker = RequestTracker()

        with RequestLifecycleContext(tracker, request_id="req_001") as ctx:
            ctx.set_metadata("order_id", "ORD-123")
            ctx.set_metadata("amount", 5000)

        # Ended normally
        assert tracker.get_pending_count() == 0


class TestShutdownIntegration:
    """Integration scenario tests"""

    def test_realistic_shutdown_scenario(self):
        """
        Realistic scenario: graceful shutdown during a deploy.
        - 3 in-flight requests
        - 2 complete normally
        - 1 is aborted by the timeout
        """
        tracker = RequestTracker()

        # In-flight requests
        tracker.start_request("fast_1")
        tracker.start_request("fast_2")
        tracker.start_request("slow_1")  # This one never completes

        shutdown_log = []

        class TestHandler(ShutdownHandler):
            def on_shutdown_start(self):
                shutdown_log.append("start")

            def on_drain_complete(self):
                shutdown_log.append("drained")

            def on_force_shutdown(self, pending):
                shutdown_log.append(f"forced:{len(pending)}")

        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            drain_timeout=0.5,
            shutdown_handler=TestHandler(),
        )

        # Initiate shutdown
        coordinator.initiate_shutdown()

        # Complete the fast requests
        time.sleep(0.1)
        tracker.end_request("fast_1")
        tracker.end_request("fast_2")

        # Wait for shutdown
        coordinator.wait_for_shutdown(timeout=2.0)

        assert "start" in shutdown_log
        assert "forced:1" in shutdown_log  # slow_1 force-aborted

        stats = coordinator.get_stats()
        assert stats.aborted_count == 1

    def test_concurrent_request_handling(self):
        """Concurrent request handling"""
        tracker = RequestTracker()

        def simulate_request(request_id, duration):
            tracker.start_request(request_id)
            time.sleep(duration)
            tracker.end_request(request_id)

        threads = []
        for i in range(5):
            t = threading.Thread(target=simulate_request, args=(f"req_{i}", 0.1))
            threads.append(t)
            t.start()

        time.sleep(0.05)
        assert tracker.get_pending_count() > 0  # Some requests still in flight

        for t in threads:
            t.join()

        assert tracker.get_pending_count() == 0  # All requests completed

    def test_shutdown_with_context_managers(self):
        """Shutdown with context managers"""
        tracker = RequestTracker()

        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            drain_timeout=5.0,
        )

        # Start a request via context manager
        def make_request():
            with track_request(tracker, endpoint="/api/test"):
                time.sleep(0.2)

        request_thread = threading.Thread(target=make_request)
        request_thread.start()

        time.sleep(0.05)

        # Initiate shutdown
        coordinator.initiate_shutdown()

        request_thread.join()
        coordinator.wait_for_shutdown(timeout=2.0)

        assert coordinator.phase == ShutdownPhase.TERMINATED
        stats = coordinator.get_stats()
        assert stats.aborted_count == 0  # The request completed normally
