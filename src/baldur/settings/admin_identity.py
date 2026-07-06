"""Admin Identity Settings — 537.

Configures the trusted proxy-forwarded header from which the PRO admin
identity resolver (``baldur_pro.services.admin_identity``) reads the
authenticated operator's email. OSS never reads this module (no resolver is
registered); it lives in OSS so the env-prefix module-equality fitness
function (#g15) and the settings-class catalog stay complete, exactly like
``ChaosSettings`` lives in OSS without OSS running a chaos scheduler (537 D-C4).

The trust gate itself is ``AdminServerSettings.trust_proxy``
(``settings/admin.py``) — shared with the OSS-owned ``client_ip`` forwarded
resolution — not a field here.

Environment Variables:
    BALDUR_ADMIN_IDENTITY_HEADER=X-Forwarded-Email
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class AdminIdentitySettings(BaseSettings):
    """Identity-header configuration for the admin transport (537 D-C4).

    The default ``X-Forwarded-Email`` is the oauth2-proxy convention. Other
    IdPs forward a different plain-email header (GCP IAP:
    ``X-Goog-Authenticated-User-Email``; Cloudflare Access:
    ``Cf-Access-Authenticated-User-Email``) — set this to match the fronting
    proxy. baldur reads only the plain-email header; JWT assertions are
    validated by the proxy, not by baldur.
    """

    model_config = make_settings_config("BALDUR_ADMIN_IDENTITY_")

    header: str = Field(
        default="X-Forwarded-Email",
        description=(
            "Proxy-forwarded header carrying the authenticated operator's "
            "email. Trusted only when BALDUR_ADMIN_TRUST_PROXY=1. A wrong or "
            "absent header fail-closes attribution to 'anonymous'."
        ),
    )


def get_admin_identity_settings() -> AdminIdentitySettings:
    """Get cached AdminIdentitySettings instance."""
    from baldur.runtime import get_runtime

    return get_runtime().get_settings(AdminIdentitySettings)


def reset_admin_identity_settings() -> None:
    """Reset cached settings — for test isolation only."""
    from baldur.runtime import get_runtime

    get_runtime().reset_settings(AdminIdentitySettings)


__all__ = [
    "AdminIdentitySettings",
    "get_admin_identity_settings",
    "reset_admin_identity_settings",
]
