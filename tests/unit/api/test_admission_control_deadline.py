"""
Unit tests — AdmissionControlMiddleware deadline-header fast-fail.

The inbound ``X-Deadline-Remaining`` header fast-fail is a pre-step that runs
before the request is delegated to ``check_admission``. The Django wrapper now
delegates it to the framework-free ``check_deadline`` helper, reading the header
via ``RequestContext.headers`` (built from ``request.headers``). It rejects with
503 (DEADLINE_FAST_FAIL) when the remaining time is below the minimum useful
threshold, otherwise sets the deadline and lets the request proceed.
"""

import json
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")

import django

django.setup()

from unittest.mock import MagicMock, patch

import pytest

from baldur.api.django.admission_control import AdmissionControlMiddleware
from baldur.api.middleware import AdmissionDecision
from baldur.scaling.deadline_context import (
    DEADLINE_HEADER,
    DEFAULT_MINIMUM_USEFUL_TIME_MS,
    _request_deadline,
)


@pytest.fixture(autouse=True)
def _reset_deadline():
    """Reset the deadline ContextVar around each test."""
    _request_deadline.set(None)
    yield
    _request_deadline.set(None)


class TestAdmissionControlDeadlineBehavior:
    """AdmissionControlMiddleware deadline-header fast-fail behavior."""

    def _create_middleware(self):
        """Build a middleware whose downstream admission is a passthrough."""
        mock_response = MagicMock()
        get_response = MagicMock(return_value=mock_response)

        mock_settings = MagicMock()
        mock_settings.enabled = True

        with patch(
            "baldur.settings.admission_control.get_admission_control_settings",
            return_value=mock_settings,
        ):
            middleware = AdmissionControlMiddleware(get_response)
            middleware._enabled = True
            middleware._settings = mock_settings

        # check_admission is a passthrough (OSS no-op) so the allow path is
        # deterministic regardless of the per-tier bulkhead registry state.
        patcher = patch(
            "baldur.api.django.admission_control.check_admission",
            return_value=AdmissionDecision(active=False),
        )
        patcher.start()
        return middleware, get_response, mock_response, patcher

    def _create_request(self, deadline_header_value=None):
        request = MagicMock()
        request.method = "GET"
        request.path = "/api/test"
        request.META = {"REMOTE_ADDR": "127.0.0.1"}
        # The Django wrapper builds RequestContext(headers=dict(request.headers
        # .items())); check_deadline reads X-Deadline-Remaining via get_header.
        request.headers = {}
        request.user.is_authenticated = False
        if deadline_header_value is not None:
            request.headers[DEADLINE_HEADER] = deadline_header_value
        return request

    def test_deadline_header_fast_fail(self):
        """Remaining 30ms -> 503 (DEADLINE_FAST_FAIL)."""
        middleware, get_response, _, patcher = self._create_middleware()
        try:
            request = self._create_request("30ms")
            response = middleware(request)
            assert response.status_code == 503
            body = json.loads(response.content)
            assert body["code"] == "DEADLINE_FAST_FAIL"
            get_response.assert_not_called()
        finally:
            patcher.stop()

    def test_deadline_header_allowed(self):
        """Remaining 5000ms -> normal passthrough."""
        middleware, get_response, mock_response, patcher = self._create_middleware()
        try:
            request = self._create_request("5000ms")
            response = middleware(request)
            get_response.assert_called_once_with(request)
            assert response == mock_response
        finally:
            patcher.stop()

    def test_no_deadline_header(self):
        """No header -> existing behavior preserved."""
        middleware, get_response, mock_response, patcher = self._create_middleware()
        try:
            request = self._create_request()
            response = middleware(request)
            get_response.assert_called_once_with(request)
            assert response == mock_response
        finally:
            patcher.stop()

    def test_invalid_deadline_header(self):
        """Malformed header -> ignored, existing behavior."""
        middleware, get_response, mock_response, patcher = self._create_middleware()
        try:
            request = self._create_request("invalid_header_value")
            response = middleware(request)
            get_response.assert_called_once_with(request)
            assert response == mock_response
        finally:
            patcher.stop()

    def test_deadline_response_retry_after(self):
        """503 fast-fail response carries Retry-After: 0."""
        middleware, _, _, patcher = self._create_middleware()
        try:
            request = self._create_request("10ms")
            response = middleware(request)
            assert response.status_code == 503
            assert response["Retry-After"] == "0"
        finally:
            patcher.stop()

    def test_deadline_response_body_structure(self):
        """The fast-fail body carries the required fields."""
        middleware, _, _, patcher = self._create_middleware()
        try:
            request = self._create_request("20ms")
            response = middleware(request)
            body = json.loads(response.content)
            assert body["error"] == "Deadline Exceeded"
            assert body["code"] == "DEADLINE_FAST_FAIL"
            assert "remaining_ms" in body
            assert body["retry_after"] == 0
        finally:
            patcher.stop()

    def test_deadline_at_exact_minimum(self):
        """A value equal to the minimum useful time passes (only below rejects)."""
        middleware, get_response, _, patcher = self._create_middleware()
        try:
            request = self._create_request(f"{DEFAULT_MINIMUM_USEFUL_TIME_MS}ms")
            middleware(request)
            get_response.assert_called_once_with(request)
        finally:
            patcher.stop()

    def test_deadline_disabled_bypasses_check(self):
        """DEADLINE_ENABLED=false -> no fast-fail even with a tight header."""
        middleware, get_response, _, patcher = self._create_middleware()
        try:
            request = self._create_request("10ms")
            import baldur.scaling.deadline_context as dc_mod

            original = dc_mod.DEADLINE_ENABLED
            dc_mod.DEADLINE_ENABLED = False
            try:
                middleware(request)
            finally:
                dc_mod.DEADLINE_ENABLED = original
            get_response.assert_called_once_with(request)
        finally:
            patcher.stop()
