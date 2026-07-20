"""Stdlib ``http.server`` based admin runtime (429 Part 2 / C5 / C7).

Daemon-thread :class:`http.server.ThreadingHTTPServer`. Dispatches to
framework-agnostic :class:`baldur.interfaces.web_framework.HandlerFunc`
functions via :class:`~baldur.api.admin.registry.AdminRegistry`.

Design constraints (from 429 Essential Trade-offs #1, #2):
    * No async / no HTTP/2 / no streaming — admin traffic is <1 RPS.
    * Plain HTTP; TLS via reverse proxy.
    * Zero new runtime dependency.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import structlog

from baldur.api.admin.auth import (
    API_KEY_HEADER,
    authenticate,
    authorize,
    check_bind_safety,
    emit_transport_warning,
)
from baldur.api.admin.registry import AdminRegistry, get_admin_registry
from baldur.interfaces.web_framework import (
    HttpMethod,
    PermissionLevel,
    RequestContext,
    ResponseContext,
)
from baldur.settings.admin import AdminServerSettings, get_admin_server_settings
from baldur.utils.network import extract_client_ip_from_headers

logger = structlog.get_logger()


def _json_default(obj: Any) -> str:
    """Fallback encoder for non-native JSON types in admin responses.

    Serializes ``datetime``/``date`` to ISO-8601 so a handler returning a raw
    temporal value (e.g. a circuit breaker's ``opened_at``) cannot crash the
    response writer. Any other unsupported type still raises ``TypeError`` so
    ``_write`` converts it into a 500 rather than silently dropping the
    connection.
    """
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


__all__ = [
    "AdminServer",
    "get_admin_server",
    "reset_admin_server",
    "start_admin_server",
    "stop_admin_server",
]


_LOCALHOST_BIND = {"127.0.0.1", "::1", "localhost"}


def _lookup_header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup.

    Works for both ``http.client.HTTPMessage`` (case-insensitive by spec) and
    a plain ``dict`` (used by unit tests), so the origin gate is testable with
    crafted header dicts independent of the live socket.
    """
    name_lower = name.lower()
    for key, value in headers.items():
        if key.lower() == name_lower:
            return value
    return None


def _request_origin_allowed(
    headers: Mapping[str, str], settings: AdminServerSettings
) -> bool:
    """Validate the request's ``Host``/``Origin`` against the admin allowlist.

    DNS-rebinding defense: a malicious page that resolves its own hostname to
    ``127.0.0.1`` can reach the admin port, but its requests carry a foreign
    ``Host``/``Origin`` header — this gate rejects them before auth runs.

    Pure function (no socket access) so it is unit-testable with crafted
    header dicts. Returns ``True`` when the request may proceed.

    Enforcement scope:
        * localhost binds — always enforced (auto loopback allowlist provides
          a zero-config DNS-rebinding defense).
        * non-localhost binds — enforced only when ``allowed_origins`` is set
          (otherwise skipped: those binds already require an API key via
          ``check_bind_safety``, Host auto-derivation is impossible under
          ``0.0.0.0``, and enforcing would break K8s ``httpGet`` probes /
          reverse-proxy Host headers).

    Header handling:
        * ``Host`` and ``Origin`` hostnames are extracted with
          ``urlsplit(...).hostname`` so the port is stripped and IPv6 brackets
          handled (``[::1]:9090`` -> ``::1``). A naive ``rsplit(":", 1)`` would
          mangle bracketed IPv6 colons.
        * An absent ``Host`` header (HTTP/1.0 / raw client; browsers always send
          one) is treated as non-browser and allowed — a missing Host cannot be
          a DNS-rebound browser request (matches the ``curl`` posture).
        * When an ``Origin`` header is present, its host must also be in the
          allowlist (browsers always send ``Origin`` on cross-origin mutations).
    """
    if not settings.is_localhost_bind and not settings.allowed_origins:
        return True

    allowed = set(settings.allowed_origins)
    if settings.is_localhost_bind:
        allowed |= _LOCALHOST_BIND
    allowed.add(settings.bind)

    host_value = _lookup_header(headers, "Host")
    if host_value:
        # Host has no scheme ("127.0.0.1:9090"); prepend "//" so urlsplit
        # parses it as a netloc rather than a path.
        host = urlsplit("//" + host_value).hostname
        if host is not None and host not in allowed:
            return False

    origin_value = _lookup_header(headers, "Origin")
    if origin_value:
        # Origin carries a scheme ("http://127.0.0.1:9090") — urlsplit parses
        # it directly. A malformed Origin yields hostname None -> rejected.
        origin_host = urlsplit(origin_value).hostname
        if origin_host is None or origin_host not in allowed:
            return False

    return True


def _apply_admin_identity(ctx: RequestContext, *, trusted: bool) -> None:
    """Populate ``ctx.user`` from the PRO identity resolver, if registered.

    No-op for OSS: ``safe_get()`` returns ``None`` (empty slot), so ``ctx.user``
    stays ``None`` and ``resolve_actor`` records ``"anonymous"`` — byte-identical
    to pre-537 behavior.

    PRO registers a concrete resolver that maps a trusted proxy-forwarded
    identity header to an :class:`~baldur.interfaces.admin_identity.AdminPrincipal`.
    Side-effect fail-open (attribution is not an authorization gate): a
    resolver exception degrades attribution to ``"anonymous"`` but never
    blocks the control action — the request was already authorized by the
    API-key / unlock gate.
    The header *value* is never logged (PII); only a coarse reason.
    """
    from baldur.factory.registry import ProviderRegistry

    try:
        resolver = ProviderRegistry.admin_identity_resolver.safe_get()
        if resolver is None:
            return
        principal = resolver.resolve(ctx, trusted=trusted)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "admin.identity_resolver_failed",
            path=ctx.path,
            reason="resolver_exception",
            error=str(exc),
        )
        return

    if principal is not None:
        ctx.user = principal


