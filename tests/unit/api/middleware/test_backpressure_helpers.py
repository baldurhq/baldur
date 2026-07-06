"""Unit tests for ``baldur.api.middleware.backpressure`` (PR4).

Scope:
    - ``check_backpressure``: disabled / allow / reject branches, plus the
      fail-open guarantee when ``baldur.scaling`` is unavailable.
    - ``apply_backpressure_headers``: header key set, empty vs non-empty
      degraded features, fail-open when scaling is unavailable.

Uses ``patch`` around ``_try_get_controllers`` (the module's lazy
singleton accessor) so tests can inject deterministic ``RateController`` /
``GracefulDegradation`` doubles without tampering with the real singletons
used by the rest of the test session.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.api.middleware import backpressure as bp_module
from baldur.api.middleware.backpressure import (
    apply_backpressure_headers,
    check_backpressure,
)
from baldur.interfaces.web_framework import (
    HttpMethod,
    RequestContext,
    ResponseContext,
)
from baldur.scaling.config import BackpressureLevel
from baldur.scaling.graceful_degradation import GracefulDegradation
from baldur.scaling.rate_controller import RateController, RateControllerState
from baldur.settings.backpressure import (
    get_backpressure_settings,
    reset_backpressure_settings,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_backpressure_settings(monkeypatch):
    """Re-enable backpressure for behavior tests (v1.1 deferred default False per impl 527)."""
    monkeypatch.setenv("BALDUR_BACKPRESSURE_BACKPRESSURE_ENABLED", "true")
    reset_backpressure_settings()
    yield
    reset_backpressure_settings()


def _make_request() -> RequestContext:
    return RequestContext(
        method=HttpMethod.GET,
        path="/api/resource/",
        client_ip="203.0.113.5",
    )


def _state(level: BackpressureLevel) -> RateControllerState:
    return RateControllerState(
        current_rate=0.0,
        target_rate=0.0,
        level=level,
        queue_size=0,
        processed_count=0,
        dropped_count=0,
    )


def _mock_controller(
    *,
    should_process: bool,
    level: BackpressureLevel = BackpressureLevel.NONE,
) -> MagicMock:
    controller = MagicMock(spec=RateController)
    controller.should_process.return_value = should_process
    controller.get_state.return_value = _state(level)
    return controller


def _mock_degradation(disabled: list[str] | None = None) -> MagicMock:
    degradation = MagicMock(spec=GracefulDegradation)
    degradation.get_disabled_features.return_value = disabled or []
    return degradation


# =============================================================================
# check_backpressure — Behavior
# =============================================================================


class TestCheckBackpressureBehavior:
    """Allow/reject decision governed by RateController.should_process()."""

    def test_returns_none_when_disabled(self):
        """backpressure_enabled=False short-circuits before any scaling probe."""
        fake_settings = MagicMock()
        fake_settings.backpressure_enabled = False
        with patch.object(
            bp_module, "get_backpressure_settings", return_value=fake_settings
        ):
            assert check_backpressure(_make_request()) is None

    def test_returns_none_when_controller_allows(self):
        controller = _mock_controller(should_process=True)
        with patch.object(
            bp_module,
            "_try_get_controllers",
            return_value=(controller, _mock_degradation()),
        ):
            assert check_backpressure(_make_request()) is None

    def test_returns_503_response_context_when_overloaded(self):
        controller = _mock_controller(
            should_process=False,
            level=BackpressureLevel.HIGH,
        )
        with patch.object(
            bp_module,
            "_try_get_controllers",
            return_value=(controller, _mock_degradation()),
        ):
            response = check_backpressure(_make_request())

        assert isinstance(response, ResponseContext)
        assert response.status_code == 503

    def test_rejection_headers_include_retry_after_and_level(self):
        """D9: ResponseContext preserves Retry-After / X-Baldur-Backpressure-Level."""
        controller = _mock_controller(
            should_process=False,
            level=BackpressureLevel.CRITICAL,
        )
        with patch.object(
            bp_module,
            "_try_get_controllers",
            return_value=(controller, _mock_degradation()),
        ):
            response = check_backpressure(_make_request())

        assert "Retry-After" in response.headers
        assert response.headers["X-Baldur-Backpressure-Level"] == (
            BackpressureLevel.CRITICAL.value
        )

    def test_rejection_retry_after_scales_with_level(self):
        """CRITICAL Retry-After must be longer than LOW's — AIMD back-off."""
        high_ctrl = _mock_controller(
            should_process=False, level=BackpressureLevel.CRITICAL
        )
        low_ctrl = _mock_controller(should_process=False, level=BackpressureLevel.LOW)
        settings = get_backpressure_settings()
        expected_critical = settings.get_retry_after_for_level(
            BackpressureLevel.CRITICAL
        )
        expected_low = settings.get_retry_after_for_level(BackpressureLevel.LOW)

        with patch.object(
            bp_module,
            "_try_get_controllers",
            return_value=(high_ctrl, _mock_degradation()),
        ):
            critical_response = check_backpressure(_make_request())
        with patch.object(
            bp_module,
            "_try_get_controllers",
            return_value=(low_ctrl, _mock_degradation()),
        ):
            low_response = check_backpressure(_make_request())

        assert int(critical_response.headers["Retry-After"]) == expected_critical
        assert int(low_response.headers["Retry-After"]) == expected_low
        assert expected_critical > expected_low

    def test_returns_none_when_scaling_unavailable_fail_open(self):
        """Scaling import failure must not block traffic — fail open."""
        with patch.object(
            bp_module,
            "_try_get_controllers",
            return_value=(None, None),
        ):
            assert check_backpressure(_make_request()) is None


