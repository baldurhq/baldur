"""OpenAPI Settings — 530 Wave 6F (API Discoverability).

Operator-facing toggles for the OpenAPI 3.0 schema and Swagger UI / ReDoc
endpoints mounted under ``/api/baldur/{schema,docs,redoc}/``. Setting
``BALDUR_OPENAPI_ENABLED=0`` removes those URL routes entirely (the URL
module's first gate per 530 D11).

The ``title``/``version``/``description`` fields are passed through to
drf-spectacular's ``SPECTACULAR_SETTINGS`` Django setting when present.

Environment Variables:
    BALDUR_OPENAPI_ENABLED=true
    BALDUR_OPENAPI_TITLE="Baldur Reliability API"
    BALDUR_OPENAPI_VERSION="1.0.0"
    BALDUR_OPENAPI_DESCRIPTION="..."

Reference: 530 D4/D11.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class OpenAPISettings(BaseSettings):
    """OpenAPI schema + docs UI feature flag and metadata."""

    model_config = make_settings_config("BALDUR_OPENAPI_")

    enabled: bool = Field(
        default=True,
        description=(
            "Enable the /schema/, /docs/, /redoc/ URL routes. When False, "
            "the urls/schema.py module contributes an empty pattern list so "
            "the surface is hidden even when drf-spectacular is installed."
        ),
    )

    title: str = Field(
        default="Baldur Reliability API",
        description="OpenAPI document title (drf-spectacular SPECTACULAR_SETTINGS.TITLE).",
    )

    version: str = Field(
        default="1.0.0",
        description="OpenAPI document version (drf-spectacular SPECTACULAR_SETTINGS.VERSION).",
    )

    description: str = Field(
        default=(
            "Self-healing reliability primitives — circuit breakers, DLQ, "
            "audit, governance, throttling, and feature toggles."
        ),
        description="OpenAPI document description (drf-spectacular SPECTACULAR_SETTINGS.DESCRIPTION).",
    )


def get_openapi_settings() -> OpenAPISettings:
    """Get cached OpenAPISettings instance."""
    from baldur.runtime import get_runtime

    return get_runtime().get_settings(OpenAPISettings)


def reset_openapi_settings() -> None:
    """Reset cached settings — for test isolation only."""
    from baldur.runtime import get_runtime

    get_runtime().reset_settings(OpenAPISettings)


__all__ = [
    "OpenAPISettings",
    "get_openapi_settings",
    "reset_openapi_settings",
]
