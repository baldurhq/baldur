"""Unit tests for ``baldur.api.middleware.circuit_breaker`` (PR4).

Scope:
    - ``check_cb_open``: preemptive rejection decision across
      ``service_name=None``, CB closed, CB open, CB half-open, CB service
      unavailable (fail-open), and disabled CB service.
    - ``record_cb_observation``: status-code-driven CB success/failure
      recording; 4xx is neither; ``service_name=None`` is a no-op.

Uses ``patch`` around ``_try_get_cb_service`` (the module's lazy singleton
accessor) to inject deterministic CB doubles without tampering with the
real ``get_circuit_breaker_service`` singleton.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from baldur.api.middleware import circuit_breaker as cb_module
from baldur.api.middleware.circuit_breaker import (
    check_cb_open,
    record_cb_observation,
)
from baldur.interfaces.web_framework import (
    HttpMethod,
    RequestContext,
    ResponseContext,
)
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.utils.time import utc_now


def _make_request(path: str = "/api/pay/") -> RequestContext:
    return RequestContext(method=HttpMethod.POST, path=path)


def _mock_cb(
    *,
    is_enabled: bool = True,
    state: str = "closed",
    opened_at: datetime | None = None,
    recovery_timeout: float = 60.0,
) -> MagicMock:
    service = MagicMock(spec=CircuitBreakerService)
    service.is_enabled = is_enabled
    state_data = MagicMock()
    state_data.state = state
    state_data.opened_at = opened_at
    service.get_or_create_state.return_value = state_data
    config = MagicMock()
    config.recovery_timeout = recovery_timeout
    service.get_effective_config.return_value = config
    service.config = config
    return service


# =============================================================================
# check_cb_open — Behavior
# =============================================================================


class TestCheckCbOpenBehavior:
    """Preemptive rejection respects CB state and the fail-open invariant."""

    def test_returns_none_when_service_name_not_supplied(self):
        """No service_name → helper is a no-op (no implicit inference)."""
        assert check_cb_open(_make_request()) is None

    def test_returns_none_when_cb_closed(self):
        with patch.object(
            cb_module, "_try_get_cb_service", return_value=_mock_cb(state="closed")
        ):
            assert check_cb_open(_make_request(), service_name="payment") is None

    def test_returns_503_when_cb_open(self):
        with patch.object(
            cb_module, "_try_get_cb_service", return_value=_mock_cb(state="open")
        ):
            response = check_cb_open(_make_request(), service_name="payment")
        assert isinstance(response, ResponseContext)
        assert response.status_code == 503

    def test_returns_503_when_cb_half_open(self):
        """HALF_OPEN must also reject — the CB is not ready for general traffic."""
        with patch.object(
            cb_module,
            "_try_get_cb_service",
            return_value=_mock_cb(state="half_open"),
        ):
            response = check_cb_open(_make_request(), service_name="payment")
        assert response.status_code == 503

    def test_accepts_case_insensitive_open_state(self):
        """CB backends may return 'OPEN' (upper) or 'open' (lower)."""
        with patch.object(
            cb_module, "_try_get_cb_service", return_value=_mock_cb(state="OPEN")
        ):
            response = check_cb_open(_make_request(), service_name="payment")
        assert response is not None
        assert response.status_code == 503

    def test_rejection_headers_include_retry_after_and_cb_state(self):
        with patch.object(
            cb_module, "_try_get_cb_service", return_value=_mock_cb(state="open")
        ):
            response = check_cb_open(_make_request(), service_name="payment")
        assert "Retry-After" in response.headers
        assert response.headers["X-Baldur-Circuit-Breaker"] == "open"

    def test_rejection_body_identifies_service_and_error_code(self):
        with patch.object(
            cb_module, "_try_get_cb_service", return_value=_mock_cb(state="open")
        ):
            response = check_cb_open(_make_request(), service_name="payment")
        assert response.body["service"] == "payment"
        assert response.body["code"] == "CIRCUIT_BREAKER_OPEN"

    def test_returns_none_when_cb_service_disabled_fail_open(self):
        """CB globally disabled → helper is a no-op, not a rejection."""
        with patch.object(
            cb_module,
            "_try_get_cb_service",
            return_value=_mock_cb(is_enabled=False, state="open"),
        ):
            assert check_cb_open(_make_request(), service_name="payment") is None

    def test_returns_none_when_cb_service_unavailable(self):
        """CB infra import failure → fail-open (never block on broken health)."""
        with patch.object(cb_module, "_try_get_cb_service", return_value=None):
            assert check_cb_open(_make_request(), service_name="payment") is None

    def test_returns_none_when_state_lookup_raises(self):
        """Unexpected CB backend error → fail-open."""
        service = _mock_cb()
        service.get_or_create_state.side_effect = RuntimeError("backend exploded")
        with patch.object(cb_module, "_try_get_cb_service", return_value=service):
            assert check_cb_open(_make_request(), service_name="payment") is None

    def test_retry_after_reflects_remaining_recovery_window(self):
        """OPEN 20s into a 60s recovery window → Retry-After ~= 40s."""
        opened = utc_now() - timedelta(seconds=20)
        with patch.object(
            cb_module,
            "_try_get_cb_service",
            return_value=_mock_cb(
                state="open", opened_at=opened, recovery_timeout=60.0
            ),
        ):
            response = check_cb_open(_make_request(), service_name="payment")
        assert 39 <= int(response.headers["Retry-After"]) <= 41

    def test_retry_after_floors_at_one_when_recovery_due(self):
        """Recovery window already elapsed → floor of 1, never 0 or negative."""
        opened = utc_now() - timedelta(seconds=120)
        with patch.object(
            cb_module,
            "_try_get_cb_service",
            return_value=_mock_cb(
                state="open", opened_at=opened, recovery_timeout=60.0
            ),
        ):
            response = check_cb_open(_make_request(), service_name="payment")
        assert int(response.headers["Retry-After"]) == 1

    def test_retry_after_uses_full_window_when_half_open(self):
        """HALF_OPEN (no opened_at) → conservative full recovery window."""
        with patch.object(
            cb_module,
            "_try_get_cb_service",
            return_value=_mock_cb(state="half_open", recovery_timeout=45.0),
        ):
            response = check_cb_open(_make_request(), service_name="payment")
        assert int(response.headers["Retry-After"]) == 45


# =============================================================================
# record_cb_observation — Behavior (side-effect)
# =============================================================================


class TestRecordCbObservationBehavior:
    """5xx → record_failure; 2xx/3xx → record_success; 4xx → neither."""

    def test_server_error_records_failure(self):
        service = _mock_cb()
        with patch.object(cb_module, "_try_get_cb_service", return_value=service):
            record_cb_observation(
                _make_request(), status_code=503, service_name="payment"
            )
        service.record_failure.assert_called_once()
        call = service.record_failure.call_args
        # service_name passed positionally, error_context as kwarg
        assert call.args[0] == "payment"
        assert call.kwargs["error_context"]["error_type"] == "HTTP_503"
        service.record_success.assert_not_called()

    def test_success_status_records_success(self):
        service = _mock_cb()
        with patch.object(cb_module, "_try_get_cb_service", return_value=service):
            record_cb_observation(
                _make_request(), status_code=200, service_name="payment"
            )
        service.record_success.assert_called_once_with("payment")
        service.record_failure.assert_not_called()

    def test_redirect_status_records_success(self):
        """3xx is in the [200, 400) success bucket."""
        service = _mock_cb()
        with patch.object(cb_module, "_try_get_cb_service", return_value=service):
            record_cb_observation(
                _make_request(), status_code=302, service_name="payment"
            )
        service.record_success.assert_called_once()

    def test_client_error_is_neither_success_nor_failure(self):
        """4xx is caller's fault — CB state must not react."""
        service = _mock_cb()
        with patch.object(cb_module, "_try_get_cb_service", return_value=service):
            record_cb_observation(
                _make_request(), status_code=404, service_name="payment"
            )
        service.record_failure.assert_not_called()
        service.record_success.assert_not_called()

    def test_no_service_name_is_noop(self):
        """service_name=None → no observation recorded, no CB pollution."""
        service = _mock_cb()
        with patch.object(cb_module, "_try_get_cb_service", return_value=service):
            record_cb_observation(_make_request(), status_code=500)
        service.record_failure.assert_not_called()
        service.record_success.assert_not_called()

    def test_disabled_service_is_noop(self):
        """is_enabled=False short-circuits — no CB method is called."""
        service = _mock_cb(is_enabled=False)
        with patch.object(cb_module, "_try_get_cb_service", return_value=service):
            record_cb_observation(
                _make_request(), status_code=500, service_name="payment"
            )
        service.record_failure.assert_not_called()
        service.record_success.assert_not_called()

    def test_cb_service_unavailable_is_noop(self):
        """CB import failure → no-op, no raise."""
        with patch.object(cb_module, "_try_get_cb_service", return_value=None):
            record_cb_observation(
                _make_request(), status_code=500, service_name="payment"
            )  # must not raise

    def test_record_failure_exception_is_swallowed(self):
        """Observation must never propagate CB backend errors to the caller."""
        service = _mock_cb()
        service.record_failure.side_effect = RuntimeError("backend down")
        with patch.object(cb_module, "_try_get_cb_service", return_value=service):
            # Must not raise — observation is fire-and-forget
            record_cb_observation(
                _make_request(), status_code=500, service_name="payment"
            )


# =============================================================================
# record_cb_observation — Contract (error_context shape)
# =============================================================================


class TestRecordCbObservationContract:
    """The error_context dict shape is consumed by audit + metrics downstream."""

    def test_error_context_carries_http_status_path_method(self):
        service = _mock_cb()
        with patch.object(cb_module, "_try_get_cb_service", return_value=service):
            record_cb_observation(
                _make_request(path="/api/pay/"),
                status_code=502,
                service_name="payment",
            )
        ec = service.record_failure.call_args.kwargs["error_context"]
        assert ec["error_type"] == "HTTP_502"
        assert ec["path"] == "/api/pay/"
        assert ec["method"] == "POST"
