"""Admin server 3-layer authentication (429 Part 2 / C6).

Layer 1 — binding:
    Default bind is ``127.0.0.1``. Non-localhost binds without an API key
    refuse to start (raises :class:`AdminAuthRequiredError`).

Layer 2 — transport:
    The admin server speaks plain HTTP by design. TLS is the deployment
    layer's responsibility (reverse proxy). Non-localhost bind without
    ``BALDUR_ADMIN_TRUST_PROXY=1`` logs a startup WARNING.

Layer 3 — authentication:
    Per-request check compares ``X-Baldur-Admin-Key`` against the configured
    secrets via :func:`hmac.compare_digest`. The operator key
    (``BALDUR_ADMIN_KEY``) resolves to :attr:`PermissionLevel.OPERATOR`; the
    optional read-only key (``BALDUR_ADMIN_READONLY_KEY``) resolves to the
    strictly-less-privileged :attr:`PermissionLevel.VIEWER`, a least-privilege
    credential for non-human integrations. Localhost requests without an
    operator key configured are mapped to :attr:`PermissionLevel.OPERATOR`
    (dev default). The operator-key match is evaluated first, so it wins over
    the readonly key when both are configured.
    :attr:`PermissionLevel.ADMIN` routes are a double-gate — they require
    an authenticated OPERATOR-or-higher AND ``BALDUR_ADMIN_UNLOCK=1``. The
    unlock flag promotes an authenticated OPERATOR to ADMIN for that
    request; neither factor alone suffices.
"""

from __future__ import annotations

import hmac
from typing import Any

import structlog

from baldur.core.exceptions import ConfigurationError
from baldur.interfaces.web_framework import PermissionLevel
from baldur.settings.admin import AdminServerSettings

logger = structlog.get_logger()

__all__ = [
    "AdminAuthRequiredError",
    "AuthOutcome",
    "check_bind_safety",
    "authenticate",
    "authorize",
    "emit_transport_warning",
]


API_KEY_HEADER = "X-Baldur-Admin-Key"


class AdminAuthRequiredError(ConfigurationError):
    """Raised when the admin server configuration is unsafe — non-localhost
    bind without an API key. Startup must fail loud rather than silently
    exposing an unauthenticated admin endpoint.

    Inherits from ``ConfigurationError`` (and thus ``BaldurError``) per
    CLAUDE.md Pattern Compliance. ``extra_context()`` exposes the offending
    bind so structured log/alert pipelines can pivot on it.
    """

    def __init__(self, message: str = "", *, bind: str | None = None) -> None:
        super().__init__(message)
        self._bind = bind

    def extra_context(self) -> dict[str, Any]:
        ctx = super().extra_context()
        if self._bind is not None:
            ctx["bind"] = self._bind
        ctx["api_key_configured"] = False
        return ctx


class AuthOutcome:
    """Result of :func:`authenticate` — carries either an effective permission
    level or a reason string. Using a small class (not a tuple) keeps the API
    evolvable without breaking callers."""

    __slots__ = ("level", "reason")

    def __init__(
        self, level: PermissionLevel | None, reason: str | None = None
    ) -> None:
        self.level = level
        self.reason = reason

    @property
    def authenticated(self) -> bool:
        return self.level is not None


def check_bind_safety(settings: AdminServerSettings) -> None:
    """Enforce Layer 1. Called before the HTTP server starts listening.

    Raises:
        AdminAuthRequiredError: non-localhost bind without an API key.
    """
    if settings.is_localhost_bind:
        return
    if settings.api_key_plain:
        return
    raise AdminAuthRequiredError(
        "ADMIN_AUTH_REQUIRED: set BALDUR_ADMIN_KEY to bind non-localhost "
        f"(bind={settings.bind!r})",
        bind=settings.bind,
    )


def emit_transport_warning(settings: AdminServerSettings) -> None:
    """Emit the Layer 2 startup warning once at server start.

    Non-localhost bind without ``trust_proxy`` means the operator has not
    affirmed a TLS-terminating proxy is in front. API keys would traverse
    plaintext HTTP — we warn loudly but do not refuse the bind (operator
    may legitimately have a proxy and simply not have flipped the flag).
    """
    if settings.is_localhost_bind:
        return
    if settings.trust_proxy:
        return
    logger.warning(
        "admin.server_bind_warning",
        bind=settings.bind,
        port=settings.port,
        hint=(
            "non-localhost bind without BALDUR_ADMIN_TRUST_PROXY=1; "
            "API keys will traverse plaintext HTTP unless a TLS-terminating "
            "proxy is in front"
        ),
    )


