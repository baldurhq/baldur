"""Unit tests for ``baldur.adapters.flask.middleware`` internals (PR4).

Scope:
    - ``_build_request_context``: Flask ``request`` proxy → ``RequestContext``
      translation inside a test request context.
    - ``_to_flask_response``: ``ResponseContext`` → Flask ``Response`` mapping
      (dict body → jsonify, bytes body → raw, string body, None body).

End-to-end request lifecycle (before_request reject → after_request headers)
is covered by the integration tests under
``tests/self_healing/integration/adapters/``.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask, g

from baldur.adapters.flask import middleware as flask_mw
from baldur.adapters.flask.middleware import (
    _FLASK_G_ENDPOINT,
    _FLASK_G_KEY,
    _FLASK_G_RED_RECORDED,
    _FLASK_G_START_TIME,
    _build_request_context,
    _to_flask_response,
    install_baldur_request_hooks,
)
from baldur.api.middleware import AdmissionDecision
from baldur.interfaces.web_framework import (
    ContentType,
    HttpMethod,
    RequestContext,
    ResponseContext,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def app() -> Flask:
    return Flask(__name__)


# =============================================================================
# _build_request_context — Contract
# =============================================================================


class TestBuildRequestContextContract:
    """Flask request proxy → RequestContext mapping is the adapter's contract."""

    def test_maps_method_and_path(self, app):
        with app.test_request_context("/api/pay/", method="POST"):
            ctx = _build_request_context()
        assert ctx.method == HttpMethod.POST
        assert ctx.path == "/api/pay/"

    def test_unknown_method_falls_back_to_get(self, app):
        """Defence against non-standard methods — never raise."""
        with app.test_request_context("/x", method="PROPFIND"):
            ctx = _build_request_context()
        assert ctx.method == HttpMethod.GET

    def test_extracts_client_ip_from_remote_addr(self, app):
        with app.test_request_context("/api", environ_base={"REMOTE_ADDR": "10.0.0.9"}):
            ctx = _build_request_context()
        assert ctx.client_ip == "10.0.0.9"

    def test_x_forwarded_for_overrides_remote_addr(self, app):
        """Proxied deployments: X-Forwarded-For wins over REMOTE_ADDR."""
        with app.test_request_context(
            "/api",
            environ_base={"REMOTE_ADDR": "10.0.0.9"},
            headers={"X-Forwarded-For": "198.51.100.1, 10.0.0.9"},
        ):
            ctx = _build_request_context()
        assert ctx.client_ip == "198.51.100.1"

    def test_parses_single_value_query_param(self, app):
        with app.test_request_context("/api?foo=bar"):
            ctx = _build_request_context()
        assert ctx.query_params["foo"] == "bar"

    def test_parses_multi_value_query_param(self, app):
        """Repeated keys collapse to a list."""
        with app.test_request_context("/api?t=a&t=b&t=c"):
            ctx = _build_request_context()
        assert ctx.query_params["t"] == ["a", "b", "c"]

    def test_extracts_common_tracing_headers(self, app):
        with app.test_request_context(
            "/api",
            headers={
                "User-Agent": "pytest/1",
                "X-Request-ID": "req-abc",
                "Content-Type": "application/json",
            },
        ):
            ctx = _build_request_context()
        assert ctx.user_agent == "pytest/1"
        assert ctx.request_id == "req-abc"
        assert ctx.content_type == "application/json"


# =============================================================================
# _to_flask_response — Contract
# =============================================================================