# =============================================================================
# apply_backpressure_headers — Behavior
# =============================================================================


class TestApplyBackpressureHeadersBehavior:
    def test_injects_level_header(self):
        controller = _mock_controller(should_process=True, level=BackpressureLevel.LOW)
        headers: dict[str, str] = {}
        with patch.object(
            bp_module,
            "_try_get_controllers",
            return_value=(controller, _mock_degradation()),
        ):
            apply_backpressure_headers(headers)
        assert headers["X-Baldur-Backpressure-Level"] == BackpressureLevel.LOW.value

    def test_emits_degraded_features_when_any_disabled(self):
        """Empty disabled-list must NOT emit the degraded-features header."""
        controller = _mock_controller(should_process=True)
        degradation = _mock_degradation(disabled=["feature_a", "feature_b"])
        headers: dict[str, str] = {}
        with patch.object(
            bp_module,
            "_try_get_controllers",
            return_value=(controller, degradation),
        ):
            apply_backpressure_headers(headers)
        assert headers["X-Baldur-Degraded-Features"] == "feature_a,feature_b"

    def test_omits_degraded_features_when_empty(self):
        controller = _mock_controller(should_process=True)
        headers: dict[str, str] = {}
        with patch.object(
            bp_module,
            "_try_get_controllers",
            return_value=(controller, _mock_degradation(disabled=[])),
        ):
            apply_backpressure_headers(headers)
        assert "X-Baldur-Degraded-Features" not in headers

    def test_noop_when_disabled(self):
        """backpressure_enabled=False leaves headers untouched."""
        fake_settings = MagicMock()
        fake_settings.backpressure_enabled = False
        headers: dict[str, str] = {"X-Existing": "keep"}
        with patch.object(
            bp_module, "get_backpressure_settings", return_value=fake_settings
        ):
            apply_backpressure_headers(headers)
        assert headers == {"X-Existing": "keep"}

    def test_noop_when_scaling_unavailable(self):
        """Scaling import failure leaves headers untouched — fail open."""
        headers: dict[str, str] = {"X-Existing": "keep"}
        with patch.object(
            bp_module,
            "_try_get_controllers",
            return_value=(None, None),
        ):
            apply_backpressure_headers(headers)
        assert headers == {"X-Existing": "keep"}

    def test_preserves_existing_headers(self):
        controller = _mock_controller(should_process=True)
        headers = {"Content-Type": "application/json"}
        with patch.object(
            bp_module,
            "_try_get_controllers",
            return_value=(controller, _mock_degradation()),
        ):
            apply_backpressure_headers(headers)
        assert headers["Content-Type"] == "application/json"
