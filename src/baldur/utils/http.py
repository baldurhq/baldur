"""
HTTP Utilities — safe stdlib-urllib wrappers.

Provides ``safe_urlopen`` that enforces an http(s)-only scheme allowlist
before delegating to :func:`urllib.request.urlopen`. Required because the
stdlib accepts ``file://``, ``ftp://``, ``gopher://`` (in older interpreters),
and other schemes that turn an attacker-controlled URL into local file
disclosure or SSRF against link-local metadata services.

All Baldur modules that fetch from a URL — Prometheus auto-tuning, RFC 3161
TSA, postmortem webhooks, multi-region health probes, audit export — MUST
funnel through this helper. Direct ``urllib.request.urlopen`` is flagged by
Bandit B310 and rejected in CI.
"""

from __future__ import annotations

import urllib.request
from typing import Any
from urllib.parse import urlsplit

__all__ = ["UnsafeURLSchemeError", "safe_urlopen"]


_ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeURLSchemeError(ValueError):
    """Raised when a URL uses a scheme outside the http(s) allowlist.

    Distinct from generic ``ValueError`` so callers can ``except`` this
    specifically — e.g. health probes that want to surface a config error
    rather than silently fall back to "endpoint unreachable".
    """


def _extract_url(req_or_url: Any) -> str:
    if isinstance(req_or_url, urllib.request.Request):
        return req_or_url.full_url
    if isinstance(req_or_url, str):
        return req_or_url
    raise TypeError(
        f"safe_urlopen accepts str or urllib.request.Request, got {type(req_or_url).__name__}"
    )


def safe_urlopen(req_or_url: Any, *, timeout: float, **kwargs: Any) -> Any:
    """Wrapper around :func:`urllib.request.urlopen` with a scheme allowlist.

    Accepts the same first argument as the stdlib (a URL string or a
    ``urllib.request.Request``). ``timeout`` is required (keyword-only) to
    prevent hung sockets from cascading into liveness failures — every Baldur
    fetch path has a budget.

    Raises:
        UnsafeURLSchemeError: scheme is not http/https.
        TypeError: argument is neither str nor Request.
    """
    url = _extract_url(req_or_url)
    scheme = urlsplit(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLSchemeError(
            f"only http/https allowed, got scheme={scheme!r} url={url[:64]!r}"
        )
    return urllib.request.urlopen(  # nosec B310 — scheme verified above
        req_or_url, timeout=timeout, **kwargs
    )
