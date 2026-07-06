"""Unit tests for the OTel auto-instrumentation bootstrap wiring (593).

Scope of ``bootstrap._instrument_otel_if_enabled`` and the shared
``bootstrap._otel_autostart_enabled`` hatch:

- Two gates short-circuit before any SDK init: the ``BALDUR_OTEL_AUTOSTART``
  hatch (test/operator opt-out, default ``"1"``) and the observability profile
  (``ObservabilitySettings.effective_otel_enabled``, driven by
  ``BALDUR_OBSERVABILITY_PROFILE``).
- When both gates pass, the SDK is initialized FIRST (so the composite
  TraceContext+Baggage propagator is live before the outbound instrumentor
  injects baggage), then the three framework-agnostic instrumentors
  (``requests`` / ``celery`` / ``logging``) run.
- ImportError (extras missing) and runtime Exception are both swallowed —
  ``init()`` must never abort on instrumentation failure.
- A second pass does not re-patch (idempotency is delegated to each
  ``instrument_*``'s ``state.*_instrumented`` guard).

Mirrors ``test_bootstrap_background_services.py``. Django request
instrumentation is wired separately in ``BaldurConfig.ready()`` and is
covered by ``observability/test_django_instrumentation.py``.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from baldur.bootstrap import _instrument_otel_if_enabled, _otel_autostart_enabled

# =============================================================================
# _otel_autostart_enabled — the shared BALDUR_OTEL_AUTOSTART hatch
# =============================================================================


class TestOtelAutostartHatchContract:
    """``_otel_autostart_enabled`` reads ``BALDUR_OTEL_AUTOSTART`` directly.

    Contract: default ``"1"`` (enabled); only the explicit opt-out tokens
    ``"0"`` / ``"false"`` / ``"no"`` (case/whitespace insensitive) disable.
    """

    def test_unset_defaults_to_enabled(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BALDUR_OTEL_AUTOSTART", None)
            assert _otel_autostart_enabled() is True

    def test_value_one_enables(self):
        with patch.dict(os.environ, {"BALDUR_OTEL_AUTOSTART": "1"}, clear=False):
            assert _otel_autostart_enabled() is True

    def test_value_true_enables(self):
        with patch.dict(os.environ, {"BALDUR_OTEL_AUTOSTART": "true"}, clear=False):
            assert _otel_autostart_enabled() is True

    def test_value_yes_enables(self):
        with patch.dict(os.environ, {"BALDUR_OTEL_AUTOSTART": "yes"}, clear=False):
            assert _otel_autostart_enabled() is True

    def test_value_zero_disables(self):
        with patch.dict(os.environ, {"BALDUR_OTEL_AUTOSTART": "0"}, clear=False):
            assert _otel_autostart_enabled() is False

    def test_value_false_disables(self):
        with patch.dict(os.environ, {"BALDUR_OTEL_AUTOSTART": "false"}, clear=False):
            assert _otel_autostart_enabled() is False

    def test_value_no_disables(self):
        with patch.dict(os.environ, {"BALDUR_OTEL_AUTOSTART": "no"}, clear=False):
            assert _otel_autostart_enabled() is False

    def test_uppercase_and_whitespace_disable_token_normalized(self):
        # Boundary: " FALSE " strips + lowercases to the opt-out token.
        with patch.dict(
            os.environ, {"BALDUR_OTEL_AUTOSTART": "  FALSE  "}, clear=False
        ):
            assert _otel_autostart_enabled() is False

    def test_uppercase_enable_token_normalized(self):
        with patch.dict(os.environ, {"BALDUR_OTEL_AUTOSTART": "TRUE"}, clear=False):
            assert _otel_autostart_enabled() is True


# =============================================================================
# _instrument_otel_if_enabled — gating + delegation
# =============================================================================


class TestInstrumentOtelIfEnabledBehavior:
    """``_instrument_otel_if_enabled`` gating and delegation order."""

    def test_autostart_disabled_skips_sdk_init_and_instrumentors(self):
        # Given the autostart hatch is opted out
        with (
            patch.dict(os.environ, {"BALDUR_OTEL_AUTOSTART": "0"}, clear=False),
            patch(
                "baldur.settings.observability.get_observability_settings"
            ) as m_settings,
            patch("baldur.observability.initialize_opentelemetry") as m_init,
            patch("baldur.observability.instrument_requests") as m_req,
            patch("baldur.observability.instrument_celery") as m_cel,
            patch("baldur.observability.instrument_logging") as m_log,
        ):
            # When
            _instrument_otel_if_enabled()

        # Then nothing is touched — not even the settings read
        m_settings.assert_not_called()
        m_init.assert_not_called()
        m_req.assert_not_called()
        m_cel.assert_not_called()
        m_log.assert_not_called()

    def test_otel_disabled_skips_sdk_init_and_instrumentors(self):
        # Given autostart on but the profile resolves OTel off
        with (
            patch.dict(os.environ, {"BALDUR_OTEL_AUTOSTART": "1"}, clear=False),
            patch(
                "baldur.settings.observability.get_observability_settings",
                return_value=MagicMock(effective_otel_enabled=False),
            ),
            patch("baldur.observability.initialize_opentelemetry") as m_init,
            patch("baldur.observability.instrument_requests") as m_req,
            patch("baldur.observability.instrument_celery") as m_cel,
            patch("baldur.observability.instrument_logging") as m_log,
        ):
            # When
            _instrument_otel_if_enabled()

        # Then the SDK is not initialized and no instrumentor runs
        m_init.assert_not_called()
        m_req.assert_not_called()
        m_cel.assert_not_called()
        m_log.assert_not_called()

    def test_local_profile_via_env_logs_instrumentation_disabled(self):
        # Given the real observability profile gate resolves OTel off via the
        # single BALDUR_OBSERVABILITY_PROFILE env var (G13 — drive the real
        # get_observability_settings() gate, not a mocked settings object).
        from structlog.testing import capture_logs

        from baldur.settings.observability import reset_observability_settings

        with (
            patch.dict(
                os.environ,
                {
                    "BALDUR_OTEL_AUTOSTART": "1",
                    "BALDUR_OBSERVABILITY_PROFILE": "local",
                },
                clear=False,
            ),
            patch("baldur.observability.initialize_opentelemetry") as m_init,
            patch("baldur.observability.instrument_requests") as m_req,
            patch("baldur.observability.instrument_celery") as m_cel,
            patch("baldur.observability.instrument_logging") as m_log,
        ):
            # Drop any cached singleton so the local profile is re-read.
            reset_observability_settings()
            try:
                # When
                with capture_logs() as logs:
                    _instrument_otel_if_enabled()
            finally:
                reset_observability_settings()

        # Then the profile gate short-circuits with the disabled DEBUG log
        assert any(e.get("event") == "otel.instrumentation_disabled" for e in logs)
        m_init.assert_not_called()
        m_req.assert_not_called()
        m_cel.assert_not_called()
        m_log.assert_not_called()

    def test_enabled_initializes_sdk_then_invokes_instrumentors(self):
        # Given both gates pass
        manager = MagicMock()
        with (
            patch.dict(os.environ, {"BALDUR_OTEL_AUTOSTART": "1"}, clear=False),
            patch(
                "baldur.settings.observability.get_observability_settings",
                return_value=MagicMock(effective_otel_enabled=True),
            ),
            patch("baldur.observability.initialize_opentelemetry") as m_init,
            patch("baldur.observability.instrument_requests") as m_req,
            patch("baldur.observability.instrument_celery") as m_cel,
            patch("baldur.observability.instrument_logging") as m_log,
        ):
            manager.attach_mock(m_init, "init")
            manager.attach_mock(m_req, "requests")
            manager.attach_mock(m_cel, "celery")
            manager.attach_mock(m_log, "logging")

            # When
            _instrument_otel_if_enabled()

        # Then SDK init + each framework-agnostic instrumentor runs exactly once
        m_init.assert_called_once()
        m_req.assert_called_once()
        m_cel.assert_called_once()
        m_log.assert_called_once()

        # And SDK init precedes the outbound instrumentor (baggage must be live
        # before instrument_requests injects it onto egress headers).
        call_names = [c[0] for c in manager.mock_calls]
        assert call_names.index("init") < call_names.index("requests")

    def test_import_error_swallowed(self):
        # Given autostart on but the instrumentation modules cannot be imported
        with (
            patch.dict(os.environ, {"BALDUR_OTEL_AUTOSTART": "1"}, clear=False),
            patch("baldur.observability.initialize_opentelemetry") as m_init,
            patch.dict("sys.modules", {"baldur.settings.observability": None}),
        ):
            # When / Then — must not raise
            _instrument_otel_if_enabled()

        m_init.assert_not_called()

    def test_runtime_error_swallowed(self):
        # Given SDK init raises after the gates pass
        with (
            patch.dict(os.environ, {"BALDUR_OTEL_AUTOSTART": "1"}, clear=False),
            patch(
                "baldur.settings.observability.get_observability_settings",
                return_value=MagicMock(effective_otel_enabled=True),
            ),
            patch(
                "baldur.observability.initialize_opentelemetry",
                side_effect=RuntimeError("tracer provider boom"),
            ),
            patch("baldur.observability.instrument_requests") as m_req,
        ):
            # When / Then — init() must continue, so the exception is swallowed
            _instrument_otel_if_enabled()

        # The downstream instrumentor is never reached
        m_req.assert_not_called()


# =============================================================================
# _instrument_otel_if_enabled — double-instrument safety (idempotency)
# =============================================================================


class TestInstrumentOtelIdempotencyBehavior:
    """A second pass leaves ``state.*_instrumented`` True without re-patching.

    Uses the REAL ``instrument_*`` functions (so their ``state.*_instrumented``
    short-circuit is exercised) with the underlying OTel Instrumentor classes
    mocked at their import sites — so no real ``requests`` / ``logging``
    monkey-patching occurs.
    """

    def setup_method(self):
        from baldur.observability import reset_opentelemetry
        from baldur.settings.otel import reset_otel_settings

        reset_opentelemetry()
        reset_otel_settings()

    def teardown_method(self):
        from baldur.observability import reset_opentelemetry
        from baldur.settings.otel import reset_otel_settings

        reset_opentelemetry()
        reset_otel_settings()

    def test_second_pass_does_not_reconstruct_instrumentors(self):
        from baldur.observability import (
            _otel_state,
            is_celery_instrumented,
            is_logging_instrumented,
            is_requests_instrumented,
        )

        # Given is_otel_enabled() will report True without real SDK init.
        state = _otel_state()
        state.initialized = True
        state.tracer_provider = MagicMock()

        requests_mod = MagicMock()
        celery_mod = MagicMock()
        logging_mod = MagicMock()

        with (
            patch.dict(os.environ, {"BALDUR_OTEL_AUTOSTART": "1"}, clear=False),
            patch(
                "baldur.settings.observability.get_observability_settings",
                return_value=MagicMock(effective_otel_enabled=True),
            ),
            # No-op the SDK init / logger provider so the real instrument_*
            # functions reach their (mocked) Instrumentor constructors.
            patch("baldur.observability.initialize_opentelemetry"),
            patch("baldur.observability.initialize_logger_provider", return_value=True),
            patch.dict(
                "sys.modules",
                {
                    "opentelemetry.instrumentation.requests": requests_mod,
                    "opentelemetry.instrumentation.celery": celery_mod,
                    "opentelemetry.instrumentation.logging": logging_mod,
                },
            ),
        ):
            # When — two consecutive passes
            _instrument_otel_if_enabled()
            _instrument_otel_if_enabled()

            # Then — flags flipped on the first pass
            assert is_requests_instrumented() is True
            assert is_celery_instrumented() is True
            assert is_logging_instrumented() is True

            # And each Instrumentor was constructed exactly once (second pass
            # short-circuits on the state guard).
            assert requests_mod.RequestsInstrumentor.call_count == 1
            assert celery_mod.CeleryInstrumentor.call_count == 1
            assert logging_mod.LoggingInstrumentor.call_count == 1
