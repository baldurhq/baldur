"""
Unit tests — AdmissionControlMiddleware reject-response header pass-through.

The dynamic Retry-After computation (scaled by BackpressureLevel) and the
tier-specific bulkhead_timeout injection now live inside the framework-free
``check_admission`` helper, which builds the rejection ``ResponseContext``.
The Django middleware's remaining responsibility is to convert that
``ResponseContext`` to a Django 503 without losing its headers — that is what
these tests verify.
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")

import django

django.setup()

from unittest.mock import MagicMock, patch

import pytest

from baldur.api.django.admission_control import AdmissionControlMiddleware
from baldur.api.middleware import AdmissionDecision
from baldur.interfaces.web_framework import ResponseContext
from baldur.settings.backpressure import (
    BackpressureLevel,
    BackpressureSettings,
    reset_backpressure_settings,
)


def _make_middleware():
    get_response = MagicMock(return_value=MagicMock(status_code=200))
    mock_settings = MagicMock()
    mock_settings.enabled = True
    with patch(
        "baldur.settings.admission_control.get_admission_control_settings",
        return_value=mock_settings,
    ):
        middleware = AdmissionControlMiddleware(get_response)
        middleware._enabled = True
        middleware._settings = mock_settings
    return middleware


def _make_request():
    request = MagicMock()
    request.method = "GET"
    request.path = "/api/test"
    request.META = {"REMOTE_ADDR": "127.0.0.1"}
    request.user.is_authenticated = False
    return request


def _rejection_for_level(level: BackpressureLevel) -> ResponseContext:
    """Build a rejection ResponseContext as check_admission would for a level."""
    retry_after = BackpressureSettings().get_retry_after_for_level(level)
    return ResponseContext(
        status_code=503,
        body={
            "error": "Service Temporarily Unavailable",
            "code": "ADMISSION_CONTROL_REJECTED",
            "retry_after": retry_after,
        },
        headers={
            "Retry-After": str(retry_after),
            "X-Baldur-Backpressure-Level": level.value,
        },
    )


class TestRejectionHeaderPassThrough:
    """The Django wrapper preserves the rejection's Retry-After header."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        reset_backpressure_settings()
        yield
        reset_backpressure_settings()

    def _run(self, level: BackpressureLevel):
        middleware = _make_middleware()
        request = _make_request()
        rejection = _rejection_for_level(level)
        with patch(
            "baldur.api.django.admission_control.check_admission",
            return_value=AdmissionDecision(rejection=rejection, active=True),
        ):
            return middleware(request)

    def test_none_level_retry_after_passes_through(self):
        response = self._run(BackpressureLevel.NONE)
        expected = BackpressureSettings().get_retry_after_for_level(
            BackpressureLevel.NONE
        )
        assert response["Retry-After"] == str(expected)

    def test_critical_level_retry_after_greater_than_none(self):
        resp_critical = self._run(BackpressureLevel.CRITICAL)
        resp_none = self._run(BackpressureLevel.NONE)
        assert int(resp_critical["Retry-After"]) > int(resp_none["Retry-After"])

    def test_medium_level_retry_after_passes_through(self):
        response = self._run(BackpressureLevel.MEDIUM)
        expected = BackpressureSettings().get_retry_after_for_level(
            BackpressureLevel.MEDIUM
        )
        assert response["Retry-After"] == str(expected)

    def test_backpressure_level_header_passes_through(self):
        response = self._run(BackpressureLevel.HIGH)
        assert response["X-Baldur-Backpressure-Level"] == BackpressureLevel.HIGH.value
