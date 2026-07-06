"""Unit tests for ``baldur.adapters.fastapi.middleware`` internals (PR4).

Scope:
    - ``_build_request_context``: ASGI scope → ``RequestContext`` translation
      (method, path, headers, client IP resolution, query-string parsing).
    - ``_send_response``: ``ResponseContext`` → ASGI response messages
      (dict/bytes/str body encoding, content-length computation, header
      flattening).

End-to-end request lifecycle (reject → forward → observe) is covered by
the integration tests under ``tests/self_healing/integration/adapters/``.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.fastapi import middleware as fastapi_mw
from baldur.adapters.fastapi.middleware import (
    BaldurMiddleware,
    _build_request_context,
    _extract_fastapi_endpoint,
    _send_response,
)
from baldur.api.middleware import AdmissionDecision
from baldur.interfaces.web_framework import (
    ContentType,
    HttpMethod,
    ResponseContext,
)

# =============================================================================
# _build_request_context — Contract
# =============================================================================


def _scope(
    *,
    method: str = "GET",
    path: str = "/api/pay/",
    headers: list[tuple[bytes, bytes]] | None = None,
    query_string: bytes = b"",
    client: tuple[str, int] | None = ("203.0.113.5", 54321),
) -> dict:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "query_string": query_string,
        "client": client,
    }


class TestBuildRequestContextContract:
    """ASGI scope → RequestContext field mapping is the adapter's contract."""

    def test_maps_method_and_path(self):
        ctx = _build_request_context(_scope(method="POST", path="/api/pay/"))
        assert ctx.method == HttpMethod.POST
        assert ctx.path == "/api/pay/"

    def test_unknown_method_falls_back_to_get(self):
        """Defence against malformed ASGI — never raise during ctx build."""
        ctx = _build_request_context(_scope(method="PROPFIND"))
        assert ctx.method == HttpMethod.GET

    def test_preserves_asgi_lowercase_header_keys(self):
        """ASGI spec mandates lowercase header keys; adapter passes them through."""
        ctx = _build_request_context(
            _scope(headers=[(b"content-type", b"application/json")])
        )
        assert ctx.headers["content-type"] == "application/json"

    def test_extracts_client_ip_from_scope(self):
        ctx = _build_request_context(_scope(client=("10.0.0.9", 80)))
        assert ctx.client_ip == "10.0.0.9"

    def test_x_forwarded_for_overrides_scope_client(self):
        """Proxied deployments: X-Forwarded-For wins over scope.client."""
        ctx = _build_request_context(
            _scope(
                client=("10.0.0.9", 80),
                headers=[(b"x-forwarded-for", b"198.51.100.1, 10.0.0.9")],
            )
        )
        assert ctx.client_ip == "198.51.100.1"

    def test_missing_client_sets_ip_to_none(self):
        ctx = _build_request_context(_scope(client=None))
        assert ctx.client_ip is None

    def test_parses_single_value_query_param(self):
        ctx = _build_request_context(_scope(query_string=b"foo=bar"))
        assert ctx.query_params["foo"] == "bar"

    def test_parses_multi_value_query_param(self):
        """Repeated keys collapse to a list."""
        ctx = _build_request_context(_scope(query_string=b"t=a&t=b&t=c"))
        assert ctx.query_params["t"] == ["a", "b", "c"]

    def test_empty_query_string_yields_empty_dict(self):
        ctx = _build_request_context(_scope(query_string=b""))
        assert ctx.query_params == {}

    def test_extracts_common_tracing_headers(self):
        ctx = _build_request_context(
            _scope(
                headers=[
                    (b"user-agent", b"pytest/1"),
                    (b"x-request-id", b"req-abc"),
                    (b"content-type", b"application/json"),
                ]
            )
        )
        assert ctx.user_agent == "pytest/1"
        assert ctx.request_id == "req-abc"
        assert ctx.content_type == "application/json"


# =============================================================================
# _send_response — Contract
# =============================================================================


