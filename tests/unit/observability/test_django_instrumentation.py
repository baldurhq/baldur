"""
DjangoInstrumentor 통합 단위 테스트.

대상: baldur.observability.instrument_django()
"""

import os
from unittest.mock import MagicMock, patch

from baldur.observability import (
    is_django_instrumented,
    reset_opentelemetry,
)
from baldur.settings.otel import reset_otel_settings


class TestInstrumentDjangoContract:
    """instrument_django() 계약 검증."""

    def setup_method(self):
        reset_opentelemetry()
        reset_otel_settings()

    def teardown_method(self):
        reset_opentelemetry()
        reset_otel_settings()

    def test_django_instrumented_flag_initial_false(self):
        """초기 상태에서 Django는 instrumented 되지 않은 상태이다."""
        assert is_django_instrumented() is False


class TestInstrumentDjangoBehavior:
    """instrument_django() 동작 검증."""

    def setup_method(self):
        reset_opentelemetry()
        reset_otel_settings()

    def teardown_method(self):
        reset_opentelemetry()
        reset_otel_settings()

    def test_returns_false_when_otel_disabled(self):
        """OTel이 비활성화되면 False를 반환한다."""
        from baldur.observability import instrument_django

        with patch("baldur.observability.is_otel_enabled", return_value=False):
            result = instrument_django()
            assert result is False

    def test_returns_false_when_django_instrument_disabled(self):
        """OTEL_DJANGO_INSTRUMENT_ENABLED=false이면 False를 반환한다."""
        from baldur.observability import instrument_django

        with patch("baldur.observability.is_otel_enabled", return_value=True):
            mock_settings = MagicMock()
            mock_settings.django_instrument_enabled = False
            with patch(
                "baldur.settings.otel.get_otel_settings",
                return_value=mock_settings,
            ):
                result = instrument_django()
                assert result is False

    def test_returns_false_when_django_instrumentor_not_installed(self):
        """opentelemetry-instrumentation-django 미설치 시 False를 반환한다."""
        from baldur.observability import instrument_django

        with patch("baldur.observability.is_otel_enabled", return_value=True):
            with patch.dict(
                "sys.modules", {"opentelemetry.instrumentation.django": None}
            ):
                result = instrument_django()
                assert result is False

    def test_idempotent_returns_true_on_second_call(self):
        """이미 instrumented 상태에서 True를 반환한다."""
        # 강제 instrumented 상태로 설정
        from baldur.observability import _otel_state, instrument_django

        state = _otel_state()
        state.django_instrumented = True
        try:
            result = instrument_django()
            assert result is True
        finally:
            state.django_instrumented = False

    def test_sets_excluded_urls_env_var(self):
        """instrument_django()가 OTEL_PYTHON_DJANGO_EXCLUDED_URLS 환경변수를 설정한다."""
        from baldur.observability import instrument_django

        mock_settings = MagicMock()
        mock_settings.django_instrument_enabled = True
        mock_settings.get_excluded_urls_list.return_value = ["/health", "/metrics"]

        mock_instrumentor = MagicMock()

        with (
            patch("baldur.observability.is_otel_enabled", return_value=True),
            patch(
                "baldur.settings.otel.get_otel_settings",
                return_value=mock_settings,
            ),
            patch.dict(os.environ, {}, clear=False),
        ):
            # DjangoInstrumentor mock
            mock_module = MagicMock()
            mock_module.DjangoInstrumentor.return_value = mock_instrumentor
            with patch.dict(
                "sys.modules",
                {"opentelemetry.instrumentation.django": mock_module},
            ):
                instrument_django()
                assert (
                    os.environ.get("OTEL_PYTHON_DJANGO_EXCLUDED_URLS")
                    == "/health,/metrics"
                )

    def test_reset_clears_django_instrumented_flag(self):
        """reset_opentelemetry()가 django_instrumented를 False로 리셋한다."""
        from baldur.observability import _otel_state

        state = _otel_state()
        state.django_instrumented = True
        reset_opentelemetry()
        assert state.django_instrumented is False


