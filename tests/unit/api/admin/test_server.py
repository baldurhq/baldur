"""Admin HTTP server runtime tests — 429 PR3-runtime.

Verification targets:
- Lifecycle (start / stop / idempotent / bound_port reflects port=0 ephemeral)
- Non-localhost + no key refuses to start (AdminAuthRequiredError propagated)
- Dispatch end-to-end (200 / 404 / 500 / 413 body-size limit)
- Auth enforcement (401 no key, 403 ADMIN without unlock, 200 correct key)
- start_admin_server / stop_admin_server module singleton behavior

Strategy: bind 127.0.0.1:0 for an OS-assigned ephemeral port; keep the
per-test server lifetime short (cold start + single HTTP roundtrip).
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest

from baldur.api.admin import (
    AdminServer,
    get_admin_server,
    reset_admin_server,
    start_admin_server,
    stop_admin_server,
)
from baldur.api.admin.auth import AdminAuthRequiredError
from baldur.api.admin.registry import (
    AdminRegistry,
    AdminRoute,
    reset_admin_registry,
)
from baldur.interfaces.web_framework import (
    HttpMethod,
    PermissionLevel,
    RequestContext,
    ResponseContext,
)
from baldur.settings.admin import AdminServerSettings

# =============================================================================
# Fixtures
# =============================================================================


def _ok_handler(ctx: RequestContext) -> ResponseContext:
    return ResponseContext.json({"ok": True, "path": ctx.path})


def _echo_handler(ctx: RequestContext) -> ResponseContext:
    return ResponseContext.json({"json": ctx.json_body, "bytes": len(ctx.body or b"")})


def _raise_handler(ctx: RequestContext) -> ResponseContext:
    raise RuntimeError("handler blew up")


# A fixed instant so the regression assertion can compare against its isoformat.
_FIXED_DT = datetime(2026, 6, 16, 3, 55, 0, tzinfo=UTC)


def _datetime_handler(ctx: RequestContext) -> ResponseContext:
    """A handler whose body carries a raw datetime (the /control/status shape)."""
    return ResponseContext.json({"opened_at": _FIXED_DT, "name": "svc"})


def _unserializable_handler(ctx: RequestContext) -> ResponseContext:
    """A handler whose body holds a value json cannot encode."""
    return ResponseContext.json({"obj": object()})


def _nonfinite_handler(ctx: RequestContext) -> ResponseContext:
    """A handler whose body holds non-finite floats (NaN / Infinity).

    The /throttle/status gradient shape: a non-finite RTT sample makes
    ``get_stats()`` carry ``nan``/``inf``. ``json.dumps`` defaults to
    ``allow_nan=True``, which emits bare ``NaN``/``Infinity`` tokens (invalid
    JSON) WITHOUT raising — so without ``allow_nan=False`` in ``_write`` this
    body escapes the serialization try/except and reaches the client as an
    unparseable payload (browser ``JSON.parse`` rejects it, the panel silently
    drops) rather than the clean 500 the writer contracts.
    """
    return ResponseContext.json({"rtt_ms": float("nan"), "peak": float("inf")})


@pytest.fixture
def registry():
    """Fresh registry isolated from the module singleton."""
    reg = AdminRegistry()
    reg.register(
        AdminRoute(HttpMethod.GET, "/public", _ok_handler, PermissionLevel.PUBLIC)
    )
    reg.register(
        AdminRoute(HttpMethod.GET, "/viewer", _ok_handler, PermissionLevel.VIEWER)
    )
    reg.register(
        AdminRoute(HttpMethod.POST, "/echo", _echo_handler, PermissionLevel.OPERATOR)
    )
    reg.register(
        AdminRoute(HttpMethod.POST, "/admin", _ok_handler, PermissionLevel.ADMIN)
    )
    reg.register(
        AdminRoute(HttpMethod.GET, "/boom", _raise_handler, PermissionLevel.PUBLIC)
    )
    reg.register(
        AdminRoute(
            HttpMethod.GET, "/datetime", _datetime_handler, PermissionLevel.PUBLIC
        )
    )
    reg.register(
        AdminRoute(
            HttpMethod.GET,
            "/unserializable",
            _unserializable_handler,
            PermissionLevel.PUBLIC,
        )
    )
    reg.register(
        AdminRoute(
            HttpMethod.GET,
            "/nonfinite",
            _nonfinite_handler,
            PermissionLevel.PUBLIC,
        )
    )
    return reg


@pytest.fixture
def running_server(registry):
    """AdminServer on 127.0.0.1:<ephemeral>, no api_key (localhost default)."""
    settings = AdminServerSettings(bind="127.0.0.1", port=0)
    server = AdminServer(settings=settings, registry=registry)
    server.start()
    try:
        yield server
    finally:
        server.stop(timeout=2.0)


@pytest.fixture(autouse=True)
def _reset_module_singleton():
    """Ensure start_admin_server/stop_admin_server tests don't leak state."""
    reset_admin_server()
    yield
    reset_admin_server()
    reset_admin_registry()


