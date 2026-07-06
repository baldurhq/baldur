"""Unit tests for testapp dummy views.

Uses DRF APIRequestFactory — requires Django settings but no database.
Skips automatically if Django is not configured.

Verification techniques applied:
- Contract: HTTP status codes and response body structure
- Behavior: exception raising, query param reading, header reading
- Side effect: time.sleep call in SlowView (via mock_sleep)
"""

from __future__ import annotations

import json

import pytest

from tests.factories.time_helpers import mock_sleep


def _django_available() -> bool:
    """Check if Django apps are fully loaded for DRF views."""
    try:
        from django.apps import apps

        if not apps.ready:
            return False

        import django.conf

        django.conf.settings.REST_FRAMEWORK  # noqa: B018
        return True
    except Exception:
        return False


_skip_no_django = pytest.mark.skipif(
    not _django_available(),
    reason="Django settings not configured",
)


def _make_request(path="/test/", method="get", **kwargs):
    """Create a DRF test request."""
    from rest_framework.test import APIRequestFactory

    factory = APIRequestFactory()
    return getattr(factory, method)(path, **kwargs)


# ============================================================
# Contract: SuccessView
# ============================================================


@_skip_no_django
class TestSuccessViewContract:
    """SuccessView HTTP response contract."""

    def test_get_returns_200(self):
        """GET /test/success/ returns HTTP 200."""
        from tests.testapp.views import SuccessView

        request = _make_request("/test/success/")
        response = SuccessView.as_view()(request)

        assert response.status_code == 200

    def test_get_returns_status_ok(self):
        """Response body contains {"status": "ok"}."""
        from tests.testapp.views import SuccessView

        request = _make_request("/test/success/")
        response = SuccessView.as_view()(request)
        body = json.loads(response.content)

        assert body == {"status": "ok"}


# ============================================================
# Behavior: ErrorView
# ============================================================


@_skip_no_django
class TestErrorViewBehavior:
    """ErrorView exception behavior for middleware testing."""

    def test_get_raises_runtime_error(self):
        """GET raises RuntimeError — middleware (not DRF) handles 500 conversion."""
        from tests.testapp.views import ErrorView

        request = _make_request("/test/error/")

        with pytest.raises(RuntimeError, match="Deliberate 500"):
            ErrorView.as_view()(request)


# ============================================================
# Behavior: SlowView
# ============================================================


@_skip_no_django
class TestSlowViewBehavior:
    """SlowView delay behavior."""

    def test_get_calls_sleep_with_default_delay(self):
        """GET without delay param sleeps for DEFAULT_SLOW_VIEW_DELAY."""
        from tests.testapp.views import DEFAULT_SLOW_VIEW_DELAY, SlowView

        request = _make_request("/test/slow/")

        with mock_sleep() as sleep_mock:
            response = SlowView.as_view()(request)

        assert response.status_code == 200
        sleep_mock.assert_called_with(DEFAULT_SLOW_VIEW_DELAY)

    def test_get_reads_delay_query_param(self):
        """GET with ?delay=0.5 sleeps for 0.5 seconds."""
        from tests.testapp.views import SlowView

        request = _make_request("/test/slow/", data={"delay": "0.5"})

        with mock_sleep() as sleep_mock:
            SlowView.as_view()(request)

        sleep_mock.assert_called_with(0.5)

    def test_get_returns_delay_in_body(self):
        """Response body includes the delay value."""
        from tests.testapp.views import SlowView

        request = _make_request("/test/slow/", data={"delay": "1.5"})

        with mock_sleep():
            response = SlowView.as_view()(request)

        body = json.loads(response.content)
        assert body["delay"] == 1.5
        assert body["status"] == "ok"


# ============================================================
# Contract: RateLimitTestView
# ============================================================


@_skip_no_django
class TestRateLimitTestViewContract:
    """RateLimitTestView HTTP response contract."""

    def test_get_returns_429(self):
        """GET /test/rate-limit/ returns HTTP 429."""
        from tests.testapp.views import RateLimitTestView

        request = _make_request("/test/rate-limit/")
        response = RateLimitTestView.as_view()(request)

        assert response.status_code == 429

    def test_get_returns_rate_limit_detail(self):
        """Response body contains rate limit exceeded message."""
        from tests.testapp.views import RateLimitTestView

        request = _make_request("/test/rate-limit/")
        response = RateLimitTestView.as_view()(request)
        body = json.loads(response.content)

        assert body == {"detail": "Rate limit exceeded"}


# ============================================================
# Behavior: TieredEndpointView
# ============================================================


@_skip_no_django
class TestTieredEndpointViewBehavior:
    """TieredEndpointView header branching behavior."""

    def test_get_defaults_to_free_tier(self):
        """Without X-API-Tier header, defaults to 'free'."""
        from tests.testapp.views import TieredEndpointView

        request = _make_request("/test/tiered/")
        response = TieredEndpointView.as_view()(request)
        body = json.loads(response.content)

        assert body["tier"] == "free"

    def test_get_reads_tier_from_header(self):
        """X-API-Tier header value is returned in response."""
        from tests.testapp.views import TieredEndpointView

        request = _make_request(
            "/test/tiered/",
            HTTP_X_API_TIER="enterprise",
        )
        response = TieredEndpointView.as_view()(request)
        body = json.loads(response.content)

        assert body["tier"] == "enterprise"

    def test_get_returns_200(self):
        """GET /test/tiered/ returns HTTP 200 regardless of tier."""
        from tests.testapp.views import TieredEndpointView

        request = _make_request("/test/tiered/", HTTP_X_API_TIER="pro")
        response = TieredEndpointView.as_view()(request)

        assert response.status_code == 200
        body = json.loads(response.content)
        assert body["status"] == "ok"
