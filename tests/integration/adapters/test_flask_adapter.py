"""Mock-based integration tests for ``baldur.adapters.flask`` (429 / PR4).

Same four-service composition as the FastAPI integration test, driven
through Flask's ``before_request`` / ``after_request`` hook lifecycle:
    rate_limit → backpressure → cb_open (reject decisions) →
    apply_rate_limit_headers + apply_backpressure_headers (success headers) →
    record_cb_observation (post-response side-effect)

Uses Flask's built-in test client, no Docker. ``init_flask`` is NOT called
directly because it invokes ``baldur.init()`` which performs expensive
one-time bootstrapping that leaks into later tests; instead these tests
wire the hooks directly via ``install_baldur_request_hooks`` (the same
function ``init_flask`` delegates to).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from baldur.adapters.flask.middleware import install_baldur_request_hooks
from baldur.api.middleware import admission as adm_module
from baldur.api.middleware import backpressure as bp_module
from baldur.api.middleware import circuit_breaker as cb_module
from baldur.api.middleware.rate_limit import reset_rate_limit_state
from baldur.scaling.config import BackpressureLevel
from baldur.scaling.graceful_degradation import GracefulDegradation
from baldur.scaling.rate_controller import RateController, RateControllerState
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.settings.backpressure import reset_backpressure_settings
from baldur.settings.rate_limit import reset_rate_limit_settings

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch):
    # #527 R5 follow-up: BackpressureSettings.backpressure_enabled defaults
    # to False (v1.1-deferred). Adapter middleware tests need the gate ON to
    # observe X-Baldur-Backpressure-Level / X-Baldur-Degraded-Features headers
    # and the 503 reject path.
    monkeypatch.setenv("BALDUR_BACKPRESSURE_BACKPRESSURE_ENABLED", "true")
    reset_rate_limit_state()
    reset_rate_limit_settings()
    reset_backpressure_settings()
    yield
    reset_rate_limit_state()
    reset_rate_limit_settings()
    reset_backpressure_settings()


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


def _mock_cb(*, state: str = "closed", is_enabled: bool = True) -> MagicMock:
    service = MagicMock(spec=CircuitBreakerService)
    service.is_enabled = is_enabled
    service.get_state.return_value = state
    # The CB middleware reads the full state object (state + opened_at for the
    # Retry-After window) via get_or_create_state, not the bare get_state string.
    state_data = service.get_or_create_state.return_value
    state_data.state = state
    state_data.opened_at = None
    service.get_effective_config.return_value.recovery_timeout = 30.0
    return service


def _build_app(
    service_name: str | None = None,
    rate_limit: int | None = 100,
    window_seconds: int | None = 60,
) -> Flask:
    """Build a test Flask app with the adapter hooks installed.

    Defaults ``rate_limit=100`` so success-path tests observe the
    ``X-RateLimit-*`` headers. Reject-path tests override this explicitly.
    """
    app = Flask(__name__)
    install_baldur_request_hooks(
        app,
        service_name=service_name,
        rate_limit=rate_limit,
        window_seconds=window_seconds,
    )
    downstream = {"calls": 0}

    @app.route("/ping")
    def ping():
        downstream["calls"] += 1
        return {"ok": True}, 200

    @app.route("/boom")
    def boom():
        downstream["calls"] += 1
        return {"error": "upstream exploded"}, 502

    app.downstream = downstream  # expose to tests
    return app


# =============================================================================
# Success path
# =============================================================================


class TestFlaskSuccessPathIntegration:
    """Request forwarded to the view function + success-side headers injected."""

    def test_allowed_request_reaches_view(self):
        app = _build_app()
        with patch.object(
            bp_module,
            "_try_get_controllers",
            return_value=(
                _mock_controller(should_process=True),
                _mock_degradation(),
            ),
        ):
            response = app.test_client().get("/ping")
        assert response.status_code == 200
        assert response.get_json() == {"ok": True}
        assert app.downstream["calls"] == 1

    def test_allowed_response_carries_rate_limit_headers(self):
        app = _build_app()
        with patch.object(
            bp_module,
            "_try_get_controllers",
            return_value=(
                _mock_controller(should_process=True),
                _mock_degradation(),
            ),
        ):
            response = app.test_client().get("/ping")
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers
        assert "X-RateLimit-Reset" in response.headers

    def test_allowed_response_carries_backpressure_level_header(self):
        app = _build_app()
        with patch.object(
            bp_module,
            "_try_get_controllers",
            return_value=(
                _mock_controller(should_process=True, level=BackpressureLevel.MEDIUM),
                _mock_degradation(),
            ),
        ):
            response = app.test_client().get("/ping")
        assert (
            response.headers["X-Baldur-Backpressure-Level"]
            == BackpressureLevel.MEDIUM.value
        )

    def test_degraded_features_header_emitted_when_present(self):
        app = _build_app()
        with patch.object(
            bp_module,
            "_try_get_controllers",
            return_value=(
                _mock_controller(should_process=True),
                _mock_degradation(disabled=["ads", "recos"]),
            ),
        ):
            response = app.test_client().get("/ping")
        assert response.headers["X-Baldur-Degraded-Features"] == "ads,recos"


# =============================================================================
# Reject path
# =============================================================================


class TestFlaskRejectPathIntegration:
    """Reject-decision helpers short-circuit before view function is called."""

    def test_rate_limit_rejects_with_429(self):
        """L1 limiter configured to 1 req/min → 2nd request returns 429."""
        app = _build_app(rate_limit=1, window_seconds=60)
        client = app.test_client()
        with patch.object(
            bp_module,
            "_try_get_controllers",
            return_value=(
                _mock_controller(should_process=True),
                _mock_degradation(),
            ),
        ):
            ok = client.get("/ping")
            rejected = client.get("/ping")

        assert ok.status_code == 200
        assert rejected.status_code == 429
        assert "Retry-After" in rejected.headers
        assert app.downstream["calls"] == 1  # 2nd req never reached view

    def test_backpressure_rejects_with_503(self):
        app = _build_app()
        # Isolate the OSS baseline backpressure path. In the monorepo
        # (baldur_pro importable via the editable src/ tree) the PRO per-tier
        # Bulkhead registry is populated, admission becomes active, and the
        # adapter deliberately skips check_backpressure (shared token bucket).
        # Force the registry empty so the OSS path runs, as in an OSS install.
        with (
            patch.object(adm_module, "_bulkhead_registry", return_value=None),
            patch.object(
                bp_module,
                "_try_get_controllers",
                return_value=(
                    _mock_controller(
                        should_process=False, level=BackpressureLevel.HIGH
                    ),
                    _mock_degradation(),
                ),
            ),
        ):
            response = app.test_client().get("/ping")
        assert response.status_code == 503
        assert response.headers["X-Baldur-Backpressure-Level"] == (
            BackpressureLevel.HIGH.value
        )
        assert "Retry-After" in response.headers
        assert app.downstream["calls"] == 0

    def test_cb_open_rejects_with_503(self):
        app = _build_app(service_name="payment")
        with (
            patch.object(
                bp_module,
                "_try_get_controllers",
                return_value=(
                    _mock_controller(should_process=True),
                    _mock_degradation(),
                ),
            ),
            patch.object(
                cb_module, "_try_get_cb_service", return_value=_mock_cb(state="open")
            ),
        ):
            response = app.test_client().get("/ping")
        assert response.status_code == 503
        assert response.headers["X-Baldur-Circuit-Breaker"] == "open"
        assert app.downstream["calls"] == 0


# =============================================================================
# Post-response observation
# =============================================================================


class TestFlaskCbObservationIntegration:
    """5xx view responses record a CB failure against the service."""

    def test_server_error_records_cb_failure(self):
        cb_service = _mock_cb()
        app = _build_app(service_name="payment")
        with (
            patch.object(
                bp_module,
                "_try_get_controllers",
                return_value=(
                    _mock_controller(should_process=True),
                    _mock_degradation(),
                ),
            ),
            patch.object(cb_module, "_try_get_cb_service", return_value=cb_service),
        ):
            response = app.test_client().get("/boom")
        assert response.status_code == 502
        cb_service.record_failure.assert_called_once()
        assert cb_service.record_failure.call_args.args[0] == "payment"

    def test_success_records_cb_success(self):
        cb_service = _mock_cb()
        app = _build_app(service_name="payment")
        with (
            patch.object(
                bp_module,
                "_try_get_controllers",
                return_value=(
                    _mock_controller(should_process=True),
                    _mock_degradation(),
                ),
            ),
            patch.object(cb_module, "_try_get_cb_service", return_value=cb_service),
        ):
            app.test_client().get("/ping")
        cb_service.record_success.assert_called_once_with("payment")

    def test_no_service_name_means_no_cb_observation(self):
        """service_name=None → observation helper is a no-op."""
        cb_service = _mock_cb()
        app = _build_app(service_name=None)
        with (
            patch.object(
                bp_module,
                "_try_get_controllers",
                return_value=(
                    _mock_controller(should_process=True),
                    _mock_degradation(),
                ),
            ),
            patch.object(cb_module, "_try_get_cb_service", return_value=cb_service),
        ):
            app.test_client().get("/boom")
        cb_service.record_failure.assert_not_called()
        cb_service.record_success.assert_not_called()