def _get(
    server: AdminServer, path: str, headers: dict | None = None
) -> tuple[int, bytes]:
    url = f"http://127.0.0.1:{server.bound_port}{path}"
    req = urllib.request.Request(url)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.status, e.read()


def _post(
    server: AdminServer,
    path: str,
    body: bytes,
    headers: dict | None = None,
) -> tuple[int, bytes]:
    url = f"http://127.0.0.1:{server.bound_port}{path}"
    req = urllib.request.Request(url, data=body, method="POST")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.status, e.read()


# =============================================================================
# Lifecycle
# =============================================================================


class TestAdminServerLifecycleBehavior:
    """start / stop / bound_port / idempotency."""

    def test_start_binds_ephemeral_port_when_port_is_zero(self, registry):
        settings = AdminServerSettings(bind="127.0.0.1", port=0)
        server = AdminServer(settings=settings, registry=registry)

        server.start()
        try:
            assert server.is_running is True
            assert server.bound_port is not None
            assert server.bound_port > 0
        finally:
            server.stop()

    def test_start_is_idempotent_when_already_running(self, running_server):
        """Calling start() on a running server must not raise or rebind."""
        port = running_server.bound_port
        running_server.start()  # idempotent
        assert running_server.is_running is True
        assert running_server.bound_port == port

    def test_stop_is_idempotent(self, registry):
        settings = AdminServerSettings(bind="127.0.0.1", port=0)
        server = AdminServer(settings=settings, registry=registry)
        server.start()

        server.stop()
        server.stop()  # double stop is safe

        assert server.is_running is False
        assert server.bound_port is None

    def test_stop_on_unstarted_server_is_safe(self, registry):
        settings = AdminServerSettings(bind="127.0.0.1", port=0)
        server = AdminServer(settings=settings, registry=registry)

        server.stop()  # never started

        assert server.is_running is False

    def test_non_localhost_without_key_refuses_to_start(self, registry):
        settings = AdminServerSettings(bind="0.0.0.0", port=0, api_key=None)
        server = AdminServer(settings=settings, registry=registry)

        with pytest.raises(AdminAuthRequiredError):
            server.start()

        assert server.is_running is False


# =============================================================================
# Dispatch
# =============================================================================


