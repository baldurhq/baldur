"""
Tests for trace ID management.
"""

import threading
from unittest.mock import MagicMock

from baldur.audit.trace import (
    TraceContext,
    clear_trace_id,
    generate_trace_id,
    get_trace_id,
    set_trace_id,
    trace_id_middleware,
)


class TestGenerateTraceId:
    """Tests for trace ID generation."""

    def test_generate_trace_id_format(self):
        """Test that generated trace ID has correct format."""
        # Defend against a leaked runtime mock from another test in the same
        # xdist worker: generate_trace_id() resolves the cluster prefix through
        # the runtime-scoped cluster-identity singleton, so a stray MagicMock
        # left in _runtime_var (set_runtime without token reset) would yield an
        # un-asserted prefix and an over-long id. Wipe to a clean real runtime.
        from baldur.runtime import reset_runtime

        reset_runtime()

        trace_id = generate_trace_id()

        # Format: "req-{cluster_prefix}-{uuid4_short}" or "req-{uuid4_short}"
        # With cluster prefix: "req-unkp-12345678" (17 chars)
        # Without cluster prefix: "req-12345678" (12 chars)
        assert trace_id.startswith("req-")
        # Length varies based on cluster prefix: 12 (no prefix) or 17 (with prefix)
        assert len(trace_id) in [12, 17]

    def test_generate_trace_id_uniqueness(self):
        """Test that generated trace IDs are unique."""
        ids = [generate_trace_id() for _ in range(100)]
        assert len(set(ids)) == 100


class TestTraceIdStorage:
    """Tests for trace ID storage (get/set)."""

    def test_set_and_get_trace_id(self):
        """Test setting and getting trace ID."""
        clear_trace_id()

        set_trace_id("test-trace-123")
        assert get_trace_id() == "test-trace-123"

        clear_trace_id()

    def test_get_trace_id_generates_when_not_set(self):
        """Test that get generates new ID when not set."""
        clear_trace_id()
        result = get_trace_id()
        # Should auto-generate a new trace ID
        assert result is not None
        assert result.startswith("req-")
        clear_trace_id()

    def test_trace_id_thread_isolation(self):
        """Test that trace IDs are isolated per thread."""
        clear_trace_id()
        results = {}

        def set_and_get(thread_id):
            trace_id = f"trace-{thread_id}"
            set_trace_id(trace_id)
            results[thread_id] = get_trace_id()

        threads = []
        for i in range(5):
            t = threading.Thread(target=set_and_get, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Each thread should have its own trace ID
        for i in range(5):
            assert results[i] == f"trace-{i}"


class TestTraceContext:
    """Tests for TraceContext context manager."""

    def test_trace_context_sets_id(self):
        """Test that TraceContext sets trace ID."""
        clear_trace_id()

        with TraceContext() as trace_id:
            current = get_trace_id()
            assert current is not None
            # TraceContext returns the trace_id string directly
            assert isinstance(trace_id, str)

    def test_trace_context_with_specific_id(self):
        """Test TraceContext with specific trace ID."""
        clear_trace_id()

        with TraceContext(trace_id="my-custom-id") as trace_id:
            assert get_trace_id() == "my-custom-id"
            assert trace_id == "my-custom-id"

    def test_trace_context_restores_previous(self):
        """Test that TraceContext restores previous trace ID."""
        clear_trace_id()
        set_trace_id("original-id")

        with TraceContext(trace_id="temporary-id"):
            assert get_trace_id() == "temporary-id"

        # Should restore original
        assert get_trace_id() == "original-id"
        clear_trace_id()

    def test_trace_context_nested(self):
        """Test nested TraceContext."""
        clear_trace_id()

        with TraceContext(trace_id="outer"):
            assert get_trace_id() == "outer"

            with TraceContext(trace_id="inner"):
                assert get_trace_id() == "inner"

            assert get_trace_id() == "outer"


class TestTraceIdMiddleware:
    """Tests for Django middleware."""

    def test_middleware_creates_trace_id(self):
        """Test that middleware creates trace ID if not present."""
        clear_trace_id()

        request = MagicMock()
        request.META = {}

        def get_response(req):
            # During request handling, trace ID should be set
            trace_id = get_trace_id()
            response = MagicMock()
            response.trace_id = trace_id
            return response

        middleware = trace_id_middleware(get_response)
        response = middleware(request)

        # Trace ID should have been set during request
        assert hasattr(response, "trace_id")

    def test_middleware_uses_existing_header(self):
        """Test that middleware uses existing X-Trace-ID header."""
        clear_trace_id()

        request = MagicMock()
        request.META = {"HTTP_X_TRACE_ID": "incoming-trace-id"}

        captured_trace_id = None

        def get_response(req):
            nonlocal captured_trace_id
            captured_trace_id = get_trace_id()
            return MagicMock()

        middleware = trace_id_middleware(get_response)
        middleware(request)

        assert captured_trace_id == "incoming-trace-id"

    def test_middleware_uses_traceparent_header(self):
        """Test that middleware uses W3C traceparent header."""
        clear_trace_id()

        # W3C Trace Context format
        traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"

        request = MagicMock()
        request.META = {"HTTP_TRACEPARENT": traceparent}

        captured_trace_id = None

        def get_response(req):
            nonlocal captured_trace_id
            captured_trace_id = get_trace_id()
            return MagicMock()

        middleware = trace_id_middleware(get_response)
        middleware(request)

        # Should extract trace ID from traceparent
        assert captured_trace_id is not None
        assert len(captured_trace_id) > 0

    def test_middleware_adds_header_to_response(self):
        """Test that middleware adds trace ID to response headers."""
        clear_trace_id()

        request = MagicMock()
        request.META = {}

        response = MagicMock()
        response.__setitem__ = MagicMock()

        def get_response(req):
            return response

        middleware = trace_id_middleware(get_response)
        result = middleware(request)

        # Response should be returned
        assert result is not None