class _AdminHTTPHandler(BaseHTTPRequestHandler):
    """Per-request handler. Created by :class:`ThreadingHTTPServer` on each
    accepted connection. Access the owning :class:`AdminServer` via
    ``self.server._baldur_admin`` (attached in :meth:`AdminServer.start`).
    """

    def log_message(self, fmt: str, *args: Any) -> None:
        """Route stdlib access logs through structlog at DEBUG."""
        logger.debug("admin.http_access", message=fmt % args)

    @property
    def admin(self) -> AdminServer:
        return self.server._baldur_admin  # type: ignore[attr-defined,no-any-return]

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch("OPTIONS")

    def _dispatch(self, method: str) -> None:
        server = self.admin
        settings = server.settings
        registry = server.registry

        # Request-origin gate (D6) — runs before route resolution and auth so
        # it uniformly protects the unmatched (404) and PUBLIC GET / paths too.
        # Closes the DNS-rebinding vector that browser exposure of the console
        # would otherwise open.
        # self.headers is an HTTPMessage; it duck-types as a header mapping and
        # its case-insensitive .get() must be preserved (do not dict()-flatten).
        if not _request_origin_allowed(
            cast("Mapping[str, str]", self.headers), settings
        ):
            logger.warning(
                "admin.request_origin_blocked",
                method=method,
                path=self.path,
                host=self.headers.get("Host"),
                origin=self.headers.get("Origin"),
            )
            self._write(
                ResponseContext.forbidden(
                    "Request origin not allowed (Host/Origin not in admin allowlist)"
                )
            )
            return

        split = urlsplit(self.path)
        raw_path = split.path or "/"
        query: dict[str, Any] = {
            k: v[0] if len(v) == 1 else v
            for k, v in parse_qs(split.query, keep_blank_values=True).items()
        }

        match = registry.resolve(method, raw_path)
        if match is None:
            self._write(
                ResponseContext.not_found(f"No admin route for {method} {raw_path}")
            )
            return

        route, path_params = match

        from_localhost = self._client_is_localhost()
        outcome = authenticate(
            self.headers.get(API_KEY_HEADER),
            settings,
            from_localhost=from_localhost,
        )
        if (
            route.permission_level != PermissionLevel.PUBLIC
            and not outcome.authenticated
        ):
            logger.info(
                "admin.auth_rejected",
                path=raw_path,
                method=method,
                reason=outcome.reason,
            )
            self._write(
                ResponseContext.unauthorized("Valid X-Baldur-Admin-Key header required")
            )
            return

        if not authorize(outcome.level, route.permission_level, settings):
            logger.info(
                "admin.authz_rejected",
                path=raw_path,
                method=method,
                required=route.permission_level.value,
                effective=outcome.level.value if outcome.level else None,
                unlocked=settings.unlock,
            )
            message = f"Route requires {route.permission_level.value}"
            if outcome.level is not None:
                # Authenticated-but-insufficient: name the held tier so a
                # least-privilege caller (e.g. an AI operator on the readonly
                # key) can self-diagnose the permission wall. Bounded to
                # authenticated callers — an unauthenticated caller already
                # got the generic 401 above and learns nothing new here.
                message += f"; your credential grants {outcome.level.value}"
            if route.permission_level == PermissionLevel.ADMIN:
                message += "; BALDUR_ADMIN_UNLOCK=1 needed for ADMIN operations"
            self._write(ResponseContext.forbidden(message))
            return

        body_bytes = self._read_body(settings.max_body_bytes)
        if body_bytes is None:
            return  # _read_body wrote a 413 response already

        json_body = self._maybe_parse_json(body_bytes)

        # Forwarded-header trust gate (537 D-C2). Only when a proxy is affirmed
        # (trust_proxy=True) do we trust X-Forwarded-* — for both the real
        # client IP (G6) and the operator identity (G1). Untrusted: the TCP
        # peer is the client and any forwarded identity header is ignored.
        is_trusted = settings.trust_proxy
        peer = self.client_address[0] if self.client_address else None
        client_ip = (
            extract_client_ip_from_headers(
                cast("Mapping[str, str]", self.headers), default=peer
            )
            if is_trusted
            else peer
        )

        ctx = RequestContext(
            method=HttpMethod(method),
            path=raw_path,
            headers=dict(self.headers.items()),
            query_params=query,
            path_params=dict(path_params),
            body=body_bytes or None,
            json_body=json_body,
            is_authenticated=outcome.authenticated,
            client_ip=client_ip,
            user_agent=self.headers.get("User-Agent"),
            request_id=str(uuid.uuid4()),
            content_type=self.headers.get("Content-Type"),
        )

        # Identity seam (537 G1/D1/D5). No-op for OSS; PRO sets ctx.user from a
        # trusted forwarded identity header so resolve_actor records the real
        # operator instead of "anonymous".
        _apply_admin_identity(ctx, trusted=is_trusted)

        try:
            response = route.handler(ctx)
        except Exception as exc:  # noqa: BLE001
            # 685 D5: a canary rollout holds the config lock for a domain this
            # write targets — a client conflict (409), not a server fault.
            # Matched by name so OSS server code does not import the PRO
            # ConfigLockError symbol (precedent: api/handlers/canary.py). The
            # message names the owning rollout id. One site covers every
            # admin-route handler (editor, SLO, governance, drift, chaos-safety).
            if type(exc).__name__ == "ConfigLockError":
                response = ResponseContext.error(
                    str(exc), status_code=409, error_code="ROLLOUT_CONFLICT"
                )
            else:
                logger.exception(
                    "admin.handler_error",
                    path=raw_path,
                    method=method,
                    error=exc,
                )
                response = ResponseContext.server_error(str(exc))

        self._write(response)

    def _client_is_localhost(self) -> bool:
        addr = self.client_address[0] if self.client_address else ""
        return addr in _LOCALHOST_BIND

    def _read_body(self, max_bytes: int) -> bytes | None:
        length_header = self.headers.get("Content-Length")
        if not length_header:
            return b""
        try:
            length = int(length_header)
        except ValueError:
            self._write(ResponseContext.bad_request("Invalid Content-Length"))
            return None
        if length < 0:
            self._write(ResponseContext.bad_request("Negative Content-Length"))
            return None
        if length > max_bytes:
            self._write(
                ResponseContext.error(
                    "Request body too large",
                    status_code=413,
                    error_code="PAYLOAD_TOO_LARGE",
                )
            )
            return None
        if length == 0:
            return b""
        return self.rfile.read(length)

    @staticmethod
    def _maybe_parse_json(body: bytes | None) -> dict | None:
        if not body:
            return None
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _write(self, response: ResponseContext) -> None:
        body = response.body
        content_type = response.content_type
        status_code = response.status_code
        if response.is_streaming:
            encoded = b""
            content_type = content_type or "application/octet-stream"
        elif isinstance(body, (dict, list)) or body is None:
            # ``default=_json_default`` serializes datetime/date to ISO-8601 so a
            # handler returning a raw temporal value (e.g. a circuit breaker's
            # ``opened_at`` via /control/status) cannot crash the writer. Any
            # other unserializable value is caught here and turned into a 500
            # rather than escaping _write and dropping the connection unanswered.
            # ``allow_nan=False`` makes a non-finite float (NaN/Infinity) raise
            # ValueError here too: at the default (True) json.dumps emits bare
            # ``NaN``/``Infinity`` tokens (invalid JSON) WITHOUT raising, the one
            # serialization failure mode that would otherwise escape the
            # try/except as an unparseable body (client-side JSON.parse failure)
            # rather than the clean 500 this writer promises.
            try:
                encoded = json.dumps(
                    body if body is not None else {},
                    default=_json_default,
                    allow_nan=False,
                ).encode("utf-8")
            except (TypeError, ValueError):
                logger.exception(
                    "admin.response_serialization_error",
                    path=self.path,
                )
                status_code = 500
                encoded = json.dumps({"error": "response serialization failed"}).encode(
                    "utf-8"
                )
            content_type = content_type or "application/json"
        elif isinstance(body, bytes):
            encoded = body
            content_type = content_type or "application/octet-stream"
        else:
            encoded = str(body).encode("utf-8")
            content_type = content_type or "text/plain"

        try:
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            if encoded:
                self.wfile.write(encoded)
        except BrokenPipeError:
            logger.debug("admin.client_disconnected", path=self.path)


