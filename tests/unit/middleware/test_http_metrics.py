"""
HTTP RED Metrics Middleware unit tests (doc 352).

Tests HttpMetricsMiddleware, AsyncHttpMetricsMiddleware, and HttpMetricsMixin
behavior including RED metrics recording, endpoint normalization,
enable/disable logic, and graceful error handling.

Uses importlib direct loading to bypass baldur.api.django.__init__.py
import chain (same pattern as test_ip_ban_middleware.py).
"""

from __future__ import annotations

import os
import sys
from importlib.util import module_from_spec, spec_from_file_location
from unittest.mock import MagicMock, Mock, patch

import pytest

# ============================================================
# http_metrics.py direct load (bypass Django init chain)
# ============================================================

_HTTP_METRICS_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "src",
        "baldur",
        "api",
        "django",
        "middleware",
        "http_metrics.py",
    )
)

_MODULE_NAME = "baldur.api.django.middleware.http_metrics"


def _load_http_metrics_module():
    """Load http_metrics.py directly without triggering Django init chain."""
    spec = spec_from_file_location(_MODULE_NAME, _HTTP_METRICS_PATH)
    module = module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


try:
    _http_metrics_module = _load_http_metrics_module()
    HttpMetricsMiddleware = _http_metrics_module.HttpMetricsMiddleware
    AsyncHttpMetricsMiddleware = _http_metrics_module.AsyncHttpMetricsMiddleware
    HttpMetricsMixin = _http_metrics_module.HttpMetricsMixin
    _MODULE_LOADED = True
except Exception as e:
    _MODULE_LOADED = False
    _LOAD_ERROR = str(e)

# ============================================================
# Helpers
# ============================================================


def _make_request(method: str = "GET", path: str = "/api/test") -> Mock:
    """Create a minimal mock Django HttpRequest."""
    request = Mock()
    request.method = method
    request.path = path
    return request


def _make_response(status_code: int = 200) -> Mock:
    """Create a minimal mock Django HttpResponse."""
    response = Mock()
    response.status_code = status_code
    return response


def _make_sync_middleware(
    response: Mock | None = None,
    side_effect: Exception | None = None,
) -> HttpMetricsMiddleware:
    """Create HttpMetricsMiddleware with a mock get_response."""
    if side_effect is not None:
        get_response = Mock(side_effect=side_effect)
    else:
        get_response = Mock(return_value=response or _make_response(200))
    mw = HttpMetricsMiddleware(get_response)
    # _is_enabled() now reads the layered metrics snapshot (686 D5); with no
    # RuntimeConfigManager registered it resolves to the env default (enabled=True).
    return mw


def _make_async_middleware(
    response: Mock | None = None,
    side_effect: Exception | None = None,
) -> AsyncHttpMetricsMiddleware:
    """Create AsyncHttpMetricsMiddleware with a mock async get_response."""

    async def async_get_response(request):
        if side_effect is not None:
            raise side_effect
        return response or _make_response(200)

    mw = AsyncHttpMetricsMiddleware(async_get_response)
    # See _make_sync_middleware — enabled resolves to the env default (True).
    return mw


# ============================================================
# Contract Tests
# ============================================================


_skip_reason = (
    f"http_metrics module load failed: {_LOAD_ERROR if not _MODULE_LOADED else ''}"
)


