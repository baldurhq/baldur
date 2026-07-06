"""
URL utilities.

Status: Internal
"""

from __future__ import annotations

from typing import overload
from urllib.parse import urljoin

__all__ = ["absolutize_against_site_url"]


@overload
def absolutize_against_site_url(url: str) -> str: ...


@overload
def absolutize_against_site_url(url: None) -> None: ...


def absolutize_against_site_url(url: str | None) -> str | None:
    """Absolutize a relative URL against the explicitly configured site base URL.

    Used by the actionable-alert URL builders so the dashboard/admin/runbook
    links they emit are renderable as Slack link buttons (Slack rejects a
    whole message carrying a relative button URL as ``invalid_blocks``).

    Returns the input unchanged when it is falsy, already absolute
    (``http(s)://`` scheme), or when ``site_url`` was left at its default —
    joining against the default base would emit misleading operator-facing
    links on multi-host deployments, so only an explicitly configured value
    (``SITE_URL`` env var or ``set_config``) is used.
    """
    if not url or url.startswith(("http://", "https://")):
        return url

    # Lazy import: keeps utils free of an import-time settings dependency.
    from baldur.settings import get_config

    config = get_config()
    # pydantic-settings counts env-sourced values as explicitly set, so
    # model_fields_set distinguishes operator configuration from the default.
    if "site_url" not in config.model_fields_set:
        return url
    return urljoin(config.site_url, url)
