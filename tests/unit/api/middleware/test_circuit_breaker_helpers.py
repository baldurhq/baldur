"""Unit tests for ``baldur.api.middleware.circuit_breaker`` (PR4).

Scope:
    - ``check_cb_open``: preemptive rejection decision across
      ``service_name=None``, CB closed, CB open, CB half-open, CB service
      unavailable (fail-open), disabled CB service, and observe-only mode.
    - ``record_cb_observation``: status-code-driven CB success/failure
      recording; 4xx is neither; ``service_name=None`` is a no-op.

Uses ``patch`` around ``_try_get_cb_service`` (the module's lazy singleton
accessor) to inject deterministic CB doubles without tampering with the
real ``get_circuit_breaker_service`` singleton.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from structlog.testing import capture_logs

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
from baldur.services.circuit_breaker.rate_limit_tracker import RateLimitTracker
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.utils.time import utc_now
from tests.factories import dry_run_active


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

    def test_a_relayed_429_records_a_failure_and_the_cascade(self):
        """A throttled upstream is a counted failure, not only a cascade entry.

        The framework-free helper is the whole inbound path on Flask and
        FastAPI. While it dispatched on 5xx alone, a 429 storm reached neither
        the failure count nor the cascade on those frameworks.
        """
        service = _mock_cb()
        with (
            patch.object(cb_module, "_try_get_cb_service", return_value=service),
            patch.object(cb_module, "get_rate_limit_tracker"),
        ):
            record_cb_observation(
                _make_request(), status_code=429, service_name="payment"
            )

        service.record_failure.assert_called_once()
        service.record_rate_limit_response.assert_called_once_with("payment")
        service.record_success.assert_not_called()

    def test_a_status_in_both_sets_records_a_failure_and_the_cascade(self):
        """Membership is non-exclusive; the dispatch is no longer if/elif.

        An operator who listed 429 as a failure code used to lose cascade
        detection for it silently.
        """
        service = _mock_cb()
        with (
            patch.object(cb_module, "_try_get_cb_service", return_value=service),
            patch.object(cb_module, "get_rate_limit_tracker"),
            patch.object(
                cb_module, "failure_status_codes", return_value=frozenset({429, 503})
            ),
            patch.object(
                cb_module, "rate_limit_status_codes", return_value=frozenset({429})
            ),
        ):
            record_cb_observation(
                _make_request(), status_code=429, service_name="payment"
            )

        service.record_failure.assert_called_once()
        service.record_rate_limit_response.assert_called_once_with("payment")

    def test_a_server_error_feeds_no_cascade(self):
        """Discriminator: the two sets are read independently, not as one."""
        service = _mock_cb()
        with (
            patch.object(cb_module, "_try_get_cb_service", return_value=service),
            patch.object(cb_module, "get_rate_limit_tracker"),
        ):
            record_cb_observation(
                _make_request(), status_code=503, service_name="payment"
            )

        service.record_rate_limit_response.assert_not_called()

    def test_every_observed_response_writes_one_request(self):
        """This helper is the only denominator writer on the frameworks it serves.

        Without the write the cascade rate would read 100% on Flask and FastAPI
        whatever the real traffic mix was, so the rate threshold could never
        discriminate.
        """
        service = _mock_cb()
        tracker = MagicMock(spec=RateLimitTracker)
        with (
            patch.object(cb_module, "_try_get_cb_service", return_value=service),
            patch.object(cb_module, "get_rate_limit_tracker", return_value=tracker),
        ):
            record_cb_observation(
                _make_request(), status_code=200, service_name="payment"
            )

        tracker.record_request.assert_called_once_with("payment")

    def test_an_unnamed_observation_writes_no_request(self):
        """The denominator write sits after the guards, not before them.

        A write above them would file every unnamed or disabled observation
        under a bucket no reader owns.
        """
        tracker = MagicMock(spec=RateLimitTracker)
        with (
            patch.object(cb_module, "_try_get_cb_service", return_value=_mock_cb()),
            patch.object(cb_module, "get_rate_limit_tracker", return_value=tracker),
        ):
            record_cb_observation(_make_request(), status_code=429)

        tracker.record_request.assert_not_called()

    def test_a_disabled_service_writes_no_request(self):
        """The same ordering, for the second guard."""
        tracker = MagicMock(spec=RateLimitTracker)
        with (
            patch.object(
                cb_module,
                "_try_get_cb_service",
                return_value=_mock_cb(is_enabled=False),
            ),
            patch.object(cb_module, "get_rate_limit_tracker", return_value=tracker),
        ):
            record_cb_observation(
                _make_request(), status_code=429, service_name="payment"
            )

        tracker.record_request.assert_not_called()

    def test_an_unavailable_cb_service_writes_no_request(self):
        """And for the third."""
        tracker = MagicMock(spec=RateLimitTracker)
        with (
            patch.object(cb_module, "_try_get_cb_service", return_value=None),
            patch.object(cb_module, "get_rate_limit_tracker", return_value=tracker),
        ):
            record_cb_observation(
                _make_request(), status_code=429, service_name="payment"
            )

        tracker.record_request.assert_not_called()

    def test_a_cascade_fault_is_swallowed(self):
        """Observation stays fire-and-forget on its newest half too."""
        service = _mock_cb()
        service.record_rate_limit_response.side_effect = RuntimeError("backend down")
        with (
            patch.object(cb_module, "_try_get_cb_service", return_value=service),
            patch.object(cb_module, "get_rate_limit_tracker"),
        ):
            record_cb_observation(
                _make_request(), status_code=429, service_name="payment"
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


# =============================================================================
# check_cb_open - Behavior (observe-only)
# =============================================================================


class TestCheckCbOpenObserveOnlyBehavior:
    """Dry-run reports the rejection this seam would send, and sends none.

    The 503 is the only intervention ``check_cb_open`` makes, so observe-only
    has to reach it here - the Django middleware gates its own preemptive
    branch and the breaker policy gates the outbound one, and a Flask/FastAPI
    deployment that kept rejecting would be the one surface where switching to
    shadow mode still refuses live traffic.
    """

    def test_an_open_cb_does_not_reject_under_dry_run(self):
        service = _mock_cb(state="open", opened_at=utc_now())
        with (
            patch.object(cb_module, "_try_get_cb_service", return_value=service),
            dry_run_active(),
        ):
            assert check_cb_open(_make_request(), service_name="payment") is None

    def test_a_half_open_cb_does_not_reject_under_dry_run(self):
        service = _mock_cb(state="half_open")
        with (
            patch.object(cb_module, "_try_get_cb_service", return_value=service),
            dry_run_active(),
        ):
            assert check_cb_open(_make_request(), service_name="payment") is None

    def test_the_same_open_cb_rejects_in_the_executing_mode(self):
        """Negative half: the mode is what changes the verdict, not the state."""
        service = _mock_cb(state="open", opened_at=utc_now())
        with patch.object(cb_module, "_try_get_cb_service", return_value=service):
            rejection = check_cb_open(_make_request(), service_name="payment")
        assert isinstance(rejection, ResponseContext)
        assert rejection.status_code == 503

    def test_the_suppressed_reject_is_logged_as_a_would_have(self):
        """The would-have record replaces the blocked-request WARNING."""
        service = _mock_cb(state="open", opened_at=utc_now())
        with capture_logs() as logs:
            with (
                patch.object(cb_module, "_try_get_cb_service", return_value=service),
                dry_run_active(),
            ):
                check_cb_open(_make_request(), service_name="payment")
        events = [entry["event"] for entry in logs]
        assert "execution_mode.intervention_suppressed" in events
        assert "middleware.request_blocked_cb_open" not in events