@pytest.mark.skipif(not _MODULE_LOADED, reason=_skip_reason)
class TestHttpMetricsMiddlewareContract:
    """HTTP Metrics Middleware design contract verification."""

    def test_module_exports_contains_expected_classes(self):
        """__all__ exports HttpMetricsMiddleware and AsyncHttpMetricsMiddleware."""
        assert "HttpMetricsMiddleware" in _http_metrics_module.__all__
        assert "AsyncHttpMetricsMiddleware" in _http_metrics_module.__all__

    def test_async_middleware_async_capable_is_true(self):
        """AsyncHttpMetricsMiddleware.async_capable == True (Django ASGI contract)."""
        assert AsyncHttpMetricsMiddleware.async_capable is True

    def test_async_middleware_sync_capable_is_false(self):
        """AsyncHttpMetricsMiddleware.sync_capable == False (Django ASGI contract)."""
        assert AsyncHttpMetricsMiddleware.sync_capable is False

    def test_normalization_error_fallback_value(self):
        """Normalization failure fallback is 'NORMALIZATION_ERROR'."""
        mixin = HttpMetricsMixin()
        request = _make_request()

        with patch(
            "baldur.metrics.endpoint_normalizer.normalize_endpoint",
            autospec=True,
            side_effect=RuntimeError("boom"),
        ):
            result = mixin._normalize(request)

        assert result == "NORMALIZATION_ERROR"

    def test_unhandled_exception_assumes_status_code_500(self):
        """_record_exception records status_code=500 for unhandled exceptions."""
        mixin = HttpMetricsMixin()
        request = _make_request()
        exc = RuntimeError("db crash")

        with (
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
            ) as mock_record,
            patch(
                "baldur.metrics.prometheus.record_http_error",
                autospec=True,
            ),
            patch.object(mixin, "_normalize", return_value="/api/test"),
        ):
            mixin._record_exception(request, exc, 0.5)

        mock_record.assert_called_once()
        assert mock_record.call_args[0][2] == 500

    def test_5xx_error_type_format(self):
        """5xx error_type format is 'HTTP_{status_code}'."""
        mixin = HttpMetricsMixin()
        request = _make_request()
        response = _make_response(503)

        with (
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
            ),
            patch(
                "baldur.metrics.prometheus.record_http_error",
                autospec=True,
            ) as mock_error,
            patch.object(mixin, "_normalize", return_value="/api"),
        ):
            mixin._record_response(request, response, 0.1)

        mock_error.assert_called_once()
        assert mock_error.call_args[0][2] == "HTTP_503"


# ============================================================
# Behavior Tests — Sync Middleware
# ============================================================