class AdminServer:
    """Daemon-thread admin HTTP server.

    Lifecycle:
        ``AdminServer(...)`` → :meth:`start` → (serving in background) →
        :meth:`stop`. :meth:`stop` is idempotent.

    Use the module-level :func:`start_admin_server` / :func:`stop_admin_server`
    helpers from application code; :class:`AdminServer` is exposed for tests
    and advanced integrations.
    """

    def __init__(
        self,
        settings: AdminServerSettings | None = None,
        registry: AdminRegistry | None = None,
    ) -> None:
        self.settings = settings or get_admin_server_settings()
        self.registry = registry or get_admin_registry()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._started = False

    @property
    def bound_port(self) -> int | None:
        """Actual port the server bound to (useful with port=0 in tests)."""
        if self._httpd is None:
            return None
        return self._httpd.server_address[1]

    @property
    def is_running(self) -> bool:
        return self._started and self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start listening. Idempotent.

        Raises:
            AdminAuthRequiredError: non-localhost bind without an API key.
        """
        with self._lock:
            if self._started:
                logger.debug("admin.server_already_started", port=self.bound_port)
                return

            check_bind_safety(self.settings)
            emit_transport_warning(self.settings)

            # Per-request socket timeout lives on the handler class
            # (BaseHTTPRequestHandler.timeout). Server-level HTTPServer.timeout
            # is only consumed by handle_request()'s select loop — it does not
            # bound serve_forever connections and caused RemoteDisconnected
            # under load in early testing.
            _AdminHTTPHandler.timeout = self.settings.request_timeout_seconds
            httpd = ThreadingHTTPServer(
                (self.settings.bind, self.settings.port), _AdminHTTPHandler
            )
            httpd.daemon_threads = True
            # Give handler instances access to this AdminServer (settings +
            # registry). stdlib BaseHTTPRequestHandler.self.server is the
            # ThreadingHTTPServer, not this AdminServer — we smuggle the
            # reference through an attribute.
            httpd._baldur_admin = self  # type: ignore[attr-defined]
            self._httpd = httpd

            thread = threading.Thread(
                target=httpd.serve_forever,
                name="baldur-admin-server",
                daemon=True,
            )
            thread.start()
            self._thread = thread
            self._started = True

            logger.info(
                "admin.server_started",
                bind=self.settings.bind,
                port=self.bound_port,
                routes=len(self.registry.all_routes()),
                api_key_configured=self.settings.api_key_plain is not None,
                unlock=self.settings.unlock,
            )

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the server. Idempotent and safe from any thread."""
        with self._lock:
            if not self._started:
                return
            httpd = self._httpd
            thread = self._thread
            self._started = False
            self._httpd = None
            self._thread = None

        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception as exc:  # noqa: BLE001
                logger.debug("admin.shutdown_error", error=exc)
            try:
                httpd.server_close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("admin.server_close_error", error=exc)

        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

        logger.info("admin.server_stopped")


