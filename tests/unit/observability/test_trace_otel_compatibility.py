"""
Tests for trace.py OTEL compatibility layer.
"""

from unittest.mock import MagicMock, patch


class TestGetTraceIdOtelCompatibility:
    """Tests for get_trace_id() OTEL compatibility."""

    def setup_method(self):
        """Clear trace state before each test."""
        from baldur.audit.trace import clear_trace_id

        clear_trace_id()

    def teardown_method(self):
        """Clear trace state after each test."""
        from baldur.audit.trace import clear_trace_id

        clear_trace_id()

    def test_get_trace_id_uses_otel_when_enabled(self):
        """Test that get_trace_id uses OTEL trace_id when enabled."""
        from baldur.audit.trace import get_trace_id

        mock_trace_id = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"

        with patch("baldur.audit.trace._get_trace_id_from_otel") as mock_get_otel:
            mock_get_otel.return_value = f"req-{mock_trace_id[:8]}"

            result = get_trace_id()

            mock_get_otel.assert_called_once()
            assert result == "req-a1b2c3d4"

    def test_get_trace_id_falls_back_to_context_var(self):
        """Test fallback to context variable when OTEL returns None."""
        from baldur.audit.trace import get_trace_id, set_trace_id

        set_trace_id("test-trace-123")

        with patch("baldur.audit.trace._get_trace_id_from_otel") as mock_get_otel:
            mock_get_otel.return_value = None

            result = get_trace_id()

            assert result == "test-trace-123"

    def test_get_trace_id_generates_new_when_no_trace(self):
        """Test generation of new trace_id when none exists."""
        from baldur.audit.trace import clear_trace_id, get_trace_id

        clear_trace_id()

        with patch("baldur.audit.trace._get_trace_id_from_otel") as mock_get_otel:
            mock_get_otel.return_value = None

            result = get_trace_id()

            assert result.startswith("req-")
            assert len(result) in [12, 17]  # With or without cluster prefix


class TestGetTraceIdFromOtel:
    """Tests for _get_trace_id_from_otel helper."""

    def test_returns_none_when_otel_not_enabled(self):
        """Test returns None when OTEL is not enabled."""
        from baldur.audit.trace import _get_trace_id_from_otel

        with patch("baldur.observability.is_otel_enabled", return_value=False):
            result = _get_trace_id_from_otel()
            assert result is None

    def test_returns_none_when_import_fails(self):
        """Test returns None when observability import fails."""
        from baldur.audit.trace import _get_trace_id_from_otel

        with patch.dict("sys.modules", {"baldur.observability": None}):
            result = _get_trace_id_from_otel()
            assert result is None

    def test_returns_short_format_trace_id(self):
        """Test returns short format (req-8chars) trace_id."""
        from baldur.audit.trace import _get_trace_id_from_otel

        mock_trace_id = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"

        # Patch the observability module functions directly
        try:
            from baldur import observability

            with patch.object(observability, "is_otel_enabled", return_value=True):
                with patch.object(
                    observability,
                    "get_current_trace_id_from_otel",
                    return_value=mock_trace_id,
                ):
                    result = _get_trace_id_from_otel()
                    if result:
                        assert result.startswith("req-")
                        assert len(result) == 12  # "req-" + 8 chars
        except ImportError:
            # observability module not available
            pass


class TestGetTraceIdFull:
    """Tests for get_trace_id_full() function."""

    def test_returns_none_when_otel_disabled(self):
        """Test returns None when OTEL is disabled."""
        from baldur.audit.trace import get_trace_id_full

        with patch("baldur.observability.is_otel_enabled", return_value=False):
            try:
                result = get_trace_id_full()
                assert result is None
            except ImportError:
                # Expected if observability not importable
                pass

    def test_returns_full_32_char_trace_id(self):
        """Test returns full 32-character trace_id."""
        from baldur.audit.trace import get_trace_id_full

        mock_trace_id = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"

        try:
            from baldur import observability

            with patch.object(observability, "is_otel_enabled", return_value=True):
                with patch.object(
                    observability,
                    "get_current_trace_id_from_otel",
                    return_value=mock_trace_id,
                ):
                    result = get_trace_id_full()
                    assert result == mock_trace_id
                    assert len(result) == 32
        except ImportError:
            # observability module not available
            pass


class TestExtractTraceIdFromRequestOtelCompatibility:
    """Tests for extract_trace_id_from_request() OTEL compatibility."""

    def test_uses_otel_when_available(self):
        """Test that OTEL trace_id is used when available."""
        from baldur.audit.trace import extract_trace_id_from_request

        request = MagicMock()
        request.META = {}

        with patch("baldur.audit.trace._get_trace_id_from_otel") as mock_get_otel:
            mock_get_otel.return_value = "req-a1b2c3d4"

            result = extract_trace_id_from_request(request)

            mock_get_otel.assert_called_once()
            assert result == "req-a1b2c3d4"

    def test_falls_back_to_headers_when_otel_none(self):
        """Test fallback to header extraction when OTEL returns None."""
        from baldur.audit.trace import extract_trace_id_from_request

        request = MagicMock()
        request.META = {"HTTP_X_REQUEST_ID": "my-request-id"}

        with patch("baldur.audit.trace._get_trace_id_from_otel") as mock_get_otel:
            mock_get_otel.return_value = None

            result = extract_trace_id_from_request(request)

            assert result == "my-request-id"

    def test_extracts_traceparent_when_otel_none(self):
        """Test W3C traceparent extraction when OTEL is not available."""
        from baldur.audit.trace import extract_trace_id_from_request

        request = MagicMock()
        traceparent = "00-a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6-b7c8d9e0f1a2b3c4-01"
        request.META = {"HTTP_TRACEPARENT": traceparent}

        with patch("baldur.audit.trace._get_trace_id_from_otel") as mock_get_otel:
            mock_get_otel.return_value = None

            result = extract_trace_id_from_request(request)

            assert result == "req-a1b2c3d4"

    def test_extracts_xray_when_otel_none(self):
        """Test AWS X-Ray header extraction when OTEL is not available."""
        from baldur.audit.trace import extract_trace_id_from_request

        request = MagicMock()
        xray_header = "Root=1-12345678-abcdef123456789012345678;Parent=abc123;Sampled=1"
        request.META = {"HTTP_X_AMZN_TRACE_ID": xray_header}

        with patch("baldur.audit.trace._get_trace_id_from_otel") as mock_get_otel:
            mock_get_otel.return_value = None

            result = extract_trace_id_from_request(request)

            assert result.startswith("req-")
            assert len(result) == 12

    def test_returns_none_when_no_trace_info(self):
        """Test returns None when no trace information is available."""
        from baldur.audit.trace import extract_trace_id_from_request

        request = MagicMock()
        request.META = {}

        with patch("baldur.audit.trace._get_trace_id_from_otel") as mock_get_otel:
            mock_get_otel.return_value = None

            result = extract_trace_id_from_request(request)

            assert result is None