class TestAdminServerDispatchBehavior:
    """End-to-end request dispatch through stdlib http.server."""

    def test_registered_route_returns_200_json(self, running_server):
        status, body = _get(running_server, "/public")
        assert status == 200
        payload = json.loads(body)
        assert payload == {"ok": True, "path": "/public"}

    def test_unregistered_route_returns_404(self, running_server):
        status, body = _get(running_server, "/does-not-exist")
        assert status == 404
        assert b"NOT_FOUND" in body or b"not_found" in body.lower()

    def test_method_mismatch_returns_404(self, running_server):
        """POST to a GET-only route is a route miss (no auto-allow)."""
        status, _ = _post(running_server, "/public", b"")
        assert status == 404

    def test_handler_exception_returns_500(self, running_server):
        """Handler-raised exceptions are caught and translated to 500.
        The server does not leak the exception back to the accepting thread."""
        status, body = _get(running_server, "/boom")
        assert status == 500
        assert b"handler blew up" in body or b"INTERNAL_ERROR" in body

    def test_json_body_is_parsed_and_passed_to_handler(self, running_server):
        payload = {"hello": "world"}
        data = json.dumps(payload).encode("utf-8")
        status, body = _post(
            running_server,
            "/echo",
            data,
            headers={"Content-Type": "application/json"},
        )
        assert status == 200
        parsed = json.loads(body)
        assert parsed["json"] == payload
        assert parsed["bytes"] == len(data)

    def test_body_over_max_size_returns_413(self, registry):
        """Bodies larger than max_body_bytes are rejected before dispatch.

        The server refuses by writing 413 and closing the connection WITHOUT
        draining the oversized body — that is the whole point of the limit (it
        must not read the attacker's bytes). Closing a socket that still has an
        unread request body makes the OS send a TCP RST, so the client may
        observe EITHER a clean 413 OR an abortive close
        (ConnectionResetError / ConnectionAbortedError, possibly wrapped in
        urllib's URLError). Both are valid "rejected before dispatch" outcomes;
        which one wins is a timing race the loopback on Windows loses more often
        than on Linux/macOS. What must NEVER happen is the body being accepted
        and dispatched to the handler (a 200 echo) — that is still asserted.
        """
        settings = AdminServerSettings(bind="127.0.0.1", port=0, max_body_bytes=1024)
        server = AdminServer(settings=settings, registry=registry)
        server.start()
        try:
            oversized = b"x" * 2048
            try:
                status, _ = _post(server, "/echo", oversized)
            except (ConnectionError, urllib.error.URLError):
                return  # abortive close == rejected before dispatch (valid)
            assert status == 413, (
                f"oversized body must be rejected before dispatch, got {status}"
            )
        finally:
            server.stop()


# =============================================================================
# Auth
# =============================================================================


