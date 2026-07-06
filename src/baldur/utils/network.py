"""
Network Utilities.

Provides canonical IP extraction for the baldur system.
All modules requiring client IP should use ``extract_client_ip``
to ensure consistent behaviour across audit, permission, actor context,
and canary feature-flag subsystems.

Header resolution order:
    1. ``X-Forwarded-For`` – de-facto standard for reverse proxies / LB
    2. ``X-Real-IP`` – commonly set by nginx
    3. ``REMOTE_ADDR`` – direct connection fallback

Why unified?
    Before this module each subsystem had its own copy with subtle
    differences (missing ``X-Real-IP``, different defaults, inconsistent
    ``strip()``).  A single canonical function eliminates IP discrepancy
    bugs where audit records and permission checks disagree on the same
    request's origin.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _resolve_forwarded_ip(
    x_forwarded_for: str | None, x_real_ip: str | None
) -> str | None:
    """Apply the shared ``X-Forwarded-For`` -> ``X-Real-IP`` precedence.

    Single source of truth for forwarded-header IP resolution, consumed by
    both :func:`extract_client_ip` (Django ``META``) and
    :func:`extract_client_ip_from_headers` (plain header mapping) so the two
    cannot drift. Returns the resolved client IP, or ``None`` when neither
    forwarded header is present — callers append their own final fallback
    (Django ``REMOTE_ADDR`` or an explicit ``default``).
    """
    # 1) X-Forwarded-For – first entry is the original client
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()

    # 2) X-Real-IP (nginx convention)
    if x_real_ip:
        return x_real_ip.strip()

    return None


def extract_client_ip(request: Any, *, default: str | None = None) -> str | None:
    """
    Extract the client IP address from a Django ``HttpRequest``.

    Safely handles objects that may not have a ``META`` attribute
    (e.g. test doubles, DRF ``Request`` wrappers) by using ``getattr``
    with an empty-dict fallback.

    Args:
        request: A Django ``HttpRequest`` (or DRF ``Request``).
        default: Value returned when no IP can be determined.
                 Callers that need a non-``None`` sentinel (e.g. audit
                 masking) can pass ``default="unknown"``.

    Returns:
        The resolved client IP string, or *default* if unavailable.
    """
    meta = getattr(request, "META", None) or {}

    resolved = _resolve_forwarded_ip(
        meta.get("HTTP_X_FORWARDED_FOR"),
        meta.get("HTTP_X_REAL_IP"),
    )
    if resolved is not None:
        return resolved

    # Direct connection fallback
    remote_addr = meta.get("REMOTE_ADDR")
    if remote_addr:
        return remote_addr

    return default


def extract_client_ip_from_headers(
    headers: Mapping[str, str], *, default: str | None = None
) -> str | None:
    """
    Extract the client IP from a plain header mapping (stdlib transport).

    Companion to :func:`extract_client_ip` for the framework-free admin server
    (``api/admin/server.py``), whose headers are a ``dict``/``HTTPMessage``
    mapping rather than a Django ``META`` dict. Both functions share
    :func:`_resolve_forwarded_ip`, so the ``X-Forwarded-For`` -> ``X-Real-IP``
    precedence cannot drift between transports.

    Header lookup is case-insensitive (proxies vary the casing).

    Args:
        headers: A mapping of HTTP header names to values.
        default: Value returned when no forwarded IP is present — the caller
                 passes the TCP peer address so an untrusted/no-proxy request
                 falls back to the real peer.

    Returns:
        The resolved client IP string, or *default* if no forwarded header is
        present.
    """
    lowered = {key.lower(): value for key, value in headers.items()}
    resolved = _resolve_forwarded_ip(
        lowered.get("x-forwarded-for"),
        lowered.get("x-real-ip"),
    )
    return resolved if resolved is not None else default
