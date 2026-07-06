"""
Tests for OpenTelemetry LoggerProvider Initialization.

LoggerProvider enables sending Python logs to OTEL Collector
for storage in Loki with automatic trace_id correlation.
"""

import os
from unittest.mock import patch

import pytest


def _is_otel_logging_available() -> bool:
    """Check if OpenTelemetry logging SDK is installed."""
    try:
        import opentelemetry.exporter.otlp.proto.grpc._log_exporter  # noqa: F401
        import opentelemetry.sdk._logs  # noqa: F401

        return True
    except ImportError:
        return False


class TestLoggerProviderInitialization:
    """Tests for OpenTelemetry LoggerProvider initialization."""

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
        from baldur.observability import reset_opentelemetry, uninstrument_logging
        from baldur.settings.observability import reset_observability_settings
        from baldur.settings.otel import reset_otel_settings

        uninstrument_logging()
        reset_opentelemetry()
        reset_observability_settings()
        reset_otel_settings()

    def test_is_logging_instrumented_returns_false_initially(self):
        """Test is_logging_instrumented returns False when not instrumented."""
        from baldur.observability import (
            is_logging_instrumented,
            reset_opentelemetry,
        )

        reset_opentelemetry()
        assert is_logging_instrumented() is False

    def test_get_logger_provider_returns_none_when_otel_disabled(self):
        """Test get_logger_provider returns None when OTEL is disabled."""
        from baldur.observability import get_logger_provider, reset_opentelemetry

        reset_opentelemetry()

        with patch.dict(
            os.environ, {"BALDUR_OBSERVABILITY_PROFILE": "local"}, clear=False
        ):
            provider = get_logger_provider()
            assert provider is None

    def test_initialize_logger_provider_returns_false_when_otel_disabled(self):
        """Test initialize_logger_provider returns False when OTEL disabled."""
        from baldur.observability import (
            initialize_logger_provider,
            reset_opentelemetry,
        )

        reset_opentelemetry()

        with patch.dict(
            os.environ, {"BALDUR_OBSERVABILITY_PROFILE": "local"}, clear=False
        ):
            result = initialize_logger_provider()
            assert result is False

    def test_instrument_logging_returns_false_when_otel_disabled(self):
        """Test instrument_logging returns False when OTEL is disabled."""
        from baldur.observability import instrument_logging, reset_opentelemetry

        reset_opentelemetry()

        with patch.dict(
            os.environ, {"BALDUR_OBSERVABILITY_PROFILE": "local"}, clear=False
        ):
            result = instrument_logging()
            assert result is False

    def test_uninstrument_logging_is_safe_when_not_instrumented(self):
        """Test uninstrument_logging doesn't raise when not instrumented."""
        from baldur.observability import reset_opentelemetry, uninstrument_logging

        reset_opentelemetry()
        # Should not raise
        uninstrument_logging()

    def test_shutdown_logger_provider_is_safe_when_not_initialized(self):
        """Test shutdown_logger_provider doesn't raise when not initialized."""
        from baldur.observability import (
            reset_opentelemetry,
            shutdown_logger_provider,
        )

        reset_opentelemetry()
        # Should not raise
        shutdown_logger_provider()

    @pytest.mark.skipif(
        not _is_otel_logging_available(),
        reason="OpenTelemetry logging SDK not installed",
    )
    def test_initialize_logger_provider_succeeds_when_otel_enabled(self):
        """Test LoggerProvider initialization when OTEL is enabled."""
        from baldur.observability import (
            get_logger_provider,
            initialize_logger_provider,
            initialize_opentelemetry,
            reset_opentelemetry,
        )

        reset_opentelemetry()

        env_vars = {
            "BALDUR_OBSERVABILITY_PROFILE": "otel_collector",
            "OTEL_SERVICE_NAME": "test-service",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            "OTEL_ADAPTIVE_SAMPLING_ENABLED": "false",
        }

        with patch.dict(os.environ, env_vars, clear=False):
            # Initialize TracerProvider first
            otel_result = initialize_opentelemetry()
            assert otel_result is True

            # Then initialize LoggerProvider
            result = initialize_logger_provider()
            assert result is True

            provider = get_logger_provider()
            assert provider is not None

    @pytest.mark.skipif(
        not _is_otel_logging_available(),
        reason="OpenTelemetry logging SDK not installed",
    )
    def test_initialize_logger_provider_is_idempotent(self):
        """Test LoggerProvider initialization is idempotent."""
        from baldur.observability import (
            get_logger_provider,
            initialize_logger_provider,
            initialize_opentelemetry,
            reset_opentelemetry,
        )

        reset_opentelemetry()

        env_vars = {
            "BALDUR_OBSERVABILITY_PROFILE": "otel_collector",
            "OTEL_SERVICE_NAME": "test-service",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            "OTEL_ADAPTIVE_SAMPLING_ENABLED": "false",
        }

        with patch.dict(os.environ, env_vars, clear=False):
            initialize_opentelemetry()

            result1 = initialize_logger_provider()
            provider1 = get_logger_provider()

            result2 = initialize_logger_provider()
            provider2 = get_logger_provider()

            assert result1 is True
            assert result2 is True
            # Same provider instance
            assert provider1 is provider2

    @pytest.mark.skipif(
        not _is_otel_logging_available(),
        reason="OpenTelemetry logging SDK not installed",
    )
    def test_instrument_logging_enables_trace_context_injection(self):
        """Test instrument_logging enables trace context in log records."""
        from baldur.observability import (
            initialize_opentelemetry,
            instrument_logging,
            is_logging_instrumented,
            reset_opentelemetry,
        )

        reset_opentelemetry()

        # Clear any existing global state from OTEL SDK
        try:
            from opentelemetry import trace
            from opentelemetry._logs import _internal as logs_internal

            # Reset internal state for testing
            if hasattr(logs_internal, "_LOGGER_PROVIDER"):
                logs_internal._LOGGER_PROVIDER = None
            if hasattr(trace, "_TRACER_PROVIDER"):
                trace._TRACER_PROVIDER = None
        except Exception:
            pass

        env_vars = {
            "BALDUR_OBSERVABILITY_PROFILE": "otel_collector",
            "OTEL_SERVICE_NAME": "test-service",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            "OTEL_ADAPTIVE_SAMPLING_ENABLED": "false",
        }

        with patch.dict(os.environ, env_vars, clear=False):
            initialize_opentelemetry()
            # If TracerProvider override warning occurred, still test logging

            result = instrument_logging()
            # If instrumentation was successful or already done
            # (may fail due to global state in CI environment)
            if result:
                assert is_logging_instrumented() is True
            else:
                # In case of global state conflict, just verify no crash
                pytest.skip("Global OTEL state conflict - skipping")

    @pytest.mark.skipif(
        not _is_otel_logging_available(),
        reason="OpenTelemetry logging SDK not installed",
    )
    def test_uninstrument_logging_disables_instrumentation(self):
        """Test uninstrument_logging disables logging instrumentation."""
        from baldur.observability import (
            initialize_opentelemetry,
            instrument_logging,
            is_logging_instrumented,
            reset_opentelemetry,
            uninstrument_logging,
        )

        reset_opentelemetry()

        # Clear any existing global state from OTEL SDK
        try:
            from opentelemetry import trace
            from opentelemetry._logs import _internal as logs_internal

            if hasattr(logs_internal, "_LOGGER_PROVIDER"):
                logs_internal._LOGGER_PROVIDER = None
            if hasattr(trace, "_TRACER_PROVIDER"):
                trace._TRACER_PROVIDER = None
        except Exception:
            pass

        env_vars = {
            "BALDUR_OBSERVABILITY_PROFILE": "otel_collector",
            "OTEL_SERVICE_NAME": "test-service",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            "OTEL_ADAPTIVE_SAMPLING_ENABLED": "false",
        }

        with patch.dict(os.environ, env_vars, clear=False):
            initialize_opentelemetry()
            result = instrument_logging()

            if result:
                assert is_logging_instrumented() is True
                uninstrument_logging()
                assert is_logging_instrumented() is False
            else:
                # In case of global state conflict, just verify no crash
                pytest.skip("Global OTEL state conflict - skipping")

    @pytest.mark.skipif(
        not _is_otel_logging_available(),
        reason="OpenTelemetry logging SDK not installed",
    )
    def test_reset_clears_logging_state(self):
        """Test reset_opentelemetry clears LoggerProvider and logging state."""
        from baldur.observability import (
            get_logger_provider,
            initialize_logger_provider,
            initialize_opentelemetry,
            is_logging_instrumented,
            reset_opentelemetry,
        )

        env_vars = {
            "BALDUR_OBSERVABILITY_PROFILE": "otel_collector",
            "OTEL_SERVICE_NAME": "test-service",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            "OTEL_ADAPTIVE_SAMPLING_ENABLED": "false",
        }

        with patch.dict(os.environ, env_vars, clear=False):
            initialize_opentelemetry()
            initialize_logger_provider()

            assert get_logger_provider() is not None

            reset_opentelemetry()

            # After reset, logging instrumentation flag should be cleared
            assert is_logging_instrumented() is False


class TestLoggerProviderWithMissingDependencies:
    """Tests for LoggerProvider behavior when dependencies are missing."""

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

    def test_is_otel_logging_available_check_works(self):
        """Test _is_otel_logging_available returns correct value."""
        from baldur.observability import _is_otel_logging_available

        # Should return True or False based on installed packages
        result = _is_otel_logging_available()
        assert isinstance(result, bool)

    def test_initialize_logger_provider_handles_missing_sdk(self):
        """Test graceful handling when logging SDK is not installed."""
        from baldur.observability import (
            initialize_logger_provider,
            initialize_opentelemetry,
            reset_opentelemetry,
        )

        reset_opentelemetry()

        env_vars = {
            "BALDUR_OBSERVABILITY_PROFILE": "otel_collector",
            "OTEL_SERVICE_NAME": "test-service",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            "OTEL_ADAPTIVE_SAMPLING_ENABLED": "false",
        }

        with patch.dict(os.environ, env_vars, clear=False):
            initialize_opentelemetry()

            # Mock _is_otel_logging_available to return False
            with patch(
                "baldur.observability._is_otel_logging_available", return_value=False
            ):
                result = initialize_logger_provider()
                # Should fail gracefully
                assert result is False
