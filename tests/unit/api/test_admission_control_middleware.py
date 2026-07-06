"""
Unit tests for AdmissionControlMiddleware (Django delegation wrapper).

The middleware now delegates the admission decision to the framework-free
``check_admission`` helper (api/middleware/admission.py) and keeps only the
Django-only wrappers (deadline-header fast-fail pre-step, RTT sampling
post-step, 503 conversion, bulkhead release, clear_deadline). These tests
exercise the wrapper's observable contract by patching ``check_admission``;
the classify -> priority -> should_allow contract lives in the
``check_admission`` helper's own tests.
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")

import django

django.setup()

from unittest.mock import MagicMock, patch

from baldur.api.django.admission_control import AdmissionControlMiddleware
from baldur.api.middleware import AdmissionDecision
from baldur.api.middleware.admission import TIER_PRIORITY_MAP
from baldur.interfaces.web_framework import ResponseContext


class TestTierPriorityMapContract:
    """TIER_PRIORITY_MAP constant contract (now owned by the helper module)."""

    def test_has_three_tiers(self):
        assert len(TIER_PRIORITY_MAP) == 3

    def test_critical_value(self):
        assert TIER_PRIORITY_MAP["critical"] == 0

    def test_standard_value(self):
        assert TIER_PRIORITY_MAP["standard"] == 50

    def test_non_essential_value(self):
        assert TIER_PRIORITY_MAP["non_essential"] == 100

    def test_ordering(self):
        assert (
            TIER_PRIORITY_MAP["critical"]
            < TIER_PRIORITY_MAP["standard"]
            < TIER_PRIORITY_MAP["non_essential"]
        )


def _make_middleware(enabled=True, response_status=200):
    """Build an AdmissionControlMiddleware with a mocked downstream."""
    mock_response = MagicMock()
    mock_response.status_code = response_status
    get_response = MagicMock(return_value=mock_response)

    mock_settings = MagicMock()
    mock_settings.enabled = enabled

    with patch(
        "baldur.settings.admission_control.get_admission_control_settings",
        return_value=mock_settings,
    ):
        middleware = AdmissionControlMiddleware(get_response)
        middleware._enabled = enabled
        middleware._settings = mock_settings

    return middleware, get_response, mock_response


def _make_request(method="GET", path="/api/baldur/config/test", meta=None):
    request = MagicMock()
    request.method = method
    request.path = path
    request.META = meta or {"REMOTE_ADDR": "127.0.0.1"}
    request.user.is_authenticated = False
    return request


class TestAdmissionControlMiddlewareDelegation:
    """AdmissionControlMiddleware delegation behavior."""

    def test_allowed_request_passes_through(self):
        """check_admission allow -> request forwarded downstream."""
        middleware, get_response, mock_response = _make_middleware()
        request = _make_request()

        with patch(
            "baldur.api.django.admission_control.check_admission",
            return_value=AdmissionDecision(active=True, tier_id="standard"),
        ):
            response = middleware(request)

        get_response.assert_called_once_with(request)
        assert response == mock_response

    def test_inactive_oss_noop_passes_through(self):
        """OSS no-op (active=False) -> request forwarded downstream."""
        middleware, get_response, mock_response = _make_middleware()
        request = _make_request()

        with patch(
            "baldur.api.django.admission_control.check_admission",
            return_value=AdmissionDecision(active=False),
        ):
            response = middleware(request)

        get_response.assert_called_once_with(request)
        assert response == mock_response

    def test_rejected_request_returns_503(self):
        """check_admission rejection -> 503 Django response, downstream skipped."""
        middleware, get_response, _ = _make_middleware()
        request = _make_request()

        rejection = ResponseContext(
            status_code=503,
            body={"code": "ADMISSION_CONTROL_REJECTED", "tier": "standard"},
            headers={"Retry-After": "5"},
        )
        with patch(
            "baldur.api.django.admission_control.check_admission",
            return_value=AdmissionDecision(rejection=rejection, active=True),
        ):
            response = middleware(request)

        assert response.status_code == 503
        get_response.assert_not_called()

    def test_rejected_response_preserves_retry_after_header(self):
        """The rejection's Retry-After header survives the Django conversion."""
        middleware, _, _ = _make_middleware()
        request = _make_request()

        rejection = ResponseContext(
            status_code=503,
            body={"code": "ADMISSION_CONTROL_REJECTED"},
            headers={"Retry-After": "7"},
        )
        with patch(
            "baldur.api.django.admission_control.check_admission",
            return_value=AdmissionDecision(rejection=rejection, active=True),
        ):
            response = middleware(request)

        assert response["Retry-After"] == "7"

    def test_options_request_bypasses_admission(self):
        """CORS preflight (OPTIONS) is always allowed without delegating."""
        middleware, get_response, mock_response = _make_middleware()
        request = _make_request(method="OPTIONS")

        with patch("baldur.api.django.admission_control.check_admission") as mock_check:
            response = middleware(request)

        mock_check.assert_not_called()
        get_response.assert_called_once_with(request)
        assert response == mock_response

    def test_disabled_middleware_passes_through(self):
        """Disabled middleware forwards without delegating."""
        middleware, get_response, _ = _make_middleware(enabled=False)
        request = _make_request()

        with patch("baldur.api.django.admission_control.check_admission") as mock_check:
            middleware(request)

        get_response.assert_called_once_with(request)
        mock_check.assert_not_called()

    def test_exception_during_processing_allows_request(self):
        """An unexpected error in admission fails open (request allowed)."""
        middleware, get_response, _ = _make_middleware()
        request = _make_request()

        with patch(
            "baldur.api.django.admission_control.check_admission",
            side_effect=RuntimeError("boom"),
        ):
            middleware(request)

        get_response.assert_called_once_with(request)

    def test_bulkhead_released_after_response(self):
        """An acquired bulkhead slot is released after the downstream returns."""
        middleware, get_response, _ = _make_middleware()
        request = _make_request()
        release = MagicMock()

        with patch(
            "baldur.api.django.admission_control.check_admission",
            return_value=AdmissionDecision(
                active=True, tier_id="standard", release=release
            ),
        ):
            middleware(request)

        release.assert_called_once_with()

    def test_clear_deadline_invoked_on_every_path(self):
        """The request-scoped deadline is cleared in the __call__ finally."""
        middleware, _, _ = _make_middleware()
        request = _make_request()

        with (
            patch(
                "baldur.api.django.admission_control.check_admission",
                return_value=AdmissionDecision(active=True, tier_id="standard"),
            ),
            patch.object(middleware, "_clear_deadline") as mock_clear,
        ):
            middleware(request)

        mock_clear.assert_called_once_with()