class TestAdminServerAuthBehavior:
    """Auth enforcement end-to-end through the HTTP server."""

    def _make_keyed_server(
        self, registry, *, unlock: bool = False, readonly_key: str | None = None
    ):
        settings = AdminServerSettings(
            bind="127.0.0.1",
            port=0,
            api_key="correct-key",
            readonly_key=readonly_key,
            unlock=unlock,
        )
        server = AdminServer(settings=settings, registry=registry)
        server.start()
        return server

    def test_viewer_route_without_key_returns_401(self, registry):
        server = self._make_keyed_server(registry)
        try:
            status, _ = _get(server, "/viewer")
            assert status == 401
        finally:
            server.stop()

    def test_viewer_route_with_wrong_key_returns_401(self, registry):
        server = self._make_keyed_server(registry)
        try:
            status, _ = _get(server, "/viewer", headers={"X-Baldur-Admin-Key": "wrong"})
            assert status == 401
        finally:
            server.stop()

    def test_viewer_route_with_correct_key_returns_200(self, registry):
        server = self._make_keyed_server(registry)
        try:
            status, _ = _get(
                server,
                "/viewer",
                headers={"X-Baldur-Admin-Key": "correct-key"},
            )
            assert status == 200
        finally:
            server.stop()

    def test_public_route_without_key_returns_200(self, registry):
        """PUBLIC routes bypass auth entirely."""
        server = self._make_keyed_server(registry)
        try:
            status, _ = _get(server, "/public")
            assert status == 200
        finally:
            server.stop()

    def test_admin_route_without_unlock_returns_403(self, registry):
        """ADMIN route rejected when BALDUR_ADMIN_UNLOCK is false, even with
        a correct API key (fail-closed default)."""
        server = self._make_keyed_server(registry, unlock=False)
        try:
            status, body = _post(
                server,
                "/admin",
                b"",
                headers={"X-Baldur-Admin-Key": "correct-key"},
            )
            assert status == 403
            assert b"UNLOCK" in body or b"unlock" in body
        finally:
            server.stop()

    def test_admin_route_with_unlock_and_correct_key_returns_200(self, registry):
        """End-to-end ADMIN reachability — the double-gate (authenticated
        OPERATOR + BALDUR_ADMIN_UNLOCK=1) must grant access. Regression
        guard: without this test, ``authorize()`` could demand an effective
        level that ``authenticate()`` never produces, silently bricking all
        ADMIN routes."""
        server = self._make_keyed_server(registry, unlock=True)
        try:
            status, _ = _post(
                server,
                "/admin",
                b"",
                headers={"X-Baldur-Admin-Key": "correct-key"},
            )
            assert status == 200
        finally:
            server.stop()

    def test_readonly_key_on_viewer_route_returns_200(self, registry):
        """End-to-end readonly→VIEWER round-trip: the readonly key reaches a
        VIEWER-tagged route (the headline new capability of 624)."""
        server = self._make_keyed_server(registry, readonly_key="readonly-key")
        try:
            status, _ = _get(
                server,
                "/viewer",
                headers={"X-Baldur-Admin-Key": "readonly-key"},
            )
            assert status == 200
        finally:
            server.stop()

    def test_viewer_credential_on_operator_route_names_required_and_held_tiers(
        self, registry
    ):
        """D8: an authenticated-but-insufficient caller (readonly → VIEWER) on an
        OPERATOR route gets a 403 naming BOTH the required tier (OPERATOR) and
        the held tier (VIEWER), so a least-privilege integration can
        self-diagnose the permission wall."""
        server = self._make_keyed_server(registry, readonly_key="readonly-key")
        try:
            status, body = _post(
                server,
                "/echo",  # OPERATOR route
                b"",
                headers={"X-Baldur-Admin-Key": "readonly-key"},
            )
            assert status == 403
            text = body.decode()
            # Tier names are PermissionLevel.value (lowercase) — source-reference
            # rather than hardcode so an enum-value change can't pass silently.
            assert PermissionLevel.OPERATOR.value in text  # required tier
            assert PermissionLevel.VIEWER.value in text  # held tier
            assert "grants" in text  # held-tier disclosure phrasing
        finally:
            server.stop()

    def test_unauthenticated_on_operator_route_omits_tier_name(self, registry):
        """D8: tier disclosure is bounded to authenticated callers. An
        unauthenticated request to a non-PUBLIC route is stopped at the 401 gate
        (before the tier-naming 403), so its body names no tier — it confirms
        nothing the caller did not already know."""
        server = self._make_keyed_server(registry, readonly_key="readonly-key")
        try:
            status, body = _post(server, "/echo", b"")  # no key header
            assert status == 401
            text = body.decode()
            assert PermissionLevel.VIEWER.value not in text
            assert "grants" not in text
        finally:
            server.stop()

    def test_operator_credential_on_admin_route_without_unlock_names_held_tier(
        self, registry
    ):
        """D8: on an ADMIN route, an authenticated OPERATOR without unlock gets a
        403 that names the held tier (OPERATOR) AND keeps the unlock hint."""
        server = self._make_keyed_server(registry, unlock=False)
        try:
            status, body = _post(
                server,
                "/admin",  # ADMIN route
                b"",
                headers={"X-Baldur-Admin-Key": "correct-key"},
            )
            assert status == 403
            text = body.decode()
            assert PermissionLevel.ADMIN.value in text  # required tier
            assert PermissionLevel.OPERATOR.value in text  # held tier
            assert "grants" in text
            assert "UNLOCK" in text  # unlock hint preserved
        finally:
            server.stop()


