"""
Tests for OpenTelemetry SDK Initialization.
"""

import os
from unittest.mock import patch

import pytest


def _is_otel_available() -> bool:
    """Check if OpenTelemetry SDK is installed."""
    try:
        import opentelemetry.sdk.trace  # noqa: F401

        return True
    except ImportError:
        return False


class TestOpenTelemetryInitialization:
    """Tests for OpenTelemetry SDK initialization module."""

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

    def test_initialization_disabled_by_default(self):
        """Test that OTEL is not initialized when the profile is local."""
        from baldur.observability import (
            initialize_opentelemetry,
            is_otel_enabled,
            reset_opentelemetry,
        )

        reset_opentelemetry()

        with patch.dict(
            os.environ, {"BALDUR_OBSERVABILITY_PROFILE": "local"}, clear=False
        ):
            result = initialize_opentelemetry()

        assert result is False
        assert is_otel_enabled() is False

    def test_is_otel_enabled_returns_false_when_disabled(self):
        """Test is_otel_enabled returns False when OTEL is disabled."""
        from baldur.observability import is_otel_enabled, reset_opentelemetry

        reset_opentelemetry()

        with patch.dict(
            os.environ, {"BALDUR_OBSERVABILITY_PROFILE": "local"}, clear=False
        ):
            assert is_otel_enabled() is False

    def test_get_tracer_returns_none_when_disabled(self):
        """Test get_tracer returns None when OTEL is disabled."""
        from baldur.observability import get_tracer, reset_opentelemetry

        reset_opentelemetry()

        with patch.dict(
            os.environ, {"BALDUR_OBSERVABILITY_PROFILE": "local"}, clear=False
        ):
            tracer = get_tracer()
            assert tracer is None

    def test_get_tracer_provider_returns_none_when_disabled(self):
        """Test get_tracer_provider returns None when OTEL is disabled."""
        from baldur.observability import get_tracer_provider, reset_opentelemetry

        reset_opentelemetry()

        with patch.dict(
            os.environ, {"BALDUR_OBSERVABILITY_PROFILE": "local"}, clear=False
        ):
            provider = get_tracer_provider()
            assert provider is None

    def test_get_current_trace_id_from_otel_returns_none_when_disabled(self):
        """Test trace ID extraction returns None when OTEL is disabled."""
        from baldur.observability import (
            get_current_trace_id_from_otel,
            reset_opentelemetry,
        )

        reset_opentelemetry()

        with patch.dict(
            os.environ, {"BALDUR_OBSERVABILITY_PROFILE": "local"}, clear=False
        ):
            trace_id = get_current_trace_id_from_otel()
            assert trace_id is None

    def test_get_current_span_id_from_otel_returns_none_when_disabled(self):
        """Test span ID extraction returns None when OTEL is disabled."""
        from baldur.observability import (
            get_current_span_id_from_otel,
            reset_opentelemetry,
        )

        reset_opentelemetry()

        with patch.dict(
            os.environ, {"BALDUR_OBSERVABILITY_PROFILE": "local"}, clear=False
        ):
            span_id = get_current_span_id_from_otel()
            assert span_id is None

    def test_get_current_span_returns_none_when_disabled(self):
        """Test get_current_span returns None when OTEL is disabled."""
        from baldur.observability import get_current_span, reset_opentelemetry

        reset_opentelemetry()

        with patch.dict(
            os.environ, {"BALDUR_OBSERVABILITY_PROFILE": "local"}, clear=False
        ):
            span = get_current_span()
            assert span is None

    def test_initialization_idempotent(self):
        """Test that initialization is idempotent."""
        from baldur.observability import (
            initialize_opentelemetry,
            reset_opentelemetry,
        )

        reset_opentelemetry()

        with patch.dict(
            os.environ, {"BALDUR_OBSERVABILITY_PROFILE": "local"}, clear=False
        ):
            result1 = initialize_opentelemetry()
            result2 = initialize_opentelemetry()

            # Both should return False (disabled)
            assert result1 is False
            assert result2 is False

    def test_shutdown_is_safe_when_not_initialized(self):
        """Test that shutdown doesn't raise when not initialized."""
        from baldur.observability import (
            reset_opentelemetry,
            shutdown_opentelemetry,
        )

        reset_opentelemetry()

        # Should not raise
        shutdown_opentelemetry()

    def test_reset_allows_reinitialization(self):
        """Test that reset allows reinitialization."""
        from baldur.observability import (
            initialize_opentelemetry,
            is_otel_enabled,
            reset_opentelemetry,
        )

        with patch.dict(
            os.environ, {"BALDUR_OBSERVABILITY_PROFILE": "local"}, clear=False
        ):
            initialize_opentelemetry()
            assert is_otel_enabled() is False

            reset_opentelemetry()

            # After reset, should be able to reinitialize
            initialize_opentelemetry()
            assert is_otel_enabled() is False