class _AsgiRecorder:
    """Collect ASGI send() calls so tests can inspect emitted messages."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)


def _run(coro):
    return asyncio.run(coro)


class TestSendResponseContract:
    """ResponseContext → ASGI emission: two messages, start then body."""

    def test_emits_start_and_body_messages(self):
        recorder = _AsgiRecorder()
        _run(
            _send_response(
                recorder,
                ResponseContext(status_code=200, body={"ok": True}),
            )
        )
        assert len(recorder.messages) == 2
        assert recorder.messages[0]["type"] == "http.response.start"
        assert recorder.messages[1]["type"] == "http.response.body"

    def test_preserves_status_code(self):
        recorder = _AsgiRecorder()
        _run(
            _send_response(
                recorder,
                ResponseContext(status_code=429, body={"error": "rate"}),
            )
        )
        assert recorder.messages[0]["status"] == 429

    def test_json_encodes_dict_body(self):
        recorder = _AsgiRecorder()
        _run(
            _send_response(
                recorder,
                ResponseContext(status_code=200, body={"ok": True, "n": 5}),
            )
        )
        body_bytes = recorder.messages[1]["body"]
        assert json.loads(body_bytes) == {"ok": True, "n": 5}

    def test_passes_bytes_body_verbatim(self):
        recorder = _AsgiRecorder()
        _run(
            _send_response(
                recorder,
                ResponseContext(
                    status_code=200,
                    body=b"\x00\x01binary",
                    content_type=ContentType.TEXT.value,
                ),
            )
        )
        assert recorder.messages[1]["body"] == b"\x00\x01binary"

    def test_encodes_string_body_as_utf8(self):
        recorder = _AsgiRecorder()
        _run(
            _send_response(
                recorder,
                ResponseContext(
                    status_code=200,
                    body="héllo",
                    content_type=ContentType.TEXT.value,
                ),
            )
        )
        assert recorder.messages[1]["body"] == "héllo".encode()

    def test_none_body_emits_empty_bytes(self):
        recorder = _AsgiRecorder()
        _run(_send_response(recorder, ResponseContext(status_code=204, body=None)))
        assert recorder.messages[1]["body"] == b""

    def test_computes_content_length_header(self):
        recorder = _AsgiRecorder()
        _run(
            _send_response(
                recorder,
                ResponseContext(status_code=200, body={"hello": "world"}),
            )
        )
        headers = {
            k.decode("latin-1"): v.decode("latin-1")
            for k, v in recorder.messages[0]["headers"]
        }
        assert headers["content-length"] == str(
            len(json.dumps({"hello": "world"}).encode("utf-8"))
        )

    def test_preserves_custom_headers(self):
        recorder = _AsgiRecorder()
        _run(
            _send_response(
                recorder,
                ResponseContext(
                    status_code=503,
                    body={"error": "down"},
                    headers={"Retry-After": "30", "X-Baldur-Custom": "yes"},
                ),
            )
        )
        headers = {
            k.decode("latin-1"): v.decode("latin-1")
            for k, v in recorder.messages[0]["headers"]
        }
        assert headers["Retry-After"] == "30"
        assert headers["X-Baldur-Custom"] == "yes"


class TestSendResponseContentType:
    """``response.content_type`` propagates to ASGI headers for every body shape."""

    @staticmethod
    def _emit_headers(body: object, content_type: str) -> dict[str, str]:
        recorder = _AsgiRecorder()
        _run(
            _send_response(
                recorder,
                ResponseContext(status_code=200, body=body, content_type=content_type),
            )
        )
        return {
            k.decode("latin-1"): v.decode("latin-1")
            for k, v in recorder.messages[0]["headers"]
        }

    def test_dict_body_sets_content_type(self):
        headers = self._emit_headers({"ok": True}, ContentType.JSON.value)
        assert headers["content-type"] == ContentType.JSON.value

    def test_bytes_body_sets_content_type(self):
        """Regression: bytes body path used to drop ``response.content_type``."""
        headers = self._emit_headers(b"\x01\x02", ContentType.TEXT.value)
        assert headers["content-type"] == ContentType.TEXT.value

    def test_string_body_sets_content_type(self):
        """Regression: str body path used to drop ``response.content_type``."""
        headers = self._emit_headers("hello", ContentType.TEXT.value)
        assert headers["content-type"] == ContentType.TEXT.value

    def test_none_body_sets_content_type(self):
        """None-body 204 responses still surface the declared content_type."""
        headers = self._emit_headers(None, ContentType.JSON.value)
        assert headers["content-type"] == ContentType.JSON.value

    def test_explicit_content_type_header_wins_over_field(self):
        """Header ``content-type`` (if set) takes precedence over the field."""
        recorder = _AsgiRecorder()
        _run(
            _send_response(
                recorder,
                ResponseContext(
                    status_code=200,
                    body={"ok": True},
                    headers={"content-type": "application/vnd.custom+json"},
                    content_type=ContentType.JSON.value,
                ),
            )
        )
        headers = {
            k.decode("latin-1"): v.decode("latin-1")
            for k, v in recorder.messages[0]["headers"]
        }
        assert headers["content-type"] == "application/vnd.custom+json"


# =============================================================================
# Admission pipeline (591) — Behavior
# =============================================================================


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _ok_app(scope, receive, send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _err_app(scope, receive, send) -> None:
    await send({"type": "http.response.start", "status": 500, "headers": []})
    await send({"type": "http.response.body", "body": b"err"})


async def _raising_app(scope, receive, send) -> None:
    raise RuntimeError("downstream blew up")


class _SpyApp:
    """Downstream ASGI app that records whether it was reached."""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        await _ok_app(scope, receive, send)


@contextmanager
def _patch_pipeline(*, admission, rate=None, cb=None, backpressure=None):
    """Patch the four reject helpers the ASGI middleware composes."""
    with (
        patch.object(fastapi_mw, "check_rate_limit", return_value=rate) as m_rate,
        patch.object(fastapi_mw, "check_admission", return_value=admission) as m_adm,
        patch.object(fastapi_mw, "check_cb_open", return_value=cb) as m_cb,
        patch.object(
            fastapi_mw, "check_backpressure", return_value=backpressure
        ) as m_bp,
    ):
        yield SimpleNamespace(rate=m_rate, admission=m_adm, cb=m_cb, backpressure=m_bp)


class TestFastapiAdmissionPipeline:
    """The release closure fires in the ``finally`` on every exit path."""

    def test_release_called_on_allow(self):
        release = MagicMock()
        recorder = _AsgiRecorder()
        with _patch_pipeline(admission=AdmissionDecision(active=True, release=release)):
            _run(BaldurMiddleware(_ok_app)(_scope(), _receive, recorder))

        release.assert_called_once_with()
        assert recorder.messages[0]["status"] == 200

    def test_release_called_on_admission_acquired_then_cb_rejected(self):
        """The CB pre-flight lives inside the release try, so its early reject frees the slot."""
        release = MagicMock()
        spy = _SpyApp()
        cb_rejection = ResponseContext(status_code=503, body={"code": "CB_OPEN"})
        recorder = _AsgiRecorder()
        with _patch_pipeline(
            admission=AdmissionDecision(active=True, release=release),
            cb=cb_rejection,
        ):
            _run(BaldurMiddleware(spy)(_scope(), _receive, recorder))

        release.assert_called_once_with()
        assert spy.called is False
        assert recorder.messages[0]["status"] == 503

    def test_release_called_on_downstream_5xx(self):
        release = MagicMock()
        recorder = _AsgiRecorder()
        with _patch_pipeline(admission=AdmissionDecision(active=True, release=release)):
            _run(BaldurMiddleware(_err_app)(_scope(), _receive, recorder))

        release.assert_called_once_with()

    def test_release_called_on_downstream_exception(self):
        release = MagicMock()
        recorder = _AsgiRecorder()
        with _patch_pipeline(admission=AdmissionDecision(active=True, release=release)):
            with pytest.raises(RuntimeError):
                _run(BaldurMiddleware(_raising_app)(_scope(), _receive, recorder))

        release.assert_called_once_with()

    def test_active_admission_skips_backpressure(self):
        recorder = _AsgiRecorder()
        with _patch_pipeline(admission=AdmissionDecision(active=True)) as mocks:
            _run(BaldurMiddleware(_ok_app)(_scope(), _receive, recorder))

        mocks.admission.assert_called_once()
        mocks.backpressure.assert_not_called()

    def test_inactive_admission_runs_backpressure(self):
        recorder = _AsgiRecorder()
        with _patch_pipeline(admission=AdmissionDecision(active=False)) as mocks:
            _run(BaldurMiddleware(_ok_app)(_scope(), _receive, recorder))

        mocks.backpressure.assert_called_once()

    def test_active_rejection_returns_503_and_skips_downstream(self):
        rejection = ResponseContext(status_code=503, body={"code": "ADMISSION"})
        spy = _SpyApp()
        recorder = _AsgiRecorder()
        with _patch_pipeline(
            admission=AdmissionDecision(active=True, rejection=rejection)
        ):
            _run(BaldurMiddleware(spy)(_scope(), _receive, recorder))

        assert recorder.messages[0]["status"] == 503
        assert spy.called is False

    @pytest.mark.parametrize("scenario", ["allow", "cb_reject", "exception"])
    def test_admission_deadline_clear_invoked_in_finally(self, scenario):
        """The degraded-deadline clear fires on every exit path (belt-and-suspenders)."""
        cb = (
            ResponseContext(status_code=503, body={"code": "CB_OPEN"})
            if scenario == "cb_reject"
            else None
        )
        app = _raising_app if scenario == "exception" else _ok_app
        recorder = _AsgiRecorder()
        with (
            _patch_pipeline(admission=AdmissionDecision(active=True), cb=cb),
            patch.object(fastapi_mw, "_clear_deadline_if_enabled") as mock_clear,
        ):
            if scenario == "exception":
                with pytest.raises(RuntimeError):
                    _run(BaldurMiddleware(app)(_scope(), _receive, recorder))
            else:
                _run(BaldurMiddleware(app)(_scope(), _receive, recorder))

        mock_clear.assert_called_once_with()


# =============================================================================
# init_fastapi (591) — Behavior
# =============================================================================


class TestInitFastapiAutowiring:
    """``init_fastapi`` is the app-construction-time auto-wiring seam (mirrors init_flask)."""

    def test_init_fastapi_adds_baldur_middleware(self):
        from fastapi import FastAPI

        from baldur.adapters.fastapi.bootstrap import init_fastapi

        app = FastAPI()
        with patch("baldur.init") as mock_init:
            init_fastapi(app)

        assert any(m.cls is BaldurMiddleware for m in app.user_middleware)
        mock_init.assert_called_once()

    def test_init_fastapi_passes_service_name_to_middleware(self):
        from fastapi import FastAPI

        from baldur.adapters.fastapi.bootstrap import init_fastapi

        app = FastAPI()
        with patch("baldur.init"):
            init_fastapi(app, service_name="payments")

        entry = next(m for m in app.user_middleware if m.cls is BaldurMiddleware)
        assert entry.kwargs["service_name"] == "payments"


# =============================================================================
# Deadline pipeline wiring (592) — Behavior
# =============================================================================


class TestFastapiMiddlewareDeadline:
    """``check_deadline`` (before admission) + ``record_rtt_sample`` (finally)."""

    def test_deadline_runs_after_rate_limit_before_admission(self):
        """Ordering invariant: rate-limit -> deadline -> admission."""
        calls: list[str] = []

        def _record(name, ret):
            def _f(*_a, **_k):
                calls.append(name)
                return ret

            return _f

        recorder = _AsgiRecorder()
        with (
            patch.object(
                fastapi_mw, "check_rate_limit", side_effect=_record("rate", None)
            ),
            patch.object(
                fastapi_mw, "check_deadline", side_effect=_record("deadline", None)
            ),
            patch.object(
                fastapi_mw,
                "check_admission",
                side_effect=_record("admission", AdmissionDecision(active=False)),
            ),
            patch.object(fastapi_mw, "check_backpressure", return_value=None),
            patch.object(fastapi_mw, "check_cb_open", return_value=None),
        ):
            _run(BaldurMiddleware(_ok_app)(_scope(), _receive, recorder))

        assert calls == ["rate", "deadline", "admission"]

    def test_deadline_rejection_skips_admission_and_downstream(self):
        """A deadline fast-fail short-circuits before admission and the app."""
        rejection = ResponseContext(
            status_code=503,
            body={"code": "DEADLINE_FAST_FAIL"},
            headers={"X-Baldur-Deadline-Rejected": "true"},
        )
        spy = _SpyApp()
        recorder = _AsgiRecorder()
        with (
            patch.object(fastapi_mw, "check_rate_limit", return_value=None),
            patch.object(fastapi_mw, "check_deadline", return_value=rejection),
            patch.object(fastapi_mw, "check_admission") as m_adm,
        ):
            _run(BaldurMiddleware(spy)(_scope(), _receive, recorder))

        assert recorder.messages[0]["status"] == 503
        headers = {
            k.decode("latin-1"): v.decode("latin-1")
            for k, v in recorder.messages[0]["headers"]
        }
        assert headers["X-Baldur-Deadline-Rejected"] == "true"
        m_adm.assert_not_called()
        assert spy.called is False

    def test_rtt_sample_fires_on_allow_with_tier(self):
        """A started request with a classified tier feeds the RTT sampler."""
        recorder = _AsgiRecorder()
        with (
            _patch_pipeline(
                admission=AdmissionDecision(active=True, tier_id="standard")
            ),
            patch.object(fastapi_mw, "check_deadline", return_value=None),
            patch.object(fastapi_mw, "record_rtt_sample") as m_rtt,
        ):
            _run(BaldurMiddleware(_ok_app)(_scope(), _receive, recorder))

        m_rtt.assert_called_once()
        tier_id, status_code, elapsed_ms = m_rtt.call_args[0]
        assert tier_id == "standard"
        assert status_code == 200
        assert isinstance(elapsed_ms, float)
        assert elapsed_ms >= 0

    def test_rtt_sample_skipped_when_request_not_started(self):
        """An admission reject (downstream never ran) -> started False -> no RTT."""
        rejection = ResponseContext(status_code=503, body={"code": "ADMISSION"})
        spy = _SpyApp()
        recorder = _AsgiRecorder()
        with (
            _patch_pipeline(
                admission=AdmissionDecision(
                    active=True, tier_id="standard", rejection=rejection
                )
            ),
            patch.object(fastapi_mw, "check_deadline", return_value=None),
            patch.object(fastapi_mw, "record_rtt_sample") as m_rtt,
        ):
            _run(BaldurMiddleware(spy)(_scope(), _receive, recorder))

        m_rtt.assert_not_called()
        assert spy.called is False

    def test_rtt_sample_skipped_when_tier_none(self):
        """OSS no-op (active=False, tier_id None) -> no RTT even though started."""
        recorder = _AsgiRecorder()
        with (
            _patch_pipeline(admission=AdmissionDecision(active=False)),
            patch.object(fastapi_mw, "check_deadline", return_value=None),
            patch.object(fastapi_mw, "record_rtt_sample") as m_rtt,
        ):
            _run(BaldurMiddleware(_ok_app)(_scope(), _receive, recorder))

        m_rtt.assert_not_called()

    def test_concurrent_requests_have_isolated_deadlines(self):
        """asyncio.gather per-Task isolation: each request sees only its own deadline.

        Validates the load-bearing per-Task-isolation claim the FastAPI clear is
        belt-and-suspenders for, and guards against a future refactor to a
        module-global deadline.
        """
        from baldur.scaling.deadline_context import get_remaining_ms

        seen: dict[str, float | None] = {}

        async def _capturing_app(scope, receive, send):
            rid = scope["path"]
            await asyncio.sleep(0)  # yield so the gathered tasks interleave
            seen[rid] = get_remaining_ms()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        mw = BaldurMiddleware(_capturing_app)

        async def _drive(rid: str, header: bytes):
            scope = _scope(path=rid, headers=[(b"x-deadline-remaining", header)])
            await mw(scope, _receive, _AsgiRecorder())

        async def _body():
            await asyncio.gather(
                _drive("/a", b"5000ms"),
                _drive("/b", b"3000ms"),
                _drive("/c", b"10000ms"),
            )

        # check_deadline / record_rtt_sample run for real; admission is inactive
        # (no PRO needed) so record_rtt_sample no-ops on tier_id None.
        with _patch_pipeline(admission=AdmissionDecision(active=False)):
            _run(_body())

        # Each request kept its own header value (minus the 50ms network buffer),
        # never another task's — no cross-Task bleed.
        assert seen["/a"] is not None
        assert 4000 < seen["/a"] <= 4951
        assert seen["/b"] is not None
        assert 2000 < seen["/b"] <= 2951
        assert seen["/c"] is not None
        assert 9000 < seen["/c"] <= 9951

    def test_finally_runs_and_propagates_on_cancelled_error(self):
        """A client-disconnect CancelledError releases the slot + clears, then propagates.

        ``release()`` is ``except Exception``-wrapped and ``clear_deadline`` is
        ``set(None)`` — neither catches ``BaseException``, so the cancellation is
        released-then-propagated, not swallowed. RTT is skipped (never started).
        """

        async def _cancelling_app(scope, receive, send):
            raise asyncio.CancelledError()

        release = MagicMock()
        recorder = _AsgiRecorder()
        with (
            _patch_pipeline(
                admission=AdmissionDecision(
                    active=True, release=release, tier_id="standard"
                )
            ),
            patch.object(fastapi_mw, "check_deadline", return_value=None),
            patch.object(fastapi_mw, "_clear_deadline_if_enabled") as m_clear,
            patch.object(fastapi_mw, "record_rtt_sample") as m_rtt,
        ):
            with pytest.raises(asyncio.CancelledError):
                _run(BaldurMiddleware(_cancelling_app)(_scope(), _receive, recorder))

        release.assert_called_once_with()
        m_clear.assert_called_once_with()
        m_rtt.assert_not_called()  # downstream never started


# =============================================================================
# HTTP RED metrics (649) — Behavior
# =============================================================================
#
# FastAPI's router sets ``scope["route"]`` in place during matching, so a fake
# downstream app that assigns it before responding models what the real router
# does (and the TestClient tests below prove the real population end-to-end).


async def _routed_ok_app(scope, receive, send) -> None:
    """Downstream that publishes its matched route, then responds 200."""
    scope["route"] = SimpleNamespace(path="/items/{item_id}")
    await _ok_app(scope, receive, send)


async def _routed_raising_app(scope, receive, send) -> None:
    """Downstream that raises a non-HTTP exception BEFORE http.response.start."""
    scope["route"] = SimpleNamespace(path="/items/{item_id}")
    raise RuntimeError("downstream blew up")


async def _routed_stream_then_raise_app(scope, receive, send) -> None:
    """Downstream that sends the response start (206), then raises mid-stream."""
    scope["route"] = SimpleNamespace(path="/items/{item_id}")
    await send({"type": "http.response.start", "status": 206, "headers": []})
    raise RuntimeError("mid-stream failure")


async def _client_disconnect_app(scope, receive, send) -> None:
    """Downstream that raises Starlette's ClientDisconnect (a client fault)."""
    from starlette.requests import ClientDisconnect

    raise ClientDisconnect()


