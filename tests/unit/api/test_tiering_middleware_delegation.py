"""Unit tests for ``TieringMiddleware``'s delegation to the shared shed helper.

After the extraction the Django class owns only the framework edges: the install
gate, the OPTIONS short-circuit, the ``RequestContext`` build, and the
``ResponseContext`` -> ``JsonResponse`` conversion. The decision itself —
the PRO gate, the two level reads, classification, the merge and the
probabilistic draw — lives in ``baldur.api.middleware.emergency_shedding`` and
is covered by ``tests/unit/api/middleware/test_emergency_shedding_helpers.py``.

These cases pin the edges, plus the behaviour change the extraction shipped: an
install with no emergency-manager slot used to raise ``RuntimeError`` on every
non-OPTIONS request and log the traceback at ERROR. It is now silent.
"""

from __future__ import annotations

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")

import django

django.setup()

import inspect
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.api.django.tiering import middleware as tiering_mw
from baldur.api.django.tiering.middleware import TieringMiddleware
from baldur.api.middleware import emergency_shedding as shedding
from baldur.interfaces.web_framework import HttpMethod, RequestContext, ResponseContext

# =============================================================================
# Doubles
# =============================================================================


class _Downstream:
    """Django's ``get_response`` — records the requests that reach the view."""

    def __init__(self, response: object = "downstream-response") -> None:
        self.response = response
        self.calls: list[object] = []

    def __call__(self, request: object) -> object:
        self.calls.append(request)
        return self.response


def _django_request(
    *,
    method: str = "GET",
    path: str = "/api/orders/",
    headers: dict[str, str] | None = None,
    meta: dict[str, str] | None = None,
    user: object | None = None,
) -> SimpleNamespace:
    """A Django ``HttpRequest`` stand-in with the attributes the wrapper reads."""
    return SimpleNamespace(
        method=method,
        path=path,
        headers=headers if headers is not None else {},
        META=meta if meta is not None else {"REMOTE_ADDR": "203.0.113.7"},
        user=user if user is not None else SimpleNamespace(is_authenticated=False),
    )


def _middleware(downstream: _Downstream) -> TieringMiddleware:
    middleware = TieringMiddleware(downstream)
    middleware._enabled = True
    return middleware


def _shed_rejection() -> ResponseContext:
    return ResponseContext(
        status_code=503,
        body={"code": "LOAD_SHEDDING", "tier": "non_essential", "retry_after": 30},
        headers={"Retry-After": "30"},
    )


# =============================================================================
# Delegation — Behavior
# =============================================================================