class TestOpenTelemetryWithOtelInstalled:
    """Tests for OTEL initialization when SDK is installed."""

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
        from baldur.observability import (
            reset_opentelemetry,
            shutdown_opentelemetry,
        )
        from baldur.settings.observability import reset_observability_settings
        from baldur.settings.otel import reset_otel_settings

        shutdown_opentelemetry()
        reset_opentelemetry()
        reset_observability_settings()
        reset_otel_settings()

    @pytest.mark.skipif(
        not _is_otel_available(),
        reason="OpenTelemetry SDK not installed",
    )
    def test_initialization_enabled_with_otel_installed(self):
        """Test OTEL initialization when enabled and SDK is installed."""
        from baldur.observability import (
            get_tracer,
            initialize_opentelemetry,
            is_otel_enabled,
            reset_opentelemetry,
        )

        reset_opentelemetry()

        env_vars = {
            "BALDUR_OBSERVABILITY_PROFILE": "otel_collector",
            "OTEL_SERVICE_NAME": "test-service",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            "OTEL_ADAPTIVE_SAMPLING_ENABLED": "false",  # Disable to avoid EmergencyMode dependency
        }

        with patch.dict(os.environ, env_vars, clear=False):
            result = initialize_opentelemetry()

            if result:
                assert is_otel_enabled() is True
                assert get_tracer() is not None


class TestOtelInitializationExporterBranch:
    """593 D8 — ``initialize_opentelemetry()`` branches on ``traces_exporter``.

    The TracerProvider, sampler, and composite propagator are installed in
    every branch; only the export path differs:
      - ``otlp``    → ``OTLPSpanExporter`` + ``BatchSpanProcessor``
      - ``console`` → ``ConsoleSpanExporter`` + ``SimpleSpanProcessor``
      - ``none``    → no span processor (provider still live, nothing exported)

    The exporter/processor classes are patched at their import sites so no real
    export thread or gRPC channel is created.
    """

    def setup_method(self):
        from baldur.observability import reset_opentelemetry
        from baldur.settings.observability import reset_observability_settings
        from baldur.settings.otel import reset_otel_settings

        reset_opentelemetry()
        reset_observability_settings()
        reset_otel_settings()

    def teardown_method(self):
        from baldur.observability import (
            reset_opentelemetry,
            shutdown_opentelemetry,
        )
        from baldur.settings.observability import reset_observability_settings
        from baldur.settings.otel import reset_otel_settings

        shutdown_opentelemetry()
        reset_opentelemetry()
        reset_observability_settings()
        reset_otel_settings()

    @pytest.mark.skipif(
        not _is_otel_available(),
        reason="OpenTelemetry SDK not installed",
    )
    def test_otlp_exporter_uses_batch_span_processor(self):
        """``otlp`` (default) constructs OTLPSpanExporter + BatchSpanProcessor."""
        from baldur.observability import (
            initialize_opentelemetry,
            reset_opentelemetry,
        )

        reset_opentelemetry()

        env_vars = {
            "BALDUR_OBSERVABILITY_PROFILE": "otel_collector",
            "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_ADAPTIVE_SAMPLING_ENABLED": "false",
        }

        with patch.dict(os.environ, env_vars, clear=False):
            with (
                patch(
                    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter."
                    "OTLPSpanExporter"
                ) as m_otlp,
                patch("opentelemetry.sdk.trace.export.BatchSpanProcessor") as m_batch,
                patch(
                    "opentelemetry.sdk.trace.export.ConsoleSpanExporter"
                ) as m_console,
                patch("opentelemetry.sdk.trace.export.SimpleSpanProcessor") as m_simple,
            ):
                result = initialize_opentelemetry()

        assert result is True
        m_otlp.assert_called_once()
        m_batch.assert_called_once()
        m_console.assert_not_called()
        m_simple.assert_not_called()

    @pytest.mark.skipif(
        not _is_otel_available(),
        reason="OpenTelemetry SDK not installed",
    )
    def test_console_exporter_uses_simple_span_processor(self):
        """``console`` constructs ConsoleSpanExporter + SimpleSpanProcessor."""
        from baldur.observability import (
            initialize_opentelemetry,
            reset_opentelemetry,
        )

        reset_opentelemetry()

        env_vars = {
            "BALDUR_OBSERVABILITY_PROFILE": "otel_collector",
            "OTEL_TRACES_EXPORTER": "console",
            "OTEL_ADAPTIVE_SAMPLING_ENABLED": "false",
        }

        with patch.dict(os.environ, env_vars, clear=False):
            with (
                patch(
                    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter."
                    "OTLPSpanExporter"
                ) as m_otlp,
                patch("opentelemetry.sdk.trace.export.BatchSpanProcessor") as m_batch,
                patch(
                    "opentelemetry.sdk.trace.export.ConsoleSpanExporter"
                ) as m_console,
                patch("opentelemetry.sdk.trace.export.SimpleSpanProcessor") as m_simple,
            ):
                result = initialize_opentelemetry()

        assert result is True
        m_console.assert_called_once()
        m_simple.assert_called_once()
        m_otlp.assert_not_called()
        m_batch.assert_not_called()

    @pytest.mark.skipif(
        not _is_otel_available(),
        reason="OpenTelemetry SDK not installed",
    )
    def test_none_exporter_adds_no_processor_provider_still_live(self):
        """``none`` installs no exporter/processor but keeps the provider live."""
        from baldur.observability import (
            initialize_opentelemetry,
            is_otel_enabled,
            reset_opentelemetry,
        )

        reset_opentelemetry()

        env_vars = {
            "BALDUR_OBSERVABILITY_PROFILE": "otel_collector",
            "OTEL_TRACES_EXPORTER": "none",
            "OTEL_ADAPTIVE_SAMPLING_ENABLED": "false",
        }

        with patch.dict(os.environ, env_vars, clear=False):
            with (
                patch(
                    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter."
                    "OTLPSpanExporter"
                ) as m_otlp,
                patch("opentelemetry.sdk.trace.export.BatchSpanProcessor") as m_batch,
                patch(
                    "opentelemetry.sdk.trace.export.ConsoleSpanExporter"
                ) as m_console,
                patch("opentelemetry.sdk.trace.export.SimpleSpanProcessor") as m_simple,
            ):
                result = initialize_opentelemetry()

        # Provider/sampler/propagator are live; the export path is empty.
        assert result is True
        assert is_otel_enabled() is True
        m_otlp.assert_not_called()
        m_batch.assert_not_called()
        m_console.assert_not_called()
        m_simple.assert_not_called()