@pytest.mark.skipif(not _MODULE_LOADED, reason=_skip_reason)
class TestHttpMetricsMiddlewareBehavior:
    """HTTP Metrics Middleware (sync) behavior verification."""

    # --- Basic recording ---

    def test_records_get_200_request_metrics(self):
        """GET 200 request records Rate + Duration, no Error."""
        mw = _make_sync_middleware(_make_response(200))
        request = _make_request("GET", "/api/users")

        with (
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
            ) as mock_req,
            patch(
                "baldur.metrics.prometheus.record_http_error",
                autospec=True,
            ) as mock_err,
            patch(
                "baldur.metrics.endpoint_normalizer.normalize_endpoint",
                autospec=True,
                return_value="/api/users",
            ),
        ):
            result = mw(request)

        assert result.status_code == 200
        mock_req.assert_called_once()
        call_args = mock_req.call_args[0]
        assert call_args[0] == "GET"
        assert call_args[1] == "/api/users"
        assert call_args[2] == 200
        assert call_args[3] > 0  # duration > 0
        mock_err.assert_not_called()

    def test_records_post_201_request_metrics(self):
        """POST 201 request records Rate + Duration, no Error."""
        mw = _make_sync_middleware(_make_response(201))
        request = _make_request("POST", "/api/items")

        with (
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
            ) as mock_req,
            patch(
                "baldur.metrics.prometheus.record_http_error",
                autospec=True,
            ) as mock_err,
            patch(
                "baldur.metrics.endpoint_normalizer.normalize_endpoint",
                autospec=True,
                return_value="/api/items",
            ),
        ):
            mw(request)

        mock_req.assert_called_once()
        assert mock_req.call_args[0][0] == "POST"
        assert mock_req.call_args[0][2] == 201
        mock_err.assert_not_called()

    def test_records_5xx_rate_and_error(self):
        """503 response records Rate + Duration + Error (RED dual recording)."""
        mw = _make_sync_middleware(_make_response(503))
        request = _make_request("GET", "/api/data")

        with (
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
            ) as mock_req,
            patch(
                "baldur.metrics.prometheus.record_http_error",
                autospec=True,
            ) as mock_err,
            patch(
                "baldur.metrics.endpoint_normalizer.normalize_endpoint",
                autospec=True,
                return_value="/api/data",
            ),
        ):
            mw(request)

        mock_req.assert_called_once()
        assert mock_req.call_args[0][2] == 503
        mock_err.assert_called_once()
        assert mock_err.call_args[0][2] == "HTTP_503"

    def test_records_4xx_rate_only_no_error(self):
        """404 response records Rate + Duration only, no Error."""
        mw = _make_sync_middleware(_make_response(404))
        request = _make_request("GET", "/api/missing")

        with (
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
            ) as mock_req,
            patch(
                "baldur.metrics.prometheus.record_http_error",
                autospec=True,
            ) as mock_err,
            patch(
                "baldur.metrics.endpoint_normalizer.normalize_endpoint",
                autospec=True,
                return_value="/api/missing",
            ),
        ):
            mw(request)

        mock_req.assert_called_once()
        assert mock_req.call_args[0][2] == 404
        mock_err.assert_not_called()

    def test_records_unhandled_exception_and_reraises(self):
        """Unhandled exception records status=500 + error_type, then re-raises."""
        exc = RuntimeError("DB connection failed")
        mw = _make_sync_middleware(side_effect=exc)
        request = _make_request("POST", "/api/create")

        with (
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
            ) as mock_req,
            patch(
                "baldur.metrics.prometheus.record_http_error",
                autospec=True,
            ) as mock_err,
            patch(
                "baldur.metrics.endpoint_normalizer.normalize_endpoint",
                autospec=True,
                return_value="/api/create",
            ),
        ):
            with pytest.raises(RuntimeError, match="DB connection failed"):
                mw(request)

        mock_req.assert_called_once()
        assert mock_req.call_args[0][2] == 500
        mock_err.assert_called_once()
        assert mock_err.call_args[0][2] == "RuntimeError"

    # --- 5xx boundary ---

    def test_499_does_not_record_error(self):
        """Status 499 (< 500) does not record error — boundary check."""
        mw = _make_sync_middleware(_make_response(499))
        request = _make_request()

        with (
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
            ),
            patch(
                "baldur.metrics.prometheus.record_http_error",
                autospec=True,
            ) as mock_err,
            patch(
                "baldur.metrics.endpoint_normalizer.normalize_endpoint",
                autospec=True,
                return_value="/api/test",
            ),
        ):
            mw(request)

        mock_err.assert_not_called()

    def test_500_records_error(self):
        """Status 500 (>= 500) records error — boundary check."""
        mw = _make_sync_middleware(_make_response(500))
        request = _make_request()

        with (
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
            ),
            patch(
                "baldur.metrics.prometheus.record_http_error",
                autospec=True,
            ) as mock_err,
            patch(
                "baldur.metrics.endpoint_normalizer.normalize_endpoint",
                autospec=True,
                return_value="/api/test",
            ),
        ):
            mw(request)

        mock_err.assert_called_once()
        assert mock_err.call_args[0][2] == "HTTP_500"

    # --- Endpoint normalization ---

    def test_endpoint_normalized_via_normalize_endpoint(self):
        """request.path is passed through normalize_endpoint()."""
        mw = _make_sync_middleware(_make_response(200))
        request = _make_request("GET", "/api/users/123/profile")

        with (
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
            ) as mock_req,
            patch(
                "baldur.metrics.prometheus.record_http_error",
                autospec=True,
            ),
            patch(
                "baldur.metrics.endpoint_normalizer.normalize_endpoint",
                autospec=True,
                return_value="/api/users/{id}/profile",
            ) as mock_norm,
        ):
            mw(request)

        mock_norm.assert_called_once_with("/api/users/123/profile", request)
        assert mock_req.call_args[0][1] == "/api/users/{id}/profile"

    def test_normalization_failure_uses_fallback(self):
        """normalize_endpoint() failure uses 'NORMALIZATION_ERROR' fallback."""
        mw = _make_sync_middleware(_make_response(200))
        request = _make_request()

        with (
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
            ) as mock_req,
            patch(
                "baldur.metrics.prometheus.record_http_error",
                autospec=True,
            ),
            patch(
                "baldur.metrics.endpoint_normalizer.normalize_endpoint",
                autospec=True,
                side_effect=RuntimeError("normalizer broken"),
            ),
        ):
            mw(request)

        assert mock_req.call_args[0][1] == "NORMALIZATION_ERROR"

    # --- Enable/Disable ---

    def test_disabled_metrics_passthrough(self):
        """MetricsSettings.enabled=False skips metrics, passes request through."""
        response = _make_response(200)
        mw = _make_sync_middleware(response)
        request = _make_request()

        disabled = MagicMock()
        disabled.enabled = False
        with (
            patch(
                "baldur.settings.layered_provider.get_layered_settings_cached",
                autospec=True,
                return_value=disabled,
            ),
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
            ) as mock_req,
        ):
            result = mw(request)

        assert result is response
        mock_req.assert_not_called()

    def test_settings_load_failure_defaults_to_enabled(self):
        """Layered read failure falls back to enabled=True (fail-open)."""
        response = _make_response(200)
        get_response = Mock(return_value=response)
        mw = HttpMetricsMiddleware(get_response)

        with (
            patch(
                "baldur.settings.layered_provider.get_layered_settings_cached",
                autospec=True,
                side_effect=ImportError("no settings module"),
            ),
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
            ) as mock_req,
            patch(
                "baldur.metrics.prometheus.record_http_error",
                autospec=True,
            ),
            patch(
                "baldur.metrics.endpoint_normalizer.normalize_endpoint",
                autospec=True,
                return_value="/api/test",
            ),
        ):
            result = mw(_make_request())
            assert mw._is_enabled() is True

        assert result is response
        mock_req.assert_called_once()

    def test_enabled_state_cached_within_ttl(self):
        """_is_enabled() reads through the 30s layered snapshot cache (686 D3/D5)
        — the underlying layered read runs once across repeated calls in the TTL."""
        get_response = Mock(return_value=_make_response(200))
        mw = HttpMetricsMiddleware(get_response)

        mock_settings = MagicMock()
        mock_settings.enabled = True

        with patch(
            "baldur.settings.layered_provider.get_layered_settings",
            autospec=True,
            return_value=mock_settings,
        ) as mock_get:
            mw._is_enabled()
            mw._is_enabled()
            mw._is_enabled()

        mock_get.assert_called_once()

    # --- Graceful error handling ---

    def test_metric_recording_failure_does_not_break_response(self):
        """record_http_request() failure still returns the response."""
        response = _make_response(200)
        mw = _make_sync_middleware(response)
        request = _make_request()

        with (
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
                side_effect=RuntimeError("prometheus down"),
            ),
            patch(
                "baldur.metrics.endpoint_normalizer.normalize_endpoint",
                autospec=True,
                return_value="/api/test",
            ),
        ):
            result = mw(request)

        assert result is response

    def test_metric_recording_failure_on_exception_still_reraises(self):
        """Exception path: metric recording failure doesn't swallow original exception."""
        exc = ValueError("original error")
        mw = _make_sync_middleware(side_effect=exc)
        request = _make_request()

        with (
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
                side_effect=RuntimeError("metrics broken"),
            ),
            patch(
                "baldur.metrics.endpoint_normalizer.normalize_endpoint",
                autospec=True,
                return_value="/api/test",
            ),
        ):
            with pytest.raises(ValueError, match="original error"):
                mw(request)

    # --- Duration ---

    def test_duration_uses_perf_counter(self):
        """Duration is measured using time.perf_counter() (monotonic)."""
        mw = _make_sync_middleware(_make_response(200))
        request = _make_request()

        with (
            patch.object(
                _http_metrics_module.time,
                "perf_counter",
                side_effect=[100.0, 100.05],
            ),
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
            ) as mock_req,
            patch(
                "baldur.metrics.prometheus.record_http_error",
                autospec=True,
            ),
            patch(
                "baldur.metrics.endpoint_normalizer.normalize_endpoint",
                autospec=True,
                return_value="/api/test",
            ),
        ):
            mw(request)

        duration = mock_req.call_args[0][3]
        assert duration == pytest.approx(0.05)

    def test_duration_includes_downstream_middleware_time(self):
        """Duration includes time spent in downstream middleware/view.

        Verifies that perf_counter is called before and after get_response,
        so downstream processing time is captured in the duration.
        """
        mw = _make_sync_middleware(_make_response(200))
        request = _make_request()

        # perf_counter returns 50.0 before get_response, 50.35 after
        # → duration = 0.35 (simulating downstream processing time)
        with (
            patch.object(
                _http_metrics_module.time,
                "perf_counter",
                side_effect=[50.0, 50.35],
            ),
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
            ) as mock_req,
            patch(
                "baldur.metrics.prometheus.record_http_error",
                autospec=True,
            ),
            patch(
                "baldur.metrics.endpoint_normalizer.normalize_endpoint",
                autospec=True,
                return_value="/api/test",
            ),
        ):
            mw(request)

        duration = mock_req.call_args[0][3]
        assert duration == pytest.approx(0.35)

    # --- Method fallback ---

    def test_none_method_uses_unknown(self):
        """request.method=None uses 'UNKNOWN' as method label."""
        mw = _make_sync_middleware(_make_response(200))
        request = _make_request()
        request.method = None

        with (
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
            ) as mock_req,
            patch(
                "baldur.metrics.prometheus.record_http_error",
                autospec=True,
            ),
            patch(
                "baldur.metrics.endpoint_normalizer.normalize_endpoint",
                autospec=True,
                return_value="/api/test",
            ),
        ):
            mw(request)

        assert mock_req.call_args[0][0] == "UNKNOWN"


