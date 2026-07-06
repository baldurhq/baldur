"""
Flask request hooks that wire ``baldur.api.middleware`` helpers.

Pattern: stash the rejection / context on Flask's ``g`` proxy in
``before_request`` so ``after_request`` can decide whether to apply success
headers (only on responses Baldur did not generate). This keeps both hooks
pure adapters around the framework-free helpers.
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
from baldur.interfaces.web_framework import (
    HttpMethod,
    RequestContext,
    ResponseContext,
)

if TYPE_CHECKING:
    from flask import Flask, Response


logger = structlog.get_logger()


__all__ = ["install_baldur_request_hooks"]


_FLASK_G_KEY = "_baldur_request_ctx"
_FLASK_G_REJECTED = "_baldur_rejected"
_FLASK_G_RELEASE = "_baldur_admission_release"
_FLASK_G_START_TIME = "_baldur_start_time"
_FLASK_G_TIER_ID = "_baldur_tier_id"
_FLASK_G_ENDPOINT = "_baldur_endpoint"
_FLASK_G_RED_RECORDED = "_baldur_red_recorded"


def install_baldur_request_hooks(  # noqa: C901, PLR0915
    app: Flask,
    service_name: str | None = None,
    rate_limit: int | None = None,
    window_seconds: int | None = None,
) -> None:
    """Register before / after / teardown hooks on ``app``.

    Idempotent only at the framework level — Flask will register the same
    callable multiple times if ``init_flask`` is called repeatedly. Callers
    are expected to call it once from their factory.

    ``rate_limit`` / ``window_seconds`` override the values from
    ``RateLimitSettings.middleware_*``. ``None`` (the default) defers to
    the settings, which are ``0`` / disabled by default.
    """
    from flask import g

    def _before_request() -> Response | None:
        # Stash the request-start timestamp at the head so RED duration is
        # available on the common (non-admission) path too — the RTT sampler
        # below stays gated on tier_id, so this does not change RTT behavior.
        setattr(g, _FLASK_G_START_TIME, time.perf_counter())
        request_ctx = _build_request_context()
        setattr(g, _FLASK_G_KEY, request_ctx)
        # URL matching precedes before_request hooks, so request.url_rule is
        # populated here. Stash the matched route template (else UNMATCHED_ROUTE)
        # so both _after_request and _teardown_request reuse the same bounded
        # endpoint label without re-reading the request proxy.
        setattr(g, _FLASK_G_ENDPOINT, _extract_flask_endpoint())

        rate_rejection = check_rate_limit(
            request_ctx,
            rate_limit=rate_limit,
            window_seconds=window_seconds,
        )
        if rate_rejection is not None:
            setattr(g, _FLASK_G_REJECTED, True)
            return _to_flask_response(rate_rejection)

        # Inbound deadline-header fast-fail — runs after the rate limiter and
        # before admission so the inbound deadline is set before admission's
        # degraded-tier forced-deadline decision reads it.
        deadline_rejection = check_deadline(request_ctx)
        if deadline_rejection is not None:
            setattr(g, _FLASK_G_REJECTED, True)
            return _to_flask_response(deadline_rejection)

        # Admission (PRO tier-based shedding) occupies the backpressure slot.
        # When active it is the single rate gate, so check_backpressure is
        # skipped (they share the same token bucket). Stash the bulkhead release
        # on `g` BEFORE the CB pre-flight so the always-on teardown releases the
        # acquired slot even when before_request short-circuits on a CB reject.
        admission = check_admission(request_ctx)
        if admission.active:
            if admission.release is not None:
                setattr(g, _FLASK_G_RELEASE, admission.release)
            # Stash the classified tier so after_request can feed the gradient
            # calculator on the non-rejected path. The start time is already
            # stashed unconditionally at the head of _before_request.
            setattr(g, _FLASK_G_TIER_ID, admission.tier_id)
            if admission.rejection is not None:
                setattr(g, _FLASK_G_REJECTED, True)
                return _to_flask_response(admission.rejection)
        else:
            bp_rejection = check_backpressure(request_ctx)
            if bp_rejection is not None:
                setattr(g, _FLASK_G_REJECTED, True)
                return _to_flask_response(bp_rejection)

        cb_rejection = check_cb_open(request_ctx, service_name=service_name)
        if cb_rejection is not None:
            setattr(g, _FLASK_G_REJECTED, True)
            return _to_flask_response(cb_rejection)
        return None

    def _after_request(response: Response) -> Response:
        request_ctx: RequestContext | None = getattr(g, _FLASK_G_KEY, None)
        if request_ctx is None:
            return response

        # Skip header injection for Baldur-generated rejection responses
        # (their headers are already authoritative).
        if not getattr(g, _FLASK_G_REJECTED, False):
            # werkzeug's `Headers` is a MutableMapping[str, str] at runtime
            # but the stub declares it as a distinct class. Cast at the
            # boundary so the helpers' generic-mapping signature holds.
            from collections.abc import MutableMapping
            from typing import cast

            headers_mapping = cast(MutableMapping[str, str], response.headers)
            apply_rate_limit_headers(
                headers_mapping,
                request_ctx,
                rate_limit=rate_limit,
                window_seconds=window_seconds,
            )
            apply_backpressure_headers(headers_mapping)
            record_cb_observation(
                request_ctx,
                response.status_code,
                service_name=service_name,
            )

            # HTTP RED metrics (Rate + Errors + Duration) — Django parity.
            # Recorded only for downstream-app responses (D4: middleware-
            # generated rejects are excluded by the _FLASK_G_REJECTED gate
            # above). The guard prevents _teardown_request's unhandled-500 path
            # from double-recording a response already counted here.
            start_time = getattr(g, _FLASK_G_START_TIME, None)
            if start_time is not None:
                duration_seconds = time.perf_counter() - start_time
                endpoint = getattr(g, _FLASK_G_ENDPOINT, "UNMATCHED_ROUTE")
                record_http_red(
                    request_ctx.method.value,
                    endpoint,
                    response.status_code,
                    duration_seconds,
                )
                setattr(g, _FLASK_G_RED_RECORDED, True)

            # RTT gradient sampling — only when admission classified a tier
            # (PRO active). start_time + tier_id were stashed in before_request;
            # tier_id is None on an early short-circuit, so the sample is
            # skipped.
            tier_id = getattr(g, _FLASK_G_TIER_ID, None)
            if start_time is not None and tier_id is not None:
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                record_rtt_sample(tier_id, response.status_code, elapsed_ms)
        return response

    def _teardown_request(exc: BaseException | None = None) -> None:
        # Always runs at request completion (even on a before_request
        # short-circuit or a downstream exception). Releases any acquired
        # admission bulkhead slot (idempotent) and clears the request-scoped
        # degraded deadline so it cannot leak onto the next request on a reused
        # sync (WSGI gthread) worker and false-reject a later `critical`
        # request at should_allow's deadline-expiry step.
        release = getattr(g, _FLASK_G_RELEASE, None)
        if release is not None:
            try:
                release()
            except Exception as release_exc:
                logger.warning("admission.release_failed", error=release_exc)
        _clear_deadline_if_enabled()

        # D5: symmetric unhandled-exception 500. A truly-unhandled non-HTTP
        # exception re-raises past finalize_request, so _after_request never ran
        # and the RED guard is unset. werkzeug HTTPExceptions (incl. the 400
        # ClientDisconnected) are converted to responses and recorded at their
        # real status via _after_request, so only a genuinely unhandled error
        # reaches this 500 path. Catch scope is `Exception` (not BaseException),
        # the WSGI analog of the FastAPI discipline, so SystemExit /
        # KeyboardInterrupt during shutdown record no spurious 500.
        if isinstance(exc, Exception) and not getattr(g, _FLASK_G_RED_RECORDED, False):
            start_time = getattr(g, _FLASK_G_START_TIME, None)
            if start_time is not None:
                request_ctx: RequestContext | None = getattr(g, _FLASK_G_KEY, None)
                method = (
                    request_ctx.method.value if request_ctx is not None else "UNKNOWN"
                )
                duration_seconds = time.perf_counter() - start_time
                endpoint = getattr(g, _FLASK_G_ENDPOINT, "UNMATCHED_ROUTE")
                record_http_red(
                    method,
                    endpoint,
                    500,
                    duration_seconds,
                    error_type=type(exc).__name__,
                )

    app.before_request(_before_request)
    app.after_request(_after_request)
    app.teardown_request(_teardown_request)


# =============================================================================
# Internal helpers
# =============================================================================


def _clear_deadline_if_enabled() -> None:
    """Clear the request-scoped degraded deadline (idempotent ``set(None)``).

    No-op when the deadline feature is disabled or its module is unavailable.
    """
    try:
        from baldur.scaling.deadline_context import DEADLINE_ENABLED, clear_deadline
    except ImportError:
        return
    if DEADLINE_ENABLED:
        clear_deadline()


def _extract_flask_endpoint() -> str:
    """Return the matched route template as a cardinality-bounded label.

    ``request.url_rule.rule`` is the registered template (e.g.
    ``/users/<int:uid>``), so all concrete paths matching one route share a
    single label — bounded by ``(# registered routes + 1)``. Unmatched / unrouted
    requests (404, ``url_rule is None``) collapse to the single ``UNMATCHED_ROUTE``
    label, identical to Django's scan-defense, keeping per-path 404 cardinality
    out of Prometheus.
    """
    from flask import request

    url_rule = request.url_rule
    if url_rule is not None:
        return url_rule.rule
    return "UNMATCHED_ROUTE"


def _build_request_context() -> RequestContext:
    """Snapshot Flask's request proxy into Baldur's ``RequestContext``."""
    from flask import request

    method_str = (request.method or "GET").upper()
    try:
        method = HttpMethod(method_str)
    except ValueError:
        method = HttpMethod.GET

    headers = dict(request.headers.items())

    # request.headers is a werkzeug EnvironHeaders with case-insensitive
    # lookup — use it before flattening into a plain dict, which loses
    # case-insensitivity.
    forwarded = request.headers.get("X-Forwarded-For")
    client_ip = forwarded.split(",")[0].strip() if forwarded else request.remote_addr

    query_params: dict[str, Any] = {}
    for k in request.args:
        values = request.args.getlist(k)
        query_params[k] = values[0] if len(values) == 1 else values

    return RequestContext(
        method=method,
        path=request.path,
        headers=headers,
        query_params=query_params,
        client_ip=client_ip,
        user_agent=request.headers.get("User-Agent"),
        request_id=request.headers.get("X-Request-ID"),
        content_type=request.content_type,
    )


def _to_flask_response(response_ctx: ResponseContext) -> Response:
    """Convert a Baldur ``ResponseContext`` to a Flask ``Response``."""
    from flask import Response, jsonify

    if isinstance(response_ctx.body, (dict, list)):
        resp = jsonify(response_ctx.body)
        resp.status_code = response_ctx.status_code
    elif isinstance(response_ctx.body, (bytes, bytearray)):
        resp = Response(
            response=bytes(response_ctx.body),
            status=response_ctx.status_code,
            content_type=response_ctx.content_type,
        )
    elif response_ctx.body is None:
        resp = Response(status=response_ctx.status_code)
    else:
        resp = Response(
            response=str(response_ctx.body),
            status=response_ctx.status_code,
            content_type=response_ctx.content_type,
        )

    for k, v in response_ctx.headers.items():
        resp.headers[k] = v
    return resp