class TestSamplerInstallation:
    """643 D4 — ``initialize_opentelemetry()`` installs the ``_select_sampler``
    result on the live ``TracerProvider``.

    The ``_select_sampler`` unit tests bypass ``initialize_opentelemetry()``;
    this closes the selection->installation seam where the strategy could be
    resolved correctly but never wired onto the provider.
    """

    def setup_method(self):
        from baldur.observability import reset_opentelemetry
        from baldur.settings.observability import reset_observability_settings
        from baldur.settings.otel import reset_otel_settings

        reset_opentelemetry()
        reset_observability_settings()
        reset_otel_settings()

    def teardown_method(self):
        from baldur.observability import (
            reset_opentelemetry,
            shutdown_opentelemetry,
        )
        from baldur.settings.observability import reset_observability_settings
        from baldur.settings.otel import reset_otel_settings

        shutdown_opentelemetry()
        reset_opentelemetry()
        reset_observability_settings()
        reset_otel_settings()

    @pytest.mark.skipif(
        not _is_otel_available(),
        reason="OpenTelemetry SDK not installed",
    )
    def test_initialize_installs_selected_sampler(self):
        """The provider's sampler is exactly what ``_select_sampler`` returns."""
        from baldur.observability import (
            _select_sampler,
            get_tracer_provider,
            initialize_opentelemetry,
            reset_opentelemetry,
        )
        from baldur.settings.otel import get_otel_settings

        reset_opentelemetry()

        env_vars = {
            "BALDUR_OBSERVABILITY_PROFILE": "otel_collector",
            # always_on -> the singleton ALWAYS_ON constant, so the live
            # provider's sampler is identity-equal to a fresh _select_sampler().
            "OTEL_TRACES_SAMPLER": "always_on",
            "OTEL_ADAPTIVE_SAMPLING_ENABLED": "false",
            # no real exporter thread / gRPC channel
            "OTEL_TRACES_EXPORTER": "none",
        }

        with patch.dict(os.environ, env_vars, clear=False):
            result = initialize_opentelemetry()

            assert result is True
            expected = _select_sampler(get_otel_settings())
            assert get_tracer_provider().sampler is expected