class TestToFlaskResponseContract:
    """ResponseContext → Flask Response mapping covers all 4 body shapes."""

    def test_dict_body_becomes_json_response(self, app):
        with app.app_context():
            ctx = ResponseContext(status_code=429, body={"error": "rate"})
            resp = _to_flask_response(ctx)
        assert resp.status_code == 429
        assert resp.get_json() == {"error": "rate"}

    def test_list_body_becomes_json_response(self, app):
        with app.app_context():
            ctx = ResponseContext(status_code=200, body=[1, 2, 3])
            resp = _to_flask_response(ctx)
        assert resp.get_json() == [1, 2, 3]

    def test_bytes_body_passes_through(self, app):
        with app.app_context():
            ctx = ResponseContext(
                status_code=200,
                body=b"\x00\x01binary",
                content_type=ContentType.TEXT.value,
            )
            resp = _to_flask_response(ctx)
        assert resp.get_data() == b"\x00\x01binary"

    def test_string_body_uses_raw_text(self, app):
        with app.app_context():
            ctx = ResponseContext(
                status_code=200,
                body="plain text",
                content_type=ContentType.TEXT.value,
            )
            resp = _to_flask_response(ctx)
        assert resp.get_data(as_text=True) == "plain text"

    def test_none_body_yields_empty_response(self, app):
        with app.app_context():
            ctx = ResponseContext(status_code=204, body=None)
            resp = _to_flask_response(ctx)
        assert resp.status_code == 204
        assert resp.get_data() == b""

    def test_custom_headers_propagate(self, app):
        with app.app_context():
            ctx = ResponseContext(
                status_code=503,
                body={"error": "down"},
                headers={"Retry-After": "30", "X-Baldur-Custom": "yes"},
            )
            resp = _to_flask_response(ctx)
        assert resp.headers["Retry-After"] == "30"
        assert resp.headers["X-Baldur-Custom"] == "yes"


# =============================================================================
# Admission pipeline (591) — Behavior
# =============================================================================


@contextmanager
def _patch_pipeline(*, admission, rate=None, cb=None, backpressure=None):
    """Patch the four reject helpers the Flask hooks compose.

    Returns the patched ``check_*`` mocks so tests can assert call counts.
    ``admission`` is the :class:`AdmissionDecision` ``check_admission`` returns;
    the others default to ``None`` (allow).
    """
    with (
        patch.object(flask_mw, "check_rate_limit", return_value=rate) as m_rate,
        patch.object(flask_mw, "check_admission", return_value=admission) as m_adm,
        patch.object(flask_mw, "check_cb_open", return_value=cb) as m_cb,
        patch.object(flask_mw, "check_backpressure", return_value=backpressure) as m_bp,
    ):
        yield SimpleNamespace(rate=m_rate, admission=m_adm, cb=m_cb, backpressure=m_bp)


