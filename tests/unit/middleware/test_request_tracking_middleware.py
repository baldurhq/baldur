"""RequestTrackingMiddleware unit tests (impl 471 D10, D11, D12).

Coverage:
- ``__call__`` success path → ``end_request(success=True)``
- ``__call__`` 5xx return path → ``end_request(success=False)`` via mark_failed
- ``__call__`` raise path → ``end_request(success=False)`` via __exit__
- request-id source: reads ``request.trace_id`` when present, falls back
  to ``generate_trace_id()`` otherwise
- in-flight count via threading.Event (deterministic)
- no-op when ``coordinator._tracker`` is None
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from baldur.api.django.middleware.request_tracking import RequestTrackingMiddleware
from baldur.core.shutdown_coordinator import RequestTracker

# =============================================================================
# Test helpers
# =============================================================================


class _FakeRequest:
    def __init__(
        self,
        path: str = "/api/orders/",
        method: str = "POST",
        trace_id: str | None = "tr-1234",
    ):
        self.path = path
        self.method = method
        self.META: dict = {}
        if trace_id is not None:
            self.trace_id = trace_id


class _FakeResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code


def _make_middleware(coordinator, get_response):
    """Build a RequestTrackingMiddleware with the coordinator pre-injected."""
    with patch(
        "baldur.core.shutdown_coordinator.get_shutdown_coordinator",
        return_value=coordinator,
    ):
        return RequestTrackingMiddleware(get_response)


# =============================================================================
# __call__ — success / 5xx / raise × tracker present / None
# =============================================================================


class TestRequestTrackingMiddlewareBehavior:
    """Lifecycle wrapping across success/5xx/raise paths."""

    def test_success_path_calls_end_request_with_success_true(self):
        """2xx response → end_request(success=True)."""
        tracker = MagicMock(spec=RequestTracker)
        coordinator = MagicMock()
        coordinator._tracker = tracker

        ok_response = _FakeResponse(status_code=200)
        get_response = MagicMock(return_value=ok_response)
        mw = _make_middleware(coordinator, get_response)

        result = mw(_FakeRequest())

        assert result is ok_response
        tracker.start_request.assert_called_once()
        tracker.end_request.assert_called_once()
        # success kwarg is True (set by RequestLifecycleContext.__exit__).
        _, kwargs = tracker.end_request.call_args
        assert kwargs["success"] is True

    def test_5xx_return_path_marks_failed(self):
        """5xx response (no exception) → end_request(success=False).

        D10 second failure mode: a custom Django 500 handler can swallow
        the exception and return 5xx — the middleware MUST call
        ``ctx.mark_failed()`` so the request lands in ABORTED, not COMPLETED.
        """
        tracker = MagicMock(spec=RequestTracker)
        coordinator = MagicMock()
        coordinator._tracker = tracker

        bad_response = _FakeResponse(status_code=503)
        get_response = MagicMock(return_value=bad_response)
        mw = _make_middleware(coordinator, get_response)

        mw(_FakeRequest())

        tracker.end_request.assert_called_once()
        _, kwargs = tracker.end_request.call_args
        assert kwargs["success"] is False

    def test_get_response_raises_propagates_and_marks_failed(self):
        """Exception from get_response propagates AND marks the request failed."""
        tracker = MagicMock(spec=RequestTracker)
        coordinator = MagicMock()
        coordinator._tracker = tracker

        get_response = MagicMock(side_effect=RuntimeError("boom"))
        mw = _make_middleware(coordinator, get_response)

        with pytest.raises(RuntimeError, match="boom"):
            mw(_FakeRequest())

        tracker.end_request.assert_called_once()
        _, kwargs = tracker.end_request.call_args
        assert kwargs["success"] is False

    def test_no_op_when_tracker_is_none(self):
        """When coordinator._tracker is None, middleware is a transparent passthrough."""
        coordinator = MagicMock()
        coordinator._tracker = None

        ok_response = _FakeResponse(status_code=200)
        get_response = MagicMock(return_value=ok_response)
        mw = _make_middleware(coordinator, get_response)

        result = mw(_FakeRequest())

        assert result is ok_response
        get_response.assert_called_once()


# =============================================================================
# Request-id source (D11)
# =============================================================================


class TestRequestTrackingMiddlewareRequestId:
    """request-id source: ``request.trace_id`` reuse + fallback."""

    def test_uses_request_trace_id_when_present(self):
        """``request.trace_id`` is forwarded to start_request as request_id."""
        tracker = MagicMock(spec=RequestTracker)
        coordinator = MagicMock()
        coordinator._tracker = tracker

        get_response = MagicMock(return_value=_FakeResponse(200))
        mw = _make_middleware(coordinator, get_response)

        mw(_FakeRequest(trace_id="upstream-trace-xyz"))

        _, kwargs = tracker.start_request.call_args
        assert kwargs["request_id"] == "upstream-trace-xyz"

    def test_falls_back_to_generate_trace_id_when_attribute_missing(self):
        """Defensive fallback when ``trace_id_middleware`` was removed/reordered."""
        tracker = MagicMock(spec=RequestTracker)
        coordinator = MagicMock()
        coordinator._tracker = tracker

        get_response = MagicMock(return_value=_FakeResponse(200))
        mw = _make_middleware(coordinator, get_response)

        with patch(
            "baldur.api.django.middleware.request_tracking.generate_trace_id",
            return_value="fallback-trace-id",
        ):
            mw(_FakeRequest(trace_id=None))

        _, kwargs = tracker.start_request.call_args
        assert kwargs["request_id"] == "fallback-trace-id"


# =============================================================================
# Pending-count visibility (D10 / G6)
# =============================================================================


class TestRequestTrackingMiddlewareInFlightVisibility:
    """In-flight count is observable to the coordinator while a request runs."""

    def test_pending_count_reflects_in_flight_request(self):
        """Tracker.pending_count == 1 while get_response is in progress.

        Uses a real RequestTracker (no mock) so the assertion exercises the
        full start_request/end_request lifecycle. Two threading.Events
        synchronize: ``inside`` lets the assertion run mid-request,
        ``finish`` releases get_response so end_request fires.
        """
        tracker = RequestTracker(max_request_age_seconds=300.0)
        coordinator = MagicMock()
        coordinator._tracker = tracker

        inside = threading.Event()
        finish = threading.Event()

        def slow_get_response(_request):
            inside.set()
            assert finish.wait(timeout=5.0)
            return _FakeResponse(200)

        mw = _make_middleware(coordinator, slow_get_response)

        thread = threading.Thread(target=lambda: mw(_FakeRequest()))
        thread.start()
        try:
            assert inside.wait(timeout=5.0)
            assert tracker.get_pending_count() == 1
        finally:
            finish.set()
            thread.join(timeout=5.0)

        # After the worker exits, the tracker should be drained.
        assert tracker.get_pending_count() == 0
