"""
Tests for Audit OTEL Integration (Phase 5).

Tests for:
- ExternalTraceContext.from_current_otel_context()
- AuditLogger trace_id_full field
"""

import os
from unittest.mock import patch


class TestExternalTraceContextOtelIntegration:
    """Tests for ExternalTraceContext OTEL integration."""

    def setup_method(self):
        """Reset OTEL state before each test."""
        from baldur.observability import reset_opentelemetry
        from baldur.settings.observability import reset_observability_settings
        from baldur.settings.otel import reset_otel_settings

        reset_opentelemetry()
        reset_observability_settings()
        reset_otel_settings()

    def teardown_method(self):
        """Clean up after each test."""
        from baldur.observability import reset_opentelemetry
        from baldur.settings.observability import reset_observability_settings
        from baldur.settings.otel import reset_otel_settings

        reset_opentelemetry()
        reset_observability_settings()
        reset_otel_settings()

    def test_from_headers_extracts_traceparent(self):
        """from_headers correctly extracts W3C traceparent."""
        from baldur.audit.cascade_event import ExternalTraceContext

        headers = {
            "traceparent": "00-a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6-1234567890abcdef-01",
            "x-request-id": "req-123",
            "x-correlation-id": "corr-456",
        }

        ctx = ExternalTraceContext.from_headers(headers)

        assert ctx.trace_id == "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
        assert ctx.span_id == "1234567890abcdef"
        assert ctx.trace_flags == "01"
        assert ctx.trace_id_short == "req-a1b2c3d4"
        assert ctx.request_id == "req-123"
        assert ctx.correlation_id == "corr-456"

    def test_from_headers_handles_baggage(self):
        """from_headers correctly parses baggage header."""
        from baldur.audit.cascade_event import ExternalTraceContext

        headers = {
            "baggage": "key1=value1, key2=value2",
        }

        ctx = ExternalTraceContext.from_headers(headers)

        assert ctx.baggage.get("key1") == "value1"
        assert ctx.baggage.get("key2") == "value2"

    def test_from_current_otel_context_when_disabled(self):
        """from_current_otel_context returns empty when OTEL disabled."""
        from baldur.audit.cascade_event import ExternalTraceContext

        with patch.dict(
            os.environ, {"BALDUR_OBSERVABILITY_PROFILE": "local"}, clear=False
        ):
            ctx = ExternalTraceContext.from_current_otel_context()

            assert ctx.trace_id is None
            assert ctx.span_id is None

    def test_to_dict_includes_trace_id_short(self):
        """to_dict includes trace_id_short field."""
        from baldur.audit.cascade_event import ExternalTraceContext

        ctx = ExternalTraceContext(
            trace_id="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
            trace_id_short="req-a1b2c3d4",
        )

        result = ctx.to_dict()

        assert result["trace_id"] == "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
        assert result["trace_id_short"] == "req-a1b2c3d4"


class TestAuditLoggerTraceIdFull:
    """Tests for AuditLogger trace_id_full field."""

    def setup_method(self):
        """Reset state before each test."""
        from baldur.audit.trace import clear_trace_id
        from baldur.observability import reset_opentelemetry
        from baldur.settings.observability import reset_observability_settings
        from baldur.settings.otel import reset_otel_settings

        reset_opentelemetry()
        reset_observability_settings()
        reset_otel_settings()
        clear_trace_id()

    def teardown_method(self):
        """Clean up after each test."""
        from baldur.audit.trace import clear_trace_id
        from baldur.observability import reset_opentelemetry
        from baldur.settings.observability import reset_observability_settings
        from baldur.settings.otel import reset_otel_settings

        reset_opentelemetry()
        reset_observability_settings()
        reset_otel_settings()
        clear_trace_id()

    def test_build_entry_includes_trace_id(self):
        """_build_h1_entry includes trace_id in details."""
        from baldur.audit.logger import AuditLogger
        from baldur.audit.trace import set_trace_id

        set_trace_id("req-test123")

        logger = AuditLogger(enable_console_log=False)
        entry = logger._build_h1_entry(
            {
                "config_type": "TEST_CONFIG",
                "config_key": "test_key",
                "action": "update",
            }
        )

        assert entry.details.get("trace_id") == "req-test123"

    def test_build_entry_trace_id_full_none_when_otel_disabled(self):
        """trace_id_full is None when OTEL is disabled."""
        from baldur.audit.logger import AuditLogger
        from baldur.audit.trace import set_trace_id

        with patch.dict(
            os.environ, {"BALDUR_OBSERVABILITY_PROFILE": "local"}, clear=False
        ):
            set_trace_id("req-abc123")

            logger = AuditLogger(enable_console_log=False)
            entry = logger._build_h1_entry(
                {
                    "config_type": "TEST_CONFIG",
                    "config_key": "key",
                    "action": "update",
                }
            )

            # trace_id_full should be None when OTEL is disabled
            assert entry.details.get("trace_id_full") is None
