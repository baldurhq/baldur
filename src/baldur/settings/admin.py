"""Admin Server Settings — 429 Part 2 (PR3).

Framework-free admin HTTP server configuration. Stdlib ``http.server`` based,
daemon-thread execution, 3-layer auth (binding / transport / API key).

Environment Variables:
    BALDUR_ADMIN_ENABLED=1
    BALDUR_ADMIN_AUTOSTART=1
    BALDUR_ADMIN_BIND=127.0.0.1
    BALDUR_ADMIN_PORT=9090
    BALDUR_ADMIN_KEY=<secret>            # operator API key (required for non-localhost)
    BALDUR_ADMIN_READONLY_KEY=<secret>   # read-only (VIEWER) key (optional, additive)
    BALDUR_ADMIN_TRUST_PROXY=0           # 1 = TLS-terminating proxy in front
    BALDUR_ADMIN_UNLOCK=0                # 1 = allow ADMIN-level operations

Reference: 429 Part 2 (C5/C6/C7/D4).
"""

from __future__ import annotations

from pydantic import (
    AliasChoices,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class AdminServerSettings(BaseSettings):
    """Admin HTTP server settings.

    Layer 1 (binding): ``bind`` defaults to ``127.0.0.1``. Non-localhost binds
    require ``api_key`` to be set.

    Layer 2 (transport): Admin server is plain HTTP by design. TLS is delegated
    to a reverse proxy (Nginx / K8s Ingress / ALB). When bind is non-localhost
    and ``trust_proxy`` is false, startup emits ``admin.server_bind_warning``.

    Layer 3 (authentication): API key comparison via ``hmac.compare_digest``
    against the ``X-Baldur-Admin-Key`` header. ``ADMIN``-level operations
    additionally require ``unlock=True`` (fail-closed default).
    """

    model_config = make_settings_config("BALDUR_ADMIN_")

    enabled: bool = Field(
        default=True,
        description="Admin server feature flag. When False, start_admin_server is a no-op.",
    )

    autostart: bool = Field(
        default=True,
        description=(
            "Whether baldur.init() auto-starts the admin server. Tests that call "
            "init() without a live server should set BALDUR_ADMIN_AUTOSTART=0."
        ),
    )

    bind: str = Field(
        default="127.0.0.1",
        description="Bind address. Non-localhost values require api_key.",
    )

    port: int = Field(
        default=9090,
        ge=0,
        le=65535,
        description=(
            "TCP port for admin HTTP server. 0 = let OS pick an ephemeral "
            "port (used by tests; inspect AdminServer.bound_port afterwards)."
        ),
    )

    api_key: SecretStr | None = Field(
        default=None,
        # Accept BALDUR_ADMIN_KEY (doc-specified, 429 Part 2 C6) or the
        # auto-derived BALDUR_ADMIN_API_KEY. Both map to this field.
        validation_alias=AliasChoices(
            "BALDUR_ADMIN_KEY",
            "BALDUR_ADMIN_API_KEY",
            "api_key",
        ),
        description=(
            "Shared secret for X-Baldur-Admin-Key header. Required when bind "
            "is non-localhost. Compared via hmac.compare_digest."
        ),
    )

    readonly_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "BALDUR_ADMIN_READONLY_KEY",
            "readonly_key",
        ),
        description=(
            "Shared secret for a read-only (VIEWER) credential on the same "
            "X-Baldur-Admin-Key header. Resolves to PermissionLevel.VIEWER — "
            "read access to VIEWER-tagged admin/observability routes without "
            "the OPERATOR/ADMIN privileges of api_key, so non-human integrations "
            "(AI operators, dashboards, status pages) can read with least "
            "privilege. Additive to api_key: it never substitutes for the "
            "operator key as the non-localhost bind-safety gate, and must differ "
            "from api_key (enforced at load). Compared via hmac.compare_digest."
        ),
    )

    trust_proxy: bool = Field(
        default=False,
        description=(
            "True when a TLS-terminating reverse proxy fronts the admin server. "
            "Suppresses the plaintext-HTTP startup warning on non-localhost binds, "
            "AND (537 D-C2) gates trust of forwarded headers: when True, the admin "
            "transport resolves the real client IP from X-Forwarded-For / X-Real-IP "
            "and the operator identity from the configured identity header "
            "(BALDUR_ADMIN_IDENTITY_HEADER). Fail-closed default (False): forwarded "
            "headers are ignored and the TCP peer is the client. "
            "OPERATOR CONTRACT (537 D6): when True, baldur trusts the identity "
            "header BLINDLY — the fronting proxy MUST (a) strip any client-supplied "
            "copy of that header and (b) set its own authenticated value. A "
            "trust_proxy=True admin port exposed without such a proxy lets any "
            "direct caller forge the audited actor."
        ),
    )

    unlock: bool = Field(
        default=False,
        description=(
            "Unlock ADMIN-level operations over HTTP. Fail-closed default — "
            "prevents accidental force-open via the admin API."
        ),
    )

    console_enabled: bool = Field(
        default=True,
        description=(
            "Serve the built-in web console at GET /. When False, GET / returns "
            "404 while the JSON API keeps serving. Safe by default — the console "
            "binds localhost and the request-origin gate closes DNS-rebinding."
        ),
    )

    allowed_origins: list[str] = Field(
        default_factory=list,
        description=(
            "Extra hostnames accepted by the request-origin gate, beyond the "
            "auto-derived loopback/bind allowlist. pydantic-settings parses the "
            "BALDUR_ADMIN_ALLOWED_ORIGINS env var as a JSON list, e.g. "
            "'[\"admin.example.com\"]'. Empty default. On non-localhost binds "
            "the origin gate is enforced only when this is explicitly set."
        ),
    )

    request_timeout_seconds: float = Field(
        default=30.0,
        ge=0.1,
        le=300.0,
        description="Per-request handler timeout before returning 504.",
    )

    max_body_bytes: int = Field(
        default=1_048_576,
        ge=1024,
        le=16 * 1024 * 1024,
        description="Maximum request body size accepted by the admin server.",
    )

    @field_validator("bind")
    @classmethod
    def _strip_bind(cls, v: str) -> str:
        return v.strip()

    @property
    def is_localhost_bind(self) -> bool:
        """True when bind is a loopback interface."""
        return self.bind in {"127.0.0.1", "::1", "localhost"}

    @property
    def api_key_plain(self) -> str | None:
        """Return plaintext API key or None."""
        if self.api_key is None:
            return None
        value = self.api_key.get_secret_value()
        return value or None

    @property
    def readonly_key_plain(self) -> str | None:
        """Return plaintext read-only (VIEWER) key or None.

        Mirrors ``api_key_plain``: an empty-string secret normalizes to
        ``None`` so it reads as "no key configured" rather than a usable
        empty credential.
        """
        if self.readonly_key is None:
            return None
        value = self.readonly_key.get_secret_value()
        return value or None

    @model_validator(mode="after")
    def _reject_equal_keys(self) -> AdminServerSettings:
        """Fail loud at load when the readonly key equals the operator key.

        Configuring both admin secrets to the same value makes that value's
        effective permission level ambiguous. Compares the ``_plain``
        accessors (which normalize empty-string to ``None``) so two
        unset/empty keys both resolve to ``None`` and pass — the guard fires
        only when both are non-``None`` and equal.
        """
        operator = self.api_key_plain
        readonly = self.readonly_key_plain
        if operator is not None and readonly is not None and operator == readonly:
            raise ValueError(
                "BALDUR_ADMIN_READONLY_KEY must differ from BALDUR_ADMIN_KEY — "
                "identical operator and readonly secrets make the credential's "
                "effective permission level ambiguous"
            )
        return self


def get_admin_server_settings() -> AdminServerSettings:
    """Get cached AdminServerSettings instance."""
    from baldur.runtime import get_runtime

    return get_runtime().get_settings(AdminServerSettings)


def reset_admin_server_settings() -> None:
    """Reset cached settings — for test isolation only."""
    from baldur.runtime import get_runtime

    get_runtime().reset_settings(AdminServerSettings)


__all__ = [
    "AdminServerSettings",
    "get_admin_server_settings",
    "reset_admin_server_settings",
]