class TestTieringMiddlewareDelegationBehavior:
    """The Django wrapper builds the context, delegates, converts the answer."""

    def test_allow_forwards_the_request_downstream(self):
        downstream = _Downstream()
        with patch.object(tiering_mw, "check_emergency_shedding", return_value=None):
            response = _middleware(downstream)(_django_request())

        assert response == downstream.response
        assert len(downstream.calls) == 1

    def test_helper_receives_a_request_context_with_the_django_fields(self):
        downstream = _Downstream()
        request = _django_request(
            method="POST",
            path="/api/baldur/config/test",
            headers={"X-Request-ID": "req-7"},
            meta={"HTTP_X_FORWARDED_FOR": "198.51.100.1", "REMOTE_ADDR": "10.0.0.9"},
        )
        with patch.object(
            tiering_mw, "check_emergency_shedding", return_value=None
        ) as mock_check:
            _middleware(downstream)(request)

        ctx = mock_check.call_args.args[0]
        assert isinstance(ctx, RequestContext)
        assert ctx.method == HttpMethod.POST
        assert ctx.path == "/api/baldur/config/test"
        assert ctx.headers["X-Request-ID"] == "req-7"
        assert ctx.client_ip == "198.51.100.1"
        assert ctx.is_authenticated is False
        assert ctx.user is None

    def test_authenticated_user_reaches_the_context(self):
        downstream = _Downstream()
        user = SimpleNamespace(is_authenticated=True, id=42)
        with patch.object(
            tiering_mw, "check_emergency_shedding", return_value=None
        ) as mock_check:
            _middleware(downstream)(_django_request(user=user))

        ctx = mock_check.call_args.args[0]
        assert ctx.is_authenticated is True
        assert ctx.user is user

    def test_unknown_http_method_falls_back_to_get(self):
        """Defence against non-standard verbs — the ctx build never raises."""
        downstream = _Downstream()
        with patch.object(
            tiering_mw, "check_emergency_shedding", return_value=None
        ) as mock_check:
            _middleware(downstream)(_django_request(method="PROPFIND"))

        assert mock_check.call_args.args[0].method == HttpMethod.GET

    def test_rejection_is_converted_to_a_503_json_response(self):
        downstream = _Downstream()
        with patch.object(
            tiering_mw, "check_emergency_shedding", return_value=_shed_rejection()
        ):
            response = _middleware(downstream)(_django_request())

        assert response.status_code == 503
        assert response["Content-Type"] == "application/json"
        assert downstream.calls == []

    def test_rejection_headers_are_copied_onto_the_django_response(self):
        """``Retry-After`` is the operator-visible half of the shed contract."""
        downstream = _Downstream()
        with patch.object(
            tiering_mw, "check_emergency_shedding", return_value=_shed_rejection()
        ):
            response = _middleware(downstream)(_django_request())

        assert response["Retry-After"] == "30"

    def test_rejection_body_survives_the_conversion(self):
        import json

        downstream = _Downstream()
        with patch.object(
            tiering_mw, "check_emergency_shedding", return_value=_shed_rejection()
        ):
            response = _middleware(downstream)(_django_request())

        assert json.loads(response.content)["code"] == "LOAD_SHEDDING"

    def test_options_short_circuits_before_the_helper(self):
        """CORS preflight is answered without building a context at all."""
        downstream = _Downstream()
        with patch.object(tiering_mw, "check_emergency_shedding") as mock_check:
            response = _middleware(downstream)(_django_request(method="OPTIONS"))

        assert response == downstream.response
        mock_check.assert_not_called()

    def test_disabled_install_skips_the_helper(self):
        """``BALDUR_TIERING_MIDDLEWARE_ENABLED=False`` is the Django install switch."""
        downstream = _Downstream()
        middleware = _middleware(downstream)
        middleware._enabled = False

        with patch.object(tiering_mw, "check_emergency_shedding") as mock_check:
            response = middleware(_django_request())

        assert response == downstream.response
        mock_check.assert_not_called()

    def test_context_build_failure_allows_the_request_and_logs_once(self):
        """The outer guard covers the ctx build and the conversion only."""
        downstream = _Downstream()
        request = _django_request()
        del request.path  # the ctx build touches it first

        with capture_logs() as logs:
            response = _middleware(downstream)(request)

        assert response == downstream.response
        errors = [
            e
            for e in logs
            if e.get("event") == "tiering_middleware.error_allowing_request"
        ]
        assert len(errors) == 1


# =============================================================================
# PRO-absent install — Behavior (negative)
# =============================================================================


class TestTieringMiddlewareProAbsentBehavior:
    """An install without the emergency service is silent, not noisy.

    Before the extraction the middleware raised ``RuntimeError`` whenever the
    ``emergency_manager`` slot was empty, and the outer handler logged the
    traceback at ERROR — one record per non-OPTIONS request on every OSS or
    unentitled Django deployment that called ``configure_baldur``.
    """

    def test_empty_slot_allows_the_request_with_no_warning_or_error_record(self):
        downstream = _Downstream()
        with (
            patch.object(shedding, "_emergency_manager", return_value=None),
            capture_logs() as logs,
        ):
            response = _middleware(downstream)(_django_request())

        assert response == downstream.response
        noisy = [
            e for e in logs if e.get("log_level") in {"warning", "error", "critical"}
        ]
        assert noisy == []

    def test_empty_slot_stays_silent_across_repeated_requests(self):
        """The old failure fired per request, so one request could not show it."""
        downstream = _Downstream()
        middleware = _middleware(downstream)

        with (
            patch.object(shedding, "_emergency_manager", return_value=None),
            capture_logs() as logs,
        ):
            for _ in range(3):
                middleware(_django_request())

        assert len(downstream.calls) == 3
        assert logs == []


# =============================================================================
# Extraction residue — Contract
# =============================================================================


class TestTieringMiddlewareExtractionContract:
    """Nothing of the decision core stayed behind in the Django module."""

    @pytest.mark.parametrize(
        "residue",
        [
            "tiering_middleware.load_shedding",
            "baldur_tiering_load_shedding_total",
            "random.Random(",
            "_should_allow_request",
            "_create_load_shedding_response",
            "_record_load_shedding_metrics",
        ],
    )
    def test_decision_core_symbol_is_gone_from_the_django_module(self, residue):
        source = inspect.getsource(tiering_mw)

        assert residue not in source

    def test_middleware_calls_the_shared_helper(self):
        """The delegation is what keeps Django and the other adapters in step."""
        assert "check_emergency_shedding" in inspect.getsource(tiering_mw)