class TestInstrumentDjangoReadyPathBehavior:
    """593 — BaldurConfig._instrument_django_if_enabled() (the ready() path).

    ``BaldurConfig.ready()`` calls this static method (after ``baldur.init()``
    and before ``WSGIHandler.load_middleware()``) to apply Django request
    instrumentation. It gates on the shared ``BALDUR_OTEL_AUTOSTART`` hatch,
    delegates to the idempotent ``instrument_django()``, and swallows
    ImportError/Exception so it can never break ``ready()``'s
    exception-propagation contract.
    """

    def setup_method(self):
        reset_opentelemetry()
        reset_otel_settings()

    def teardown_method(self):
        reset_opentelemetry()
        reset_otel_settings()

    def test_autostart_disabled_skips_instrument_django(self):
        """BALDUR_OTEL_AUTOSTART=0이면 instrument_django()를 호출하지 않는다."""
        from baldur.adapters.django.apps import BaldurConfig

        with patch.dict(os.environ, {"BALDUR_OTEL_AUTOSTART": "0"}, clear=False):
            with patch("baldur.observability.instrument_django") as mock_instrument:
                BaldurConfig._instrument_django_if_enabled()

        mock_instrument.assert_not_called()

    def test_autostart_enabled_otel_on_instruments_django(self):
        """autostart on + OTel 활성 + DjangoInstrumentor mock → instrumented=True.

        ``BALDUR_OBSERVABILITY_PROFILE=otel_collector`` + ``BALDUR_OTEL_AUTOSTART=1`` 조건에서 ready() 경로가
        ``instrument_django()``에 도달하여 ``is_django_instrumented()``가 True가 됨을
        검증한다. SDK 실초기화를 피하기 위해 ``is_otel_enabled``를 패치하는 것은
        같은 파일의 ``test_sets_excluded_urls_env_var`` 선례를 따른다.
        """
        from baldur.adapters.django.apps import BaldurConfig

        mock_settings = MagicMock()
        mock_settings.django_instrument_enabled = True
        mock_settings.get_excluded_urls_list.return_value = []

        mock_module = MagicMock()
        mock_module.DjangoInstrumentor.return_value = MagicMock()

        with (
            patch.dict(os.environ, {"BALDUR_OTEL_AUTOSTART": "1"}, clear=False),
            patch("baldur.observability.is_otel_enabled", return_value=True),
            patch(
                "baldur.settings.otel.get_otel_settings",
                return_value=mock_settings,
            ),
            patch.dict(
                "sys.modules",
                {"opentelemetry.instrumentation.django": mock_module},
            ),
        ):
            BaldurConfig._instrument_django_if_enabled()

            assert is_django_instrumented() is True
            mock_module.DjangoInstrumentor.return_value.instrument.assert_called_once()

    def test_instrument_django_import_error_swallowed(self):
        """instrument_django()가 ImportError를 던져도 ready() 경로는 살아남는다."""
        from baldur.adapters.django.apps import BaldurConfig

        with (
            patch.dict(os.environ, {"BALDUR_OTEL_AUTOSTART": "1"}, clear=False),
            patch(
                "baldur.observability.instrument_django",
                side_effect=ImportError("opentelemetry-instrumentation-django missing"),
            ),
        ):
            # 예외가 전파되지 않아야 함
            BaldurConfig._instrument_django_if_enabled()

    def test_instrument_django_runtime_error_swallowed(self):
        """instrument_django()가 일반 예외를 던져도 ready() 경로는 살아남는다."""
        from baldur.adapters.django.apps import BaldurConfig

        with (
            patch.dict(os.environ, {"BALDUR_OTEL_AUTOSTART": "1"}, clear=False),
            patch(
                "baldur.observability.instrument_django",
                side_effect=RuntimeError("instrumentor boom"),
            ),
        ):
            # 예외가 전파되지 않아야 함
            BaldurConfig._instrument_django_if_enabled()
