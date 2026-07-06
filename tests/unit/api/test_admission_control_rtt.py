"""
Unit tests — AdmissionControlMiddleware RTT sample collection (post-step).

RTT gradient sampling runs after the downstream returns, keyed by the tier
classified by ``check_admission`` (exposed on ``AdmissionDecision.tier_id``).
The Django wrapper now delegates it to the framework-free ``record_rtt_sample``
helper (``api/middleware/deadline.py``), measuring ``elapsed_ms`` itself. Triple
filtering: 2xx only, above the minimum threshold, probabilistic sampling.
Collection failures are fail-open.
"""

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")

import django

django.setup()

from unittest.mock import MagicMock, patch

import pytest

from baldur.api.django.admission_control import AdmissionControlMiddleware
from baldur.api.middleware import AdmissionDecision
from baldur.scaling.deadline_context import _request_deadline
from baldur_pro.services.throttle.gradient import reset_gradient_calculators

# The relocated ``_RTT_MIN_SAMPLE_MS`` / ``_RTT_SAMPLE_RATE`` default contract now
# lives in the OSS-always ``tests/unit/scaling/test_deadline_context.py``
# (``TestDeadlineContextRttConstants``) — verified without a PRO install.


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset the calculator registry and the deadline ContextVar per test."""
    reset_gradient_calculators()
    _request_deadline.set(None)
    yield
    reset_gradient_calculators()
    _request_deadline.set(None)


class TestAdmissionControlRttSamplingBehavior:
    """AdmissionControlMiddleware RTT sample collection behavior."""

    def _create_middleware(self, response_status=200, tier_id="standard"):
        """Build a middleware whose admission allows under PRO for ``tier_id``."""
        mock_response = MagicMock()
        mock_response.status_code = response_status
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

        # check_admission allows under PRO (active=True) and reports the tier so
        # the Django RTT post-step runs and names the per-tier calculator.
        patcher = patch(
            "baldur.api.django.admission_control.check_admission",
            return_value=AdmissionDecision(active=True, tier_id=tier_id),
        )
        patcher.start()
        return middleware, get_response, mock_response, patcher

    def _create_request(self):
        request = MagicMock()
        request.path = "/api/test"
        request.method = "GET"
        request.META = {"REMOTE_ADDR": "127.0.0.1"}
        # The Django wrapper builds RequestContext(headers=dict(request.headers
        # .items())); a real dict avoids MagicMock leaking into the context.
        request.headers = {}
        request.user.is_authenticated = False
        return request

    def test_2xx_response_triggers_rtt_sampling(self):
        """HTTP 200 -> RTT sample collection is attempted."""
        middleware, _, _, patcher = self._create_middleware(response_status=200)
        try:
            request = self._create_request()
            with (
                patch("baldur.api.middleware.deadline.random") as mock_random,
                patch("baldur.api.django.admission_control.time") as mock_time,
                patch(
                    "baldur_pro.services.throttle.gradient.get_gradient_calculator"
                ) as mock_get_calc,
            ):
                mock_random.random.return_value = 0.0
                mock_time.perf_counter.side_effect = [0.0, 0.010]
                mock_calc = MagicMock()
                mock_get_calc.return_value = mock_calc

                middleware(request)

                mock_get_calc.assert_called_once_with("admission_control:standard")
                mock_calc.add_sample.assert_called_once()
                sampled_ms = mock_calc.add_sample.call_args[0][0]
                assert sampled_ms == pytest.approx(10.0, abs=1.0)
        finally:
            patcher.stop()

    def test_4xx_response_not_sampled(self):
        middleware, _, _, patcher = self._create_middleware(response_status=400)
        try:
            request = self._create_request()
            with (
                patch("baldur.api.middleware.deadline.random") as mock_random,
                patch(
                    "baldur_pro.services.throttle.gradient.get_gradient_calculator"
                ) as mock_get_calc,
            ):
                mock_random.random.return_value = 0.0
                middleware(request)
                mock_get_calc.assert_not_called()
        finally:
            patcher.stop()

    def test_5xx_response_not_sampled(self):
        middleware, _, _, patcher = self._create_middleware(response_status=500)
        try:
            request = self._create_request()
            with (
                patch("baldur.api.middleware.deadline.random") as mock_random,
                patch(
                    "baldur_pro.services.throttle.gradient.get_gradient_calculator"
                ) as mock_get_calc,
            ):
                mock_random.random.return_value = 0.0
                middleware(request)
                mock_get_calc.assert_not_called()
        finally:
            patcher.stop()

    def test_below_min_threshold_not_sampled(self):
        middleware, _, _, patcher = self._create_middleware(response_status=200)
        try:
            request = self._create_request()
            with (
                patch("baldur.api.middleware.deadline.random") as mock_random,
                patch("baldur.api.django.admission_control.time") as mock_time,
                patch(
                    "baldur_pro.services.throttle.gradient.get_gradient_calculator"
                ) as mock_get_calc,
            ):
                mock_random.random.return_value = 0.0
                mock_time.perf_counter.side_effect = [0.0, 0.003]  # 3ms < 5ms
                middleware(request)
                mock_get_calc.assert_not_called()
        finally:
            patcher.stop()

    def test_sampling_rate_respected(self):
        middleware, _, _, patcher = self._create_middleware(response_status=200)
        try:
            request = self._create_request()
            with (
                patch("baldur.api.middleware.deadline.random") as mock_random,
                patch("baldur.api.django.admission_control.time") as mock_time,
                patch(
                    "baldur_pro.services.throttle.gradient.get_gradient_calculator"
                ) as mock_get_calc,
            ):
                mock_random.random.return_value = 0.5  # 0.5 >= 0.1 -> skip
                mock_time.perf_counter.side_effect = [0.0, 0.100]
                middleware(request)
                mock_get_calc.assert_not_called()
        finally:
            patcher.stop()

    def test_tier_separated_calculator_name(self):
        """A critical-tier request uses the critical calculator name."""
        middleware, _, _, patcher = self._create_middleware(
            response_status=200, tier_id="critical"
        )
        try:
            request = self._create_request()
            with (
                patch("baldur.api.middleware.deadline.random") as mock_random,
                patch("baldur.api.django.admission_control.time") as mock_time,
                patch(
                    "baldur_pro.services.throttle.gradient.get_gradient_calculator"
                ) as mock_get_calc,
            ):
                mock_random.random.return_value = 0.0
                mock_time.perf_counter.side_effect = [0.0, 0.050]
                mock_calc = MagicMock()
                mock_get_calc.return_value = mock_calc

                middleware(request)

                mock_get_calc.assert_called_once_with("admission_control:critical")
        finally:
            patcher.stop()

    def test_rtt_collection_fail_open(self):
        """An exception during RTT collection does not affect the response."""
        middleware, get_response, mock_response, patcher = self._create_middleware(
            response_status=200
        )
        try:
            request = self._create_request()
            with (
                patch("baldur.api.middleware.deadline.random") as mock_random,
                patch("baldur.api.django.admission_control.time") as mock_time,
                patch(
                    "baldur_pro.services.throttle.gradient.get_gradient_calculator",
                    side_effect=RuntimeError("unexpected error"),
                ),
            ):
                mock_random.random.return_value = 0.0
                mock_time.perf_counter.side_effect = [0.0, 0.050]
                response = middleware(request)

            assert response == mock_response
            get_response.assert_called_once_with(request)
        finally:
            patcher.stop()

    def test_inactive_admission_skips_rtt(self):
        """OSS no-op (active=False) -> no RTT sampling (no tier classified)."""
        mock_response = MagicMock()
        mock_response.status_code = 200
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

        request = self._create_request()
        with (
            patch(
                "baldur.api.django.admission_control.check_admission",
                return_value=AdmissionDecision(active=False),
            ),
            patch("baldur.api.middleware.deadline.random") as mock_random,
            patch("baldur.api.django.admission_control.time") as mock_time,
            patch(
                "baldur_pro.services.throttle.gradient.get_gradient_calculator"
            ) as mock_get_calc,
        ):
            mock_random.random.return_value = 0.0
            mock_time.perf_counter.side_effect = [0.0, 0.050]
            middleware(request)
            mock_get_calc.assert_not_called()