class TestFlaskAdmissionPipeline:
    """Admission occupies the backpressure slot; release runs in teardown."""

    @pytest.fixture
    def client_app(self):
        """Flask app with Baldur hooks installed + a route that records calls."""
        app = Flask(__name__)
        state = SimpleNamespace(view_calls=0)

        @app.route("/ping")
        def _ping():
            state.view_calls += 1
            return {"ok": True}

        @app.route("/boom")
        def _boom():
            state.view_calls += 1
            raise RuntimeError("downstream blew up")

        install_baldur_request_hooks(app)
        return SimpleNamespace(client=app.test_client(), state=state)

    def test_active_admission_skips_backpressure(self, client_app):
        """When admission is active it is the single rate gate (shared bucket)."""
        with _patch_pipeline(admission=AdmissionDecision(active=True)) as mocks:
            response = client_app.client.get("/ping")

        assert response.status_code == 200
        mocks.admission.assert_called_once()
        mocks.backpressure.assert_not_called()

    def test_inactive_admission_runs_backpressure(self, client_app):
        """OSS no-op (active=False) -> the adapter falls back to check_backpressure."""
        with _patch_pipeline(admission=AdmissionDecision(active=False)) as mocks:
            response = client_app.client.get("/ping")

        assert response.status_code == 200
        mocks.backpressure.assert_called_once()

    def test_admission_rejection_returns_503_and_skips_cb(self, client_app):
        """An active rejection short-circuits before the CB pre-flight + view."""
        rejection = ResponseContext(status_code=503, body={"code": "ADMISSION"})
        with _patch_pipeline(
            admission=AdmissionDecision(active=True, rejection=rejection)
        ) as mocks:
            response = client_app.client.get("/ping")

        assert response.status_code == 503
        mocks.cb.assert_not_called()
        assert client_app.state.view_calls == 0

    def test_release_called_in_teardown_on_allow(self, client_app):
        """The acquired bulkhead slot is released after a normal request."""
        release = MagicMock()
        with _patch_pipeline(admission=AdmissionDecision(active=True, release=release)):
            response = client_app.client.get("/ping")

        assert response.status_code == 200
        release.assert_called_once_with()

    def test_release_called_when_cb_rejects_after_admission_acquired(self, client_app):
        """Release is stashed BEFORE the CB pre-flight, so a CB reject still frees it."""
        release = MagicMock()
        cb_rejection = ResponseContext(status_code=503, body={"code": "CB_OPEN"})
        with _patch_pipeline(
            admission=AdmissionDecision(active=True, release=release),
            cb=cb_rejection,
        ):
            response = client_app.client.get("/ping")

        assert response.status_code == 503
        release.assert_called_once_with()

    def test_release_called_on_downstream_exception(self, client_app):
        """teardown_request always runs, so the slot is freed even on a 500."""
        release = MagicMock()
        with _patch_pipeline(admission=AdmissionDecision(active=True, release=release)):
            response = client_app.client.get("/boom")

        assert response.status_code == 500
        release.assert_called_once_with()

    @pytest.mark.parametrize(
        "scenario",
        ["allow", "cb_reject", "exception"],
    )
    def test_admission_deadline_clear_invoked_in_teardown(self, client_app, scenario):
        """The degraded-deadline ContextVar clear fires on every teardown path."""
        cb = (
            ResponseContext(status_code=503, body={"code": "CB_OPEN"})
            if scenario == "cb_reject"
            else None
        )
        path = "/boom" if scenario == "exception" else "/ping"
        with (
            _patch_pipeline(admission=AdmissionDecision(active=True), cb=cb),
            patch.object(flask_mw, "_clear_deadline_if_enabled") as mock_clear,
        ):
            client_app.client.get(path)

        mock_clear.assert_called_once_with()

    def test_admission_deadline_clear_prevents_cross_request_leak(self, client_app):
        """Behavioral: a deadline set during the request is cleared by teardown.

        Guards the cross-request stale-deadline false-reject of a later
        ``critical`` request on a reused sync (WSGI gthread) worker.
        """
        from baldur.scaling.deadline_context import (
            clear_deadline,
            get_remaining_ms,
            set_deadline,
        )

        clear_deadline()
        set_deadline(5000)
        assert get_remaining_ms() is not None

        with _patch_pipeline(admission=AdmissionDecision(active=True)):
            client_app.client.get("/ping")

        assert get_remaining_ms() is None


# =============================================================================
# Deadline pipeline wiring (592) — Behavior
# =============================================================================


