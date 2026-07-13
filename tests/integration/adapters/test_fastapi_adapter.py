"""Mock-based integration tests for ``baldur.adapters.fastapi`` (429 / PR4).

Composes 4 services through one request lifecycle:
    rate_limit → backpressure → cb_open (reject decisions) →
    apply_rate_limit_headers + apply_backpressure_headers (success headers) →
    record_cb_observation (post-response side-effect)

Uses FastAPI ``TestClient`` with a minimal downstream ``/ping`` route, no
Docker / Redis. The rate-limit sliding window uses the in-process L1
limiter that ships with PR4; Redis hybrid mode is out of scope for this
adapter (see api/middleware/rate_limit.py module docstring).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from baldur.adapters.fastapi import BaldurMiddleware
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
) -> FastAPI:
    """Build a test FastAPI app with the adapter middleware.

    Defaults ``rate_limit=100`` so success-path tests observe the
    ``X-RateLimit-*`` headers. Reject-path tests override this explicitly.
    """
    app = FastAPI()
    app.add_middleware(
        BaldurMiddleware,
        service_name=service_name,
        rate_limit=rate_limit,
        window_seconds=window_seconds,
    )

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.get("/boom")
    def boom():
        from fastapi import HTTPException

        raise HTTPException(status_code=502, detail="upstream exploded")

    return app


# =============================================================================
# Success path
# =============================================================================


class TestFastApiSuccessPathIntegration:
    """Request forwarded to downstream + success-side headers injected."""

    def test_allowed_request_reaches_downstream(self):
        client = TestClient(_build_app())
        with patch.object(
            bp_module,
            "_try_get_controllers",
            return_value=(
                _mock_controller(should_process=True),
                _mock_degradation(),
            ),
        ):
            response = client.get("/ping")
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    def test_allowed_response_carries_rate_limit_headers(self):
        client = TestClient(_build_app())
        with patch.object(
            bp_module,
            "_try_get_controllers",
            return_value=(
                _mock_controller(should_process=True),
                _mock_degradation(),
            ),
        ):
            response = client.get("/ping")
        assert "x-ratelimit-limit" in response.headers
        assert "x-ratelimit-remaining" in response.headers
        assert "x-ratelimit-reset" in response.headers

    def test_allowed_response_carries_backpressure_level_header(self):
        client = TestClient(_build_app())
        with patch.object(
            bp_module,
            "_try_get_controllers",
            return_value=(
                _mock_controller(should_process=True, level=BackpressureLevel.LOW),
                _mock_degradation(),
            ),
        ):
            response = client.get("/ping")
        assert (
            response.headers["x-baldur-backpressure-level"]
            == BackpressureLevel.LOW.value
        )

    def test_degraded_features_header_emitted_when_present(self):
        client = TestClient(_build_app())
        with patch.object(
            bp_module,
            "_try_get_controllers",
            return_value=(
                _mock_controller(should_process=True),
                _mock_degradation(disabled=["ads", "recos"]),
            ),
        ):
            response = client.get("/ping")
        assert response.headers["x-baldur-degraded-features"] == "ads,recos"


# =============================================================================
# Reject path
# =============================================================================


class TestFastApiRejectPathIntegration:
    """Reject-decision helpers short-circuit before downstream is called."""

    def test_rate_limit_rejects_with_429(self):
        """L1 limiter configured to 1 req/min → 2nd request returns 429."""
        downstream_calls = {"n": 0}

        app = FastAPI()
        app.add_middleware(BaldurMiddleware, rate_limit=1, window_seconds=60)

        @app.get("/ping")
        def ping():
            downstream_calls["n"] += 1
            return {"ok": True}

        client = TestClient(app)
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
        assert "retry-after" in rejected.headers
        assert downstream_calls["n"] == 1  # 2nd req never reached downstream

    def test_backpressure_rejects_with_503(self):
        client = TestClient(_build_app())
        # Isolate the OSS baseline backpressure path. With no PRO per-tier
        # Bulkhead registry, check_admission is a no-op and check_backpressure
        # is the active rate gate. In the monorepo (baldur_pro importable via
        # the editable src/ tree) the registry slot is populated, admission
        # becomes active, and the adapter deliberately skips check_backpressure
        # (shared token bucket) — so this path must be exercised with the
        # registry forced empty, as it would be in an OSS-only install.
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
            response = client.get("/ping")
        assert response.status_code == 503
        assert response.headers["x-baldur-backpressure-level"] == (
            BackpressureLevel.HIGH.value
        )
        assert "retry-after" in response.headers

    def test_cb_open_rejects_with_503(self):
        client = TestClient(_build_app(service_name="payment"))
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
            response = client.get("/ping")
        assert response.status_code == 503
        assert response.headers["x-baldur-circuit-breaker"] == "open"


# =============================================================================
# Post-response observation
# =============================================================================


class TestFastApiCbObservationIntegration:
    """5xx downstream responses record a CB failure against the service."""

    def test_server_error_records_cb_failure(self):
        cb_service = _mock_cb()
        client = TestClient(_build_app(service_name="payment"))
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
            response = client.get("/boom")
        assert response.status_code == 502
        cb_service.record_failure.assert_called_once()
        assert cb_service.record_failure.call_args.args[0] == "payment"

    def test_success_records_cb_success(self):
        cb_service = _mock_cb()
        client = TestClient(_build_app(service_name="payment"))
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
            client.get("/ping")
        cb_service.record_success.assert_called_once_with("payment")

    def test_no_service_name_means_no_cb_observation(self):
        """service_name=None → observation helper is a no-op."""
        cb_service = _mock_cb()
        client = TestClient(_build_app(service_name=None))
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
            client.get("/boom")
        cb_service.record_failure.assert_not_called()
        cb_service.record_success.assert_not_called()