class TestExtractFastapiEndpoint:
    """``_extract_fastapi_endpoint``: matched route template, else UNMATCHED_ROUTE."""

    def test_matched_route_returns_template(self):
        scope = {"route": SimpleNamespace(path="/items/{item_id}")}
        assert _extract_fastapi_endpoint(scope) == "/items/{item_id}"

    def test_no_route_key_returns_unmatched(self):
        """An unmatched request (no scope["route"]) collapses to one bounded label."""
        assert _extract_fastapi_endpoint({}) == "UNMATCHED_ROUTE"

    def test_route_without_path_returns_unmatched(self):
        """R1: a route object lacking a ``.path`` (plain-Starlette) degrades safely."""
        assert _extract_fastapi_endpoint({"route": object()}) == "UNMATCHED_ROUTE"


class TestFastapiRedMetrics:
    """HTTP RED (Rate/Errors/Duration) recording at Django parity (649).

    ``record_http_red`` is imported into the FastAPI middleware module namespace,
    so it is patched as ``fastapi_mw.record_http_red`` (the same pattern the RTT /
    pipeline tests use for the framework-free helpers).
    """

    def test_records_red_when_response_started(self):
        """SC2: a downstream response (started) drives record_http_red once."""
        recorder = _AsgiRecorder()
        with (
            _patch_pipeline(admission=AdmissionDecision(active=False)),
            patch.object(fastapi_mw, "record_http_red") as m_red,
        ):
            _run(BaldurMiddleware(_routed_ok_app)(_scope(), _receive, recorder))

        m_red.assert_called_once()
        method, endpoint, status_code, duration = m_red.call_args.args
        assert method == "GET"
        assert endpoint == "/items/{item_id}"
        assert status_code == 200
        assert isinstance(duration, float)
        assert duration >= 0
        assert "error_type" not in m_red.call_args.kwargs

    def test_reject_early_return_skips_red(self):
        """SC6: a rate-limit reject early-returns (started False) -> no RED (D4)."""
        rejection = ResponseContext(status_code=429, body={"code": "RATE_LIMIT"})
        recorder = _AsgiRecorder()
        with (
            _patch_pipeline(admission=AdmissionDecision(active=False), rate=rejection),
            patch.object(fastapi_mw, "record_http_red") as m_red,
        ):
            _run(BaldurMiddleware(_routed_ok_app)(_scope(), _receive, recorder))

        assert recorder.messages[0]["status"] == 429
        m_red.assert_not_called()

    def test_downstream_raise_before_start_records_500_and_reraises(self):
        """SC7/D5: a pre-start raise records a 500 with the exc name, then re-raises."""
        recorder = _AsgiRecorder()
        with (
            _patch_pipeline(admission=AdmissionDecision(active=False)),
            patch.object(fastapi_mw, "record_http_red") as m_red,
        ):
            with pytest.raises(RuntimeError):
                _run(
                    BaldurMiddleware(_routed_raising_app)(_scope(), _receive, recorder)
                )

        m_red.assert_called_once()
        method, endpoint, status_code, _ = m_red.call_args.args
        assert method == "GET"
        assert endpoint == "/items/{item_id}"
        assert status_code == 500
        assert m_red.call_args.kwargs["error_type"] == "RuntimeError"

    def test_client_disconnect_before_start_records_nothing_and_reraises(self):
        """SC8: a ClientDisconnect is a client fault -> no RED, re-raised (false-alarm guard)."""
        from starlette.requests import ClientDisconnect

        recorder = _AsgiRecorder()
        with (
            _patch_pipeline(admission=AdmissionDecision(active=False)),
            patch.object(fastapi_mw, "record_http_red") as m_red,
        ):
            with pytest.raises(ClientDisconnect):
                _run(
                    BaldurMiddleware(_client_disconnect_app)(
                        _scope(), _receive, recorder
                    )
                )

        m_red.assert_not_called()

    def test_cancelled_error_propagates_with_no_red(self):
        """SC8: asyncio.CancelledError (BaseException) propagates untouched, no RED.

        The exception-wrap catch scope is ``Exception`` (not BaseException), so a
        cancellation never reaches the 500-record branch.
        """
        recorder = _AsgiRecorder()

        async def _cancelling_app(scope, receive, send):
            raise asyncio.CancelledError()

        with (
            _patch_pipeline(admission=AdmissionDecision(active=False)),
            patch.object(fastapi_mw, "record_http_red") as m_red,
        ):
            with pytest.raises(asyncio.CancelledError):
                _run(BaldurMiddleware(_cancelling_app)(_scope(), _receive, recorder))

        m_red.assert_not_called()

    def test_raise_after_start_records_exactly_once_with_sent_status(self):
        """D5 guard: a raise after http.response.start records the sent status once, no 500.

        The except's 500 record is gated on ``not started``, mutually exclusive
        with the finally's record (fires when started), so a streaming generator
        failing mid-stream records exactly once — the already-sent 206, never a
        spurious second 500.
        """
        recorder = _AsgiRecorder()
        with (
            _patch_pipeline(admission=AdmissionDecision(active=False)),
            patch.object(fastapi_mw, "record_http_red") as m_red,
        ):
            with pytest.raises(RuntimeError):
                _run(
                    BaldurMiddleware(_routed_stream_then_raise_app)(
                        _scope(), _receive, recorder
                    )
                )

        m_red.assert_called_once()
        _, endpoint, status_code, _ = m_red.call_args.args
        assert endpoint == "/items/{item_id}"
        assert status_code == 206
        assert "error_type" not in m_red.call_args.kwargs


