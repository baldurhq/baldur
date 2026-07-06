"""
FastAPI ASGI middleware that composes Baldur's framework-free helpers.

Wraps each request with the four-step pipeline:
    1. Build ``RequestContext`` from the ASGI scope + headers.
    2. Run reject-decision helpers (rate limit → backpressure → CB pre-flight).
       First non-None response short-circuits.
    3. Forward to the downstream app.
    4. On the response, apply success-side headers and record the CB
       observation.

The middleware accepts an optional ``service_name`` kwarg so callers who
already know the upstream identity (gateway / BFF deployments) opt into CB
pre-flight + observation. Without it, the CB helpers are no-ops.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from baldur.api.middleware import (
    apply_backpressure_headers,
    apply_rate_limit_headers,
    check_admission,
    check_backpressure,
    check_cb_open,
    check_deadline,
    check_rate_limit,
    record_cb_observation,
    record_http_red,
    record_rtt_sample,
)
from baldur.interfaces.web_framework import HttpMethod, RequestContext

if TYPE_CHECKING:
    from baldur.interfaces.web_framework import ResponseContext

logger = structlog.get_logger()


__all__ = ["BaldurMiddleware"]


class BaldurMiddleware:
    """ASGI middleware integrating Baldur's middleware helpers.

    Compatible with FastAPI's ``app.add_middleware(BaldurMiddleware, ...)``
    and Starlette's middleware stack. Pure ASGI — no FastAPI-specific
    imports beyond the type hint, so the same class drops into any
    Starlette-based app.

    Exception handling: this middleware does not convert exceptions from
    the downstream app — it re-raises so Starlette's ``ServerErrorMiddleware``
    (installed by default, outermost in the stack) still catches unhandled
    exceptions and emits the 500 response. If the downstream raises before
    ``http.response.start`` is sent, this middleware skips the CB observation
    (the error never reached the HTTP surface this middleware observes) but
    records a RED 500 for the latency histogram before re-raising — the
    metrics analog of Django's ``_record_exception``. A ``ClientDisconnect``
    is re-raised without a RED record (client fault, not a server 5xx), and
    ``BaseException`` (e.g. ``asyncio.CancelledError``) propagates untouched.
    """

    def __init__(
        self,
        app: Any,
        service_name: str | None = None,
        rate_limit: int | None = None,
        window_seconds: int | None = None,
    ) -> None:
        self.app = app
        self.service_name = service_name
        self.rate_limit = rate_limit
        self.window_seconds = window_seconds

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:  # noqa: C901, PLR0912, PLR0915
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_ctx = _build_request_context(scope)
        start_time = time.perf_counter()

        # Reject-decision pipeline. Rate limit runs first (acquires no resource,
        # sets no deadline), so it stays outside the release try/finally.
        rate_rejection = check_rate_limit(
            request_ctx,
            rate_limit=self.rate_limit,
            window_seconds=self.window_seconds,
        )
        if rate_rejection is not None:
            await _send_response(send, rate_rejection)
            return

        # Inbound deadline-header fast-fail — after the rate limiter, before
        # admission so the inbound deadline is set before admission's degraded-
        # tier forced-deadline decision reads it. The deadline set here is
        # cleared by the outer finally on the allow path; on this early-return
        # reject path ASGI per-Task context isolation discards it (no leak).
        deadline_rejection = check_deadline(request_ctx)
        if deadline_rejection is not None:
            await _send_response(send, deadline_rejection)
            return

        # Admission (PRO tier-based shedding) occupies the backpressure slot.
        # When active it is the single rate gate, so check_backpressure is
        # skipped (shared token bucket). The CB pre-flight + its early reject
        # `return` MUST live inside this try so the acquired bulkhead slot is
        # released on the admission-acquired-then-CB-rejected path; the finally
        # also clears the degraded deadline (belt-and-suspenders on ASGI, which
        # is per-Task context-isolated) and records the RTT sample.
        admission = check_admission(request_ctx)
        release = admission.release
        # Initialized before the try so the outer finally can read
        # captured["started"] / captured["status"] for RTT sampling even when a
        # reject path returns before the downstream app runs.
        captured: dict[str, Any] = {
            "status": 200,
            "headers": [],
            "started": False,
        }
        try:
            if admission.active:
                if admission.rejection is not None:
                    await _send_response(send, admission.rejection)
                    return
            else:
                bp_rejection = check_backpressure(request_ctx)
                if bp_rejection is not None:
                    await _send_response(send, bp_rejection)
                    return

            cb_rejection = check_cb_open(request_ctx, service_name=self.service_name)
            if cb_rejection is not None:
                await _send_response(send, cb_rejection)
                return

            async def send_wrapper(message: dict) -> None:
                if message["type"] == "http.response.start":
                    captured["status"] = message["status"]
                    # Convert ASGI headers (byte tuples) to dict for mutation
                    native_headers: dict[str, str] = {}
                    for k, v in message.get("headers", []):
                        native_headers[k.decode("latin-1")] = v.decode("latin-1")

                    apply_rate_limit_headers(
                        native_headers,
                        request_ctx,
                        rate_limit=self.rate_limit,
                        window_seconds=self.window_seconds,
                    )
                    apply_backpressure_headers(native_headers)

                    message["headers"] = [
                        (k.encode("latin-1"), v.encode("latin-1"))
                        for k, v in native_headers.items()
                    ]
                    captured["started"] = True
                await send(message)

            try:
                await self.app(scope, receive, send_wrapper)
            except Exception as exc:
                # D5: a truly-unhandled exception escapes here — this middleware
                # sits outside ExceptionMiddleware, so non-HTTPException errors
                # re-raise past it. Catch scope is `Exception` (not
                # BaseException), so asyncio.CancelledError (client-disconnect
                # task cancellation) propagates untouched with no spurious 500.
                if _is_client_disconnect(exc):
                    # Client vanished mid-request — not a server fault. Re-raise
                    # without a RED record so a flaky-client disconnect cannot
                    # inflate the 5xx error series or the 500-latency bucket.
                    logger.debug("http_red.client_disconnect")
                    raise
                # Record a 500 ONLY when the response never started — mutually
                # exclusive with the outer finally's D4 record (which fires when
                # started=True), so a raise *after* http.response.start (e.g. a
                # streaming generator failing mid-stream) records exactly once:
                # the already-sent status via D4, never a spurious second 500.
                if not captured["started"]:
                    duration_seconds = time.perf_counter() - start_time
                    record_http_red(
                        request_ctx.method.value,
                        _extract_fastapi_endpoint(scope),
                        500,
                        duration_seconds,
                        error_type=type(exc).__name__,
                    )
                # Re-raise so ServerErrorMiddleware still emits its 500 response.
                raise
            finally:
                if captured["started"]:
                    record_cb_observation(
                        request_ctx,
                        captured["status"],
                        service_name=self.service_name,
                    )
        finally:
            if release is not None:
                try:
                    release()
                except Exception as release_exc:
                    logger.warning("admission.release_failed", error=release_exc)
            # HTTP RED metrics (Rate + Errors + Duration) — Django parity.
            # Recorded for downstream-app responses only (D4: reject early-
            # returns leave started=False). Mutually exclusive with the D5
            # except's 500 record (gated on `not started`), so each request
            # records at most once.
            if captured["started"]:
                duration_seconds = time.perf_counter() - start_time
                record_http_red(
                    request_ctx.method.value,
                    _extract_fastapi_endpoint(scope),
                    captured["status"],
                    duration_seconds,
                )
            # RTT gradient sampling — only when the request reached the
            # downstream app (captured["started"]) and admission classified a
            # tier (PRO active). A reject path leaves started=False / tier None.
            if captured["started"] and admission.tier_id is not None:
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                record_rtt_sample(admission.tier_id, captured["status"], elapsed_ms)
            _clear_deadline_if_enabled()


# =============================================================================
# Internal helpers
# =============================================================================


def _clear_deadline_if_enabled() -> None:
    """Clear the request-scoped degraded deadline (idempotent ``set(None)``).

    No-op when the deadline feature is disabled or its module is unavailable.
    ASGI is per-Task context-isolated, so this is belt-and-suspenders here, but
    it keeps the FastAPI teardown symmetric with the Flask / Django paths.
    """
    try:
        from baldur.scaling.deadline_context import DEADLINE_ENABLED, clear_deadline
    except ImportError:
        return
    if DEADLINE_ENABLED:
        clear_deadline()


def _extract_fastapi_endpoint(scope: dict) -> str:
    """Return the matched route template as a cardinality-bounded label.

    FastAPI's router sets ``scope["route"]`` (an ``APIRoute``) in place during
    matching, so ``route.path`` (e.g. ``/items/{item_id}``) is visible to this
    outer pure-ASGI middleware after ``await self.app(...)``. All concrete paths
    matching one route share a single label — bounded by ``(# routes + 1)``.
    Unmatched requests, and a plain-Starlette app whose ``Route.matches`` never
    sets ``scope["route"]``, collapse to the single ``UNMATCHED_ROUTE`` label,
    identical to Django's scan-defense.
    """
    route = scope.get("route")
    if route is not None:
        path = getattr(route, "path", None)
        if path is not None:
            return path
    return "UNMATCHED_ROUTE"


def _is_client_disconnect(exc: BaseException) -> bool:
    """True when ``exc`` is Starlette's ``ClientDisconnect``.

    Lazy import keeps this module free of a top-level Starlette dependency
    (consistent with its pure-ASGI design); a missing Starlette degrades to
    ``False`` (the exception is then treated as a normal unhandled error).
    """
    try:
        from starlette.requests import ClientDisconnect
    except ImportError:
        return False
    return isinstance(exc, ClientDisconnect)


def _build_request_context(scope: dict) -> RequestContext:
    """Translate an ASGI HTTP scope into Baldur's ``RequestContext``."""
    raw_headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
    headers: dict[str, str] = {}
    for k, v in raw_headers:
        headers[k.decode("latin-1")] = v.decode("latin-1")

    method_str = scope.get("method", "GET").upper()
    try:
        method = HttpMethod(method_str)
    except ValueError:
        method = HttpMethod.GET

    client = scope.get("client")
    client_ip = client[0] if isinstance(client, (tuple, list)) and client else None
    forwarded = headers.get("x-forwarded-for")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()

    query_bytes: bytes = scope.get("query_string", b"")
    query_params: dict[str, Any] = {}
    if query_bytes:
        from urllib.parse import parse_qs

        # Different binding from `for k, v in raw_headers` above (bytes vs str).
        for q_key, q_values in parse_qs(query_bytes.decode("latin-1")).items():
            query_params[q_key] = q_values[0] if len(q_values) == 1 else q_values

    return RequestContext(
        method=method,
        path=scope.get("path", "/"),
        headers=headers,
        query_params=query_params,
        client_ip=client_ip,
        user_agent=headers.get("user-agent"),
        request_id=headers.get("x-request-id"),
        content_type=headers.get("content-type"),
    )


async def _send_response(send: Any, response: ResponseContext) -> None:
    """Emit a Baldur ``ResponseContext`` over ASGI."""
    import json as _json

    body_bytes: bytes
    headers = dict(response.headers)
    if isinstance(response.body, (bytes, bytearray)):
        body_bytes = bytes(response.body)
    elif isinstance(response.body, str):
        body_bytes = response.body.encode("utf-8")
    elif response.body is None:
        body_bytes = b""
    else:
        body_bytes = _json.dumps(response.body).encode("utf-8")

    headers.setdefault("content-type", response.content_type)
    headers.setdefault("content-length", str(len(body_bytes)))

    await send(
        {
            "type": "http.response.start",
            "status": response.status_code,
            "headers": [
                (k.encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()
            ],
        }
    )
    await send({"type": "http.response.body", "body": body_bytes})