def authenticate(
    header_value: str | None,
    settings: AdminServerSettings,
    *,
    from_localhost: bool,
) -> AuthOutcome:
    """Layer 3. Resolve the effective :class:`PermissionLevel` for a request.

    Rules (evaluated in this order):
      * Operator-key match: header equals ``api_key`` → OPERATOR. Checked
        first — before the no-key dev default and before the readonly key —
        so the operator key always wins, even on a localhost bind that also
        configures a readonly key.
      * Readonly-key match: header equals ``readonly_key`` → VIEWER. The
        strictly-less-privileged read-only credential for non-human
        integrations; reaches read-only routes only.
      * No ``api_key`` configured + localhost bind: treat as OPERATOR (no
        auth required — matches Prometheus / node_exporter local-only
        default).
      * Missing / wrong header: unauthenticated. Handler decides 401 vs
        PUBLIC passthrough based on :class:`PermissionLevel`.

    Both match branches guard ``key is not None and header_value`` before
    :func:`hmac.compare_digest`: the operator-match is lifted above the
    ``api_key is None`` early-return that previously guaranteed non-``None``
    operands, and ``readonly_key`` is unset (``None``) by default — without
    the guard the default config would crash on every wrong-key request.

    ``from_localhost`` is a defence-in-depth flag. If Layer 1 correctly
    refused the process start, a non-localhost request without a key cannot
    reach this function — but we double-check anyway.
    """
    api_key = settings.api_key_plain
    readonly_key = settings.readonly_key_plain

    # Operator-key match → OPERATOR. Lifted above the `api_key is None` dev
    # default (below) so a no-operator-key + readonly-key + localhost config
    # cannot let the dev default silently resolve a readonly holder to
    # OPERATOR. The lift removed the early-return that guaranteed non-None
    # operands, so guard both before compare_digest.
    if (
        api_key is not None
        and header_value
        and hmac.compare_digest(header_value.encode("utf-8"), api_key.encode("utf-8"))
    ):
        return AuthOutcome(PermissionLevel.OPERATOR)

    # Readonly-key match → VIEWER (strictly less privileged than OPERATOR).
    # After the operator-match so an equal-key misconfiguration (already
    # rejected at settings load) still resolves to OPERATOR. `readonly_key`
    # is None by default, so guard it before compare_digest.
    if (
        readonly_key is not None
        and header_value
        and hmac.compare_digest(
            header_value.encode("utf-8"), readonly_key.encode("utf-8")
        )
    ):
        return AuthOutcome(PermissionLevel.VIEWER)

    # Fall-through — outcomes/reasons unchanged from the pre-readonly design.
    if api_key is None:
        if from_localhost:
            return AuthOutcome(PermissionLevel.OPERATOR)
        return AuthOutcome(
            None, reason="api_key_not_configured_for_non_localhost_client"
        )

    if not header_value:
        return AuthOutcome(None, reason="missing_api_key_header")

    return AuthOutcome(None, reason="invalid_api_key")


_LEVEL_ORDER: dict[PermissionLevel, int] = {
    PermissionLevel.PUBLIC: 0,
    PermissionLevel.AUTHENTICATED: 1,
    PermissionLevel.VIEWER: 2,
    PermissionLevel.OPERATOR: 3,
    PermissionLevel.ADMIN: 4,
}


def authorize(
    effective: PermissionLevel | None,
    required: PermissionLevel,
    settings: AdminServerSettings,
) -> bool:
    """Compare the authenticated level to the route's required level.

    :attr:`PermissionLevel.ADMIN` is a **double-gate**: an authenticated
    caller with :attr:`PermissionLevel.OPERATOR`-or-higher AND
    ``settings.unlock=True`` (``BALDUR_ADMIN_UNLOCK=1``) are both required.
    ``authenticate()`` never returns ADMIN directly — ``unlock`` is the
    promotion flag that elevates an authenticated OPERATOR to ADMIN for
    this request only. Fail-closed default prevents accidental destructive
    operations via the admin API.

    Returns True when access is allowed.
    """
    if required == PermissionLevel.PUBLIC:
        return True
    if effective is None:
        return False
    if required == PermissionLevel.ADMIN:
        if not settings.unlock:
            return False
        # Double-gate: authenticated OPERATOR+ plus unlock flipped ⇒ ADMIN.
        return _LEVEL_ORDER[effective] >= _LEVEL_ORDER[PermissionLevel.OPERATOR]
    return _LEVEL_ORDER[effective] >= _LEVEL_ORDER[required]