class _AdminShutdownHandler:
    """Bridges :class:`AdminServer` to the central ShutdownCoordinator.

    Not a subclass of :class:`ShutdownHandler` at import-time — duck-typed so
    ``baldur.api.admin`` does not need to import
    ``baldur.core.shutdown_coordinator`` during normal (non-shutdown) runtime.
    """

    def __init__(self, server: AdminServer) -> None:
        self._server = server

    def on_shutdown_start(self) -> None:
        # Non-blocking — stop() is quick. serve_forever's shutdown flag is set
        # here so no new requests are accepted while drain runs.
        try:
            self._server.stop(timeout=0.1)
        except Exception as exc:  # noqa: BLE001
            logger.debug("admin.shutdown_start_error", error=exc)

    def is_drain_complete(self) -> bool:
        return not self._server.is_running

    def on_drain_complete(self) -> None:
        self._server.stop(timeout=1.0)

    def on_force_shutdown(self, pending_requests: list) -> None:  # type: ignore[type-arg]
        self._server.stop(timeout=0.1)


_admin_server: AdminServer | None = None
_admin_server_lock = threading.Lock()


def get_admin_server() -> AdminServer | None:
    """Return the running server instance, or None if not started."""
    return _admin_server


def start_admin_server(
    port: int | None = None,
    bind: str | None = None,
    *,
    register_shutdown: bool = True,
) -> AdminServer:
    """Public entry point — start the admin server.

    Arguments override the corresponding settings when provided; otherwise
    settings (``BALDUR_ADMIN_*``) apply. Subsequent calls return the already
    running server.

    Args:
        port: Override ``BALDUR_ADMIN_PORT``.
        bind: Override ``BALDUR_ADMIN_BIND``.
        register_shutdown: When True (default), integrates with
            :class:`~baldur.core.shutdown_coordinator.ShutdownCoordinator` so
            the server stops cleanly on process shutdown.

    Returns:
        The :class:`AdminServer` instance.

    Raises:
        AdminAuthRequiredError: non-localhost bind without an API key.
    """
    global _admin_server
    with _admin_server_lock:
        if _admin_server is not None and _admin_server.is_running:
            return _admin_server

        settings = get_admin_server_settings()
        if port is not None or bind is not None:
            settings = settings.model_copy(
                update={
                    k: v
                    for k, v in {"port": port, "bind": bind}.items()
                    if v is not None
                }
            )

        server = AdminServer(settings=settings)
        server.start()
        _admin_server = server

    if register_shutdown:
        _register_with_shutdown_coordinator(server)

    return server


def stop_admin_server(timeout: float = 5.0) -> None:
    """Stop the singleton admin server if running. Idempotent."""
    global _admin_server
    with _admin_server_lock:
        server = _admin_server
        _admin_server = None
    if server is not None:
        server.stop(timeout=timeout)


def reset_admin_server() -> None:
    """Reset the module-level singleton — test isolation only."""
    stop_admin_server(timeout=1.0)


def _register_with_shutdown_coordinator(server: AdminServer) -> None:
    try:
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator
    except Exception as exc:  # noqa: BLE001
        logger.debug("admin.shutdown_coordinator_unavailable", error=exc)
        return
    try:
        get_shutdown_coordinator().register_handler(
            _AdminShutdownHandler(server)  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001
        # Coordinator is present but rejected our handler — admin server will
        # run without graceful drain on SIGTERM. Surface this, because
        # in-flight ADMIN ops (DLQ purge, emergency release) may be
        # interrupted during process shutdown.
        logger.warning("admin.shutdown_handler_registration_failed", error=exc)