class TestFastapiRedMetricsNativeRoute:
    """SC3: FastAPI populates ``scope["route"]`` in place — proven end-to-end.

    A real FastAPI app + TestClient is the only way to verify the load-bearing
    D2/R1 claim that the router mutates the shared scope dict so ``route.path``
    is visible to the outer pure-ASGI middleware after ``await self.app(...)``.
    """

    @staticmethod
    def _app():
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/items/{item_id}")
        def _read_item(item_id: int):
            return {"item_id": item_id}

        app.add_middleware(BaldurMiddleware)
        return app

    def test_native_route_collapses_concrete_paths_to_one_label(self):
        """Two concrete paths on one route share a single bounded label."""
        from fastapi.testclient import TestClient

        client = TestClient(self._app())
        with patch.object(fastapi_mw, "record_http_red") as m_red:
            client.get("/items/123")
            client.get("/items/456")

        endpoints = [call.args[1] for call in m_red.call_args_list]
        assert endpoints == ["/items/{item_id}", "/items/{item_id}"]

    def test_unrouted_path_records_unmatched_route(self):
        """An unrouted FastAPI path collapses to the single UNMATCHED_ROUTE label."""
        from fastapi.testclient import TestClient

        client = TestClient(self._app())
        with patch.object(fastapi_mw, "record_http_red") as m_red:
            response = client.get("/no/such/path")

        assert response.status_code == 404
        m_red.assert_called_once()
        _, endpoint, status_code, _ = m_red.call_args.args
        assert endpoint == "UNMATCHED_ROUTE"
        assert status_code == 404