# ============================================================
# Behavior Tests — Async Middleware
# ============================================================


@pytest.mark.skipif(not _MODULE_LOADED, reason=_skip_reason)
class TestAsyncHttpMetricsMiddlewareBehavior:
    """Async HTTP Metrics Middleware behavior verification."""

    @pytest.mark.asyncio
    async def test_async_records_get_200_request_metrics(self):
        """Async GET 200 request records Rate + Duration."""
        mw = _make_async_middleware(_make_response(200))
        request = _make_request("GET", "/api/users")

        with (
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
            ) as mock_req,
            patch(
                "baldur.metrics.prometheus.record_http_error",
                autospec=True,
            ) as mock_err,
            patch(
                "baldur.metrics.endpoint_normalizer.normalize_endpoint",
                autospec=True,
                return_value="/api/users",
            ),
        ):
            result = await mw(request)

        assert result.status_code == 200
        mock_req.assert_called_once()
        assert mock_req.call_args[0][0] == "GET"
        assert mock_req.call_args[0][2] == 200
        mock_err.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_records_5xx_rate_and_error(self):
        """Async 503 response records Rate + Duration + Error."""
        mw = _make_async_middleware(_make_response(503))
        request = _make_request("GET", "/api/data")

        with (
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
            ) as mock_req,
            patch(
                "baldur.metrics.prometheus.record_http_error",
                autospec=True,
            ) as mock_err,
            patch(
                "baldur.metrics.endpoint_normalizer.normalize_endpoint",
                autospec=True,
                return_value="/api/data",
            ),
        ):
            await mw(request)

        mock_req.assert_called_once()
        mock_err.assert_called_once()
        assert mock_err.call_args[0][2] == "HTTP_503"

    @pytest.mark.asyncio
    async def test_async_records_unhandled_exception_and_reraises(self):
        """Async unhandled exception records metrics then re-raises."""
        exc = RuntimeError("async db error")
        mw = _make_async_middleware(side_effect=exc)
        request = _make_request("POST", "/api/create")

        with (
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
            ) as mock_req,
            patch(
                "baldur.metrics.prometheus.record_http_error",
                autospec=True,
            ) as mock_err,
            patch(
                "baldur.metrics.endpoint_normalizer.normalize_endpoint",
                autospec=True,
                return_value="/api/create",
            ),
        ):
            with pytest.raises(RuntimeError, match="async db error"):
                await mw(request)

        mock_req.assert_called_once()
        assert mock_req.call_args[0][2] == 500
        mock_err.assert_called_once()
        assert mock_err.call_args[0][2] == "RuntimeError"

    @pytest.mark.asyncio
    async def test_async_disabled_metrics_passthrough(self):
        """Async middleware disabled: passes through without recording."""
        response = _make_response(200)
        mw = _make_async_middleware(response)
        request = _make_request()

        disabled = MagicMock()
        disabled.enabled = False
        with (
            patch(
                "baldur.settings.layered_provider.get_layered_settings_cached",
                autospec=True,
                return_value=disabled,
            ),
            patch(
                "baldur.metrics.prometheus.record_http_request",
                autospec=True,
            ) as mock_req,
        ):
            result = await mw(request)

        assert result is response
        mock_req.assert_not_called()