class TestFlaskMiddlewareDeadline:
    """``check_deadline`` (before admission) + ``record_rtt_sample`` (after)."""

    @pytest.fixture
    def client_app(self):
        app = Flask(__name__)
        state = SimpleNamespace(view_calls=0)

        @app.route("/ping")
        def _ping():
            state.view_calls += 1
            return {"ok": True}

        install_baldur_request_hooks(app)
        return SimpleNamespace(client=app.test_client(), state=state)

    def test_deadline_runs_after_rate_limit_before_admission(self, client_app):
        """Ordering invariant: rate-limit -> deadline -> admission."""
        calls: list[str] = []

        def _record(name, ret):
            def _f(*_a, **_k):
                calls.append(name)
                return ret

            return _f

        with (
            patch.object(
                flask_mw, "check_rate_limit", side_effect=_record("rate", None)
            ),
            patch.object(
                flask_mw, "check_deadline", side_effect=_record("deadline", None)
            ),
            patch.object(
                flask_mw,
                "check_admission",
                side_effect=_record("admission", AdmissionDecision(active=False)),
            ),
            patch.object(flask_mw, "check_backpressure", return_value=None),
            patch.object(flask_mw, "check_cb_open", return_value=None),
        ):
            client_app.client.get("/ping")

        assert calls == ["rate", "deadline", "admission"]

    def test_deadline_rejection_skips_admission_and_view(self, client_app):
        """A deadline fast-fail short-circuits before admission and the view."""
        rejection = ResponseContext(
            status_code=503,
            body={"code": "DEADLINE_FAST_FAIL"},
            headers={"X-Baldur-Deadline-Rejected": "true"},
        )
        with (
            patch.object(flask_mw, "check_rate_limit", return_value=None),
            patch.object(flask_mw, "check_deadline", return_value=rejection),
            patch.object(flask_mw, "check_admission") as m_adm,
        ):
            response = client_app.client.get("/ping")

        assert response.status_code == 503
        assert response.headers["X-Baldur-Deadline-Rejected"] == "true"
        m_adm.assert_not_called()
        assert client_app.state.view_calls == 0

    def test_rtt_sample_fires_on_allow_with_tier(self, client_app):
        """A non-rejected request with a classified tier feeds the RTT sampler."""
        with (
            _patch_pipeline(
                admission=AdmissionDecision(active=True, tier_id="standard")
            ),
            patch.object(flask_mw, "check_deadline", return_value=None),
            patch.object(flask_mw, "record_rtt_sample") as m_rtt,
        ):
            response = client_app.client.get("/ping")

        assert response.status_code == 200
        m_rtt.assert_called_once()
        tier_id, status_code, elapsed_ms = m_rtt.call_args[0]
        assert tier_id == "standard"
        assert status_code == 200
        assert isinstance(elapsed_ms, float)
        assert elapsed_ms >= 0

    def test_rtt_sample_skipped_on_rejection(self, client_app):
        """A rejected response (REJECTED flag set) skips the after_request RTT."""
        rejection = ResponseContext(status_code=503, body={"code": "ADMISSION"})
        with (
            _patch_pipeline(
                admission=AdmissionDecision(
                    active=True, tier_id="standard", rejection=rejection
                )
            ),
            patch.object(flask_mw, "check_deadline", return_value=None),
            patch.object(flask_mw, "record_rtt_sample") as m_rtt,
        ):
            response = client_app.client.get("/ping")

        assert response.status_code == 503
        m_rtt.assert_not_called()

    def test_rtt_sample_skipped_when_tier_none(self, client_app):
        """OSS no-op (active=False, tier_id None) -> no RTT sample."""
        with (
            _patch_pipeline(admission=AdmissionDecision(active=False)),
            patch.object(flask_mw, "check_deadline", return_value=None),
            patch.object(flask_mw, "record_rtt_sample") as m_rtt,
        ):
            client_app.client.get("/ping")

        m_rtt.assert_not_called()

    def test_inbound_deadline_header_cleared_after_request(self, client_app):
        """The real check_deadline sets a deadline from the header; teardown clears it.

        Guards the cross-request leak on a reused sync worker via the actual
        inbound-header path (not a manual ``set_deadline``).
        """
        from baldur.scaling.deadline_context import clear_deadline, get_remaining_ms

        clear_deadline()
        # check_deadline is NOT patched here -> the real helper reads the header.
        with _patch_pipeline(admission=AdmissionDecision(active=False)):
            client_app.client.get("/ping", headers={"X-Deadline-Remaining": "5000ms"})

        assert get_remaining_ms() is None


# =============================================================================
# HTTP RED metrics (649) — Behavior
# =============================================================================


