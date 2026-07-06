"""Web-console + origin-gate integration tests — 536.

End-to-end through the in-process stdlib ``ThreadingHTTPServer`` on an
OS-assigned ephemeral port (the ``test_server.py`` pattern). These prove the
composed request lifecycle inside ``_dispatch``, not single-function behavior:

- the request-origin gate sits **in front of** route resolution and auth — a
  foreign ``Origin`` is rejected with 403 even on a PUBLIC route;
- curl-style flows (loopback Host, no Origin) still pass unchanged;
- ``GET /`` returns 200 ``text/html`` (with the CSP header) when the console is
  enabled, and 404 when disabled while the JSON API keeps serving;
- a destructive ADMIN route still 403s without ``BALDUR_ADMIN_UNLOCK=1`` (the
  auth path is unchanged by D6 — the gate does not weaken it).
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from baldur.api.admin import AdminServer, reset_admin_server
from baldur.api.admin.registry import (
    AdminRegistry,
    AdminRoute,
    reset_admin_registry,
)
from baldur.api.admin.routes.console import _register_console_routes
from baldur.interfaces.web_framework import (
    HttpMethod,
    PermissionLevel,
    RequestContext,
    ResponseContext,
)
from baldur.settings.admin import AdminServerSettings

_HANDLER_SETTINGS = "baldur.api.admin.console.handler.get_admin_server_settings"


def _ok_handler(ctx: RequestContext) -> ResponseContext:
    return ResponseContext.json({"ok": True, "path": ctx.path})


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    reset_admin_server()
    yield
    reset_admin_server()
    reset_admin_registry()


@pytest.fixture
def console_registry() -> AdminRegistry:
    """Registry with the console GET / plus JSON routes spanning permission
    levels, so console + JSON coexistence and auth are exercised together."""
    reg = AdminRegistry()
    reg.register(
        AdminRoute(HttpMethod.GET, "/public", _ok_handler, PermissionLevel.PUBLIC)
    )
    reg.register(
        AdminRoute(HttpMethod.POST, "/admin", _ok_handler, PermissionLevel.ADMIN)
    )
    _register_console_routes(reg)
    return reg


@pytest.fixture
def console_server(console_registry) -> Iterator[AdminServer]:
    """Localhost console server on an ephemeral port (origin gate enforced)."""
    settings = AdminServerSettings(bind="127.0.0.1", port=0)
    server = AdminServer(settings=settings, registry=console_registry)
    server.start()
    try:
        yield server
    finally:
        server.stop(timeout=2.0)


def _request(
    server: AdminServer,
    path: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    url = f"http://127.0.0.1:{server.bound_port}{path}"
    req = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.status, dict(exc.headers), exc.read()


# =============================================================================
# Origin gate — runs before route resolution and auth
# =============================================================================


class TestOriginGateIntegration:
    """The D6 gate uniformly protects every path, including PUBLIC and 404."""

    def test_foreign_origin_rejected_with_403_even_on_public_route(
        self, console_server
    ):
        """A cross-origin browser request carries a foreign Origin; the gate
        rejects it before the PUBLIC route's auth-bypass can run."""
        status, _headers, _body = _request(
            console_server,
            "/public",
            headers={"Origin": "http://evil.example.com"},
        )
        assert status == 403

    def test_foreign_origin_rejected_on_unmatched_path(self, console_server):
        """Placing the gate first also origin-protects the 404 path."""
        status, _headers, _body = _request(
            console_server,
            "/does-not-exist",
            headers={"Origin": "http://evil.example.com"},
        )
        assert status == 403

    def test_curl_style_request_without_origin_passes(self, console_server):
        """No Origin + loopback Host (urllib default) → unchanged, allowed."""
        status, _headers, body = _request(console_server, "/public")
        assert status == 200
        assert b'"ok": true' in body

    def test_same_origin_fetch_passes(self, console_server):
        """A same-origin console fetch carries a loopback Origin → allowed."""
        origin = f"http://127.0.0.1:{console_server.bound_port}"
        status, _headers, _body = _request(
            console_server, "/public", headers={"Origin": origin}
        )
        assert status == 200


# =============================================================================
# Console serving — GET / lifecycle
# =============================================================================


class TestConsoleServingIntegration:
    """GET / through the full server with the console enabled/disabled."""

    def test_get_root_returns_200_text_html(self, console_server):
        status, headers, body = _request(console_server, "/")
        assert status == 200
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        assert b"__BALDUR_PANELS__" in body

    def test_console_response_carries_csp_header(self, console_server):
        """The CSP header propagates end-to-end through _write (D12)."""
        _status, headers, _body = _request(console_server, "/")
        csp = headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        assert "nonce-" in csp

    def test_disabled_console_returns_404_while_json_keeps_serving(
        self, console_server
    ):
        """With the console disabled, GET / is 404 but JSON endpoints serve."""
        with patch(
            _HANDLER_SETTINGS,
            return_value=AdminServerSettings(console_enabled=False),
        ):
            console_status, _h, _b = _request(console_server, "/")
            json_status, _h2, json_body = _request(console_server, "/public")

        assert console_status == 404
        assert json_status == 200
        assert b'"ok": true' in json_body


# =============================================================================
# Auth unchanged — D6 gate does not weaken the auth path
# =============================================================================


class TestAuthUnchangedByOriginGate:
    """The destructive ADMIN double-gate still fires behind the origin gate."""

    def test_admin_route_still_403_without_unlock(self, console_registry):
        """Correct key but BALDUR_ADMIN_UNLOCK off → 403 (auth path unchanged).
        The origin gate (loopback Host, no Origin) passes, so the 403 proves the
        ADMIN double-gate still enforces."""
        settings = AdminServerSettings(
            bind="127.0.0.1", port=0, api_key="correct-key", unlock=False
        )
        server = AdminServer(settings=settings, registry=console_registry)
        server.start()
        try:
            status, _headers, body = _request(
                server,
                "/admin",
                method="POST",
                body=b"",
                headers={"X-Baldur-Admin-Key": "correct-key"},
            )
            assert status == 403
            assert b"UNLOCK" in body or b"unlock" in body
        finally:
            server.stop(timeout=2.0)