# ============================================================
# Behavior Tests — perf_counter Migration (prometheus.py)
# ============================================================


class TestPerfCounterMigrationBehavior:
    """http_request_timer()/timer() perf_counter migration verification."""

    def test_http_request_timer_uses_perf_counter(self):
        """http_request_timer() uses time.perf_counter() for timing."""
        from baldur.metrics.prometheus import BaldurMetrics

        metrics = BaldurMetrics(prefix="test_timer_pc")

        with patch(
            "baldur.metrics.prometheus.time.perf_counter",
            side_effect=[1000.0, 1000.25],
        ) as mock_pc:
            with metrics.http_request_timer("GET", "/api"):
                pass

        assert mock_pc.call_count == 2

    def test_timer_uses_perf_counter(self):
        """timer() uses time.perf_counter() for timing."""
        from baldur.metrics.prometheus import BaldurMetrics

        metrics = BaldurMetrics(prefix="test_timer_pc2")

        with patch(
            "baldur.metrics.prometheus.time.perf_counter",
            side_effect=[2000.0, 2000.1],
        ) as mock_pc:
            with metrics.timer("test_domain"):
                pass

        assert mock_pc.call_count == 2

    def test_http_request_timer_no_status_code_local_variable(self):
        """Dead code removed: http_request_timer has no status_code variable."""
        import inspect

        from baldur.metrics.prometheus import BaldurMetrics

        source = inspect.getsource(BaldurMetrics.http_request_timer)
        assert "status_code" not in source