# =============================================================================
# Module-singleton wrappers
# =============================================================================


class TestStartAdminServerBehavior:
    """start_admin_server / stop_admin_server module singleton."""

    def test_start_admin_server_returns_running_instance(self):
        server = start_admin_server(port=0, register_shutdown=False)
        try:
            assert server.is_running is True
            assert get_admin_server() is server
        finally:
            stop_admin_server()

    def test_double_start_returns_same_instance(self):
        first = start_admin_server(port=0, register_shutdown=False)
        try:
            second = start_admin_server(port=0, register_shutdown=False)
            assert first is second
        finally:
            stop_admin_server()

    def test_stop_admin_server_clears_singleton(self):
        start_admin_server(port=0, register_shutdown=False)
        stop_admin_server()
        assert get_admin_server() is None

    def test_start_with_port_override_binds_requested_port(self):
        """port kwarg overrides BALDUR_ADMIN_PORT."""
        server = start_admin_server(port=0, register_shutdown=False)
        try:
            assert server.bound_port is not None
        finally:
            stop_admin_server()


# =============================================================================
# Response serialization (impl 634 verify fix)
# =============================================================================


class TestAdminServerSerializationBehavior:
    """Non-native JSON types in a response body serialize safely (634).

    ``_write`` does the single ``json.dumps`` for every dict/list handler body.
    Before 634's verify fix it had no datetime encoder and no try/except, so a
    handler returning a raw ``datetime`` (e.g. ``/control/status``'s
    ``opened_at`` after 634 D1 wired the console panel to it) crashed the writer
    and dropped the connection unanswered — the console panel showed
    "fetch failed". The fix encodes datetime/date as ISO-8601 and converts any
    remaining serialization failure into a 500 instead of a dropped connection.
    """

    def test_datetime_in_body_serializes_to_iso_200(self, running_server):
        """A raw datetime body returns 200 with an ISO-8601 string."""
        status, body = _get(running_server, "/datetime")
        assert status == 200
        payload = json.loads(body)
        assert payload["opened_at"] == _FIXED_DT.isoformat()

    def test_unserializable_body_returns_500_not_dropped_connection(
        self, running_server
    ):
        """An un-encodable body surfaces as a parseable 500 — never the
        pre-fix RemoteDisconnected (connection dropped before any response)."""
        status, body = _get(running_server, "/unserializable")
        assert status == 500
        payload = json.loads(body)
        assert "error" in payload

    def test_nonfinite_float_body_returns_500_not_invalid_json(self, running_server):
        """A NaN/Infinity float body surfaces as a parseable 500 — never the
        bare ``NaN``/``Infinity`` tokens ``json.dumps`` emits under the default
        ``allow_nan=True``. Those tokens are invalid JSON: the connection is not
        dropped (no TypeError) and the writer returns 200, but the client's
        ``JSON.parse`` rejects the body and the panel silently drops. ``_write``
        passes ``allow_nan=False`` so this becomes a clean, parseable 500 — the
        third serialization failure mode, alongside datetime (→ISO 200) and
        un-encodable types (→500)."""
        status, body = _get(running_server, "/nonfinite")
        assert status == 500
        payload = json.loads(body)
        assert "error" in payload
        # The body must be strict-valid JSON (no bare NaN/Infinity tokens, which
        # Python's lenient json.loads would otherwise parse back without error).
        assert b"NaN" not in body
        assert b"Infinity" not in body


# =============================================================================
# Concurrency smoke
# =============================================================================


class TestAdminServerConcurrencyBehavior:
    """ThreadingHTTPServer should serve concurrent requests without corruption."""

    def test_parallel_requests_all_return_200(self, running_server):
        results: list[int] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def _worker():
            try:
                status, _ = _get(running_server, "/public")
                with lock:
                    results.append(status)
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == []
        assert results == [200] * 8