class TestFlaskRedMetrics:
    """HTTP RED (Rate/Errors/Duration) recording at Django parity (649).

    ``record_http_red`` is imported into the Flask middleware module namespace,
    so it is patched as ``flask_mw.record_http_red`` (the same pattern the RTT /
    pipeline tests use for the framework-free helpers). The native-route label
    runs against a real ``Flask`` app so ``request.url_rule`` populates.
    """

    @pytest.fixture
    def client(self):
        app = Flask(__name__)

        @app.route("/users/<int:uid>")
        def _user(uid):
            return {"uid": uid}

        @app.route("/ping")
        def _ping():
            return {"ok": True}

        install_baldur_request_hooks(app)
        return app.test_client()

    def test_after_request_records_red_on_success(self, client):
        """SC1: a downstream 2xx drives record_http_red once with the elapsed seconds."""
        with patch.object(flask_mw, "record_http_red") as m_red:
            response = client.get("/ping")

        assert response.status_code == 200
        m_red.assert_called_once()
        method, endpoint, status_code, duration = m_red.call_args.args
        assert method == "GET"
        assert endpoint == "/ping"
        assert status_code == 200
        assert isinstance(duration, float)
        assert duration >= 0
        # The success path passes no explicit error_type (helper derives HTTP_<code>).
        assert "error_type" not in m_red.call_args.kwargs

    def test_native_route_collapses_concrete_paths_to_one_label(self, client):
        """SC3: two concrete paths on one route share a single bounded label."""
        with patch.object(flask_mw, "record_http_red") as m_red:
            client.get("/users/123")
            client.get("/users/456")

        endpoints = [call.args[1] for call in m_red.call_args_list]
        assert endpoints == ["/users/<int:uid>", "/users/<int:uid>"]

    def test_unrouted_path_records_unmatched_route(self, client):
        """SC3: a 404/unrouted path collapses to the single UNMATCHED_ROUTE label."""
        with patch.object(flask_mw, "record_http_red") as m_red:
            response = client.get("/no/such/path")

        assert response.status_code == 404
        m_red.assert_called_once()
        _, endpoint, status_code, _ = m_red.call_args.args
        assert endpoint == "UNMATCHED_ROUTE"
        assert status_code == 404

    def test_reject_response_skips_red_recording(self, client):
        """SC6: a middleware-generated reject (429) is excluded from RED (D4)."""
        rejection = ResponseContext(status_code=429, body={"code": "RATE_LIMIT"})
        with (
            patch.object(flask_mw, "check_rate_limit", return_value=rejection),
            patch.object(flask_mw, "record_http_red") as m_red,
        ):
            response = client.get("/ping")

        assert response.status_code == 429
        m_red.assert_not_called()

    # --- D5: unhandled-exception 500 via the teardown closure -----------------
    #
    # In non-propagate mode (the production default) Flask's handle_exception
    # routes the synthesized 500 through finalize_request, so _after_request
    # records it and the guard makes the teardown a no-op. The teardown's own
    # 500 path only fires when _after_request could not run (PROPAGATE_EXCEPTIONS
    # / a failed after_request). Exercising the registered teardown closure
    # directly with a controlled exception is the deterministic way to assert
    # that path and its catch-scope / guard discipline.

    @staticmethod
    def _invoke_teardown(exc, *, already_recorded=False):
        """Run the registered teardown closure with ``exc`` inside a request
        context; return the patched ``record_http_red`` mock for assertions."""
        app = Flask(__name__)
        install_baldur_request_hooks(app)
        teardown = app.teardown_request_funcs[None][-1]
        request_ctx = RequestContext(method=HttpMethod.POST, path="/pay", headers={})

        with app.test_request_context("/pay", method="POST"):
            setattr(g, _FLASK_G_START_TIME, time.perf_counter())
            setattr(g, _FLASK_G_KEY, request_ctx)
            setattr(g, _FLASK_G_ENDPOINT, "/pay")
            if already_recorded:
                setattr(g, _FLASK_G_RED_RECORDED, True)
            with patch.object(flask_mw, "record_http_red") as m_red:
                teardown(exc)
        return m_red

    def test_teardown_records_500_on_unhandled_exception(self):
        """SC7/D5: an unhandled non-HTTP exception records a 500 with the exc name."""
        m_red = self._invoke_teardown(RuntimeError("boom"))

        m_red.assert_called_once()
        method, endpoint, status_code, _ = m_red.call_args.args
        assert method == "POST"
        assert endpoint == "/pay"
        assert status_code == 500
        assert m_red.call_args.kwargs["error_type"] == "RuntimeError"

    def test_teardown_skips_when_after_request_already_recorded(self):
        """Idempotency: the RED guard prevents a double-record after _after_request."""
        m_red = self._invoke_teardown(RuntimeError("boom"), already_recorded=True)
        m_red.assert_not_called()

    @pytest.mark.parametrize("exc", [SystemExit(), KeyboardInterrupt()])
    def test_teardown_baseexception_records_nothing(self, exc):
        """Boundary: a BaseException (shutdown signal) records no spurious 500.

        The teardown catch scope is ``Exception`` (the WSGI analog of the FastAPI
        discipline), so SystemExit / KeyboardInterrupt fall through with no record.
        """
        m_red = self._invoke_teardown(exc)
        m_red.assert_not_called()

    def test_teardown_no_exception_records_nothing(self):
        """A clean teardown (exc is None) records nothing — _after_request owns it."""
        m_red = self._invoke_teardown(None)
        m_red.assert_not_called()
